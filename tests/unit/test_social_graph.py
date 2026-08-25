import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.models import GitHubProfile, Person, Repository
from pauk.pipeline.stages.social_graph import SocialGraphStage, is_itmo_organization
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore
from pauk.storage.static import StaticStore


def person(pid, name, *, github=None, itmo=True):
    return Person(id=pid, openalex_id=pid, is_itmo=itmo, name_en=name, github=github)


def profile(login, *, account_type="user", name=None, bio=None, location=None):
    return GitHubProfile(id=f"github_{login.lower()}", login=login, type=account_type,
                         name=name, description=bio, location=location)


def repository(owner, name, *, contributors=()):
    url = f"https://github.com/{owner}/{name}"
    return Repository(id=f"github_{owner.lower()}_{name.lower()}", name=name, url=url,
                      owner_login=owner, contributors=list(contributors), cited_urls=[url])


class IsItmoOrganizationTest(unittest.TestCase):
    CATALOG = frozenset({"licaibeerlab", "ctlab"})

    def judge(self, login, catalog=CATALOG, **profile_kwargs):
        given = profile(login, account_type="organization", **profile_kwargs) \
            if profile_kwargs else None
        return is_itmo_organization(login, given, catalog)

    def test_a_catalogued_organization_is_followed(self):
        # Its profile says nothing and its login gives nothing away; the
        # curated list is the only thing that knows.
        self.assertTrue(self.judge("LicAiBeerLab"))

    def test_the_catalogue_is_matched_case_insensitively(self):
        self.assertTrue(self.judge("CTLab"))

    def test_itmo_in_the_login_is_enough(self):
        self.assertTrue(self.judge("itmo-infocom"))

    def test_itmo_glued_to_the_end_of_a_login_needs_the_catalogue(self):
        # AlgoMathITMO is ITMO's, but no word boundary separates the name,
        # and loosening the pattern would make RITMO ours. Catalogued instead.
        self.assertFalse(self.judge("AlgoMathITMO", catalog=frozenset()))
        self.assertTrue(self.judge("AlgoMathITMO", catalog=frozenset({"algomathitmo"})))

    def test_an_organization_naming_itmo_in_its_profile(self):
        self.assertTrue(self.judge("aimclub", name="AIM.club, ITMO University"))

    def test_the_city_spelled_with_a_full_stop(self):
        self.assertTrue(self.judge("ai-chem", name="Center for AI in Chemistry",
                                   location="Russia, St. Petersburg"))

    def test_the_city_spelled_with_a_hyphen(self):
        self.assertTrue(self.judge("some-lab", location="St-Petersburg, Russia"))

    def test_the_city_spelled_in_cyrillic(self):
        self.assertTrue(self.judge("Digiratory", name="Digiratory",
                                   location="Санкт-Петербург"))

    def test_an_employee_committing_there_does_not_make_it_ours(self):
        # The rule this replaces followed any organization a confirmed
        # account had committed to, which on real data meant google,
        # microsoft, JetBrains and llvm-mirror.
        self.assertFalse(self.judge("google", name="Google",
                                    location="United States of America"))

    def test_ritmo_is_not_itmo(self):
        self.assertFalse(self.judge("ritmo", name="RITMO, University of Oslo"))

    def test_an_unlisted_organization_without_a_profile_is_not_followed(self):
        self.assertFalse(is_itmo_organization("some-lab", None, self.CATALOG))


class SocialGraphStageTest(unittest.TestCase):
    def build(self, people, repositories, profiles):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Settings(data_dir=Path(tmp.name))
        db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(db, "sample")
        self.raw = RawStore(db, "sample")
        self.config = config
        self.prepared.write_models("persons", people)
        self.prepared.write_models("repositories", repositories)
        self.prepared.write_models("github_profiles", profiles)

    def seeds_for(self, people, repositories, profiles):
        self.build(people, repositories, profiles)
        stage = SocialGraphStage(self.prepared, self.raw, self.config)
        return stage._seeds(people, repositories, {p.id: p for p in profiles})

    def test_confirmed_accounts_are_seeds(self):
        seeds = self.seeds_for(
            [person("A1", "Ivan Petrov", github="ipetrov"), person("A2", "Maria S.")],
            [], [])
        self.assertEqual(seeds, ["ipetrov"])

    def test_an_itmo_organization_is_a_seed_and_a_stranger_is_not(self):
        seeds = self.seeds_for(
            [person("A1", "Ivan Petrov", github="ipetrov")],
            [repository("some-lab", "tool", contributors=["ipetrov"]),
             repository("google", "lib", contributors=["stranger"])],
            [profile("some-lab", account_type="organization",
                     name="Some Lab, ITMO University"),
             profile("google", account_type="organization", name="Google")],
        )
        self.assertEqual(seeds, ["ipetrov", "some-lab"])

    def test_an_organization_a_confirmed_account_committed_to_is_not_a_seed(self):
        # ipetrov contributed to google's library; that is a fact about
        # ipetrov, not about google, and following it would walk into
        # hundreds of repositories full of strangers.
        seeds = self.seeds_for(
            [person("A1", "Ivan Petrov", github="ipetrov")],
            [repository("google", "lib", contributors=["ipetrov", "stranger"])],
            [profile("google", account_type="organization", name="Google",
                     location="United States of America")],
        )
        self.assertEqual(seeds, ["ipetrov"])

    def test_a_personal_account_owning_a_repository_is_not_a_seed_by_itself(self):
        # Being cited does not make someone ITMO staff; only github_match
        # decides that, and only then do they seed the walk.
        seeds = self.seeds_for(
            [person("A1", "Ivan Petrov")],
            [repository("stranger", "tool")],
            [profile("stranger")],
        )
        self.assertEqual(seeds, [])

    @patch("pauk.pipeline.stages.social_graph.GitHubClient")
    def test_repositories_already_harvested_are_not_walked_again(self, client):
        self.build([person("A1", "Ivan Petrov", github="ipetrov")],
                   [repository("ipetrov", "tool")], [])
        client.return_value.user_repositories.return_value = [
            {"html_url": "https://github.com/ipetrov/tool"},      # already known
            {"html_url": "https://github.com/ipetrov/other"},
        ]
        client.return_value.get_repository.return_value = {
            "owner": {"login": "ipetrov", "type": "User"}}
        client.return_value.contributors.return_value = []
        client.return_value.commits.return_value = []
        client.return_value.get_user.return_value = {"login": "ipetrov"}
        result = SocialGraphStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(result["social_repositories"], 1)

    @patch("pauk.pipeline.stages.social_graph.GitHubClient")
    def test_people_found_on_a_walked_repository_become_candidates(self, client):
        self.build([person("A1", "Ivan Petrov", github="ipetrov")],
                   [repository("ipetrov", "known")], [])
        client.return_value.user_repositories.return_value = [
            {"html_url": "https://github.com/ipetrov/lab-tool"}]
        client.return_value.get_repository.return_value = {
            "owner": {"login": "ipetrov", "type": "User"}}
        client.return_value.contributors.return_value = [{"login": "colleague", "type": "User"}]
        client.return_value.commits.return_value = [
            {"author": {"login": "colleague"},
             "commit": {"author": {"email": "colleague@itmo.ru", "name": "Anna Sidorova"}}}]
        client.return_value.get_user.side_effect = lambda login: {"login": login, "name": "Anna Sidorova"}
        result = SocialGraphStage(self.prepared, self.raw, self.config).run()
        profiles = {p.login: p for p in self.prepared.read_models("github_profiles", GitHubProfile)}
        self.assertEqual(result["social_accounts"], 2)
        self.assertEqual(profiles["colleague"].emails, ["colleague@itmo.ru"])
        self.assertEqual(profiles["colleague"].repos, ["https://github.com/ipetrov/lab-tool"])

    @patch("pauk.pipeline.stages.social_graph.GitHubClient")
    def test_repositories_walked_by_an_earlier_run_are_not_walked_again(self, client):
        # A walked repository leaves its mark on the profiles it produced,
        # not in repositories.jsonl; reading only the latter sends every
        # later run over the same hundreds of repositories.
        self.build([person("A1", "Ivan Petrov", github="ipetrov")], [],
                   [GitHubProfile(id="github_colleague", login="colleague",
                                  repos=["https://github.com/ipetrov/lab"])])
        client.return_value.user_repositories.return_value = [
            {"html_url": "https://github.com/ipetrov/lab"}]
        result = SocialGraphStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(result["social_repositories"], 0)
        self.assertFalse(client.return_value.get_repository.called)

    @patch("pauk.pipeline.stages.social_graph.GitHubClient")
    def test_the_walk_stops_when_a_ring_finds_nothing_new(self, client):
        self.build([person("A1", "Ivan Petrov", github="ipetrov")],
                   [repository("ipetrov", "tool")], [])
        # The seed owns only what was already harvested.
        client.return_value.user_repositories.return_value = [
            {"html_url": "https://github.com/ipetrov/tool"}]
        result = SocialGraphStage(self.prepared, self.raw, self.config).run()
        self.assertEqual((result["social_rings"], result["social_repositories"]), (0, 0))

    @patch("pauk.pipeline.stages.social_graph.GitHubMatchStage")
    @patch("pauk.pipeline.stages.social_graph.GitHubClient")
    def test_matching_runs_between_rings_to_produce_new_seeds(self, client, matcher):
        # Without matching, a candidate found on one ring never becomes a
        # seed and the walk would end after the first.
        self.build([person("A1", "Ivan Petrov", github="ipetrov")],
                   [repository("ipetrov", "known")], [])
        client.return_value.user_repositories.side_effect = lambda login, limit: {
            "ipetrov": [{"html_url": "https://github.com/ipetrov/lab"}],
        }.get(login, [])
        client.return_value.get_repository.return_value = {
            "owner": {"login": "ipetrov", "type": "User"}}
        client.return_value.contributors.return_value = [{"login": "colleague", "type": "User"}]
        client.return_value.commits.return_value = []
        client.return_value.get_user.return_value = {"login": "colleague"}
        SocialGraphStage(self.prepared, self.raw, self.config).run()
        self.assertTrue(matcher.called, "github_match must run between rings")

    @patch("pauk.pipeline.stages.social_graph.GitHubClient")
    def test_a_seed_is_never_walked_twice(self, client):
        self.build([person("A1", "Ivan Petrov", github="ipetrov")], [], [])
        calls = []

        def repositories_of(login, limit):
            calls.append(login)
            # Always offers one new repository, so only the seed guard can
            # end the walk; without it this would spin to the ring ceiling.
            return [{"html_url": f"https://github.com/{login}/repo{len(calls)}"}]

        client.return_value.user_repositories.side_effect = repositories_of
        client.return_value.get_repository.return_value = {"owner": {}}
        client.return_value.contributors.return_value = []
        client.return_value.commits.return_value = []
        SocialGraphStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(calls, ["ipetrov"])

    @patch("pauk.pipeline.stages.social_graph.GitHubClient")
    def test_a_seed_whose_repositories_cannot_be_read_is_skipped(self, client):
        self.build([person("A1", "Ivan Petrov", github="ipetrov")], [], [])
        client.return_value.user_repositories.side_effect = RuntimeError("404")
        result = SocialGraphStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(result["social_repositories"], 0)


if __name__ == "__main__":
    unittest.main()

class ItmoGithubOrgCatalogTest(unittest.TestCase):
    """The curated list of ITMO organizations StaticStore hands the stage."""

    def store(self, payload=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        if payload is not None:
            (root / "itmo_github_orgs.json").write_text(
                json.dumps(payload), encoding="utf-8")
        return StaticStore(root)

    def test_logins_are_lowercased_for_matching(self):
        catalog = self.store({"organizations": [{"login": "AlgoMathITMO"}]}).itmo_github_orgs
        self.assertEqual(catalog, frozenset({"algomathitmo"}))

    def test_blank_and_missing_logins_are_skipped(self):
        catalog = self.store({"organizations": [
            {"login": "ctlab"}, {"login": "  "}, {"note": "no login at all"},
        ]}).itmo_github_orgs
        self.assertEqual(catalog, frozenset({"ctlab"}))

    def test_a_missing_file_is_an_empty_catalogue_not_an_error(self):
        # The rules work without it; only the labs it names go unrecognised.
        self.assertEqual(self.store().itmo_github_orgs, frozenset())

    def test_the_catalogue_shipped_with_the_repository_parses(self):
        catalog = StaticStore(Path("data/static")).itmo_github_orgs
        self.assertIn("licaibeerlab", catalog)
        self.assertNotIn("google", catalog)
