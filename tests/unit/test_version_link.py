import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.cache.graph_snapshot import write_snapshot
from pauk.graph import version_link
from pauk.graph.overrides import DELETE, LINK, record_relationship_override
from pauk.graph.version_judge import VERDICTS_COLLECTION, VersionJudge, build_prompt, parse_verdict, stored_verdicts
from pauk.graph.version_link import edge_direction, plan_version_edges, run_version_link, run_version_pair
from pauk.gui.graph_builder.builder import write_site_data
from pauk.gui.graph_builder.tests.test_graph_builder import BuildGraphDataIntegrationTest
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
        self.assertEqual(edges[("W1", "W2")]["status"], "same")
        self.assertEqual(edges[("W1", "W2")]["confirmation_source"], "auto")
        self.assertEqual(edges[("W1", "W2")]["confirmation_policy"], "rules-v1")
        self.assertEqual(result["version_edges"], 1)
        self.assertEqual(result["version_auto_confirmed"], 1)
        self.assertEqual(result["version_manual_pending"], 0)
        self.assertEqual(self.db[review.COLLECTION].count_documents({}), 0)

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

    def test_a_held_edge_is_confirmed_after_the_pair_drops_out_of_the_rules(self):
        pub(self.graph, "W1", "Graphene", doi="10.1/a")
        pub(self.graph, "W2", "Spiders", doi="10.1/b")
        self.graph.edges[("Publication", "IS_VERSION", "Publication", "W1", "W2")] = {
            "bucket": "STRONG_FUZZY", "status": "held"}
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME)
        self.assertEqual(self.run_link()["version_edges"], 1)
        self.assertEqual(versions(self.graph)[("W1", "W2")]["status"], "same")
        self.assertEqual(self.run_link()["version_edges"], 0)

    def test_late_same_recovers_the_edge_the_current_run_removed(self):
        pub(self.graph, "W1", "Graphene", doi="10.1/a")
        pub(self.graph, "W2", "Spiders", doi="10.1/b")
        self.graph.edges[("Publication", "IS_VERSION", "Publication", "W1", "W2")] = {
            "bucket": "STRONG_FUZZY", "status": "held"}
        self.answered_mid_run(lambda: review.record_verdict(
            self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME))
        self.assertEqual(versions(self.graph), {("W1", "W2"): {
            "bucket": "STRONG_FUZZY", "status": "same", "confirmation_source": "review"}})

    def test_the_newest_answer_wins_when_original_pairs_fold_onto_one(self):
        pub(self.graph, "W1", "One work about spider webs and neural nets", doi="10.1/a")
        pub(self.graph, "W2", "One work about spider webs and neural nets", doi="10.1/b")
        self.graph.nodes["Publication"]["W2"]["merged_ids"] = ["W9"]
        newer = review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.DIFFERENT)
        older = review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W9"], review.SAME)
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        self.db[review.COLLECTION].update_one({"_id": older["_id"]}, {"$set": {"decided_at": moment}})
        self.db[review.COLLECTION].update_one({"_id": newer["_id"]}, {
            "$set": {"decided_at": moment + timedelta(days=1)}})
        self.run_link()
        self.assertEqual(versions(self.graph), {})
        self.db[review.COLLECTION].update_one({"_id": newer["_id"]}, {"$set": {"decided_at": moment}})
        self.assertEqual(review.version_decisions(self.db, {"W9": "W2"}),
                         {frozenset(("W1", "W2")): review.DIFFERENT})

    def test_pair_job_uses_the_latest_answer_and_skips_global_search(self):
        pub(self.graph, "W1", "One work about spider webs and neural nets", doi="10.1/a")
        pub(self.graph, "W2", "One work about spider webs and neural nets", doi="10.1/b")
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.DIFFERENT)
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME)
        with patch.object(version_link, "find_version_candidates") as search, patch.object(
            self.graph, "fetch_publications_for_dedup", wraps=self.graph.fetch_publications_for_dedup,
        ) as read:
            self.assertEqual(run_version_pair(self.config, self.db, ("W1", "W2"))["version_edges"], 1)
            read.assert_called_once_with(["W1", "W2"])
            search.assert_not_called()
        self.assertEqual(run_version_pair(self.config, self.db, ("W1", "W2"))["version_edges"], 0)

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
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME)
        self.run_link()
        self.assertEqual(set(versions(self.graph)), {("W1", "W2")})
        self.assertEqual(versions(self.graph)[("W1", "W2")]["status"], "same")

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

    def answered_mid_run(self, answer):
        """Run link with `answer()` called after it read the answers, before it writes."""
        find = version_link.find_version_candidates

        def find_then_answer(*args, **kwargs):
            report = find(*args, **kwargs)
            answer()
            return report

        with patch.object(version_link, "find_version_candidates", side_effect=find_then_answer):
            return self.run_link()

    def test_different_given_during_the_run_is_not_linked_back(self):
        pub(self.graph, "W1", "One work about spider webs and neural nets", doi="10.1/a")
        pub(self.graph, "W2", "One work about spider webs and neural nets", doi="10.1/b")
        self.graph.edges[("Publication", "IS_VERSION", "Publication", "W2", "W1")] = {
            "bucket": "MISSED_BY_NORM", "status": "held"}
        result = self.answered_mid_run(lambda: review.record_verdict(
            self.db, review.PUB_VERSION, ["W1", "W2"], review.DIFFERENT))
        self.assertEqual(versions(self.graph), {})
        self.assertEqual(result["version_late_answers"], 1)

    def test_same_given_during_the_run_stays_confirmed(self):
        pub(self.graph, "W1", "One work about spider webs and neural nets", doi="10.1/a")
        pub(self.graph, "W2", "One work about spider webs and neural nets", doi="10.1/b")
        self.answered_mid_run(lambda: review.record_verdict(
            self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME))
        (row,) = versions(self.graph).values()
        self.assertEqual(row["status"], "same")

    def test_different_withdrawn_during_the_run_is_linked_again(self):
        pub(self.graph, "W1", "One work about spider webs and neural nets", doi="10.1/a")
        pub(self.graph, "W2", "One work about spider webs and neural nets", doi="10.1/b")
        review.record_held(self.db, [{"status": "held", "entity": "publication", "bucket": "MISSED_BY_NORM",
                                      "record_a": "W1", "record_b": "W2"}])
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.DIFFERENT)
        self.answered_mid_run(lambda: review.withdraw(self.db, review.PUB_VERSION, ["W1", "W2"]))
        (row,) = versions(self.graph).values()
        self.assertEqual((row["bucket"], row["status"]), ("MISSED_BY_NORM", "same"))
        self.assertEqual(row["confirmation_source"], "auto")
        self.assertEqual(review.count(self.db, answered=False), 0)

    def test_an_answered_pair_is_not_asked_again_after_a_fold(self):
        pub(self.graph, "W1", "One work about spider webs and neural nets", doi="10.1/a")
        pub(self.graph, "W2", "One work about spider webs and neural nets", doi="10.1/b")
        self.graph.nodes["Publication"]["W2"]["merged_ids"] = ["W9"]
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W9"], review.SAME)
        self.run_link()
        self.assertEqual(review.count(self.db, answered=False), 0)
        (row,) = versions(self.graph).values()
        self.assertEqual(row["status"], "same")

    def test_different_removes_an_edge_added_by_hand(self):
        pub(self.graph, "W1", "Graphene", doi="10.1/a")
        pub(self.graph, "W3", "Lasers", doi="10.1/c")
        self.graph.edges[("Publication", "IS_VERSION", "Publication", "W1", "W3")] = {}
        record_relationship_override(self.db, "Publication", "IS_VERSION", "Publication",
                                     "W1", "W3", LINK)
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W3"], review.DIFFERENT)
        self.run_link()
        self.assertEqual(versions(self.graph), {})

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
        self.assertEqual(row["status"], "held")
        self.assertNotIn("confirmation_source", row)
        self.assertEqual(result["version_auto_confirmed"], 0)
        self.assertEqual(result["version_manual_pending"], 1)
        self.assertEqual(result["version_llm_calls"], 0)


    def _confident_pair(self):
        title = "One work about spider webs and neural nets"
        pub(self.graph, "W1", title, doi="10.1/a", authors=("A1",))
        pub(self.graph, "W2", title, doi="10.1/b", authors=("A1",))

    def test_auto_confirmation_retires_old_questions_without_faking_a_human_answer(self):
        self._confident_pair()
        nodes_before = {label: {key: props.copy() for key, props in nodes.items()}
                        for label, nodes in self.graph.nodes.items()}
        review.record_held(self.db, version_link.find_version_candidates(self.graph), source=review.GRAPH)
        self.assertEqual(review.count(self.db, answered=False), 1)
        self.graph.edges["Publication", "IS_VERSION", "Publication", "W2", "W1"] = {
            "bucket": "MISSED_BY_NORM", "status": "held"}
        result = self.run_link()
        self.assertEqual(result["version_questions_retired"], 1)
        self.assertEqual(result["version_auto_confirmed"], 1)
        self.assertEqual(result["version_review_confirmed"], 0)
        self.assertEqual(result["version_manual_pending"], 0)
        self.assertEqual(self.db[review.COLLECTION].count_documents({}), 0)
        self.assertEqual(self.graph.nodes, nodes_before)
        again = self.run_link()
        self.assertEqual(again["version_edges"], 0)
        self.assertEqual(again["version_auto_confirmed"], 1)

    def test_old_answered_and_skipped_questions_are_not_erased(self):
        self._confident_pair()
        rows = [{"status": "held", "entity": "publication", "bucket": "MISSED_BY_NORM",
                 "record_a": a, "record_b": b}
                for a, b in (("W1", "W2"), ("W3", "W4"), ("W5", "W6"))]
        review.record_held(self.db, rows)
        self.db[review.COLLECTION].update_one({"members": ["W3", "W4"]}, {
            "$set": {"skipped_at": datetime.now(UTC)}})
        review.record_verdict(self.db, review.PUB_VERSION, ["W5", "W6"], review.SAME)
        self.run_link()
        remaining = list(self.db[review.COLLECTION].find())
        self.assertEqual({tuple(row["members"]) for row in remaining}, {("W3", "W4"), ("W5", "W6")})
        self.assertTrue(any(row.get("verdict") == review.SAME for row in remaining))

    def test_auto_confirmation_does_not_stick_when_evidence_degrades(self):
        self._confident_pair()
        self.run_link()
        self.graph.nodes["Publication"]["W1"]["title"] = "Robust output feedback MPC: an interval observer approach"
        self.graph.nodes["Publication"]["W2"]["title"] = "Robust output feedback MPC for LPV systems using interval observers"
        result = self.run_link()
        (props,) = versions(self.graph).values()
        self.assertEqual((props["bucket"], props["status"]), ("MEDIUM_FUZZY", "held"))
        self.assertNotIn("confirmation_source", props)
        self.assertNotIn("confirmation_policy", props)
        self.assertEqual(result["version_auto_confirmed"], 0)
        self.assertEqual(result["version_manual_pending"], 1)
        self.graph.nodes["Publication"]["W2"]["title"] = (
            "Front Cover: " + self.graph.nodes["Publication"]["W1"]["title"])
        self.run_link()
        (props,) = versions(self.graph).values()
        self.assertEqual(props["bucket"], "COVER")
        self.assertFalse({"status", "confirmation_source", "confirmation_policy"} & props.keys())
        self.graph.nodes["Publication"]["W2"]["title"] = "Unrelated study of distant galaxies and dark matter spectra"
        self.run_link()
        self.assertEqual(versions(self.graph), {})

    def test_manual_same_replaces_auto_provenance_and_survives_rule_changes(self):
        self._confident_pair()
        self.run_link()
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME)
        for title in (self.graph.nodes["Publication"]["W2"]["title"], "Unrelated galaxies and dark matter"):
            self.graph.nodes["Publication"]["W2"]["title"] = title
            result = self.run_link()
            (props,) = versions(self.graph).values()
            self.assertEqual((props["status"], props["confirmation_source"]), ("same", "review"))
            self.assertNotIn("confirmation_policy", props)
            self.assertEqual(result["version_auto_confirmed"], 0)
            self.assertEqual(result["version_review_confirmed"], 1)

    def test_missing_dates_keep_an_exact_title_pair_in_manual_review(self):
        self._confident_pair()
        self.graph.nodes["Publication"]["W2"]["publication_date"] = None
        result = self.run_link()
        self.assertEqual(result["version_auto_confirmed"], 0)
        self.assertEqual(result["version_manual_pending"], 1)
        self.assertEqual(review.count(self.db, answered=False), 1)

    def test_scoped_job_retires_an_auto_question_without_global_search(self):
        self._confident_pair()
        review.record_held(self.db, version_link.find_version_candidates(self.graph))
        with patch.object(version_link, "find_version_candidates") as search:
            run_version_pair(self.config, self.db, ("W1", "W2"))
            search.assert_not_called()
        (props,) = versions(self.graph).values()
        self.assertEqual((props["status"], props["confirmation_source"]), ("same", "auto"))
        self.assertEqual(review.count(self.db, answered=False), 0)

    def test_withdrawn_medium_answer_returns_to_manual_review(self):
        pub(self.graph, "W1", "Robust output feedback MPC: an interval observer approach", doi="10.1/a")
        pub(self.graph, "W2", "Robust output feedback MPC for LPV systems using interval observers", doi="10.1/b")
        self.run_link()
        review.record_verdict(self.db, review.PUB_VERSION, ["W1", "W2"], review.SAME)
        run_version_pair(self.config, self.db, ("W1", "W2"))
        review.withdraw(self.db, review.PUB_VERSION, ["W1", "W2"])
        run_version_pair(self.config, self.db, ("W1", "W2"))
        (props,) = versions(self.graph).values()
        self.assertEqual(props["status"], "held")
        self.assertNotIn("confirmation_source", props)
        self.assertNotIn("confirmation_policy", props)
        self.assertEqual(review.count(self.db, answered=False), 1)

    def test_a_manual_claim_does_not_keep_obsolete_auto_acceptance(self):
        self._confident_pair()
        self.run_link()
        (direction,) = versions(self.graph)
        record_relationship_override(self.db, "Publication", "IS_VERSION", "Publication", *direction, LINK)
        self.graph.nodes["Publication"]["W2"]["title"] = "Unrelated galaxies and dark matter"
        self.run_link()
        (props,) = versions(self.graph).values()
        self.assertFalse({"status", "confirmation_source", "confirmation_policy"} & props.keys())

    def test_automatic_and_manual_acceptance_reach_both_gui_variants(self):
        self._confident_pair()
        pub(self.graph, "W3", "Robust output feedback MPC: an interval observer approach",
            doi="10.1/c", authors=("A1",))
        pub(self.graph, "W4", "Robust output feedback MPC for LPV systems using interval observers",
            doi="10.1/d", authors=("A1",))
        result = self.run_link()
        self.assertEqual((result["version_auto_confirmed"], result["version_manual_pending"]), (1, 1))
        db = BuildGraphDataIntegrationTest._sample_db()
        template = db["publications"][0]
        db["publications"] = [{**template, "id": pid, **props}
                              for pid, props in self.graph.nodes["Publication"].items()]
        db["authorship"] = [{"pid": pid, "per": "A1", "position": 1, "is_corresponding": False}
                            for pid in self.graph.nodes["Publication"]]
        db["pub_depts"] = [{"pid": pid, "did": "d1"} for pid in self.graph.nodes["Publication"]]
        snapshot, output = self.config.data_dir / "snapshot.json", self.config.data_dir / "gui"
        for accepted in (1, 2):
            db["pub_versions"] = [{"version_pid": source, "pid": target, **props}
                                  for (source, target), props in versions(self.graph).items()]
            write_snapshot(snapshot, db)
            write_site_data(snapshot, output, seed=1)
            for variant in ("private", "public"):
                graph = json.loads((output / variant / "graph-data.json").read_text(encoding="utf-8"))
                self.assertEqual(len(graph["version_edges"]), accepted)
                self.assertTrue(all(set(edge) == {"s", "t"} for edge in graph["version_edges"]))
                self.assertNotIn("confirmation_source", json.dumps(graph))
                self.assertNotIn("confirmation_policy", json.dumps(graph))
            if accepted == 1:
                review.record_verdict(self.db, review.PUB_VERSION, ["W3", "W4"], review.SAME)
                run_version_pair(self.config, self.db, ("W3", "W4"))

    def test_even_routed_medium_candidates_require_a_human(self):
        self._confident_pair()
        rows = self.graph.fetch_publications_for_dedup()
        for bucket in ("MEDIUM_FUZZY", "MEDIUM_FUZZY_STRONG", "MEDIUM_FUZZY_LLM_SAME", "MEDIUM_FUZZY_CONF_JOURNAL"):
            with self.subTest(bucket=bucket):
                desired = plan_version_edges(rows, [{
                    "record_a": "W1", "record_b": "W2", "bucket": bucket, "mergeable": True, "llm_verdict": True,
                }], {})
                (props,) = desired.values()
                self.assertEqual(props["status"], "held")
                self.assertIsNone(props["confirmation_source"])
                self.assertIsNone(props["confirmation_policy"])


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
