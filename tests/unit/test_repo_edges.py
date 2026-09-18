"""Pipeline helpers for repositories. The map-side repository edges and
clusters of the old GUI (#155) go with its port to pauk/gui/web."""

import unittest

from pauk.pipeline.stages.repositories import _payload_date, _url_repo_id


class PayloadDateTest(unittest.TestCase):
    def test_github_timestamp_becomes_a_date(self):
        self.assertEqual(str(_payload_date("2024-05-17T09:31:02Z")), "2024-05-17")

    def test_missing_or_unparsable_value_is_none(self):
        self.assertIsNone(_payload_date(None))
        self.assertIsNone(_payload_date(""))
        self.assertIsNone(_payload_date("last tuesday"))


class UrlRepoIdTest(unittest.TestCase):
    """The key both passes of RepositoriesStage claim their work by."""

    def test_owner_and_name_are_lowercased(self):
        self.assertEqual(_url_repo_id("https://github.com/Org/Repo"), "github_org_repo")

    def test_a_trailing_slash_does_not_change_the_key(self):
        self.assertEqual(_url_repo_id("https://github.com/org/repo/"),
                         _url_repo_id("https://github.com/org/repo"))

    def test_anything_that_is_not_a_repository_url_has_no_key(self):
        self.assertIsNone(_url_repo_id("https://gitlab.com/org/repo"))
        self.assertIsNone(_url_repo_id("https://github.com/org/repo/tree/main"))
        self.assertIsNone(_url_repo_id(None))


if __name__ == "__main__":
    unittest.main()
