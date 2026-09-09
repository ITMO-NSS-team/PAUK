import unittest

import mongomock
from fastapi.testclient import TestClient

from pauk.admin import deps
from pauk.admin.app import build
from pauk.admin.auth import create_user
from pauk.settings import Settings
from pauk.storage import review
from tests.unit.test_admin_nodes import FakePanelGraph
from tests.unit.test_review_store import held_group, held_pair


class ReviewPageTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        create_user(self.db, "guest", "hunter2", role="viewer")
        review.record_held(self.db, [
            held_pair("A1", "A2"),
            held_pair("A3", "A4", because="only one person is ITMO-affiliated"),
            held_group(),
        ])
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)

    def sign_in(self, login="roman"):
        self.client.post("/login", data={"login": login, "password": "hunter2"})

    def csrf(self):
        body = self.client.get("/review").text
        return body.split('name="csrf" value="')[1].split('"')[0]

    def answer(self, members, verdict, kind=review.PAIR, note=""):
        return self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": kind, "members": ",".join(members),
            "verdict": verdict, "note": note})

    def test_the_queue_is_closed_without_a_session(self):
        self.assertEqual(self.client.get("/review").status_code, 401)

    def test_the_default_tab_holds_back_the_long_pile(self):
        self.sign_in()
        body = self.client.get("/review").text
        # The pair worth asking about and the refused group, not the 
        # "only one is ITMO" pair.
        self.assertIn("A1", body)
        self.assertNotIn("A4", body)

    def test_every_question_is_reachable(self):
        # Nothing the page filters is hidden from it.
        self.sign_in()
        self.assertIn("A4", self.client.get("/review", params={"tab": "open"}).text)

    def test_a_viewer_sees_the_queue_without_the_buttons(self):
        self.sign_in(login="guest")
        body = self.client.get("/review").text
        self.assertIn("A1", body)
        self.assertNotIn("/review/answer", body)

    def test_a_viewer_cannot_answer(self):
        # The token has to be the viewer's own, from the viewer's own
        # session. Borrowing an editor's token proves only that the CSRF
        # check works, which is a different test.
        self.sign_in(login="guest")
        response = self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.PAIR, "members": "A1,A2",
            "verdict": "different"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(review.count(self.db, answered=True), 0)

    def test_answering_records_the_verdict_and_who_gave_it(self):
        self.sign_in()
        self.assertEqual(self.answer(["A1", "A2"], "different", note="two physicists").status_code,
                         303)
        (row,) = review.questions(self.db, answered=True)
        self.assertEqual(row["verdict"], review.DIFFERENT)
        self.assertEqual(row["actor"], "user:roman")
        self.assertEqual(row["note"], "two physicists")

    def test_a_form_without_its_token_changes_nothing(self):
        self.sign_in()
        response = self.client.post("/review/answer", data={
            "csrf": "stale", "kind": review.PAIR, "members": "A1,A2", "verdict": "same"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(review.count(self.db, answered=True), 0)

    def test_skipping_is_not_an_answer(self):
        self.sign_in()
        self.answer(["A1", "A2"], "skip")
        self.assertEqual(review.decisions(self.db), {})
        self.assertEqual(review.count(self.db, skipped=True), 1)

    def test_a_group_cannot_be_called_one_person(self):
        self.sign_in()
        response = self.answer(["A1", "A2", "A3"], "same", kind=review.GROUP)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(review.count(self.db, answered=True), 0)

    def test_the_group_row_offers_no_such_button(self):
        # The refusal above is the backstop; the page should not put the
        # button there in the first place.
        self.sign_in()
        body = self.client.get("/review").text
        group_row = body.split("группа из")[1].split("</tr>")[0]
        self.assertNotIn('value="same"', group_row)
        self.assertIn("оставить раздельно", group_row)

    def test_an_unknown_verdict_is_refused(self):
        self.sign_in()
        self.assertEqual(self.answer(["A1", "A2"], "maybe").status_code, 400)

    def test_withdrawing_puts_the_question_back_in_the_queue(self):
        self.sign_in()
        self.answer(["A1", "A2"], "different")
        self.assertEqual(review.count(self.db, answered=False, skipped=False), 2)
        response = self.client.post("/review/withdraw", data={
            "csrf": self.csrf(), "kind": review.PAIR, "members": "A1,A2"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(review.count(self.db, answered=True), 0)
        self.assertEqual(review.count(self.db, answered=False, skipped=False), 3)

    def test_names_are_shown_against_the_right_people(self):
        row = held_pair()
        row["person_a"], row["name_a"] = "A9", "Zinaida Orlova"
        row["person_b"], row["name_b"] = "A0", "Ivan Smirnov"
        review.record_held(self.db, [row])
        self.sign_in()
        body = self.client.get("/review").text
        first = body.index("Ivan Smirnov")
        self.assertLess(first, body.index("Zinaida Orlova"))
        self.assertLess(body.index("A0", first - 400), body.index("Zinaida Orlova"))

    def test_the_reason_is_shown_in_russian(self):
        self.sign_in()
        body = self.client.get("/review", params={"tab": "open"}).text
        self.assertIn("одинаковое имя, ничем не подтверждено", body)
        self.assertIn("только один из двоих в ИТМО", body)
        self.assertIn("разных значения поля ORCID", body)


if __name__ == "__main__":
    unittest.main()


class AnsweredTabTest(unittest.TestCase):
    """What the page promises about an answer has to be true of it."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        review.record_held(self.db, [held_pair("A1", "A2"), held_pair("A3", "A4")])
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def body(self):
        return self.client.get("/review", params={"tab": "answered"}).text

    def test_a_merge_says_when_it_will_happen(self):
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.SAME)
        self.assertIn("сольётся на следующем прогоне", self.body())

    def test_a_merge_that_happened_says_so_instead(self):
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.SAME)
        review.mark_applied(self.db, review.PAIR, ["A1", "A2"])
        body = self.body()
        self.assertIn("слито", body)
        self.assertNotIn("сольётся на следующем прогоне", body)

    def test_keeping_two_people_apart_promises_no_merge(self):
        # There is nothing to apply: the answer only stops the rules from
        # merging the pair later.
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.DIFFERENT)
        self.assertNotIn("сольётся", self.body())


class FoldNowTest(unittest.TestCase):
    """A confirmed pair is folded at once when both people are in the graph.

    The queue is fed by two passes. One runs inside a collection, before
    anything is published, and names people who have no nodes yet; the
    other runs over the graph itself. Only the second can be acted on
    immediately, and the page has to say which happened.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        review.record_held(self.db, [held_pair("A1", "A2")])
        self.graph = FakePanelGraph()
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_if_up] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def csrf(self):
        return self.client.get("/review").text.split('name="csrf" value="')[1].split('"')[0]

    def answer(self, verdict="same", members=("A1", "A2")):
        return self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.PAIR,
            "members": ",".join(members), "verdict": verdict})

    def test_two_nodes_are_folded_at_once(self):
        self.graph.add("Person", "A1", name_raw="Ivan Smirnov")
        self.graph.add("Person", "A2", name_raw="Ivan Smirnov")
        response = self.answer()
        self.assertIn("done=merged", response.headers["location"])
        self.assertEqual(set(self.graph.nodes), {("Person", "A1")})
        self.assertEqual(self.graph.nodes[("Person", "A1")]["merged_ids"], ["A2"])
        (row,) = review.questions(self.db, answered=True)
        self.assertIsNotNone(row["applied_at"])

    def test_the_work_of_the_folded_record_comes_along(self):
        # The real client moves the duplicate's edges onto the survivor.
        # A fold that only deleted the node would leave the publication with
        # no author, and nothing on the page would show it.
        self.graph.add("Person", "A1", name_raw="Ivan Smirnov")
        self.graph.add("Person", "A2", name_raw="Ivan Smirnov")
        self.graph.add("Publication", "W1")
        self.graph.relationships[("Person", "AUTHORED", "Publication", "A1", "W1")] = {}
        self.graph.relationships[("Person", "AUTHORED", "Publication", "A2", "W2")] = {}
        self.answer()
        moved = {key[3] for key in self.graph.relationships if key[1] == "AUTHORED"}
        self.assertEqual(moved, {"A1"})

    def test_the_survivor_is_the_one_with_more_work(self):
        # The same rule the dedup uses. Two rules would fold the same pair
        # the other way round depending on who did it.
        self.graph.add("Person", "A1", name_raw="Ivan Smirnov")
        self.graph.add("Person", "A2", name_raw="Ivan Smirnov")
        self.graph.add("Publication", "W1")
        self.graph.relationships[("Person", "AUTHORED", "Publication", "A2", "W1")] = {}
        self.answer()
        self.assertEqual(set(self.graph.nodes) & {("Person", "A1"), ("Person", "A2")},
                         {("Person", "A2")})

    def test_a_pair_with_no_nodes_only_waits(self):
        response = self.answer()
        self.assertIn("done=waiting", response.headers["location"])
        (row,) = review.questions(self.db, answered=True)
        self.assertNotIn("applied_at", row)

    def test_the_decision_stands_even_with_the_graph_down(self):
        # Recording is the valuable half: the rules apply it themselves.
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_if_up] = lambda: None
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"login": "roman", "password": "hunter2"})
        token = client.get("/review").text.split('name="csrf" value="')[1].split('"')[0]
        response = client.post("/review/answer", data={
            "csrf": token, "kind": review.PAIR, "members": "A1,A2", "verdict": "same"})
        self.assertIn("done=waiting", response.headers["location"])
        self.assertEqual(review.decisions(self.db), {frozenset({"A1", "A2"}): review.SAME})

    def test_keeping_two_people_apart_folds_nothing(self):
        self.graph.add("Person", "A1")
        self.graph.add("Person", "A2")
        self.answer(verdict="different")
        self.assertEqual(set(self.graph.nodes), {("Person", "A1"), ("Person", "A2")})


class DisputedTabTest(unittest.TestCase):
    """Where the rules have changed their mind about a settled pair."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        review.record_held(self.db, [held_pair("A1", "A2")])
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.DIFFERENT,
                              actor="user:andrey", note="two physicists")
        review.record_disputed(self.db, [{
            "status": "disputed", "person_a": "A1", "name_a": "Ivan Smirnov",
            "person_b": "A2", "name_b": "Ivan Smirnov", "rule": "same_name"}])
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def body(self, tab="disputed"):
        return self.client.get("/review", params={"tab": tab}).text

    def csrf(self):
        return self.body().split('name="csrf" value="')[1].split('"')[0]

    def test_the_tab_shows_what_the_rules_now_say(self):
        body = self.body()
        self.assertIn("одинаковое имя, и есть чем подтвердить", body)
        self.assertIn("разные люди", body)

    def test_the_answer_is_shown_as_still_in_force(self):
        # Nothing was merged. Saying otherwise would send somebody looking
        # for a merge that never happened.
        self.assertIn("user:andrey", self.body())
        self.assertNotIn("сольётся", self.body())

    def test_confirming_it_again_clears_the_disagreement(self):
        response = self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.PAIR, "members": "A1,A2",
            "verdict": "different", "note": "two physicists", "tab": "disputed"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(review.count(self.db, disputed=True), 0)
        self.assertEqual(review.decisions(self.db),
                         {frozenset({"A1", "A2"}): review.DIFFERENT})

    def test_changing_your_mind_puts_the_question_back(self):
        response = self.client.post("/review/withdraw", data={
            "csrf": self.csrf(), "kind": review.PAIR, "members": "A1,A2",
            "tab": "disputed"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(review.count(self.db, disputed=True), 0)
        self.assertEqual(review.count(self.db, answered=False), 1)

    def test_an_empty_tab_holds_nothing(self):
        # Checked by what the page offers, not by its wording: the sentence
        # is somebody's to reword, the absence of a row is not.
        review.withdraw(self.db, review.PAIR, ["A1", "A2"])
        self.assertNotIn("/review/withdraw", self.body())
        self.assertEqual(review.count(self.db, disputed=True), 0)


class SplitFromThePageTest(unittest.TestCase):
    """The buttons a refused group actually offers."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        review.record_held(self.db, [held_group(("A1", "A2", "A3"))])
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def csrf(self):
        return self.client.get("/review").text.split('name="csrf" value="')[1].split('"')[0]

    def split(self, same):
        # Repeated fields go as a list in the value: httpx encodes a list of
        # pairs as something the form parser does not read back.
        return self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.GROUP, "members": "A1,A2,A3",
            "verdict": "split", "same": list(same)})

    def test_the_checkboxes_reach_the_form(self):
        # They sit in a different cell, so they are tied to the form by id
        # rather than by nesting. A wrong id submits nothing and the page
        # looks like it silently ignored the choice.
        body = self.client.get("/review").text
        form_id = body.split('id="answer-')[1].split('"')[0]
        self.assertEqual(body.count(f'form="answer-{form_id}"'), 3)

    def test_splitting_records_every_pair(self):
        self.assertEqual(self.split(["A1", "A2"]).status_code, 303)
        found = review.decisions(self.db)
        self.assertEqual(found[frozenset({"A1", "A2"})], review.SAME)
        self.assertEqual(found[frozenset({"A1", "A3"})], review.DIFFERENT)
        self.assertEqual(found[frozenset({"A2", "A3"})], review.DIFFERENT)

    def test_splitting_closes_the_group_question(self):
        self.split(["A1", "A2"])
        self.assertEqual(review.count(self.db, kind=review.GROUP, answered=False), 0)

    def test_the_page_says_what_will_happen(self):
        self.assertIn("done=split", self.split(["A1", "A2"]).headers["location"])

    def test_marking_everybody_is_refused(self):
        self.assertEqual(self.split(["A1", "A2", "A3"]).status_code, 400)
        self.assertEqual(review.decisions(self.db), {})

    def test_marking_nobody_is_refused(self):
        self.assertEqual(self.split([]).status_code, 400)
        self.assertEqual(review.decisions(self.db), {})

    def test_a_group_offers_no_plain_merge(self):
        body = self.client.get("/review").text
        self.assertNotIn('value="same"', body)
        self.assertIn('value="split"', body)


class GitHubQuestionTest(unittest.TestCase):
    """An account against the author it may belong to.

    A pair like the others, only its halves are different things: one is a
    node the panel can open, the other lives on GitHub.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        review.record_held(self.db, [{
            "status": "held", "login": "XieN-N", "person": "A5140754163",
            "name_raw": "Stanislav Shtuka", "url": "https://github.com/XieN-N",
            "score": 0.6, "signals": ["name_exact"],
            "repos": ["https://github.com/screemix/Wikontic"],
            "held_because": ["имя совпадает целиком, но больше ничего не подтверждает"],
        }])
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def body(self):
        return self.client.get("/review").text

    def csrf(self):
        return self.body().split('name="csrf" value="')[1].split('"')[0]

    def test_the_account_points_at_github_and_the_person_at_their_card(self):
        # Sending a login to /nodes/Person would open a card for a node that
        # does not exist, and the reviewer needs to look at the profile.
        body = self.body()
        self.assertIn('href="https://github.com/XieN-N"', body)
        self.assertIn("/nodes/Person/A5140754163", body)
        self.assertNotIn("/nodes/Person/XieN-N", body)

    def test_what_the_match_rests_on_is_spelled_out(self):
        body = self.body()
        self.assertIn("имя совпадает целиком", body)
        self.assertIn("https://github.com/screemix/Wikontic", body)

    def test_the_buttons_ask_about_an_account_not_about_people(self):
        body = self.body()
        self.assertIn("это его аккаунт", body)
        self.assertNotIn("один человек", body)

    def test_answering_records_a_github_verdict(self):
        response = self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.GITHUB,
            "members": "A5140754163,XieN-N", "verdict": "same"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(review.github_decisions(self.db),
                         {frozenset({"A5140754163", "XieN-N"}): review.SAME})

    def test_nothing_is_folded_in_the_graph(self):
        # An account is not a node to merge; the collection stage links it
        # on its next run.
        self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.GITHUB,
            "members": "A5140754163,XieN-N", "verdict": "same"})
        (row,) = review.questions(self.db, answered=True)
        self.assertNotIn("applied_at", row)

    def test_the_page_says_when_it_will_take_effect(self):
        self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.GITHUB,
            "members": "A5140754163,XieN-N", "verdict": "same"})
        self.assertIn("привяжется на следующем сборе",
                      self.client.get("/review", params={"tab": "answered"}).text)


class StaffChoiceTest(unittest.TestCase):
    """Choosing which catalog record a person is.

    Not a yes or no. Two namesakes are both plausible and exactly one is
    right, so the answer names a record instead of taking a side.
    """

    RECORDS = ["kuznetsov|andrei|gennadevich", "kuznetsov|andrei|dmitrievich"]

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        review.record_held(self.db, [{
            "status": "held", "person": "A1", "name_raw": "Andrei Kuznetsov",
            "records": self.RECORDS,
            "record_names": ["Кузнецов Андрей Геннадьевич", "Кузнецов Андрей Дмитриевич"],
            "held_because": ["каталог знает несколько человек с таким именем"],
        }])
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def body(self, tab="pressing"):
        return self.client.get("/review", params={"tab": tab}).text

    def csrf(self):
        return self.body().split('name="csrf" value="')[1].split('"')[0]

    def choose(self, chosen):
        return self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.STAFF, "verdict": "choose",
            "members": ",".join(["A1", *self.RECORDS]), "person": "A1",
            "chosen": chosen})

    def test_the_records_are_offered_as_a_choice(self):
        body = self.body()
        self.assertIn("Кузнецов Андрей Геннадьевич", body)
        self.assertIn('type="radio"', body)
        self.assertIn("это выбранная запись", body)
        self.assertIn("никто из них", body)

    def test_only_the_person_has_a_card_to_open(self):
        # A catalog record is a row in a CSV, not a node.
        body = self.body()
        self.assertIn("/nodes/Person/A1", body)
        self.assertNotIn("/nodes/Person/kuznetsov", body)

    def test_choosing_records_the_record(self):
        self.assertEqual(self.choose(self.RECORDS[0]).status_code, 303)
        self.assertEqual(review.staff_choices(self.db), {"A1": self.RECORDS[0]})

    def test_choosing_nobody_settles_the_question_without_a_record(self):
        self.assertEqual(self.choose("").status_code, 303)
        self.assertEqual(review.staff_choices(self.db), {})
        self.assertEqual(review.count(self.db, answered=True), 1)

    def test_a_record_outside_the_question_is_refused(self):
        response = self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.STAFF, "verdict": "choose",
            "members": ",".join(["A1", *self.RECORDS]), "person": "A1",
            "chosen": "somebody|else|entirely"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(review.staff_choices(self.db), {})

    def test_an_answer_about_a_record_is_not_an_answer_about_people(self):
        self.choose(self.RECORDS[0])
        self.assertEqual(review.decisions(self.db), {})

    def test_the_answered_tab_shows_which_record_won(self):
        self.choose(self.RECORDS[0])
        body = self.body("answered")
        self.assertIn("запись выбрана", body)
        self.assertIn("сведёт записи на следующем прогоне", body)
