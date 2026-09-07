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

An organization is only followed when it says ITMO itself — in its login, in
its profile, or by being in data/static/itmo_github_orgs.json. The
alternative is following every organization whose library a paper cited,
which means walking into google and microsoft for nothing.

The walk runs ring by ring until it converges. Seeds are only the accounts
already confirmed — following the unproven ones would mean four hundred
seeds and twelve thousand repositories. That is why github_match runs
between rings: it turns candidates found on this ring into seeds for the
next. A ring that walks no new repository ends the walk.
"""

from __future__ import annotations

import logging

from pauk.models import GitHubProfile, Person, Repository
from pauk.sources.github import GitHubClient
from pauk.storage.static import StaticStore

from .base import EnrichmentStage
from .github_match import ITMO_IN_TEXT, GitHubMatchStage
from .repositories import COMMIT_PAGES, _git_identities, _is_person

logger = logging.getLogger(__name__)

# Repositories taken from one seed, newest first. A prolific account has
# hundreds, and the ones it touched recently are the ones with people on
# them; the rest are forks and abandoned coursework.
MAX_REPOS_PER_SEED = 30

# Rings walked before giving up on convergence. On earlier data the graph
# settled in two; the rest is headroom, not an expectation.
MAX_RINGS = 5


def is_itmo_organization(login: str, profile: GitHubProfile | None,
                         catalog: frozenset[str]) -> bool:
    """Whether an organization is ITMO's, and so worth walking into.

    Three ways to tell, all of them about the organization itself: it is in
    the curated catalogue, its login says ITMO, or its profile does.

    Sharing a member with a confirmed account is deliberately *not* one of
    them. That rule read as "an ITMO employee committed here", which is true
    of google, microsoft, JetBrains and llvm-mirror — on real data it was the
    only rule that ever fired, and it made seeds of all four. Walking into
    them costs far more than API calls now: everyone credited on the
    repositories they lead to becomes a GitHubProfile node.

    A lab whose profile says nothing and whose login gives nothing away is
    invisible here by design; that is what the catalogue is for.
    """
    if login.lower() in catalog:
        return True
    if ITMO_IN_TEXT.search(login):
        return True
    if profile is None:
        return False
    text = f"{profile.name or ''} {profile.description or ''} {profile.location or ''}"
    return bool(ITMO_IN_TEXT.search(text))


class SocialGraphStage(EnrichmentStage):
    """Harvests repositories of accounts already tied to ITMO."""

    name = "social_graph"

    def _seeds(self, people: list[Person], repositories: list[Repository],
               profiles: dict[str, GitHubProfile]) -> list[str]:
        confirmed = {person.github for person in people if person.github}
        catalog = StaticStore(self.config.static_dir).itmo_github_orgs
        owner_logins = {repository.owner_login for repository in repositories
                        if repository.owner_login}

        organizations = [
            login for login in owner_logins
            if (profiles.get(f"github_{login.lower()}") or GitHubProfile(
                id="", login=login)).type == "organization"
            and is_itmo_organization(login, profiles.get(f"github_{login.lower()}"), catalog)
        ]
        return sorted(confirmed | set(organizations))

    def _harvest(self, client: GitHubClient, owner: str, name: str, url: str,
                 profiles: dict[str, GitHubProfile]) -> int:
        """Accounts behind one repository, added to the candidate pool."""
        try:
            payload = client.get_repository(owner, name)
            contributors = client.contributors(owner, name)
            identities = _git_identities(client.commits(owner, name, COMMIT_PAGES))
        except Exception:
            return 0
        self.raw.append("github", payload, {"repository": url})

        owner_data = payload.get("owner") or {}
        logins = set()
        if _is_person(owner_data.get("login") or "", owner_data.get("type")):
            logins.add(owner_data["login"])
        for contributor in contributors:
            login = contributor.get("login") or ""
            if _is_person(login, contributor.get("type")):
                logins.add(login)

        added = 0
        for login in sorted(logins):
            profile_id = f"github_{login.lower()}"
            emails, commit_names = identities.get(login, (set(), set()))
            known = profiles.get(profile_id)
            if known is None:
                try:
                    user = client.get_user(login)
                except Exception:
                    user = {}
                self.raw.append("github_user", user, {"login": login})
                added += 1
            else:
                user = {}
            profile_email = (user.get("email") or "").strip().lower()
            if profile_email and "noreply" not in profile_email:
                emails = emails | {profile_email}
            profiles[profile_id] = GitHubProfile(
                id=profile_id,
                login=login,
                name=user.get("name") or (known.name if known else None),
                html_url=user.get("html_url") or (known.html_url if known else None),
                description=user.get("bio") or (known.description if known else None),
                location=user.get("location") or (known.location if known else None),
                company=user.get("company") or (known.company if known else None),
                type=(user.get("type") or "").lower() or (known.type if known else None),
                emails=sorted(set(known.emails if known else []) | emails),
                commit_names=sorted(set(known.commit_names if known else []) | commit_names),
                repos=sorted(set(known.repos if known else []) | {url}),
            )
        return added

    def run(self) -> dict[str, int]:
        people = list(self.prepared.read_models("persons", Person))
        repositories = list(self.prepared.read_models("repositories", Repository))
        profiles = {profile.id: profile
                    for profile in self.prepared.read_models("github_profiles", GitHubProfile)}
        client = GitHubClient(self.config.request_timeout, self.config.github_token)

        # Repositories already harvested, from both sources that record one:
        # the cited repositories the repositories stage fetched, and the ones
        # this walk visited, which are named on the profiles it collected.
        # Reading only the first would send every later run over the same
        # hundreds of repositories again.
        visited = {repository.url for repository in repositories}
        visited |= {url for profile in profiles.values() for url in profile.repos}
        walked = added_profiles = rings = 0
        walked_seeds: set[str] = set()

        for ring in range(1, MAX_RINGS + 1):
            seeds = [seed for seed in self._seeds(people, repositories, profiles)
                     if seed not in walked_seeds]
            if not seeds:
                logger.info("social_graph: ring %d has no new seeds, converged", ring)
                break
            walked_seeds.update(seeds)
            logger.info("social_graph: ring %d, %d new seeds", ring, len(seeds))

            fresh: list[tuple[str, str, str]] = []
            for seed in seeds:
                try:
                    owned = client.user_repositories(seed, MAX_REPOS_PER_SEED)
                except Exception:
                    continue
                for repository in owned:
                    url = (repository.get("html_url") or "").rstrip("/")
                    if not url or url in visited:
                        continue
                    parts = url.split("github.com/")[-1].split("/")
                    if len(parts) != 2:
                        continue
                    visited.add(url)
                    fresh.append((parts[0], parts[1], url))

            if not fresh:
                logger.info("social_graph: ring %d walked nothing new, converged", ring)
                break
            for owner, name, url in fresh:
                added_profiles += self._harvest(client, owner, name, url, profiles)
            walked += len(fresh)
            rings = ring
            logger.info("social_graph: ring %d walked %d repositories", ring, len(fresh))

            self.prepared.write_models("github_profiles", profiles.values())
            # Matching is what turns this ring's candidates into the next
            # ring's seeds; without it the walk would stop here.
            GitHubMatchStage(self.prepared, self.raw, self.config, force=True).run()
            people = list(self.prepared.read_models("persons", Person))
            profiles = {profile.id: profile
                        for profile in self.prepared.read_models("github_profiles", GitHubProfile)}
        else:
            logger.info("social_graph: stopped at the %d-ring ceiling", MAX_RINGS)

        self.prepared.write_models("github_profiles", profiles.values())
        logger.info("social_graph: %d rings, %d repositories walked, %d new accounts",
                    rings, walked, added_profiles)
        return {"social_rings": rings, "social_repositories": walked,
                "social_accounts": added_profiles}