"""Find member accounts through the repositories a paper never cited.

The harvest reaches only people who worked on code a paper linked to.
Most employees who write code never had it cited: the repository sits on
their own account, or on a lab's, with nothing pointing at it from a
publication.

This walks outward from what is already known. Every confirmed account and
every organization of the university is a seed; their public repositories are
harvested for the people behind them, and those people are matched like any
other candidate. A person found this way becomes a seed once github_match confirms them,
so running the two in turn walks the graph outward one ring at a time.

An organization is only followed when it names the university itself (in its
login, its profile, or data/static/<institution>/github_orgs.json); otherwise the walk would
enter every organization whose library a paper cited.

Seeds are only confirmed accounts: following unproven ones would multiply
seeds and repositories. github_match runs between rings to turn candidates
into the next ring's seeds. A ring that walks no new repository ends the walk.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable

from pauk.institution import Institution
from pauk.models import GitHubProfile, Person, Repository
from pauk.settings import settings
from pauk.sources.github import GitHubClient
from pauk.storage.static import StaticStore

from .base import EnrichmentStage
from .github_match import GitHubMatchStage
from .repo_people import COMMIT_PAGES, _git_identities, _is_person

logger = logging.getLogger(__name__)

# Repositories taken from one seed, newest first. A prolific account has
# hundreds, and the ones it touched recently are the ones with people on
# them; the rest are forks and abandoned coursework.
MAX_REPOS_PER_SEED = 30

# Rings walked before giving up on convergence; the graph normally settles in two.
MAX_RINGS = 5


def member_organization_status(login: str, profile: GitHubProfile | None,
                               catalog: frozenset[str], *,
                               repositories: Iterable[Repository] = (),
                               confirmed: Collection[str] = (),
                               institution: Institution | None = None) -> tuple[str, str]:
    """Classify the evidence that an organization belongs to the university.

    Only ``confirmed`` organizations are safe traversal seeds. ``possible`` is
    diagnostic: a city or contributor can guide a review but must not make us
    walk every organization in the city.
    """
    institution = institution or settings.institution
    login_lower = login.lower()
    if login_lower in catalog:
        return "confirmed", "catalog"
    if institution.identity_re.search(login):
        return "confirmed", "login"
    profile = profile or GitHubProfile(id="", login=login)
    identity_text = " ".join(filter(None, (
        profile.name, profile.description, profile.company,
    )))
    if institution.identity_re.search(identity_text):
        return "confirmed", "profile"
    weak_text = " ".join(filter(None, (
        profile.name, profile.description, profile.company, profile.location,
    )))
    if institution.city_re.search(weak_text):
        return "possible", "city"
    confirmed_lower = {item.lower() for item in confirmed}
    if any(
        (repository.owner_login or "").lower() == login_lower
        and any(contributor.lower() in confirmed_lower for contributor in repository.contributors)
        for repository in repositories
    ):
        # An employee may contribute to upstream projects outside the university.
        return "possible", "member_contributor"
    return "not_confirmed", ""


class SocialGraphStage(EnrichmentStage):
    """Harvests repositories of accounts already tied to the university."""

    name = "social_graph"

    def _seeds(self, people: list[Person], repositories: list[Repository],
               profiles: dict[str, GitHubProfile]) -> list[str]:
        confirmed = {person.github for person in people if person.github}
        catalog = StaticStore(self.config.static_dir).github_orgs
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
            status, reason = member_organization_status(
                login, profile, catalog, repositories=repositories, confirmed=confirmed,
                institution=self.config.institution,
            )
            if status == "confirmed":
                logger.info("social_graph: confirmed member organization %s (%s)", login, reason)
                organizations.append(login)
            elif status == "possible":
                logger.info("social_graph: possible member organization %s (%s), not a seed",
                            login, reason)
        return sorted(confirmed | set(organizations))

    def _harvest(self, client: GitHubClient, owner: str, name: str, url: str,
                 profiles: dict[str, GitHubProfile], payload: dict | None = None) -> int:
        """Accounts behind one repository, added to the candidate pool.

        `payload` is the repository as the owner's repository list served it;
        given, it saves a request.
        """
        try:
            payload = payload or client.get_repository(owner, name)
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
        new = [login for login in sorted(logins) if f"github_{login.lower()}" not in profiles]
        try:
            fetched_profiles = client.get_users(new) if new else {}
        except Exception:
            fetched_profiles = {login: {} for login in new}
        for login in sorted(logins):
            profile_id = f"github_{login.lower()}"
            emails, commit_names = identities.get(login, (set(), set()))
            known = profiles.get(profile_id)
            if known is None:
                user = fetched_profiles.get(login, {})
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
        # those the repositories stage fetched and those this walk visited
        # (named on the profiles it collected). Reading only the first would
        # re-walk the same repositories on every run.
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
                    parts = url.split("github.com/")[-1].split("/")
                    if len(parts) != 2:
                        continue
                    visited.add(url)
                    fresh.append((parts[0], parts[1], url, repository))

            if not fresh:
                logger.info("social_graph: ring %d walked nothing new, converged", ring)
                break
            for owner, name, url, payload in fresh:
                added_profiles += self._harvest(client, owner, name, url, profiles, payload)
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
