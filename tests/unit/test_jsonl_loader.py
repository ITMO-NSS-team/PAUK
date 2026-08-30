import unittest

from pauk.graph.jsonl_loader import extract_repo_links, load_prepared_rows, normalize_repo_url
from tests.bench.mocks import RecordingNeo4jClient


class NormalizeRepoUrlTest(unittest.TestCase):
    def test_case_and_cosmetic_suffixes_are_ignored(self):
        canonical = normalize_repo_url("https://github.com/Org/Repo")
        for variant in (
            "https://github.com/org/repo",
            "https://github.com/Org/Repo/",
            "https://github.com/Org/Repo.git",
            "https://www.github.com/Org/Repo",
            "https://www.github.com/Org/Repo.GIT",
        ):
            self.assertEqual(normalize_repo_url(variant), canonical)


class ExtractRepoLinksTest(unittest.TestCase):
    def test_known_repository_matched_case_insensitively_by_stored_url(self):
        known = {normalize_repo_url("https://github.com/Org/Repo"): "https://github.com/Org/Repo"}
        row = {"publication_id": "W1", "links": [{
            "url": "https://github.com/org/repo",
            "occurrences": [{"context": "code", "page_number": None}, {"context": "again", "page_number": 3}],
        }]}
        candidates, repo_edges, candidate_edges, promotions = extract_repo_links(row, known)
        self.assertEqual(candidates, [])
        self.assertEqual(candidate_edges, [])
        # The edge targets the URL as stored on the Repository node. Occurrences
        # flatten to parallel arrays; page_number=None (abstract) becomes 0
        # since Neo4j array properties can't hold null.
        self.assertEqual(repo_edges, [(
            "W1", "https://github.com/Org/Repo",
            {"context": ["code", "again"], "page_number": [0, 3]},
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
                "is_relevant": None,
                "llm_confidence": 0.2,
                "llm_reason": "insufficient context",
            },
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
        self.assertEqual(edge["llm_confidence"], 0.2)

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
        self.assertTrue(edge["is_relevant"])
        self.assertEqual(edge["llm_confidence"], 0.9)


if __name__ == "__main__":
    unittest.main()
