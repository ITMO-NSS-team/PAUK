import unittest

import mongomock

from pauk.graph.jsonl_loader import extract_repo_links, load_prepared_rows, normalize_repo_url
from pauk.graph.load import ENTITY_FILES
from pauk.graph.mutations import merge_nodes
from pauk.graph.unmerge import split_person
from pauk.models import Person, Publication
from pauk.storage import PreparedStore
from pauk.urls import deduplicate_repo_urls
from tests.bench.mocks import RecordingNeo4jClient
from tests.unit.test_admin_nodes import FakePanelGraph


class NormalizeRepoUrlTest(unittest.TestCase):
    def test_case_and_cosmetic_suffixes_are_ignored(self):
        canonical = normalize_repo_url("https://github.com/Org/Repo")
        for variant in (
            "https://github.com/org/repo",
            "https://github.com/Org/Repo/",
            "https://github.com/Org/Repo.git",
            "https://www.github.com/Org/Repo",
            "https://www.github.com/Org/Repo.GIT",
            "http://github.com/Org/Repo",
            "HTTP://WWW.GITHUB.COM/Org/Repo.git/",
        ):
            self.assertEqual(normalize_repo_url(variant), canonical)

    def test_deduplication_preserves_the_first_spelling(self):
        urls = ["http://github.com/Org/Repo", "https://github.com/org/repo.git/"]
        self.assertEqual(deduplicate_repo_urls(urls), urls[:1])

    def test_non_http_schemes_are_not_rewritten(self):
        self.assertEqual(normalize_repo_url("ssh://github.com/org/repo"), "ssh://github.com/org/repo")


class MentionsSynchronizationTest(unittest.TestCase):
    URL = "https://github.com/org/repo"
    OLD = "https://github.com/org/old"

    def rows(self, links, status="completed"):
        return {
            "publications.jsonl": [{"id": "W1", "_processing": {"code_links": {"status": status}}}],
            "repositories.jsonl": [{"id": "R1", "url": self.URL}],
            "repo_links.jsonl": [{"publication_id": "W1", "links": [{"url": url} for url in links]}],
        }

    def test_republish_removes_stale_repository_and_candidate_edges_only(self):
        client = RecordingNeo4jClient()
        rows = self.rows([self.URL, self.OLD])
        rows["repositories.jsonl"].append({"id": "R2", "url": self.OLD})
        load_prepared_rows(client, rows)
        load_prepared_rows(client, self.rows([self.URL, "https://github.com/org/candidate"]))
        self.assertNotIn(("Publication", "MENTIONS_LINK", "Repository", "W1", self.OLD), client.edges)
        load_prepared_rows(client, self.rows([self.URL]))
        self.assertEqual(list(client.edges), [("Publication", "MENTIONS_LINK", "Repository", "W1", self.URL)])
        self.assertIn("R2", client.nodes["Repository"])

    def test_completed_empty_clears_links_without_touching_other_publications(self):
        client = RecordingNeo4jClient()
        rows = self.rows([self.URL])
        rows["publications.jsonl"].append({"id": "W2"})
        rows["repo_links.jsonl"].append({"publication_id": "W2", "links": [{"url": self.URL}]})
        load_prepared_rows(client, rows)
        load_prepared_rows(client, self.rows([], "completed_empty"))
        self.assertEqual(list(client.edges), [("Publication", "MENTIONS_LINK", "Repository", "W2", self.URL)])

    def test_incomplete_extraction_does_not_remove_evidence(self):
        for status in ("failed", "pending", "running", None):
            with self.subTest(status=status):
                client = RecordingNeo4jClient()
                load_prepared_rows(client, self.rows([self.URL]))
                load_prepared_rows(client, self.rows([], status))
                self.assertEqual(len(client.edges), 1)

    def test_missing_or_partial_repo_link_is_not_an_empty_result(self):
        for repo_links in ([], [{"publication_id": "W1"}]):
            with self.subTest(repo_links=repo_links):
                client = RecordingNeo4jClient()
                load_prepared_rows(client, self.rows([self.URL]))
                rows = self.rows([])
                rows["repo_links.jsonl"] = repo_links
                load_prepared_rows(client, rows)
                self.assertEqual(len(client.edges), 1)

    def test_manual_claims_survive_cleanup(self):
        client = RecordingNeo4jClient()
        load_prepared_rows(client, self.rows([self.URL, self.OLD]))
        claims = {("Publication", "MENTIONS_LINK", "Repository", "W1", self.URL),
                  ("Publication", "MENTIONS_LINK", "LinkCandidate", "W1", self.OLD)}
        load_prepared_rows(client, self.rows([], "completed_empty"), claimed_relationships=claims)
        self.assertEqual(len(client.edges), 2)

    def test_legacy_publication_links_do_not_resurrect_removed_urls(self):
        client = RecordingNeo4jClient()
        rows = self.rows([])
        rows["publications.jsonl"][0]["mentions_links"] = [
            {"target_kind": "repository", "repository_url": self.URL},
        ]
        load_prepared_rows(client, rows)
        self.assertEqual(client.edges, {})

    def test_http_link_uses_existing_https_repository(self):
        client = RecordingNeo4jClient()
        load_prepared_rows(client, self.rows([self.URL.replace("https:", "http:")]))
        self.assertEqual(list(client.edges), [("Publication", "MENTIONS_LINK", "Repository", "W1", self.URL)])

    def test_folded_repository_is_not_removed_by_cleanup(self):
        client = RecordingNeo4jClient()
        client.upsert_nodes_batch("Repository", [("R0", {"url": self.OLD, "merged_ids": ["R1"]})])
        load_prepared_rows(client, self.rows([self.URL]))
        self.assertEqual(len(client.edges), 1)
        self.assertIn("R0", client.nodes["Repository"])

    def test_promoted_manual_candidate_claim_survives_cleanup(self):
        client = RecordingNeo4jClient()
        rows = self.rows([self.URL])
        rows["repositories.jsonl"] = []
        load_prepared_rows(client, rows)
        claims = {("Publication", "MENTIONS_LINK", "LinkCandidate", "W1", self.URL)}
        load_prepared_rows(client, self.rows([self.URL]), claimed_relationships=claims)
        self.assertEqual(list(client.edges), [("Publication", "MENTIONS_LINK", "Repository", "W1", self.URL)])

    def test_completed_aliases_union_their_results_after_a_fold(self):
        client = RecordingNeo4jClient()
        rows = self.rows([self.URL])
        rows["publications.jsonl"].append({"id": "W2", "merged_ids": ["W1"],
                                           "_processing": {"code_links": {"status": "completed_empty"}}})
        rows["repo_links.jsonl"].append({"publication_id": "W2", "links": []})
        load_prepared_rows(client, rows)
        self.assertEqual(list(client.edges), [("Publication", "MENTIONS_LINK", "Repository", "W2", self.URL)])

    def test_incomplete_alias_blocks_cleanup_of_the_folded_publication(self):
        client = RecordingNeo4jClient()
        load_prepared_rows(client, self.rows([self.URL]))
        rows = self.rows([])
        rows["publications.jsonl"].append({"id": "W2", "merged_ids": ["W1"]})
        load_prepared_rows(client, rows)
        self.assertEqual(len(client.edges), 1)

    def test_cleanup_is_idempotent(self):
        client = RecordingNeo4jClient()
        load_prepared_rows(client, self.rows([self.URL, self.OLD]))
        load_prepared_rows(client, self.rows([self.URL]))
        before = dict(client.edges)
        load_prepared_rows(client, self.rows([self.URL]))
        self.assertEqual(client.edges, before)


class ExtractRepoLinksTest(unittest.TestCase):
    def test_pdf_version_ids_are_parallel_arrays_without_nulls(self):
        row = {"publication_id": "W1", "links": [{"url": "https://github.com/org/repo", "occurrences": [
            {"context": "abstract"}, {"context": "version", "page_number": 2, "pdf_id": "W2"},
        ]}]}
        _, _, edges, _ = extract_repo_links(row, {})
        self.assertEqual(edges[0][2]["pdf_id"], ["", "W2"])
        self.assertEqual(edges[0][2]["page_number"], [0, 2])

    def test_known_repository_matched_case_insensitively_by_stored_url(self):
        known = {normalize_repo_url("https://github.com/Org/Repo"): "https://github.com/Org/Repo"}
        row = {"publication_id": "W1", "links": [{
            "url": "https://github.com/org/repo",
            "occurrences": [
                {"context": "code", "page_number": None, "source": "abstract"},
                {
                    "context": "again",
                    "page_number": 3,
                    "source": "citation",
                    "reference_label": "9",
                },
            ],
        }]}
        candidates, repo_edges, candidate_edges, promotions = extract_repo_links(row, known)
        self.assertEqual(candidates, [])
        self.assertEqual(candidate_edges, [])
        # The edge targets the URL as stored on the Repository node. Occurrences
        # flatten to parallel arrays; page_number=None (abstract) becomes 0
        # since Neo4j array properties can't hold null.
        self.assertEqual(repo_edges, [(
            "W1", "https://github.com/Org/Repo",
            {
                "context": ["code", "again"],
                "page_number": [0, 3],
                "pdf_id": ["", ""],
                "context_source": ["abstract", "citation"],
                "reference_label": ["", "9"],
            },
        )])
        self.assertEqual(promotions, [("https://github.com/org/repo", "https://github.com/Org/Repo")])

    def test_unknown_url_becomes_link_candidate(self):
        row = {"publication_id": "W1", "links": [{"url": "https://example.org/data", "host": "example.org"}]}
        candidates, repo_edges, candidate_edges, promotions = extract_repo_links(row, {})
        self.assertEqual(candidates, [("https://example.org/data", {"url": "https://example.org/data", "host": "example.org"})])
        self.assertEqual(repo_edges, [])
        self.assertEqual(candidate_edges, [("W1", "https://example.org/data", {})])
        self.assertEqual(promotions, [])

    def test_link_without_url_is_skipped(self):
        row = {"publication_id": "W1", "links": [{"url": None, "context": "broken"}]}
        self.assertEqual(extract_repo_links(row, {}), ([], [], [], []))

    def test_successful_uncertain_verdict_explicitly_clears_old_boolean(self):
        known = {normalize_repo_url("https://github.com/org/repo"): "https://github.com/org/repo"}
        row = {"publication_id": "W1", "links": [{
            "url": "https://github.com/org/repo",
            "classification_status": "classified",
            "is_relevant": None,
            "llm_confidence": 0.2,
            "llm_reason": "insufficient context",
        }]}

        _, repo_edges, _, _ = extract_repo_links(
            row,
            known,
            synchronize_relevance=True,
        )

        self.assertEqual(repo_edges, [(
            "W1",
            "https://github.com/org/repo",
            {
                "classification_status": "classified",
                "is_relevant": None,
                "llm_confidence": 0.2,
                "llm_reason": "insufficient context",
            },
        )])

    def test_pending_status_is_not_mixed_with_a_previous_graph_verdict(self):
        known = {normalize_repo_url("https://github.com/org/repo"): "https://github.com/org/repo"}
        row = {"publication_id": "W1", "links": [{
            "url": "https://github.com/org/repo",
            "classification_status": "pending",
        }]}

        _, repo_edges, _, _ = extract_repo_links(row, known)

        self.assertEqual(repo_edges, [(
            "W1",
            "https://github.com/org/repo",
            {},
        )])


class ImplementsSynchronizationTest(unittest.TestCase):
    @staticmethod
    def _rows(publication_ids: list[str], relevance_status: str = "completed") -> dict[str, list[dict]]:
        return {
            "publications.jsonl": [{
                "id": "W1",
                "title": "paper",
                "_processing": {
                    "link_relevance": {"status": relevance_status},
                },
            }],
            "repositories.jsonl": [{
                "id": "github_org_repo",
                "name": "repo",
                "url": "https://github.com/org/repo",
                "publication_ids": publication_ids,
                "_processing": {
                    "repositories": {"status": "completed"},
                },
            }],
        }

    def test_republish_removes_an_implements_edge_after_reclassification(self):
        client = RecordingNeo4jClient()
        load_prepared_rows(client, self._rows(["W1"]))
        self.assertIn(
            ("Repository", "IMPLEMENTS", "Publication", "github_org_repo", "W1"),
            client.edges,
        )

        load_prepared_rows(client, self._rows([]))

        self.assertNotIn(
            ("Repository", "IMPLEMENTS", "Publication", "github_org_repo", "W1"),
            client.edges,
        )

    def test_failed_reclassification_does_not_delete_the_last_known_edge(self):
        client = RecordingNeo4jClient()
        load_prepared_rows(client, self._rows(["W1"]))

        load_prepared_rows(client, self._rows([], relevance_status="failed"))

        self.assertIn(
            ("Repository", "IMPLEMENTS", "Publication", "github_org_repo", "W1"),
            client.edges,
        )

    def test_manual_claim_survives_completed_empty_without_changing_properties(self):
        client = RecordingNeo4jClient()
        load_prepared_rows(client, self._rows(["W1"]))
        edge = ("Repository", "IMPLEMENTS", "Publication", "github_org_repo", "W1")
        client.edges[edge] = {"note": "manually confirmed"}

        for status in ("completed", "completed_empty"):
            with self.subTest(status=status):
                load_prepared_rows(client, self._rows([], status), claimed_relationships={edge})
                self.assertEqual(client.edges[edge], {"note": "manually confirmed"})

    def test_manual_claim_resolves_repository_and_publication_aliases(self):
        client = RecordingNeo4jClient()
        load_prepared_rows(client, self._rows(["W1"]))
        client.nodes["Repository"]["github_org_repo"]["merged_ids"] = ["R0"]
        client.nodes["Publication"]["W1"]["merged_ids"] = ["W0"]
        claim = ("Repository", "IMPLEMENTS", "Publication", "R0", "W0")

        load_prepared_rows(client, self._rows([]), claimed_relationships={claim})

        self.assertIn(("Repository", "IMPLEMENTS", "Publication", "github_org_repo", "W1"), client.edges)

    def test_manual_claim_does_not_expand_the_cleanup_scope(self):
        client = RecordingNeo4jClient()
        rows = self._rows(["W1", "W2"])
        rows["publications.jsonl"].append({"id": "W2"})
        load_prepared_rows(client, rows)
        claim = ("Repository", "IMPLEMENTS", "Publication", "github_org_repo", "W2")
        client.upsert_nodes_batch("Repository", [("R2", {"url": "https://github.com/org/other"})])
        client.upsert_relationships_batch("Repository", "Publication", "IMPLEMENTS", [("R2", "W2", {})])

        load_prepared_rows(client, self._rows([]), claimed_relationships={claim})

        self.assertIn(claim, client.edges)
        self.assertIn(("Repository", "IMPLEMENTS", "Publication", "R2", "W2"), client.edges)
        self.assertNotIn(("Repository", "IMPLEMENTS", "Publication", "github_org_repo", "W1"), client.edges)

    def test_a_claim_does_not_recreate_an_absent_relationship(self):
        client = RecordingNeo4jClient()
        claim = ("Repository", "IMPLEMENTS", "Publication", "github_org_repo", "W1")

        load_prepared_rows(client, self._rows([]), claimed_relationships={claim})

        self.assertNotIn(claim, client.edges)


class RelevancePropertySynchronizationTest(unittest.TestCase):
    @staticmethod
    def _rows(*, status: str, has_code: bool, code_url, relevance, confidence, reason,
              publication_ids: list[str]) -> dict[str, list[dict]]:
        return {
            "publications.jsonl": [{
                "id": "W1",
                "title": "paper",
                "has_code": has_code,
                "code_url": code_url,
                "_processing": {"link_relevance": {"status": status}},
            }],
            "repositories.jsonl": [{
                "id": "github_org_repo",
                "name": "repo",
                "url": "https://github.com/org/repo",
                "publication_ids": publication_ids,
                "_processing": {"repositories": {"status": "completed"}},
            }],
            "repo_links.jsonl": [{
                "publication_id": "W1",
                "links": [{
                    "url": "https://github.com/org/repo",
                    "classification_status": "classified" if status == "completed" else "failed",
                    "is_relevant": relevance,
                    "llm_confidence": confidence,
                    "llm_reason": reason,
                }],
            }],
        }

    def test_successful_reclassification_clears_code_url_and_true_edge_property(self):
        client = RecordingNeo4jClient()
        old_url = '["https://github.com/org/repo"]'
        load_prepared_rows(client, self._rows(
            status="completed",
            has_code=True,
            code_url=old_url,
            relevance=True,
            confidence=0.9,
            reason="authors' repository",
            publication_ids=["W1"],
        ))

        load_prepared_rows(client, self._rows(
            status="completed",
            has_code=False,
            code_url=None,
            relevance=None,
            confidence=0.2,
            reason="insufficient context",
            publication_ids=[],
        ))

        publication = client.nodes["Publication"]["W1"]
        self.assertFalse(publication["has_code"])
        self.assertNotIn("code_url", publication)
        edge = client.edges[(
            "Publication",
            "MENTIONS_LINK",
            "Repository",
            "W1",
            "https://github.com/org/repo",
        )]
        self.assertNotIn("is_relevant", edge)
        self.assertEqual(edge["classification_status"], "classified")
        self.assertEqual(edge["llm_confidence"], 0.2)
        self.assertNotIn(
            ("Repository", "IMPLEMENTS", "Publication", "github_org_repo", "W1"),
            client.edges,
        )

    def test_failed_reclassification_preserves_last_complete_graph_state(self):
        client = RecordingNeo4jClient()
        old_url = '["https://github.com/org/repo"]'
        load_prepared_rows(client, self._rows(
            status="completed",
            has_code=True,
            code_url=old_url,
            relevance=True,
            confidence=0.9,
            reason="authors' repository",
            publication_ids=["W1"],
        ))

        load_prepared_rows(client, self._rows(
            status="failed",
            has_code=False,
            code_url=None,
            relevance=None,
            confidence=None,
            reason=None,
            publication_ids=[],
        ))

        publication = client.nodes["Publication"]["W1"]
        self.assertTrue(publication["has_code"])
        self.assertEqual(publication["code_url"], old_url)
        edge = client.edges[(
            "Publication",
            "MENTIONS_LINK",
            "Repository",
            "W1",
            "https://github.com/org/repo",
        )]
        self.assertEqual(edge["classification_status"], "classified")
        self.assertTrue(edge["is_relevant"])
        self.assertEqual(edge["llm_confidence"], 0.9)


if __name__ == "__main__":
    unittest.main()


class MergesSurviveAPublishTest(unittest.TestCase):
    """A fold the rows never learned about must outlive a republish.

    Only the collection stage writes a fold into the prepared rows. The
    graph-wide pass and the review queue write it onto the node and nowhere
    else, and publishing the survivor from its row used to replace that list
    with the row's empty one — after which the alias map resolved nothing
    and the duplicate came back with all of its relationships.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
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
        self.publish()

    def publish(self):
        load_prepared_rows(self.graph, {
            filename: list(self.prepared.read_rows(entity))
            for entity, filename in ENTITY_FILES.items()})

    def people(self):
        return sorted(node_id for label, node_id in self.graph.nodes if label == "Person")

    def authored(self):
        return sorted((src_id, tgt_id) for _s, rel, _t, src_id, tgt_id
                      in self.graph.relationships if rel == "AUTHORED")

    def test_the_pair_is_two_records_until_something_folds_it(self):
        self.assertEqual(self.people(), ["A1", "A2"])

    def test_a_fold_made_on_the_graph_outlives_a_publish(self):
        merge_nodes(self.graph, "Person", "A2", "A1")
        self.publish()
        self.assertEqual(self.people(), ["A1"])

    def test_and_the_work_stays_where_the_fold_put_it(self):
        merge_nodes(self.graph, "Person", "A2", "A1")
        self.publish()
        self.assertEqual(self.authored(), [("A1", "W1"), ("A1", "W2")])

    def test_the_survivor_still_answers_for_the_folded_id(self):
        merge_nodes(self.graph, "Person", "A2", "A1")
        self.publish()
        self.assertEqual(self.graph.fetch_merged_id_map("Person"), {"A2": "A1"})

    def test_a_fold_taken_apart_is_not_put_back(self):
        # The other direction, and the one a union gets wrong if it only
        # ever adds: the undo removes the id from the node, and the publish
        # must not restore it from a row that never had it either.
        merge_nodes(self.graph, "Person", "A2", "A1")
        split_person(self.graph, self.db, ["A1", "A2"])
        self.publish()
        self.assertEqual(self.people(), ["A1", "A2"])
        self.assertEqual(self.authored(), [("A1", "W1"), ("A2", "W2")])


class StoppedMidPublishError(Exception):
    """Stands in for the worker's Cancelled, which lives a layer away."""


class PublishProgressTest(unittest.TestCase):
    """A publish says how far it has got, and can be given up between chunks.

    Until now the whole load was one call that came back when it was done.
    A cancel pressed during it was noticed after it finished, which for a
    large group is the one moment nobody was waiting for.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "sample")
        self.prepared.write_models("persons", [
            Person(id=f"A{index}", openalex_id=f"A{index}", name_raw=f"Person {index}",
                   is_itmo=True, authored=[{"publication_id": "W1", "position": 1}])
            for index in range(3)])
        self.prepared.write_models("publications", [Publication(id="W1", title="W1")])
        self.graph = FakePanelGraph()

    def publish(self, report=None):
        load_prepared_rows(self.graph, {
            filename: list(self.prepared.read_rows(entity))
            for entity, filename in ENTITY_FILES.items()}, report=report)

    def test_it_says_what_it_is_doing(self):
        told = []
        self.publish(report=lambda *row: told.append(row))
        steps = {step for step, _done, _total in told}
        self.assertEqual(steps, {"выкладка узлов", "выкладка связей"})

    def test_the_counts_add_up_to_everything_there_was(self):
        told = []
        self.publish(report=lambda *row: told.append(row))
        nodes = [row for row in told if row[0] == "выкладка узлов"][-1]
        self.assertEqual(nodes[1], nodes[2])
        links = [row for row in told if row[0] == "выкладка связей"][-1]
        self.assertEqual(links[1], links[2])

    def test_a_publish_can_be_given_up_part_way(self):
        def refuse(step, done, total):
            raise StoppedMidPublishError(step)

        with self.assertRaises(StoppedMidPublishError):
            self.publish(report=refuse)

    def test_and_what_it_wrote_before_that_is_there(self):
        # Nothing is rolled back: the group is loaded in part, and the next
        # publish finishes it because every write is a MERGE.
        def refuse(step, done, total):
            if step == "выкладка связей":
                raise StoppedMidPublishError(step)

        with self.assertRaises(StoppedMidPublishError):
            self.publish(report=refuse)
        self.assertEqual(len([key for key in self.graph.nodes if key[0] == "Person"]), 3)
        self.publish()
        self.assertEqual(len(self.graph.relationships), 3)

    def test_a_publish_nobody_is_watching_still_works(self):
        self.publish()
        self.assertEqual(len([key for key in self.graph.nodes if key[0] == "Person"]), 3)
