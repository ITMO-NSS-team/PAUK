import unittest

import mongomock
from fastapi.testclient import TestClient

from pauk.admin.app import build
from pauk.admin.auth import COOKIE, SESSIONS, create_user, set_active
from pauk.settings import Settings


class PanelTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="admin")
        create_user(self.db, "guest", "hunter2", role="viewer")
        # follow_redirects off: a redirect to /login is itself the answer
        # under test, and following it hides which status came back.
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)

    def sign_in(self, login="roman", password="hunter2"):
        return self.client.post("/login", data={"login": login, "password": password})

    def test_the_panel_is_closed_without_a_session(self):
        self.assertEqual(self.client.get("/").status_code, 401)

    def test_a_browser_is_sent_to_the_login_page_rather_than_shown_json(self):
        response = self.client.get("/", headers={"accept": "text/html"})
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login?next="))

    def test_signing_in_returns_you_to_the_page_you_wanted(self):
        response = self.client.post(
            "/login", data={"login": "roman", "password": "hunter2", "next": "/nodes/Person"})
        self.assertEqual(response.headers["location"], "/nodes/Person")

    def test_the_login_refuses_to_bounce_anywhere_but_this_site(self):
        # Otherwise ?next=https://evil.example turns the login into an
        # open redirect wearing our address.
        for target in ("https://evil.example", "//evil.example", "http://evil.example/x"):
            response = self.client.post(
                "/login", data={"login": "roman", "password": "hunter2", "next": target})
            self.assertEqual(response.headers["location"], "/", target)

    def test_the_favicon_points_at_the_logo(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response.headers["location"], "/assets/logo.jpg")

    def test_an_invented_cookie_does_not_open_it(self):
        self.client.cookies.set(COOKIE, "made-up")
        self.assertEqual(self.client.get("/").status_code, 401)

    def test_signing_in_sets_a_cookie_and_lets_the_panel_open(self):
        response = self.sign_in()
        self.assertEqual(response.status_code, 303)
        self.assertIn(COOKIE, response.cookies)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_the_session_cookie_is_hidden_from_scripts(self):
        header = self.sign_in().headers["set-cookie"]
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=lax", header)

    def test_a_wrong_password_gives_401_and_no_cookie(self):
        response = self.sign_in(password="nope")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(COOKIE, response.cookies)

    def test_the_login_page_never_says_which_half_was_wrong(self):
        # The page shows one fixed sentence, so a wrong password and a
        # login that does not exist are indistinguishable from outside.
        wrong = self.sign_in(password="nope").text
        missing = self.sign_in(login="nobody").text
        self.assertIn("Неверный логин или пароль", wrong)
        self.assertEqual(wrong, missing)
        self.assertNotIn("no such user", wrong)

    def test_a_signed_in_user_is_sent_away_from_the_login_page(self):
        self.sign_in()
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")

    def test_logging_out_drops_the_session_row_as_well_as_the_cookie(self):
        token = self.sign_in().cookies[COOKIE]
        csrf = self.db[SESSIONS].find_one({"_id": token})["csrf"]
        self.client.post("/logout", data={"csrf": csrf})
        self.assertEqual(self.db[SESSIONS].count_documents({"_id": token}), 0)
        self.assertEqual(self.client.get("/").status_code, 401)

    def test_a_session_stops_working_the_moment_the_account_is_blocked(self):
        self.sign_in()
        self.assertEqual(self.client.get("/").status_code, 200)
        set_active(self.db, "roman", False)
        self.assertEqual(self.client.get("/").status_code, 401)

    def test_the_panel_lists_the_labels_that_can_be_edited(self):
        self.sign_in()
        body = self.client.get("/").text
        self.assertIn("Person", body)
        self.assertIn("Repository", body)

    def test_a_viewer_gets_in_and_is_shown_as_read_only(self):
        self.sign_in(login="guest")
        body = self.client.get("/").text
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("viewer", body)

    def test_the_look_comes_from_the_map_own_files(self):
        # Logo and fonts are served from pauk/gui/web rather than copied,
        # so the panel and the map cannot drift apart visually.
        self.assertEqual(self.client.get("/static/panel.css").status_code, 200)
        self.assertEqual(self.client.get("/assets/logo.jpg").status_code, 200)
        self.assertEqual(
            self.client.get("/assets/fonts/golos-text-cyrillic.woff2").status_code, 200)

    def test_the_admin_port_serves_nothing_else_from_the_map(self):
        # Only the two asset paths are mounted. Mounting the whole web
        # directory would put the map's data dump on the admin port too.
        for path in ("/assets/graph-data.js", "/assets/index.html", "/assets/style.css"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_every_page_carries_the_same_title_shape(self):
        # The login page has to be read before signing in: afterwards it
        # redirects, and a redirect has no body.
        self.assertIn("<title>Вход · PAUK</title>", self.client.get("/login").text)
        self.sign_in()
        self.assertIn("· PAUK</title>", self.client.get("/").text)

    def test_the_panel_offers_no_way_to_create_an_account(self):
        # Accounts come from `pauk admin user add` only; a registration
        # route would be a way in past the shell.
        routes = {getattr(route, "path", "") for route in self.client.app.routes}
        self.assertNotIn("/register", routes)
        self.assertNotIn("/signup", routes)


if __name__ == "__main__":
    unittest.main()
