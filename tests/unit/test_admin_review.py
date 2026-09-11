import re
import unittest

import mongomock
from fastapi.testclient import TestClient

from pauk.admin import deps
from pauk.admin.app import build
from pauk.admin.auth import create_user
from pauk.graph.jsonl_loader import load_prepared_rows
from pauk.graph.load import ENTITY_FILES
from pauk.graph.mutations import merge_nodes
from pauk.models import Person, Publication
from pauk.settings import Settings
from pauk.storage import PreparedStore, review
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

    def test_marking_everybody_comes_back_with_a_word(self):
        # Back to the queue, not to an error page: the checkboxes are three
        # clicks to redo and a status code explains nothing.
        response = self.split(["A1", "A2", "A3"])
        self.assertEqual(response.status_code, 303)
        self.assertIn("problem=", response.headers["location"])
        self.assertEqual(review.decisions(self.db), {})

    def test_marking_one_comes_back_with_a_word(self):
        response = self.split(["A1"])
        self.assertEqual(response.status_code, 303)
        self.assertIn("problem=", response.headers["location"])
        self.assertEqual(review.decisions(self.db), {})

    def test_marking_nobody_comes_back_with_a_word(self):
        response = self.split([])
        self.assertEqual(response.status_code, 303)
        self.assertIn("problem=", response.headers["location"])
        self.assertEqual(review.decisions(self.db), {})

    def test_the_word_is_shown_on_the_page(self):
        location = self.split(["A1"]).headers["location"]
        self.assertIn("Отметьте хотя бы двоих", self.client.get(location).text)

    def test_leaving_the_group_apart_still_works_with_a_box_ticked(self):
        # The checkbox travels with the form whatever button was pressed;
        # "leave them apart" has no business reading it.
        response = self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.GROUP, "members": "A1,A2,A3",
            "verdict": "different", "same": ["A1"]})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("problem=", response.headers["location"])
        self.assertEqual(review.count(self.db, answered=True), 1)

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
            "held_because": ["the name matches exactly and nothing else backs it"],
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
            "held_because": ["the catalog holds several people under this name"],
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


class ChoiceFormGuardTest(unittest.TestCase):
    """The form has to say who the question is about."""

    RECORDS = ["kuznetsov|andrei|gennadevich", "kuznetsov|andrei|dmitrievich"]

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        review.record_held(self.db, [{
            "status": "held", "person": "A1", "name_raw": "Andrei Kuznetsov",
            "records": self.RECORDS,
            "record_names": ["Кузнецов Андрей Геннадьевич", "Кузнецов Андрей Дмитриевич"],
            "record_degrees": ["к.т.н.", None],
            "held_because": ["the catalog holds several people under this name"],
        }])
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def csrf(self):
        return self.client.get("/review").text.split('name="csrf" value="')[1].split('"')[0]

    def post(self, person):
        return self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.STAFF, "verdict": "choose",
            "members": ",".join(["A1", *self.RECORDS]), "person": person,
            "chosen": self.RECORDS[0]})

    def test_a_person_outside_the_question_is_refused(self):
        self.assertEqual(self.post("A9").status_code, 400)
        self.assertEqual(review.staff_choices(self.db), {})

    def test_no_person_at_all_is_refused(self):
        # Left through, it builds a key with a blank segment and an answer
        # about nobody.
        self.assertEqual(self.post("").status_code, 400)
        self.assertEqual(review.staff_choices(self.db), {})

    def test_the_degree_is_shown_where_the_catalog_has_one(self):
        # It is often what tells two namesakes apart.
        self.assertIn("к.т.н.", self.client.get("/review").text)


class QuestionKindIsVisibleTest(unittest.TestCase):
    """Four different questions sit in one table, one under another.

    Which one a row is used to be readable only off the buttons beside it,
    and only if you already knew what those meant.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        review.record_held(self.db, [
            held_pair("A1", "A2"),
            held_group(("B1", "B2", "B3")),
            {"status": "held", "login": "XieN-N", "person": "A9",
             "name_raw": "Stanislav Shtuka", "url": "https://github.com/XieN-N",
             "signals": ["name_exact"], "repos": [],
             "held_because": ["the name matches exactly and nothing else backs it"]},
            {"status": "held", "person": "A8", "name_raw": "Andrei Kuznetsov",
             "records": ["a|b|c", "a|b|d"], "record_names": ["Кузнецов А. Б.", "Кузнецов А. Д."],
             "held_because": ["the catalog holds several people under this name"]},
        ])
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def test_every_row_says_what_it_asks_about(self):
        body = self.client.get("/review", params={"tab": "open"}).text
        for said in ("две записи", "группа из 3", "аккаунт GitHub", "запись каталога"):
            with self.subTest(said=said):
                self.assertIn(said, body)

    def test_the_section_is_not_named_after_duplicates_alone(self):
        # Three of the four questions are not about duplicates at all.
        body = self.client.get("/review").text
        self.assertIn("Спорные случаи", body)
        self.assertNotIn("Разбор дублей", body)


class ColumnsSayWhoSpeaksTest(unittest.TestCase):
    """Three different voices used to sit in one cell.

    What the rules collected, what a person decided, and what the rules say
    now read as one list, and nothing told them apart.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        create_user(self.db, "guest", "hunter2", role="viewer")
        review.record_held(self.db, [held_pair("A1", "A2")])
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.DIFFERENT,
                              actor="user:andrey", note="разные кафедры")
        review.record_disputed(self.db, [{
            "status": "disputed", "person_a": "A1", "name_a": "A", "person_b": "A2",
            "name_b": "B", "rule": "same_name"}])
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)

    def cells(self, login="roman"):
        self.client.post("/login", data={"login": login, "password": "hunter2"})
        body = self.client.get("/review", params={"tab": "answered"}).text
        row = body.split("<tr>")[2]
        return re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)

    def test_what_the_rules_collected_stands_alone(self):
        known = self.cells()[1]
        self.assertIn("соавторов", known)
        self.assertNotIn("user:andrey", known)
        self.assertNotIn("разные люди", known)

    def test_what_a_person_decided_stands_alone(self):
        decided = self.cells()[3]
        self.assertIn("разные люди", decided)
        self.assertIn("user:andrey", decided)
        self.assertNotIn("соавторов", decided)

    def test_what_the_rules_say_now_sits_by_the_old_reason(self):
        # It is the rules changing their mind, not part of the answer.
        why = self.cells()[2]
        self.assertIn("теперь связывают", why)

    def test_a_viewer_sees_the_answer_without_the_buttons(self):
        # The column used to appear only for an editor, so a viewer could
        # not see what had been decided at all.
        decided = self.cells(login="guest")[3]
        self.assertIn("разные люди", decided)
        self.assertNotIn("/review/withdraw", decided)


class QuestionWithNoEvidenceTest(unittest.TestCase):
    """A question that came into being as an answer.

    `record_split` writes verdicts about pairs the rules never held, so the
    document has members and nothing else. The page has to stay readable.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.DIFFERENT,
                              actor="user:roman")
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def cells(self):
        body = self.client.get("/review", params={"tab": "answered"}).text
        return re.findall(r"<td[^>]*>(.*?)</td>", body.split("<tr>")[2], re.S)

    def test_the_row_still_says_who_it_is_about(self):
        # Pairing members with an empty name list used to drop every subject
        # and leave the row about nobody.
        about = self.cells()[0]
        self.assertIn("A1", about)
        self.assertIn("A2", about)

    def test_a_missing_reason_is_a_dash_not_an_empty_cell(self):
        # Otherwise the only thing in the cell is the green "the rules now
        # say" chip, and it reads as the reason.
        review.record_disputed(self.db, [{
            "status": "disputed", "person_a": "A1", "name_a": "A", "person_b": "A2",
            "name_b": "B", "rule": "same_name"}])
        why = self.cells()[2]
        self.assertIn("—", why)
        self.assertIn("теперь связывают", why)


class SkippedTabTest(unittest.TestCase):
    """What is offered on a question somebody already put off."""

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        review.record_held(self.db, [held_pair("A1", "A2")])
        review.skip(self.db, review.PAIR, ["A1", "A2"], actor="user:roman")
        self.client = TestClient(build(Settings(), self.db), follow_redirects=False)
        self.client.post("/login", data={"login": "roman", "password": "hunter2"})

    def body(self, tab):
        return self.client.get("/review", params={"tab": tab}).text

    def test_putting_it_off_again_is_not_offered(self):
        skipped = self.body("skipped")
        self.assertNotIn('value="skip"', skipped)
        self.assertIn("отложил user:roman", skipped)

    def test_answering_it_still_is(self):
        # The point of the tab is to come back and decide.
        skipped = self.body("skipped")
        self.assertIn('value="same"', skipped)
        self.assertIn('value="different"', skipped)

    def test_an_untouched_question_still_offers_it(self):
        review.record_held(self.db, [held_pair("B1", "B2")])
        self.assertIn('value="skip"', self.body("pressing"))


class SplitBackTest(unittest.TestCase):
    """Undoing a fold from the page it was made on.

    The pair is folded through the panel first, so what the button takes
    apart is a real merge and not a graph arranged to look like one.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        create_user(self.db, "roman", "hunter2", role="editor")
        create_user(self.db, "guest", "hunter2", role="viewer")
        review.record_held(self.db, [held_pair("A1", "A2")])
        self.prepared = PreparedStore(self.db, "sample")
        self.prepared.write_models("persons", [
            Person(id="A1", openalex_id="A1", name_raw="Ivan Smirnov", is_itmo=True,
                   authored=[{"publication_id": "W1", "position": 1}]),
            Person(id="A2", openalex_id="A2", name_raw="I. Smirnov", is_itmo=False,
                   authored=[{"publication_id": "W2", "position": 1}]),
        ])
        self.prepared.write_models("publications", [
            Publication(id="W1", title="W1"), Publication(id="W2", title="W2")])
        self.graph = FakePanelGraph()
        load_prepared_rows(self.graph, {
            filename: list(self.prepared.read_rows(entity))
            for entity, filename in ENTITY_FILES.items()})
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_if_up] = lambda: self.graph
        self.client = TestClient(app, follow_redirects=False)
        self.sign_in()

    def sign_in(self, login="roman"):
        self.client.post("/login", data={"login": login, "password": "hunter2"})

    def body(self):
        return self.client.get("/review", params={"tab": "answered"}).text

    def csrf(self):
        return self.body().split('name="csrf" value="')[1].split('"')[0]

    def fold(self):
        self.client.post("/review/answer", data={
            "csrf": self.csrf(), "kind": review.PAIR, "members": "A1,A2", "verdict": "same"})

    def split_back(self):
        return self.client.post("/review/split-back", data={
            "csrf": self.csrf(), "kind": review.PAIR, "members": "A1,A2", "tab": "answered"})

    def test_an_applied_answer_offers_it(self):
        self.fold()
        self.assertIn("/review/split-back", self.body())

    def test_and_does_not_offer_to_only_take_the_answer_back(self):
        # Withdrawing alone would leave one node where the person expects
        # two, and the button would be a lie about what it does.
        self.fold()
        self.assertNotIn("/review/withdraw", self.body())

    def test_an_answer_still_waiting_offers_the_plain_undo(self):
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.SAME)
        body = self.body()
        self.assertIn("/review/withdraw", body)
        self.assertNotIn("/review/split-back", body)

    def test_the_record_and_its_work_come_back(self):
        self.fold()
        self.assertEqual(self.split_back().status_code, 303)
        self.assertIn(("Person", "A2"), self.graph.nodes)
        authored = {(src_id, tgt_id) for _s, rel, _t, src_id, tgt_id
                    in self.graph.relationships if rel == "AUTHORED"}
        self.assertEqual(authored, {("A1", "W1"), ("A2", "W2")})

    def test_the_answer_flips_so_the_next_run_agrees(self):
        self.fold()
        self.split_back()
        (row,) = review.questions(self.db, answered=True)
        self.assertEqual(row["verdict"], review.DIFFERENT)
        self.assertNotIn("applied_at", row)
        self.assertEqual(review.decisions(self.db), {frozenset({"A1", "A2"}): review.DIFFERENT})

    def test_the_page_says_it_happened(self):
        self.fold()
        location = self.split_back().headers["location"]
        self.assertIn("done=apart", location)
        self.assertIn("Записи разделены", self.client.get(location).text)

    def test_with_no_row_to_rebuild_from_the_fold_stands(self):
        self.fold()
        self.db.persons.delete_one({"id": "A2"})
        location = self.split_back().headers["location"]
        self.assertIn("problem=", location)
        self.assertIn("Вернуть запись нечем", self.client.get(location).text)
        self.assertNotIn(("Person", "A2"), self.graph.nodes)
        (row,) = review.questions(self.db, answered=True)
        self.assertEqual(row["verdict"], review.SAME)
        self.assertIsNotNone(row["applied_at"])

    def test_with_the_graph_down_nothing_is_rewritten(self):
        # The store must not say the pair came apart while the graph still
        # holds one node for it.
        self.fold()
        app = build(Settings(), self.db)
        app.dependency_overrides[deps.graph_if_up] = lambda: None
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"login": "roman", "password": "hunter2"})
        token = client.get("/review", params={"tab": "answered"}) \
            .text.split('name="csrf" value="')[1].split('"')[0]
        location = client.post("/review/split-back", data={
            "csrf": token, "kind": review.PAIR, "members": "A1,A2"}).headers["location"]
        self.assertIn("problem=", location)
        (row,) = review.questions(self.db, answered=True)
        self.assertEqual(row["verdict"], review.SAME)

    def test_a_viewer_cannot_split_anything(self):
        self.fold()
        self.sign_in(login="guest")
        response = self.client.post("/review/split-back", data={
            "csrf": self.csrf(), "kind": review.PAIR, "members": "A1,A2"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(("Person", "A2"), self.graph.nodes)

    def test_a_fold_this_answer_did_not_make_is_left_alone(self):
        # Folded by a pipeline run instead of from here, so the answer
        # carries no applied_at. Undoing it would take the graph apart and
        # then fail to rewrite the answer, leaving the two disagreeing.
        review.record_verdict(self.db, review.PAIR, ["A1", "A2"], review.SAME)
        merge_nodes(self.graph, "Person", "A2", "A1")
        response = self.client.post("/review/split-back", data={
            "csrf": self.csrf(), "kind": review.PAIR, "members": "A1,A2"})
        self.assertIn("problem=", response.headers["location"])
        self.assertNotIn(("Person", "A2"), self.graph.nodes)
