import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.graph import version_link
from pauk.graph.overrides import DELETE, LINK, record_relationship_override
from pauk.graph.version_judge import VERDICTS_COLLECTION, VersionJudge, build_prompt, parse_verdict, stored_verdicts
from pauk.graph.version_link import edge_direction, plan_version_edges, run_version_link
from pauk.settings import Settings
from pauk.storage import review
from tests.bench.mocks import RecordingNeo4jClient


def pub(client, pid, title, *, doi=None, journal=None, venue_type=None, type="article",
        day="2026-01-01", abstract=None, authors=("P1", "P2")):
    client.nodes.setdefault("Publication", {})[pid] = {
        "title": title, "doi": doi, "journal": journal, "venue_type": venue_type,
        "type": type, "publication_date": day, "abstract": abstract,
    }
    for position, person_id in enumerate(authors, start=1):
        client.nodes.setdefault("Person", {}).setdefault(person_id, {"name_raw": person_id})
        client.edges[("Person", "AUTHORED", "Publication", person_id, pid)] = {"position": position}


def versions(client):
    return {(src, tgt): props for (s, rel, t, src, tgt), props in client.edges.items()
            if rel == "IS_VERSION"}


class _Graph(RecordingNeo4jClient):
    def close(self):
        pass


class VersionLinkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Settings(data_dir=Path(tmp.name))
        self.db = mongomock.MongoClient().db
        self.graph = _Graph()
        patcher = patch.object(version_link, "audited_client", return_value=self.graph)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_link(self, **kwargs):
        return run_version_link(self.config, self.db, **kwargs)

    def test_preprint_points_at_its_article_with_the_bucket(self):
        pub(self.graph, "W1", "Deep learning for spider web classification",
            doi="10.48550/arxiv.2501.00001", journal="arXiv", type="preprint", day="2025-01-01")
        pub(self.graph, "W2", "Deep learning for spider web classification tasks",
            doi="10.3390/s1", journal="Sensors", day="2025-09-01")
        result = self.run_link()
        edges = versions(self.graph)
        self.assertEqual(set(edges), {("W1", "W2")})
        self.assertEqual(edges[("W1", "W2")]["bucket"], "PREPRINT_PUBLISHED")
        self.assertEqual(edges[("W1", "W2")]["status"], "held")
        self.assertEqual(result["version_edges"], 1)
        (asked,) = list(self.db[review.COLLECTION].find())
        self.assertEqual(asked["kind"], review.PUB_VERSION)
        self.assertEqual(asked["members"], ["W1", "W2"])

    def test_a_look_alike_is_linked_but_not_asked_about(self):
        pub(self.graph, "W1", "Hydrogel actuators by autocatalytic reaction networks",
            doi="10.1/a", journal="Advanced Materials")
        pub(self.graph, "W2", "Front Cover: Hydrogel actuators by autocatalytic reaction networks",
            doi="10.1/b", journal="Advanced Materials")
        self.run_link()
        edges = versions(self.graph)
        self.assertEqual(set(edges), {("W2", "W1")})
        self.assertEqual(edges[("W2", "W1")]["bucket"], "COVER")
        self.assertNotIn("status", edges[("W2", "W1")])
        self.assertEqual(self.db[review.COLLECTION].count_documents({}), 0)

    def test_answers_confirm_or_remove_the_edge(self):
        pub(self.graph, "W1", "One work about spider webs and neural nets", doi="10.1/a")
        pub(self.graph, "W2", "One work about spider webs and neural nets", doi="10.1/b")
        pub(self.graph, "W3", "Another study of graphene terahertz response", doi="10.1/c")
        pub(self.graph, "W4", "Another study of graphene terahertz response", doi="10.1/d")
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME)
        review.record_verdict(self.db, review.PUB_VERSION, ["W3", "W4"], review.DIFFERENT)
        self.run_link()
        edges = versions(self.graph)
        self.assertEqual([props["status"] for props in edges.values()], ["same"])
        self.assertFalse({("W3", "W4"), ("W4", "W3")} & set(edges))

    def test_a_declined_pair_stays_declined_without_the_llm(self):
        # The verdict that routed a pair out of the queue must outlive the run
        # that paid for it, or the next run puts it back as plain MEDIUM_FUZZY.
        pub(self.graph, "W1", "Robust output feedback MPC: an interval observer approach",
            doi="10.1/a", journal="Automatica", venue_type="journal", day="2020-12-01")
        pub(self.graph, "W2", "Robust output feedback MPC for LPV systems using interval observers",
            doi="10.1/b", journal="Systems & Control Letters", venue_type="journal", day="2021-08-01")
        self.run_link()
        self.assertEqual(self.db[review.COLLECTION].count_documents({}), 1)
        self.db[VERDICTS_COLLECTION].insert_one(
            {"_id": "W1:W2", "members": ["W1", "W2"], "same_work": False, "reason": "two papers"})
        result = self.run_link()
        (row,) = versions(self.graph).values()
        self.assertEqual(row["bucket"], "MEDIUM_FUZZY_RELATED")
        # Its open question goes too: there is no version left to confirm.
        self.assertEqual(self.db[review.COLLECTION].count_documents({}), 0)
        self.assertEqual(result["version_questions_retired"], 1)

    def test_a_pair_the_rules_drop_loses_its_edge_unless_added_by_hand(self):
        pub(self.graph, "W1", "Graphene", doi="10.1/a")
        pub(self.graph, "W2", "Spiders", doi="10.1/b")
        pub(self.graph, "W3", "Lasers", doi="10.1/c")
        self.graph.edges[("Publication", "IS_VERSION", "Publication", "W1", "W2")] = {"bucket": "STRONG_FUZZY"}
        self.graph.edges[("Publication", "IS_VERSION", "Publication", "W1", "W3")] = {}
        record_relationship_override(self.db, "Publication", "IS_VERSION", "Publication",
                                     "W1", "W3", LINK)
        result = self.run_link()
        self.assertEqual(set(versions(self.graph)), {("W1", "W3")})
        self.assertEqual(result["version_edges_removed"], 1)

    def test_a_confirmed_edge_outlives_the_rules_losing_the_pair(self):
        pub(self.graph, "W1", "Graphene", doi="10.1/a")
        pub(self.graph, "W2", "Spiders", doi="10.1/b")
        self.graph.edges[("Publication", "IS_VERSION", "Publication", "W1", "W2")] = {
            "bucket": "STRONG_FUZZY", "status": "same"}
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME)
        result = self.run_link()
        self.assertEqual(set(versions(self.graph)), {("W1", "W2")})
        self.assertEqual(result["version_edges_removed"], 0)

    def test_an_edge_deleted_by_hand_is_not_written_again(self):
        pub(self.graph, "W1", "Deep learning for spider web classification",
            doi="10.48550/arxiv.2501.00001", journal="arXiv", type="preprint", day="2025-01-01")
        pub(self.graph, "W2", "Deep learning for spider web classification tasks",
            doi="10.3390/s1", journal="Sensors", day="2025-09-01")
        record_relationship_override(self.db, "Publication", "IS_VERSION", "Publication",
                                     "W1", "W2", DELETE)
        result = self.run_link()
        self.assertEqual(versions(self.graph), {})
        self.assertEqual(result["version_edges"], 0)
        self.assertEqual(self.db[review.COLLECTION].count_documents({}), 0)

    def test_an_edge_added_by_hand_follows_a_folded_publication(self):
        pub(self.graph, "W1", "Graphene", doi="10.1/a")
        pub(self.graph, "W2", "Spiders", doi="10.1/b")
        self.graph.nodes["Publication"]["W1"]["merged_ids"] = ["W9"]
        # The fold moved W9 -> W2 over to the survivor; the claim still names W9.
        self.graph.edges[("Publication", "IS_VERSION", "Publication", "W1", "W2")] = {}
        record_relationship_override(self.db, "Publication", "IS_VERSION", "Publication",
                                     "W9", "W2", LINK)
        result = self.run_link()
        self.assertEqual(set(versions(self.graph)), {("W1", "W2")})
        self.assertEqual(result["version_edges_removed"], 0)

    def test_an_edge_deleted_by_hand_stays_deleted_after_a_fold(self):
        pub(self.graph, "W1", "One work about spider webs and neural nets", doi="10.1/a")
        pub(self.graph, "W2", "One work about spider webs and neural nets", doi="10.1/b")
        self.graph.nodes["Publication"]["W1"]["merged_ids"] = ["W9"]
        record_relationship_override(self.db, "Publication", "IS_VERSION", "Publication",
                                     "W9", "W2", DELETE)
        self.run_link()
        self.assertEqual(versions(self.graph), {})
        self.assertEqual(self.db[review.COLLECTION].count_documents({}), 0)

    def test_a_confirmed_pair_that_turns_around_keeps_one_edge(self):
        # W1 has become the newer record, so the rules now point W2 at it.
        pub(self.graph, "W1", "One work about spider webs and neural nets", doi="10.1/a", day="2026-06-01")
        pub(self.graph, "W2", "One work about spider webs and neural nets", doi="10.1/b", day="2026-01-01")
        self.graph.edges[("Publication", "IS_VERSION", "Publication", "W1", "W2")] = {
            "bucket": "MISSED_BY_NORM", "status": "same"}
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME)
        self.run_link()
        edges = versions(self.graph)
        self.assertEqual(set(edges), {("W2", "W1")})
        self.assertEqual(edges[("W2", "W1")]["status"], "same")

    def test_the_hand_made_direction_wins_over_the_rules(self):
        pub(self.graph, "W1", "One work about spider webs and neural nets", doi="10.1/a", day="2026-06-01")
        pub(self.graph, "W2", "One work about spider webs and neural nets", doi="10.1/b", day="2026-01-01")
        self.graph.edges[("Publication", "IS_VERSION", "Publication", "W1", "W2")] = {}
        record_relationship_override(self.db, "Publication", "IS_VERSION", "Publication",
                                     "W1", "W2", LINK)
        self.run_link()
        self.assertEqual(set(versions(self.graph)), {("W1", "W2")})

    def test_a_verdict_follows_a_folded_publication(self):
        pub(self.graph, "W1", "Robust output feedback MPC: an interval observer approach",
            doi="10.1/a", journal="Automatica", venue_type="journal", day="2020-12-01")
        pub(self.graph, "W2", "Robust output feedback MPC for LPV systems using interval observers",
            doi="10.1/b", journal="Systems & Control Letters", venue_type="journal", day="2021-08-01")
        self.graph.nodes["Publication"]["W2"]["merged_ids"] = ["W9"]
        self.db[VERDICTS_COLLECTION].insert_one(
            {"_id": "W1:W9", "members": ["W1", "W9"], "same_work": False, "reason": "two papers"})
        self.run_link()
        (row,) = versions(self.graph).values()
        self.assertEqual(row["bucket"], "MEDIUM_FUZZY_RELATED")

    def test_the_llm_is_asked_before_the_graph_is_held(self):
        pub(self.graph, "W1", "Robust output feedback MPC: an interval observer approach",
            doi="10.1/a", journal="Automatica", venue_type="journal", day="2020-12-01")
        pub(self.graph, "W2", "Robust output feedback MPC for LPV systems using interval observers",
            doi="10.1/b", journal="Systems & Control Letters", venue_type="journal", day="2021-08-01")
        llm = _StubLlm({"same_work": False, "reason": "two papers"})
        locks = self.db["job_locks"]

        def chat_json(prompt):
            self.assertEqual(locks.count_documents({}), 0)
            return llm.reply

        llm.chat_json = chat_json
        with patch.object(version_link, "OpenRouterClient", return_value=llm):
            self.run_link(llm=True)
        (row,) = versions(self.graph).values()
        self.assertEqual(row["bucket"], "MEDIUM_FUZZY_RELATED")

    def test_known_llm_verdict_routes_medium_fuzzy_without_a_call(self):
        # Title similarity in 0.78..0.90, all authors shared: MEDIUM_FUZZY.
        pub(self.graph, "W1", "Robust output feedback MPC: an interval observer approach",
            doi="10.1/a", journal="IEEE Conference on Decision and Control", venue_type="conference",
            day="2020-12-01")
        pub(self.graph, "W2", "Robust output feedback MPC for LPV systems using interval observers",
            doi="10.1/b", journal="IEEE Transactions on Automatic Control", venue_type="journal",
            day="2021-08-01")
        self.run_link()
        (row,) = [props for props in versions(self.graph).values()]
        self.assertEqual(row["bucket"], "MEDIUM_FUZZY")
        self.db[VERDICTS_COLLECTION].insert_one(
            {"_id": "W1:W2", "members": ["W1", "W2"], "same_work": True, "reason": "same MPC scheme"})
        result = self.run_link()
        (row,) = [props for props in versions(self.graph).values()]
        self.assertEqual(row["bucket"], "MEDIUM_FUZZY_LLM_SAME")
        self.assertEqual(row["llm_reason"], "same MPC scheme")
        self.assertEqual(result["version_llm_calls"], 0)


class EdgeDirectionTest(unittest.TestCase):
    def test_article_is_the_target_over_a_preprint(self):
        article = {"id": "W2", "type": "article", "publication_date": "2024-01-01"}
        preprint = {"id": "W1", "type": "preprint", "publication_date": "2025-01-01"}
        self.assertEqual(edge_direction(article, preprint, {}), ("W1", "W2"))

    def test_notice_points_at_the_original(self):
        a, b = {"id": "W1"}, {"id": "W2"}
        self.assertEqual(edge_direction(a, b, {"erratum": "W1", "original": "W2"}), ("W1", "W2"))

    def test_a_different_answer_drops_the_pair(self):
        rows = [{"id": "W1"}, {"id": "W2"}]
        report = [{"record_a": "W1", "record_b": "W2", "bucket": "STRONG_FUZZY", "mergeable": True}]
        self.assertEqual(plan_version_edges(rows, report, {frozenset(("W1", "W2")): "different"}), {})


class _StubLlm:
    model = "stub"

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.last_error = None
        self.last_response = None
        self.last_usage = None

    def chat_json(self, prompt):
        self.calls += 1
        return self.reply


class VersionJudgeTest(unittest.TestCase):
    a = {"id": "W1", "title": "A", "authors": [{"name": f"N{i}"} for i in range(10)], "abstract": "x" * 2000}
    b = {"id": "W2", "title": "B"}

    def test_prompt_caps_authors_and_abstract(self):
        prompt = build_prompt(self.a, self.b)
        self.assertIn("и ещё 2", prompt)
        self.assertNotIn("x" * 1201, prompt)

    def test_a_new_verdict_is_stored_for_later_runs(self):
        db = mongomock.MongoClient().db
        VersionJudge({}, _StubLlm({"same_work": False, "reason": "r"}), store=db)(self.a, self.b)
        self.assertEqual(stored_verdicts(db), {frozenset(("W1", "W2")): {"same_work": False, "reason": "r"}})

    def test_known_verdict_is_reused_unless_forced(self):
        llm = _StubLlm({"same_work": False, "reason": "r"})
        known = {frozenset(("W1", "W2")): {"same_work": True, "reason": "old"}}
        self.assertTrue(VersionJudge(known, llm)(self.a, self.b)["same_work"])
        self.assertEqual(llm.calls, 0)
        self.assertFalse(VersionJudge(dict(known), llm, force=True)(self.a, self.b)["same_work"])
        self.assertEqual(llm.calls, 1)

    def test_a_failed_forced_call_keeps_the_known_verdict(self):
        known = {frozenset(("W1", "W2")): {"same_work": False, "reason": "old"}}
        judge = VersionJudge(dict(known), _StubLlm(None), force=True)
        self.assertEqual(judge(self.a, self.b), {"same_work": False, "reason": "old"})
        self.assertEqual(judge.failed, 1)

    def test_a_malformed_reply_is_no_verdict(self):
        judge = VersionJudge({}, _StubLlm({"same_work": "yes"}))
        self.assertIsNone(judge(self.a, self.b))
        self.assertEqual(judge.failed, 1)
        with self.assertRaises(ValueError):
            parse_verdict({})


if __name__ == "__main__":
    unittest.main()
