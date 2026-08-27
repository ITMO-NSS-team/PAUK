"""The people behind a repository, collected separately from its metadata.

Split out of `repositories` deliberately. Both jobs used to share one
`processing` entry, so refreshing a repository's metadata meant re-walking
every contributor and re-fetching every profile: on the August 2026 data that
was 6143 of 6864 GitHub requests, and it burned the whole hourly quota to
pick up fields that arrive free in the repository payload.

Two stages give the two jobs separate `processing` state, so each can be
stale — and re-run — on its own. A `--skip-accounts` flag could not: the row
would still claim `repositories: completed` with people data from an older
run, and nothing would say which half was old.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pauk.models import GitHubProfile, Repository
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.redaction import redact_text
from pauk.sources.github import GitHubClient

from .base import EnrichmentStage
from .repositories import _github_owner_name

# Pages of commits read per repository, 100 commits each. Three is what the
# previous pipeline used: enough for the git identities of everyone who
# worked on a paper's code, without paying for the whole history.
COMMIT_PAGES = 3

# GitHub hides a user's address behind this domain when they ask it to; it
# identifies the account, not the person, so it is no use for matching.
NOREPLY_EMAIL = "users.noreply.github.com"

# Accounts that commit on behalf of tooling rather than a person.
# web-flow is what GitHub signs commits made through its web editor with,
# so it turns up in almost every repository.
BOT_LOGIN = re.compile(r"\[bot\]$|^dependabot|^github-actions|^renovate|^web-flow$", re.I)


def _is_person(login: str, account_type: str | None) -> bool:
    # Case-insensitive on purpose: the API answers "User", but a type read
    # back from a stored GitHubProfile was lowercased on the way in.
    return (bool(login) and not BOT_LOGIN.search(login)
            and (account_type or "User").casefold() == "user")


def _git_identities(commits: list[dict]) -> dict[str, tuple[set[str], set[str]]]:
    """Emails and names each account used in its commits, keyed by login.

    A commit pairs the GitHub account that owns it with the git identity
    configured on the machine that made it. Commits whose email matches no
    account carry no login and are skipped: there is nobody to attribute
    them to.
    """
    identities: dict[str, tuple[set[str], set[str]]] = {}
    for commit in commits:
        login = ((commit.get("author") or {}).get("login") or "")
        if not login:
            continue
        author = (commit.get("commit") or {}).get("author") or {}
        emails, names = identities.setdefault(login, (set(), set()))
        email = (author.get("email") or "").strip().lower()
        if email and NOREPLY_EMAIL not in email:
            emails.add(email)
        if author.get("name"):
            names.add(author["name"].strip())
    return identities


def _is_filled(profile: GitHubProfile | None) -> bool:
    """Whether a stored profile already carries what GET /users/{login} adds.

    An account reached from a second repository, or on a re-run, has nothing
    new to learn from the endpoint. In the August 2026 run all 5059 profiles
    already existed and all 5188 calls were spent re-fetching them.
    """
    return profile is not None and bool(profile.html_url)


class RepoPeopleStage(EnrichmentStage):
    name = "repo_people"
    progress_label = "Repositories: collecting the people behind them"

    def _harvest(self, client: GitHubClient, repo: Repository, owner: str, name: str,
                 profiles: dict[str, GitHubProfile]) -> None:
        """Collect the people behind one repository into github_profiles.

        These are the candidates an author is later matched against: the
        owner and everyone credited with a commit, each carrying the emails
        and names their commits reveal. Organizations and bots are skipped —
        neither is a person anyone can be matched to.
        """
        contributors = client.contributors(owner, name)
        identities = _git_identities(client.commits(owner, name, COMMIT_PAGES))

        owner_profile = profiles.get(f"github_{(repo.owner_login or '').lower()}")
        roles: dict[str, str] = {}
        if repo.owner_login and _is_person(repo.owner_login,
                                           owner_profile.type if owner_profile else None):
            roles[repo.owner_login] = "owner"
        for contributor in contributors:
            login = contributor.get("login") or ""
            if _is_person(login, contributor.get("type")):
                roles.setdefault(login, "contributor")

        repo.contributors = sorted(roles)
        for login in sorted(roles):
            profile_id = f"github_{login.lower()}"
            emails, commit_names = identities.get(login, (set(), set()))
            known = profiles.get(profile_id)
            payload: dict = {}
            if self.force or not _is_filled(known):
                try:
                    payload = client.get_user(login)
                except Exception:
                    payload = {}
                self.raw.append("github_user", payload, {"login": login})
            profile_email = (payload.get("email") or "").strip().lower()
            if profile_email and NOREPLY_EMAIL not in profile_email:
                emails = emails | {profile_email}
            profiles[profile_id] = GitHubProfile(
                id=profile_id,
                login=login,
                name=payload.get("name") or (known.name if known else None),
                html_url=payload.get("html_url") or (known.html_url if known else None),
                description=payload.get("bio") or (known.description if known else None),
                location=payload.get("location") or (known.location if known else None),
                company=payload.get("company") or (known.company if known else None),
                type=(payload.get("type") or "").lower() or (known.type if known else None),
                emails=sorted(set(known.emails if known else []) | emails),
                commit_names=sorted(set(known.commit_names if known else []) | commit_names),
                repos=sorted(set(known.repos if known else []) | {repo.url}),
            )

    def run(self) -> dict[str, int]:
        repositories = {
            repository.id: repository
            for repository in self.prepared.read_models("repositories", Repository)
        }
        profiles = {
            profile.id: profile
            for profile in self.prepared.read_models("github_profiles", GitHubProfile)
        }
        pending = [
            repo for repo in repositories.values()
            if self.in_scope("repositories", repo.id)
            and _github_owner_name(repo.url) is not None
            and self.needs_attempt(repo.processing.get(self.name))
        ]
        client = GitHubClient(self.config.request_timeout, self.config.github_token)
        changed = 0
        for repo in self.progress(sorted(pending, key=lambda r: r.id),
                                  total=len(pending), unit="repository"):
            owner, name = _github_owner_name(repo.url)
            state = repo.processing.get(self.name)
            try:
                self._harvest(client, repo, owner, name, profiles)
                status, error = ProcessingStatus.COMPLETED, None
            except Exception as exc:
                # GitHub answers 403 on repositories it has not analysed yet.
                # That is a failure of this stage alone; the repository's own
                # metadata, gathered by `repositories`, stays untouched.
                status, error = ProcessingStatus.FAILED, redact_text(exc)
            repo.processing[self.name] = ProcessingState(
                status=status,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(UTC),
                error=error,
                result_count=len(repo.contributors) if status is ProcessingStatus.COMPLETED else None,
            )
            changed += 1
        self.prepared.upsert_models("repositories", repositories.values())
        self.prepared.upsert_models("github_profiles", profiles.values())
        return {"repositories": changed, "github_profiles": len(profiles)}
