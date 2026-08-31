import statistics
import time
import unittest
from datetime import UTC, datetime, timedelta

import mongomock

from pauk.admin import auth
from pauk.admin.auth import (
    SESSIONS,
    USERS,
    AuthError,
    authenticate,
    check_csrf,
    close_session,
    create_user,
    hash_password,
    list_users,
    open_session,
    read_session,
    set_active,
    verify_password,
)


class PasswordTest(unittest.TestCase):
    def test_a_password_verifies_against_its_own_hash(self):
        stored = hash_password("correct horse")
        self.assertTrue(verify_password("correct horse", stored))
        self.assertFalse(verify_password("correct hors", stored))

    def test_the_same_password_hashes_differently_every_time(self):
        # A shared salt would make two accounts with one password visibly
        # identical in the database.
        self.assertNotEqual(hash_password("secret"), hash_password("secret"))

    def test_an_empty_password_is_refused(self):
        with self.assertRaises(AuthError):
            hash_password("")

    def test_a_malformed_hash_fails_instead_of_raising(self):
        # A hand-edited document must not take the login route down.
        self.assertFalse(verify_password("secret", "not-a-hash"))
        self.assertFalse(verify_password("secret", "md5$aa$bb"))


class UserTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_a_user_is_stored_under_a_lowercase_login(self):
        create_user(self.db, "Roman", "hunter2", role="admin")
        (row,) = list_users(self.db)
        self.assertEqual(row["_id"], "roman")
        self.assertEqual(row["role"], "admin")

    def test_listing_users_never_returns_the_hash(self):
        create_user(self.db, "roman", "hunter2")
        (row,) = list_users(self.db)
        self.assertNotIn("password_hash", row)

    def test_the_same_login_cannot_be_taken_twice(self):
        create_user(self.db, "roman", "hunter2")
        with self.assertRaises(AuthError):
            create_user(self.db, "roman", "other")

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(AuthError):
            create_user(self.db, "roman", "hunter2", role="root")

    def test_a_known_user_logs_in(self):
        create_user(self.db, "roman", "hunter2", role="editor")
        user = authenticate(self.db, "Roman", "hunter2")
        self.assertEqual(user.login, "roman")
        self.assertEqual(user.actor, "user:roman")
        self.assertTrue(user.can_write)

    def test_a_viewer_cannot_write(self):
        create_user(self.db, "guest", "hunter2", role="viewer")
        self.assertFalse(authenticate(self.db, "guest", "hunter2").can_write)

    def test_a_wrong_password_and_a_missing_user_look_the_same(self):
        create_user(self.db, "roman", "hunter2")
        with self.assertRaises(AuthError) as wrong:
            authenticate(self.db, "roman", "nope")
        with self.assertRaises(AuthError) as missing:
            authenticate(self.db, "nobody", "hunter2")
        self.assertEqual(str(wrong.exception), str(missing.exception))

    def test_a_disabled_user_cannot_log_in(self):
        create_user(self.db, "roman", "hunter2")
        set_active(self.db, "roman", False)
        with self.assertRaises(AuthError):
            authenticate(self.db, "roman", "hunter2")


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.user = authenticate(self.db, "roman", "hunter2")

    def test_a_session_is_readable_by_its_token(self):
        token = open_session(self.db, self.user)
        session = read_session(self.db, token)
        self.assertEqual(session["login"], "roman")
        self.assertEqual(session["role"], "editor")

    def test_no_token_and_an_unknown_token_give_nothing(self):
        self.assertIsNone(read_session(self.db, None))
        self.assertIsNone(read_session(self.db, "made-up"))

    def test_logging_out_ends_the_session(self):
        token = open_session(self.db, self.user)
        self.assertTrue(close_session(self.db, token))
        self.assertIsNone(read_session(self.db, token))

    def test_an_expired_session_is_refused_and_swept_away(self):
        token = open_session(self.db, self.user)
        self.db[SESSIONS].update_one(
            {"_id": token}, {"$set": {"expires_at": datetime.now(UTC) - timedelta(minutes=1)}})
        self.assertIsNone(read_session(self.db, token))
        self.assertEqual(self.db[SESSIONS].count_documents({"_id": token}), 0)

    def test_disabling_an_account_ends_the_sessions_it_already_had(self):
        # The reason sessions live in Mongo rather than in a signed cookie:
        # a cookie could not be revoked before it expired.
        token = open_session(self.db, self.user)
        set_active(self.db, "roman", False)
        self.assertIsNone(read_session(self.db, token))

    def test_a_session_dies_even_if_the_account_was_disabled_behind_our_back(self):
        # set_active deletes the sessions itself; this covers the other
        # path — the flag flipped straight in Mongo, by hand or by another
        # tool — where reading the session is the only remaining check.
        token = open_session(self.db, self.user)
        self.db[USERS].update_one({"_id": "roman"}, {"$set": {"active": False}})
        self.assertIsNone(read_session(self.db, token))
        self.assertEqual(self.db[SESSIONS].count_documents({"_id": token}), 0)

    def test_a_role_change_takes_effect_on_the_open_session(self):
        token = open_session(self.db, self.user)
        self.db[USERS].update_one({"_id": "roman"}, {"$set": {"role": "viewer"}})
        self.assertEqual(read_session(self.db, token)["role"], "viewer")

    def test_a_form_needs_this_session_own_csrf_token(self):
        token = open_session(self.db, self.user)
        session = read_session(self.db, token)
        self.assertTrue(check_csrf(session, session["csrf"]))
        self.assertFalse(check_csrf(session, "forged"))
        self.assertFalse(check_csrf(session, None))

    def test_a_non_ascii_csrf_token_is_refused_rather_than_crashing(self):
        # compare_digest rejects str with non-ASCII characters, and the
        # submitted value is whatever the form sent.
        token = open_session(self.db, self.user)
        self.assertFalse(check_csrf(read_session(self.db, token), "подделка"))

    def test_two_sessions_of_one_user_have_different_csrf_tokens(self):
        first = read_session(self.db, open_session(self.db, self.user))
        second = read_session(self.db, open_session(self.db, self.user))
        self.assertNotEqual(first["csrf"], second["csrf"])


if __name__ == "__main__":
    unittest.main()


class LoginTimingTest(unittest.TestCase):
    """A login that does not exist must not answer at a different speed.

    Deriving a throwaway hash per call cost a second scrypt, so a missing
    account answered about twice as slowly as a wrong password — which
    tells an attacker which logins are real just as plainly as an error
    message would.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2")
        auth._placeholder = None

    def measure(self, login, password, runs=7):
        timings = []
        for _ in range(runs):
            self.db[auth.ATTEMPTS].delete_many({})
            started = time.perf_counter()
            with self.assertRaises(AuthError):
                authenticate(self.db, login, password)
            timings.append(time.perf_counter() - started)
        return statistics.median(timings)

    def test_a_missing_login_takes_about_as_long_as_a_wrong_password(self):
        authenticate(self.db, "roman", "hunter2")  # warm the placeholder up
        wrong = self.measure("roman", "nope")
        absent = self.measure("nobody-here", "nope")
        self.assertLess(absent / wrong, 1.4, "время ответа выдаёт, есть ли такой логин")

    def test_the_placeholder_is_derived_once(self):
        auth._placeholder = None
        with self.assertRaises(AuthError):
            authenticate(self.db, "nobody-here", "nope")
        first = auth._placeholder
        with self.assertRaises(AuthError):
            authenticate(self.db, "nobody-here", "nope")
        self.assertIs(auth._placeholder, first)


class LockoutTest(unittest.TestCase):
    """Guessing has to cost something, or scrypt is just a speed bump."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2")

    def fail(self, times):
        for _ in range(times):
            with self.assertRaises(AuthError):
                authenticate(self.db, "roman", "nope")

    def test_the_right_password_still_works_below_the_limit(self):
        self.fail(auth.MAX_FAILURES - 1)
        self.assertEqual(authenticate(self.db, "roman", "hunter2").login, "roman")

    def test_the_account_locks_at_the_limit(self):
        self.fail(auth.MAX_FAILURES)
        with self.assertRaises(auth.TooManyAttempts):
            authenticate(self.db, "roman", "hunter2")

    def test_signing_in_clears_the_count(self):
        self.fail(auth.MAX_FAILURES - 1)
        authenticate(self.db, "roman", "hunter2")
        self.assertIsNone(self.db[auth.ATTEMPTS].find_one({"_id": "roman"}))

    def test_the_lock_lifts_when_it_expires(self):
        self.fail(auth.MAX_FAILURES)
        past = datetime.now(UTC) - timedelta(minutes=1)
        self.db[auth.ATTEMPTS].update_one({"_id": "roman"},
                                          {"$set": {"locked_until": past}})
        self.assertEqual(authenticate(self.db, "roman", "hunter2").login, "roman")

    def test_a_stored_time_without_a_zone_does_not_raise(self):
        # pymongo hands datetimes back naive; comparing one with an aware
        # now() is a TypeError, not a refusal.
        self.fail(auth.MAX_FAILURES)
        naive = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
        self.db[auth.ATTEMPTS].update_one({"_id": "roman"},
                                          {"$set": {"locked_until": naive}})
        with self.assertRaises(auth.TooManyAttempts):
            authenticate(self.db, "roman", "hunter2")

    def test_failures_spread_out_do_not_add_up(self):
        self.fail(auth.MAX_FAILURES - 1)
        long_ago = datetime.now(UTC) - timedelta(minutes=auth.LOCKOUT_MINUTES + 1)
        self.db[auth.ATTEMPTS].update_one({"_id": "roman"}, {"$set": {"first_at": long_ago}})
        self.fail(1)
        self.assertEqual(authenticate(self.db, "roman", "hunter2").login, "roman")

    def test_one_account_locking_leaves_another_alone(self):
        create_user(self.db, "petrov", "hunter2")
        self.fail(auth.MAX_FAILURES)
        self.assertEqual(authenticate(self.db, "petrov", "hunter2").login, "petrov")
