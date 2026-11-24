"""The people behind a repository, collected separately from its metadata.

Kept apart from `repositories` so the two jobs have separate `processing`
state: refreshing repository metadata must not re-walk every contributor and
re-fetch every profile (the bulk of GitHub requests), and each half can be
stale and re-run on its own.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pauk.models import GitHubProfile, Repository
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.redaction import redact_text
from pauk.sources.base import HttpRequestError
from pauk.sources.github import GitHubClient

from .base import EnrichmentStage
from .repositories import _github_owner_name

# Pages of commits read per repository, 100 commits each: enough for the git
# identities of everyone who worked on a paper's code, without the whole history.
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
    account carry no login and are skipped.
    """
    identities: dict[str, tuple[set[str], set[str]]] = {}
    for commit in commits:
        login = ((commit.get("author") or {}).get("login") or "")
        if not login:
            continue
        author = (commit.get("commit") or {}).get("author") or {}
        emails, names = identities.setdefault(login.lower(), (set(), set()))
        email = (author.get("email") or "").strip().lower()
        if email and NOREPLY_EMAIL not in email:
            emails.add(email)
        if author.get("name"):
            names.add(author["name"].strip())
    return identities


def _apply_profile_details(profile: GitHubProfile, payload: dict) -> None:
    profile.name = payload.get("name") or profile.name
    profile.html_url = payload.get("html_url") or profile.html_url
    profile.description = payload.get("bio") or payload.get("description") or profile.description
    profile.location = payload.get("location") or profile.location
    profile.company = payload.get("company") or profile.company
    profile.type = (payload.get("type") or "").lower() or profile.type
    profile_email = (payload.get("email") or "").strip().lower()
    if profile_email and NOREPLY_EMAIL not in profile_email:
        profile.emails = sorted(set(profile.emails) | {profile_email})
    profile.profile_fetched = True


class RepoPeopleStage(EnrichmentStage):
    name = "repo_people"
    progress_label = "Repositories: collecting the people behind them"

    def _repo_in_scope(self, repo: Repository) -> bool:
        """What a publication-scoped run means for a repository row.

        `in_scope` alone would answer True for every row here, because it lets a
        publication-scoped selection through untouched and would walk the whole
        group. `RepositoriesStage` reads the scope off the RepoLink row; by this
        stage the Repository carries its publications itself.
        """
        if self.selection is None:
            return True
        if self.selection.entity in {"repo_links", "publications"}:
            return bool(set(repo.publication_ids) & self.selection.ids)
        return self.in_scope("repositories", repo.id)

    def _harvest(self, client: GitHubClient, repo: Repository, owner: str, name: str,
                 profiles: dict[str, GitHubProfile], *, refreshed: set[str] | None = None) -> None:
        """Collect the people behind one repository into github_profiles.

        These are the candidates an author is later matched against: the
        owner and everyone credited with a commit, each carrying the emails
        and names their commits reveal. Organizations and bots are skipped.
        """
        contributors = client.contributors(owner, name)
        identities = _git_identities(client.commits(owner, name, COMMIT_PAGES))
        if refreshed is None:
            refreshed = set()

        owner_profile = profiles.get(f"github_{(repo.owner_login or '').lower()}")
        logins: dict[str, str] = {}
        if repo.owner_login and _is_person(repo.owner_login,
                                           owner_profile.type if owner_profile else None):
            logins[repo.owner_login.lower()] = repo.owner_login
        for contributor in contributors:
            login = contributor.get("login") or ""
            if _is_person(login, contributor.get("type")):
                logins.setdefault(login.lower(), login)

        repo.contributors = sorted(logins.values())
        candidates = {}
        for key, login in sorted(logins.items()):
            profile_id = f"github_{key}"
            profile = profiles.get(profile_id)
            if profile is None:
                profile = profiles[profile_id] = GitHubProfile(id=profile_id, login=login)
            emails, commit_names = identities.get(key, (set(), set()))
            profile.emails = sorted(set(profile.emails) | emails)
            profile.commit_names = sorted(set(profile.commit_names) | commit_names)
            profile.repos = sorted(set(profile.repos) | {repo.url})
            if key not in refreshed and (self.force or not profile.profile_fetched):
                candidates[key] = profile

        if not candidates:
            return
        failure = None
        for login, payload in client.get_users(candidates).items():
            if isinstance(payload, HttpRequestError):
                if failure is None:
                    failure = payload
            else:
                profile = candidates[login]
                self.raw.append("github_user", payload, {"login": profile.login})
                _apply_profile_details(profile, payload)
                refreshed.add(login)
        if failure is not None:
            raise failure.with_traceback(None)

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
            if self._repo_in_scope(repo)
            and _github_owner_name(repo.url) is not None
            and self.needs_attempt(repo.processing.get(self.name))
        ]
        client = GitHubClient(self.config.request_timeout, self.config.github_token)
        refreshed: set[str] = set()
        changed = 0
        for repo in self.progress(sorted(pending, key=lambda r: r.id),
                                  total=len(pending), unit="repository"):
            owner, name = _github_owner_name(repo.url)
            state = repo.processing.get(self.name)
            try:
                self._harvest(client, repo, owner, name, profiles, refreshed=refreshed)
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
