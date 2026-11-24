"""Find ITMO accounts through the repositories a paper never cited.

The harvest reaches only people who worked on code a paper linked to.
Most employees who write code never had it cited: the repository sits on
their own account, or on a lab's, with nothing pointing at it from a
publication.

This walks outward from what is already known. Every confirmed account and
every ITMO-affiliated organization is a seed; their public repositories are
harvested for the people behind them, and those people are matched like any
other candidate. A person found this way becomes a seed once github_match confirms them,
so running the two in turn walks the graph outward one ring at a time.

An organization is only followed when it says ITMO itself (in its login,
its profile, or data/static/itmo_github_orgs.json); otherwise the walk would
enter every organization whose library a paper cited.

Seeds are only confirmed accounts: following unproven ones would multiply
seeds and repositories. github_match runs between rings to turn candidates
into the next ring's seeds. A ring that walks no new repository ends the walk.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable

from pauk.models import GitHubProfile, Person, Repository
from pauk.sources.base import HttpRequestError
from pauk.sources.github import GitHubClient
from pauk.storage.static import StaticStore

from .base import EnrichmentStage
from .github_match import ITMO_IDENTITY_PATTERN, PETERSBURG_PATTERN, GitHubMatchStage
from .repo_people import COMMIT_PAGES, _apply_profile_details, _git_identities, _is_person
from .repositories import _github_owner_name

logger = logging.getLogger(__name__)

# Repositories taken from one seed, newest first. A prolific account has
# hundreds, and the ones it touched recently are the ones with people on
# them; the rest are forks and abandoned coursework.
MAX_REPOS_PER_SEED = 30

# Rings walked before giving up on convergence; the graph normally settles in two.
MAX_RINGS = 5


def itmo_organization_status(login: str, profile: GitHubProfile | None,
                             catalog: frozenset[str], *,
                             repositories: Iterable[Repository] = (),
                             confirmed: Collection[str] = ()) -> tuple[str, str]:
    """Classify the evidence that an organization belongs to ITMO.

    Only ``confirmed`` organizations are safe traversal seeds. ``possible`` is
    diagnostic: a city or contributor can guide a review but must not make us
    walk every Saint Petersburg organization.
    """
    login_lower = login.lower()
    if login_lower in catalog:
        return "confirmed", "catalog"
    if ITMO_IDENTITY_PATTERN.search(login):
        return "confirmed", "login"
    profile = profile or GitHubProfile(id="", login=login)
    identity_text = " ".join(filter(None, (
        profile.name, profile.description, profile.company,
    )))
    if ITMO_IDENTITY_PATTERN.search(identity_text):
        return "confirmed", "profile"
    weak_text = " ".join(filter(None, (
        profile.name, profile.description, profile.company, profile.location,
    )))
    if PETERSBURG_PATTERN.search(weak_text):
        return "possible", "petersburg"
    confirmed_lower = {item.lower() for item in confirmed}
    if any(
        (repository.owner_login or "").lower() == login_lower
        and any(contributor.lower() in confirmed_lower for contributor in repository.contributors)
        for repository in repositories
    ):
        # An employee may contribute to upstream projects outside ITMO.
        return "possible", "itmo_contributor"
    return "not_confirmed", ""


class SocialGraphStage(EnrichmentStage):
    """Harvests repositories of accounts already tied to ITMO."""

    name = "social_graph"

    def _seeds(self, people: list[Person], repositories: list[Repository],
               profiles: dict[str, GitHubProfile]) -> list[str]:
        confirmed = {person.github for person in people if person.github}
        catalog = StaticStore(self.config.static_dir).itmo_github_orgs
        owner_logins = {repository.owner_login for repository in repositories
                        if repository.owner_login}

        organizations = []
        for login in owner_logins:
            profile = profiles.get(f"github_{login.lower()}")
            account_type = profile.type if profile else None
            # The curated catalogue identifies organizations even before a profile is fetched.
            if account_type != "organization" and not (
                account_type is None and login.lower() in catalog
            ):
                continue
            status, reason = itmo_organization_status(
                login, profile, catalog, repositories=repositories, confirmed=confirmed,
            )
            if status == "confirmed":
                logger.info("social_graph: confirmed ITMO organization %s (%s)", login, reason)
                organizations.append(login)
            elif status == "possible":
                logger.info("social_graph: possible ITMO organization %s (%s), not a seed",
                            login, reason)
        return sorted(confirmed | set(organizations))

    def _fetch_profiles(self, client: GitHubClient, profiles: Iterable[GitHubProfile],
                        attempted: set[str]) -> bool:
        pending = {profile.login.lower(): profile for profile in profiles
                   if not profile.profile_fetched and profile.login.lower() not in attempted}
        if not pending:
            return False
        attempted.update(pending)
        updated = False
        failed = 0
        for login, payload in client.get_users(pending).items():
            if isinstance(payload, HttpRequestError):
                failed += 1
                continue
            profile = pending[login]
            self.raw.append("github_user", payload, {"login": profile.login})
            _apply_profile_details(profile, payload)
            updated = True
        if failed:
            logger.warning("social_graph: could not fetch %d of %d GitHub profiles", failed, len(pending))
        return updated

    def _harvest(self, client: GitHubClient, owner: str, name: str, url: str, payload: dict,
                 profiles: dict[str, GitHubProfile], attempted: set[str]) -> int:
        """Accounts behind one repository, added to the candidate pool."""
        try:
            owner_data = payload.get("owner") or {}
            if not owner_data.get("login") or not owner_data.get("type"):
                payload = client.get_repository(owner, name)
            contributors = client.contributors(owner, name)
            identities = _git_identities(client.commits(owner, name, COMMIT_PAGES))
        except Exception:
            return 0
        self.raw.append("github", payload, {"repository": url})

        owner_data = payload.get("owner") or {}
        logins: dict[str, str] = {}
        if _is_person(owner_data.get("login") or "", owner_data.get("type")):
            login = owner_data["login"]
            logins[login.lower()] = login
        for contributor in contributors:
            login = contributor.get("login") or ""
            if _is_person(login, contributor.get("type")):
                logins.setdefault(login.lower(), login)

        added = 0
        candidates = []
        for key, login in sorted(logins.items()):
            profile_id = f"github_{key}"
            emails, commit_names = identities.get(key, (set(), set()))
            profile = profiles.get(profile_id)
            if profile is None:
                profile = profiles[profile_id] = GitHubProfile(id=profile_id, login=login)
                added += 1
            profile.emails = sorted(set(profile.emails) | emails)
            profile.commit_names = sorted(set(profile.commit_names) | commit_names)
            profile.repos = sorted(set(profile.repos) | {url})
            candidates.append(profile)
        self._fetch_profiles(client, candidates, attempted)
        return added

    def _match_candidates(self, profiles: dict[str, GitHubProfile]) -> tuple[list[Person], dict[str, GitHubProfile]]:
        self.prepared.write_models("github_profiles", profiles.values())
        GitHubMatchStage(self.prepared, self.raw, self.config, force=True).run()
        people = list(self.prepared.read_models("persons", Person))
        profiles = {profile.id: profile
                    for profile in self.prepared.read_models("github_profiles", GitHubProfile)}
        return people, profiles

    def run(self) -> dict[str, int]:
        people = list(self.prepared.read_models("persons", Person))
        repositories = list(self.prepared.read_models("repositories", Repository))
        profiles = {profile.id: profile
                    for profile in self.prepared.read_models("github_profiles", GitHubProfile)}
        client = GitHubClient(self.config.request_timeout, self.config.github_token)

        # Repositories already harvested, from both sources that record one:
        # those the repositories stage fetched and those this walk visited
        # (named on the profiles it collected). Reading only the first would
        # re-walk the same repositories on every run.
        known_urls = {repository.url for repository in repositories}
        visited = known_urls | {url for profile in profiles.values() for url in profile.repos}
        walked = added_profiles = rings = 0
        walked_seeds: set[str] = set()
        attempted_profiles: set[str] = set()
        # The repositories are already visited even if their profile requests failed.
        retry_profiles = (profile for profile in profiles.values()
                          if any(url not in known_urls for url in profile.repos))
        if self._fetch_profiles(client, retry_profiles, attempted_profiles):
            people, profiles = self._match_candidates(profiles)

        for ring in range(1, MAX_RINGS + 1):
            seeds = [seed for seed in self._seeds(people, repositories, profiles)
                     if seed not in walked_seeds]
            if not seeds:
                logger.info("social_graph: ring %d has no new seeds, converged", ring)
                break
            walked_seeds.update(seeds)
            logger.info("social_graph: ring %d, %d new seeds", ring, len(seeds))

            fresh: list[tuple[str, str, str, dict]] = []
            for seed in seeds:
                try:
                    owned = client.user_repositories(seed, MAX_REPOS_PER_SEED)
                except Exception:
                    continue
                for repository in owned:
                    url = (repository.get("html_url") or "").rstrip("/")
                    if not url or url in visited:
                        continue
                    parsed = _github_owner_name(url)
                    if parsed is None:
                        continue
                    visited.add(url)
                    owner, name = parsed
                    fresh.append((owner, name, url, repository))

            if not fresh:
                logger.info("social_graph: ring %d walked nothing new, converged", ring)
                break
            for owner, name, url, payload in fresh:
                added_profiles += self._harvest(client, owner, name, url, payload, profiles, attempted_profiles)
            walked += len(fresh)
            rings = ring
            logger.info("social_graph: ring %d walked %d repositories", ring, len(fresh))

            # Matching is what turns this ring's candidates into the next
            # ring's seeds; without it the walk would stop here.
            people, profiles = self._match_candidates(profiles)
        else:
            logger.info("social_graph: stopped at the %d-ring ceiling", MAX_RINGS)

        self.prepared.write_models("github_profiles", profiles.values())
        logger.info("social_graph: %d rings, %d repositories walked, %d new accounts",
                    rings, walked, added_profiles)
        return {"social_rings": rings, "social_repositories": walked,
                "social_accounts": added_profiles}
