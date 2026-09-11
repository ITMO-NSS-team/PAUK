"""What the panel says when Mongo is not there.

Accounts, sessions, decisions and the queue all live in Mongo, so an
unreachable Mongo is the whole panel being down. It used to answer every
request with a stack trace after waiting half a minute for a database that
was never coming.
"""

import unittest

import mongomock
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

from pauk.admin.app import build
from pauk.admin.auth import COOKIE, MAX_FAILURES, create_user
from pauk.settings import Settings


class Silent:
    """A database that refuses every call, the way a stopped Mongo does."""

    def __getattr__(self, name):
        raise ServerSelectionTimeoutError("localhost:27017: Connection refused")

    def __getitem__(self, name):
        raise ServerSelectionTimeoutError("localhost:27017: Connection refused")


class MongoIsDownTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(build(Settings(), Silent()), follow_redirects=False)
        # Somebody already signed in, which is the case that matters: their
        # cookie sends the panel to Mongo for a session on every request.
        # A visitor with no cookie is never a session to look up, and being
        # sent to the login form is the right answer for them.
        self.client.cookies.set(COOKIE, "a-token-from-before-the-outage")

    def test_a_page_says_what_is_broken(self):
        response = self.client.get("/audit", headers={"accept": "text/html"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("MongoDB не отвечает", response.text)

    def test_a_script_gets_the_same_answer_as_json(self):
        response = self.client.get("/audit")
        self.assertEqual(response.status_code, 503)
        self.assertIn("MongoDB не отвечает", response.json()["detail"])

    def test_a_visitor_with_no_cookie_is_sent_to_the_form(self):
        response = TestClient(build(Settings(), Silent()), follow_redirects=False).get(
            "/audit", headers={"accept": "text/html"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("/login", response.headers["location"])

    def test_signing_in_says_so_in_the_form(self):
        response = self.client.post("/login", data={"login": "roman", "password": "x"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("MongoDB не отвечает", response.text)

    def test_nothing_answers_with_a_stack_trace(self):
        for path in ("/", "/audit", "/overrides", "/review", "/jobs", "/login"):
            with self.subTest(path=path):
                response = self.client.get(path, headers={"accept": "text/html"})
                self.assertNotEqual(response.status_code, 500)


class MongoIsUpTest(unittest.TestCase):
    """The guard must not swallow the ordinary answers."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)

    def test_a_wrong_password_is_still_a_wrong_password(self):
        response = self.client.post("/login", data={"login": "roman", "password": "nope"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("MongoDB не отвечает", response.text)

    def test_a_right_password_still_signs_in(self):
        response = self.client.post("/login", data={"login": "roman", "password": "hunter2"})
        self.assertEqual(response.status_code, 303)


if __name__ == "__main__":
    unittest.main()


class LockedOutTest(unittest.TestCase):
    """A locked account has to say so, not pretend the password was wrong.

    auth.py counts failures for logins that do not exist too, so the wait
    gives an attacker nothing — and somebody who mistyped needs to know
    whether to wait or to go and ask for help. The form used to replace
    every message with "wrong login or password", including this one.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)

    def attempt(self, password="nope"):
        return self.client.post("/login", data={"login": "roman", "password": password})

    def test_the_wait_is_stated(self):
        for _ in range(MAX_FAILURES):
            self.attempt()
        response = self.attempt()
        self.assertEqual(response.status_code, 429)
        self.assertIn("Слишком много попыток", response.text)

    def test_a_wrong_password_still_says_nothing(self):
        # Which half was wrong stays a secret.
        response = self.attempt()
        self.assertIn("Неверный логин или пароль", response.text)
        self.assertNotIn("Слишком много попыток", response.text)
