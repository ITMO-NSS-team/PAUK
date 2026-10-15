"""Who is allowed into the panel, and how the panel remembers them.

The panel is the only door to the graph and shows names and addresses of real
people, so access is never anonymous.

- Passwords are hashed with the standard-library `hashlib.scrypt` to keep the
  dependency list short.
- Sessions live in Mongo rather than in a signed cookie, because a signed cookie
  cannot be revoked when an account is disabled.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pymongo.database import Database

logger = logging.getLogger("pauk.admin")

USERS = "admin_users"
SESSIONS = "admin_sessions"

COOKIE = "pauk_admin"
SESSION_HOURS = 12

ATTEMPTS = "admin_login_attempts"

# Failed logins tolerated, and for how long. Counted per login, not per address.
MAX_FAILURES = 30
LOCKOUT_MINUTES = 15

# scrypt cost: n=2**14 is about a hundred milliseconds per hash on a laptop.
_N, _R, _P, _SALT, _KEY = 2**14, 8, 1, 16, 32

ROLES = ("admin", "editor", "viewer")
CAN_WRITE = frozenset({"admin", "editor"})

# Starting a run is not editing a record: neither is undone by a counter-edit.
CAN_RUN = frozenset({"admin"})


class AuthError(Exception):
    """Login refused. Deliberately says nothing about which half was wrong."""


class TooManyAttempts(AuthError):
    """The account is locked for a while after too many failures.

    Carries the wait in minutes. It is not a secret: failures are counted for
    nonexistent logins too.
    """

    def __init__(self, message: str, minutes: int) -> None:
        super().__init__(message)
        self.minutes = minutes


def _now() -> datetime:
    moment = datetime.now(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def hash_password(password: str) -> str:
    """Return `scrypt$<salt>$<hash>`, both halves hex-encoded."""
    if not password:
        raise AuthError("password is empty")
    salt = secrets.token_bytes(_SALT)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_KEY)
    return f"scrypt${salt.hex()}${derived.hex()}"


_placeholder: str | None = None


def _placeholder_hash() -> str:
    """A hash to check a made-up password against, derived once.

    Deriving per call would make an unknown login answer about twice as slowly
    as a known one. Lazy because every `pauk` command imports this module.
    """
    global _placeholder
    if _placeholder is None:
        _placeholder = hash_password(secrets.token_urlsafe(16))
    return _placeholder


def verify_password(password: str, stored: str) -> bool:
    """Whether the password matches, compared without leaking timing."""
    try:
        scheme, salt_hex, expected_hex = stored.split("$")
    except ValueError:
        logger.warning("stored password hash is malformed")
        return False
    if scheme != "scrypt":
        logger.warning("unknown password scheme: %s", scheme)
        return False
    derived = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                             n=_N, r=_R, p=_P, dklen=_KEY)
    return hmac.compare_digest(derived.hex(), expected_hex)


@dataclass(frozen=True)
class User:
    login: str
    role: str

    @property
    def can_write(self) -> bool:
        return self.role in CAN_WRITE

    @property
    def can_run(self) -> bool:
        """Whether this account may start a pipeline run."""
        return self.role in CAN_RUN

    @property
    def actor(self) -> str:
        """How this user is named in the audit log."""
        return f"user:{self.login}"


def create_user(db: Database, login: str, password: str, role: str = "editor") -> dict:
    """Add an account. Fails rather than overwrite an existing login."""
    login = login.strip().lower()
    if not login:
        raise AuthError("login is empty")
    if role not in ROLES:
        raise AuthError(f"unknown role: {role!r} (known: {', '.join(ROLES)})")
    if db[USERS].find_one({"_id": login}):
        raise AuthError(f"user {login!r} already exists")
    document = {
        "_id": login,
        "password_hash": hash_password(password),
        "role": role,
        "active": True,
        "created_at": _now(),
    }
    db[USERS].insert_one(document)
    logger.info("admin user created: %s (%s)", login, role)
    return document


def set_active(db: Database, login: str, active: bool) -> bool:
    """Enable or disable an account.

    Disabling also drops the user's sessions so the account stops working at once.
    """
    result = db[USERS].update_one({"_id": login.strip().lower()}, {"$set": {"active": active}})
    if result.matched_count and not active:
        db[SESSIONS].delete_many({"login": login.strip().lower()})
    return result.matched_count > 0


def list_users(db: Database) -> list[dict]:
    return list(db[USERS].find({}, {"password_hash": False}).sort("_id"))


def authenticate(db: Database, login: str, password: str) -> User:
    """Check a login and password.

    Raises:
        AuthError: No such user, wrong password, or the account is disabled.
            The message is the same for all three so logins cannot be probed.
    """
    login = login.strip().lower()
    _refuse_while_locked(db, login)
    row = db[USERS].find_one({"_id": login})
    if row is None or not row.get("active", False):
        # Against a stand-in, so a missing user costs the same as a wrong password.
        verify_password(password, _placeholder_hash())
        _count_failure(db, login)
        raise AuthError("wrong login or password")
    if not verify_password(password, row["password_hash"]):
        _count_failure(db, login)
        raise AuthError("wrong login or password")
    db[ATTEMPTS].delete_one({"_id": login})
    return User(login=row["_id"], role=row.get("role", "viewer"))


def _refuse_while_locked(db: Database, login: str) -> None:
    """Raise if this login is inside its lockout.

    Raises:
        TooManyAttempts: The lock is still on.
    """
    row = db[ATTEMPTS].find_one({"_id": login})
    if row is None:
        return
    until = row.get("locked_until")
    if until is None:
        return
    if _aware(until) <= _now():
        db[ATTEMPTS].delete_one({"_id": login})
        return
    minutes = max(int((_aware(until) - _now()).total_seconds() // 60) + 1, 1)
    raise TooManyAttempts(f"too many failed attempts; try again in {minutes} min",
                          minutes)


def _count_failure(db: Database, login: str) -> None:
    """Record one failure, locking the account once there are enough.

    The window slides from the first failure of a run.
    """
    now = _now()
    row = db[ATTEMPTS].find_one({"_id": login})
    if row is None or _aware(row.get("first_at", now)) + timedelta(minutes=LOCKOUT_MINUTES) < now:
        db[ATTEMPTS].replace_one({"_id": login},
                                 {"_id": login, "failures": 1, "first_at": now}, upsert=True)
        return
    failures = row.get("failures", 0) + 1
    update: dict = {"$set": {"failures": failures}}
    if failures >= MAX_FAILURES:
        update["$set"]["locked_until"] = now + timedelta(minutes=LOCKOUT_MINUTES)
        logger.warning("login %s locked after %d failed attempts", login, failures)
    db[ATTEMPTS].update_one({"_id": login}, update)


def _aware(moment: datetime) -> datetime:
    """A stored time with a timezone on it.

    pymongo hands datetimes back naive, and comparing one with the aware
    `_now()` raises.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def session_key(token: str) -> str:
    """How a session token is stored: its SHA-256, never the token itself.

    A token is a bearer credential, so a dump of `admin_sessions` must not hand
    out live sessions. A fast hash suffices since the token is 256 random bits,
    and the session is read on every request.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def open_session(db: Database, user: User) -> str:
    """Start a session and return the token that names it.

    Only the token's hash is stored.
    """
    token = secrets.token_urlsafe(32)
    db[SESSIONS].insert_one({
        "_id": session_key(token),
        "login": user.login,
        "role": user.role,
        "csrf": secrets.token_urlsafe(24),
        "created_at": _now(),
        "expires_at": _now() + timedelta(hours=SESSION_HOURS),
    })
    logger.info("session opened for %s", user.login)
    return token


def read_session(db: Database, token: str | None) -> dict | None:
    """The session behind a cookie, or None if there is not a live one.

    An expired row is deleted on the way past, which keeps the collection
    from growing without a scheduled job.
    """
    if not token:
        return None
    key = session_key(token)
    row = db[SESSIONS].find_one({"_id": key})
    if row is None:
        return None
    expires = row.get("expires_at")
    if expires is not None and _aware(expires) < _now():
        db[SESSIONS].delete_one({"_id": key})
        return None
    # The account may have been disabled after the session was opened.
    account = db[USERS].find_one({"_id": row["login"]}, {"active": True, "role": True})
    if account is None or not account.get("active", False):
        db[SESSIONS].delete_one({"_id": key})
        return None
    row["role"] = account.get("role", row.get("role", "viewer"))
    return row


def close_session(db: Database, token: str | None) -> bool:
    if not token:
        return False
    return db[SESSIONS].delete_one({"_id": session_key(token)}).deleted_count > 0


def check_csrf(session: dict, submitted: str | None) -> bool:
    """Whether a form carried this session's token."""
    expected = session.get("csrf")
    if not expected or not submitted:
        return False
    # As bytes: compare_digest refuses str with non-ASCII characters.
    return hmac.compare_digest(expected.encode(), submitted.encode())
