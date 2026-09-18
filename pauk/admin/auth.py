"""Who is allowed into the panel, and how the panel remembers them.

The panel is the only door to the graph: Neo4j is not exposed outside the
perimeter, so there is no second way in and no second place to check
permissions. It also shows the names and addresses of real people, which
makes anonymous access wrong even inside a VPN.

Two decisions worth stating, because both could reasonably have gone the
other way:

- **Passwords are hashed with `hashlib.scrypt`**, from the standard
  library, rather than `bcrypt` or `passlib`. The repository keeps its
  dependency list short, and scrypt with the parameters below is a sound
  choice for a handful of accounts.
- **Sessions live in Mongo, not in a signed cookie.** A signed cookie
  cannot be revoked: disabling an account would leave every browser that
  already holds one logged in until it expires. A server-side row can be
  deleted, which is what "log out everywhere" and "disable this user"
  both need.
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

# Failed logins tolerated before an account stops answering, and for how
# long. Counted per login rather than per address: the panel sits behind a
# VPN and often behind one proxy, so addresses say little, while the thing
# worth protecting is the account. The lock is short on purpose — it costs
# an attacker their guessing rate and costs the owner one coffee break.
MAX_FAILURES = 30
LOCKOUT_MINUTES = 15

# scrypt cost. n=2**14 keeps a single hash near a hundred milliseconds on a
# laptop — slow enough to make guessing expensive, fast enough that a login
# form still feels instant.
_N, _R, _P, _SALT, _KEY = 2**14, 8, 1, 16, 32

ROLES = ("admin", "editor", "viewer")
CAN_WRITE = frozenset({"admin", "editor"})

# Starting a run is not editing a record. A publish rewrites the whole
# graph, a collection run spends hours and an API quota, and neither can be
# taken back by a counter-edit — so they need the role that was until now
# only a name.
CAN_RUN = frozenset({"admin"})


class AuthError(Exception):
    """Login refused. Deliberately says nothing about which half was wrong."""


class TooManyAttempts(AuthError):
    """The account is locked for a while after too many failures."""


def _now() -> datetime:
    moment = datetime.now(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def hash_password(password: str) -> str:
    """Return `scrypt$<salt>$<hash>`, both halves hex-encoded.

    The salt is stored beside the hash because it has to be: verifying a
    password means repeating the derivation with the same salt, and there
    is nowhere else to keep it.
    """
    if not password:
        raise AuthError("password is empty")
    salt = secrets.token_bytes(_SALT)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_KEY)
    return f"scrypt${salt.hex()}${derived.hex()}"


_placeholder: str | None = None


def _placeholder_hash() -> str:
    """A hash to check a made-up password against, derived once.

    Deriving a fresh one per call cost a second scrypt, so a login that
    does not exist answered about twice as slowly as one that does — the
    opposite of the intent, and just as good a way to tell them apart.
    Lazy rather than at import: `pauk.admin.auth` is pulled in by every
    `pauk` command, and none of the others should pay for a key derivation.
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

    Disabling also drops the user's sessions: leaving them alive would mean
    a disabled account keeps working until its cookie expires, which is the
    exact failure the server-side session table exists to prevent.
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
        AuthError: No such user, wrong password, or the account is
            disabled. The message is the same for all three on purpose —
            telling an attacker which logins exist is free information.
    """
    login = login.strip().lower()
    _refuse_while_locked(db, login)
    row = db[USERS].find_one({"_id": login})
    if row is None or not row.get("active", False):
        # Verify anyway, against a stand-in, so a missing user takes the
        # same one key derivation as a wrong password and the two cannot be
        # told apart by how long the answer took.
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
        TooManyAttempts: The lock is still on. The wait is stated: it is
            not a secret, and a person who mistyped their password needs to
            know whether to wait or to ask for help.
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
    raise TooManyAttempts(f"too many failed attempts; try again in {minutes} min")


def _count_failure(db: Database, login: str) -> None:
    """Record one failure, locking the account once there are enough.

    The window slides from the first failure of a run: thirty typos spread
    over a working day are somebody forgetting a password, while thirty in
    a quarter of an hour are not a person typing.
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

    pymongo hands datetimes back naive, in UTC; comparing one with an aware
    `_now()` raises instead of answering.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def session_key(token: str) -> str:
    """How a session token is stored.

    Not the token itself. A session token is a bearer credential: whoever
    holds it is signed in, no password needed. Kept verbatim, one read of
    `admin_sessions` — a dump, a backup, a copy made for support — handed
    over every live session, while the passwords beside them were hashed.

    A fast hash, deliberately, and not `scrypt` like a password. A password
    is slow-hashed because it is guessable; this is 256 bits of randomness
    with nothing to guess. The session is read on every single request, so
    a slow hash here would tax every page for no gain at all.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def open_session(db: Database, user: User) -> str:
    """Start a session and return the token that names it.

    The token goes to the browser in a cookie and is not kept anywhere
    else; the row holds only its hash, which is enough to find it again
    and to end the session from the server side.
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

    An expired row is deleted on the way past: sessions are read on every
    request, which makes this the cheapest place to keep the collection
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
    """Whether a form carried this session's token.

    Cookies travel with a cross-site POST on their own, so the cookie alone
    cannot prove the request came from our page; a value the attacker's
    page has no way to read can.
    """
    expected = session.get("csrf")
    if not expected or not submitted:
        return False
    # Compared as bytes: compare_digest refuses str with non-ASCII
    # characters, and the submitted value is whatever the form sent.
    return hmac.compare_digest(expected.encode(), submitted.encode())
