import unittest

from pauk.graph.jsonl_loader import extract_repo_links, normalize_repo_url


class NormalizeRepoUrlTest(unittest.TestCase):
    def test_case_and_cosmetic_suffixes_are_ignored(self):
        canonical = normalize_repo_url("https://github.com/Org/Repo")
        for variant in (
            "https://github.com/org/repo",
            "https://github.com/Org/Repo/",
            "https://github.com/Org/Repo.git",
            "https://www.github.com/Org/Repo.GIT",
        ):
            self.assertEqual(normalize_repo_url(variant), canonical)


class ExtractRepoLinksTest(unittest.TestCase):
    def test_known_repository_matched_case_insensitively_by_stored_url(self):
        known = {normalize_repo_url("https://github.com/Org/Repo"): "https://github.com/Org/Repo"}
        row = {"publication_id": "W1", "links": [{"url": "https://github.com/org/repo", "context": "code"}]}
        candidates, repo_edges, candidate_edges, promotions = extract_repo_links(row, known)
        self.assertEqual(candidates, [])
        self.assertEqual(candidate_edges, [])
        # The edge targets the URL as stored on the Repository node.
        self.assertEqual(repo_edges, [("W1", "https://github.com/Org/Repo", {"context": "code"})])
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


if __name__ == "__main__":
    unittest.main()
