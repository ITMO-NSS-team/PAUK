import threading
import unittest
from unittest.mock import patch

import mongomock
from fastapi.testclient import TestClient

from pauk.admin import app as app_module
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

    def test_the_favicon_is_served_as_a_file(self):
        # Not a redirect: browsers cache a 301 hard, so a 404 caught once
        # would outlive the fix.
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")

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

    def test_a_viewer_gets_in_and_is_shown_as_read_only(self):
        self.sign_in(login="guest")
        body = self.client.get("/").text
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("viewer", body)

    def test_the_look_comes_from_the_map_own_files(self):
        # Logo and fonts are served from pauk/gui/web rather than copied,
        # so the panel and the map cannot drift apart visually.
        self.assertEqual(self.client.get("/static/panel.css").status_code, 200)
        self.assertEqual(
            self.client.get("/assets/fonts/golos-text-cyrillic.woff2").status_code, 200)
        # The icons the map itself uses, from vendor/icons where it keeps them.
        for icon in ("pauk-frame.png", "pauk-frame-8x.png", "pauk-web-4x.png"):
            self.assertEqual(self.client.get(f"/assets/icons/{icon}").status_code, 200, icon)

    def test_the_admin_port_serves_nothing_else_from_the_map(self):
        # Only the two asset paths are mounted. Mounting the whole web
        # directory would put the map's data dump on the admin port too.
        for path in ("/assets/graph-data.js", "/assets/index.html", "/assets/style.css"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_every_page_carries_the_same_title(self):
        # One fixed title everywhere, so the tab reads the same wherever
        # you are in the panel.
        self.assertIn("<title>Admin PAUK</title>", self.client.get("/login").text)
        self.sign_in()
        # Pages that need the graph are covered in GraphUnavailableTest;
        # here the point is only that the title never changes.
        self.assertIn("<title>Admin PAUK</title>", self.client.get("/").text)

    def test_the_logo_is_the_tab_icon_and_the_way_home(self):
        self.sign_in()
        body = self.client.get("/").text
        self.assertIn('rel="icon"', body)
        self.assertIn('<a href="/" class="logo"', body)

    def test_the_root_and_the_tag_show_the_same_icon(self):
        # A browser asks for /favicon.ico by itself on the site root, and
        # uses the <link> tag elsewhere. Two different pictures meant the
        # spider appeared on /nodes/... and nowhere on /.
        served = self.client.get("/favicon.ico").content
        linked = self.client.get("/assets/icons/pauk-frame.png").content
        self.assertEqual(served, linked)

    def test_the_icon_is_the_spider_and_not_the_web(self):
        # pauk-web.png is, despite the name, a cobweb; the spider is
        # pauk-frame.png. Putting the wrong one in the tab is easy and
        # invisible from the code alone.
        self.sign_in()
        body = self.client.get("/").text
        self.assertIn("pauk-frame", body)
        self.assertNotIn("pauk-web", body)

    def test_the_panel_is_light_only(self):
        css = self.client.get("/static/panel.css").text
        self.assertNotIn("data-theme", css)
        self.assertNotIn("prefers-color-scheme", css)

    def test_all_three_font_ranges_are_served(self):
        # The map ships three: latin, cyrillic and cyrillic-ext. Missing one
        # drops that range to a system fallback mid-sentence.
        for part in ("latin", "cyrillic", "cyrillic-ext"):
            self.assertEqual(
                self.client.get(f"/assets/fonts/golos-text-{part}.woff2").status_code, 200, part)
        self.assertEqual(self.client.get("/static/panel.css").text.count("@font-face"), 3)

    def test_the_panel_offers_no_way_to_create_an_account(self):
        # Accounts come from `pauk admin user add` only; a registration
        # route would be a way in past the shell.
        routes = {getattr(route, "path", "") for route in self.client.app.routes}
        self.assertNotIn("/register", routes)
        self.assertNotIn("/signup", routes)


if __name__ == "__main__":
    unittest.main()


class GraphUnavailableTest(unittest.TestCase):
    """What the panel does when Neo4j is not configured or not running."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="admin")
        # No NEO4J_PASSWORD: exactly the state a fresh checkout is in.
        self.client = TestClient(build(Settings(neo4j_password=""), self.db),
                                 follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def test_a_missing_password_is_503_not_500(self):
        response = self.client.get("/nodes/Person")
        self.assertEqual(response.status_code, 503)

    def test_a_browser_is_told_what_is_wrong_in_words(self):
        response = self.client.get("/nodes/Person", headers={"accept": "text/html"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("База графа не отвечает", response.text)
        self.assertIn("NEO4J_PASSWORD", response.text)

    def test_the_parts_that_do_not_need_the_graph_keep_working(self):
        # Signing in and the overview read Mongo only, so an unreachable
        # graph must not lock people out of the panel entirely.
        self.assertEqual(self.client.get("/").status_code, 200)


class OverviewTest(unittest.TestCase):
    """The overview page and what the number beside a label means."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="admin")
        self.client = TestClient(build(Settings(neo4j_password=""), self.db),
                                 follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def test_the_overview_opens_even_with_no_graph(self):
        # Counting needs Neo4j; the page must not depend on it.
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("не отвечает", response.text)

    def test_labels_are_listed_and_link_to_their_search(self):
        body = self.client.get("/").text
        for label in ("Person", "Repository", "GitHubProfile"):
            self.assertIn(f'href="/nodes/{label}"', body)

    def test_both_numbers_are_labelled_so_neither_is_guessed_at(self):
        # A bare "Person 34" read as thirty-four people; it was the field
        # count. Both numbers are now spelled out.
        from unittest import mock
        counts = dict.fromkeys(
            ("Person", "Repository", "Publication", "Department",
             "Organization", "GitHubProfile", "LinkCandidate"), 0)
        counts["Person"] = 3
        with mock.patch.object(self.client.app.state.graph, "audited"), \
             mock.patch("pauk.admin.app.count_nodes", return_value=counts):
            body = " ".join(self.client.get("/").text.split())
        # The number sits in its own span, so match around the markup.
        self.assertIn(">3</span> узла", body)
        self.assertIn("34 поля", body)
        self.assertIn(">0</span> узлов", body)

    def test_the_numbers_agree_with_the_noun(self):
        from pauk.admin.deps import plural
        self.assertEqual(plural(1, "узел", "узла", "узлов"), "узел")
        self.assertEqual(plural(2, "узел", "узла", "узлов"), "узла")
        self.assertEqual(plural(5, "узел", "узла", "узлов"), "узлов")
        self.assertEqual(plural(11, "узел", "узла", "узлов"), "узлов")
        self.assertEqual(plural(21, "узел", "узла", "узлов"), "узел")

    def test_the_overview_asks_the_driver_not_to_wait_or_retry(self):
        # Retries suit a batch job: the driver backs off for tens of seconds
        # on an unreachable host, and a page rendered for a person cannot.
        # Driver options now belong to the shared graph, opened once for
        # the service rather than per page.
        from pauk.admin.app import COUNT_TIMEOUT
        self.assertLessEqual(COUNT_TIMEOUT, 5)

    def test_a_graph_that_errors_does_not_take_the_overview_down(self):
        from unittest import mock
        with mock.patch.object(self.client.app.state.graph, "audited",
                               side_effect=OSError("no route to host")):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("не отвечает", response.text)

    def test_the_logo_is_the_only_way_home(self):
        # A "Граф" item beside it repeated the same action and read as
        # filler; the logo carries it alone.
        header = self.client.get("/").text.split("<header>")[1].split("</header>")[0]
        self.assertIn('href="/" class="logo"', header)
        self.assertNotIn("Граф", header)

    def test_the_feed_is_a_section_of_its_own_in_the_header(self):
        header = self.client.get("/").text.split("<header>")[1].split("</header>")[0]
        self.assertIn('class="section', header)
        self.assertIn("Журнал правок", header)


class ActorContextTest(unittest.TestCase):
    """Naming the actor must survive FastAPI's thread pool."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        from tests.unit.test_admin_nodes import FakePanelGraph
        self.graph = FakePanelGraph()
        self.graph.nodes[("Person", "A1")] = {"id": "A1", "name_ru": "Иван"}
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def test_a_request_through_the_graph_leaves_no_error_behind(self):
        # A generator dependency is entered and resumed in different
        # contexts, so resetting a contextvar token across that boundary
        # raises "created in a different Context" — after the response has
        # already been sent, which is why it only ever showed in the log.
        from unittest import mock
        with mock.patch.object(self.client.app.state.graph, "audited",
                               return_value=self.graph):
            response = self.client.get("/nodes/Person/A1")
        self.assertEqual(response.status_code, 200)

    def test_the_edit_is_recorded_under_the_signed_in_user(self):
        from unittest import mock
        token = self.client.cookies[COOKIE]
        csrf = self.db[SESSIONS].find_one({"_id": token})["csrf"]
        with mock.patch.object(self.client.app.state.graph, "audited",
                               return_value=self.graph):
            self.client.post("/nodes/Person/A1", data={"csrf": csrf, "name_ru": "Пётр"})
        (override,) = list(self.db["graph_overrides"].find())
        self.assertEqual(override["actor"], "user:roman")

    def test_the_client_is_told_who_is_editing(self):
        # The name has to reach the client itself. A contextvar set in the
        # dependency is invisible in the route — it runs in another
        # context — and every entry came out as "unknown".
        from unittest import mock
        with mock.patch.object(self.client.app.state.graph, "audited",
                               return_value=self.graph) as opened:
            self.client.get("/nodes/Person/A1")
        _, options = opened.call_args
        self.assertEqual(options["actor"], "user:roman")
        self.assertEqual(options["source"], "admin-ui")

    def test_setting_the_actor_twice_does_not_raise(self):
        # What the block form could not do here: leave one scope and enter
        # another from a different context.
        from pauk.graph.audit import set_actor
        set_actor("user:one", source="admin-ui")
        set_actor("user:two", source="admin-ui")
        from pauk.graph import audit
        self.assertEqual(audit._actor_var.get(), "user:two")


class StylesheetVersionTest(unittest.TestCase):
    """The stylesheet address changes when the file does."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="admin")
        self.client = TestClient(build(Settings(neo4j_password=""), self.db))
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def test_the_link_carries_a_version(self):
        # Browsers hold CSS in cache firmly enough that a layout fix could
        # miss an open tab entirely — the header and the filters stayed in
        # their old arrangement while the file already differed.
        body = self.client.get("/").text
        self.assertRegex(body, r'href="/static/panel\.css\?v=\d+"')

    def test_the_versioned_address_is_served(self):
        import re
        body = self.client.get("/").text
        href = re.search(r'href="(/static/panel\.css\?v=\d+)"', body).group(1)
        self.assertEqual(self.client.get(href).status_code, 200)

    def test_the_version_follows_the_file(self):
        import pathlib
        import re

        from pauk.admin.app import build as build_app
        css = pathlib.Path("pauk/admin/static/panel.css")
        before = re.search(r'\?v=(\d+)', self.client.get("/").text).group(1)
        css.touch()
        client = TestClient(build_app(Settings(neo4j_password=""), self.db))
        client.post("/login", data={"login": "roman", "password": "hunter2"})
        after = re.search(r'\?v=(\d+)', client.get("/").text).group(1)
        self.assertNotEqual(before, after)


class SharedDriverTest(unittest.TestCase):
    """One driver for the service, opened once and closed once.

    Routes are sync, so FastAPI runs them in a threadpool and the first
    requests really do arrive together: without a lock each of them built
    its own driver and every loser's connection pool stayed open with
    nothing holding it. And nothing closed the survivor either — the
    wrappers deliberately do not, which leaves exactly one place that must.
    """

    def setUp(self):
        self.built, self.closed = [], []
        test = self

        class RecordingShared:
            def __init__(self, *args, **kwargs):
                test.built.append(self)

            def audited(self, **who):
                return object()

            def close(self):
                test.closed.append(self)

        self.patch = patch.object(app_module, "SharedGraph", RecordingShared)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_only_one_driver_is_built_under_a_burst(self):
        lazy = app_module._LazyGraph(Settings(), None)
        start = threading.Barrier(8)

        def hit():
            start.wait()
            lazy.audited(actor="user:roman", source="admin-ui")

        threads = [threading.Thread(target=hit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.built), 1)

    def test_the_driver_is_closed_when_the_service_stops(self):
        db = mongomock.MongoClient()["pauk_test"]
        application = build(Settings(), db)
        application.state.graph.audited(actor="user:roman", source="admin-ui")
        with TestClient(application):
            pass
        self.assertEqual(len(self.closed), 1)

    def test_stopping_without_ever_touching_the_graph_is_fine(self):
        db = mongomock.MongoClient()["pauk_test"]
        with TestClient(build(Settings(), db)):
            pass
        self.assertEqual(self.built, [])
