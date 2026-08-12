import re
from datetime import UTC, date, datetime
from urllib.parse import urlparse

from pauk.models import GitHubProfile, RepoLink, Repository
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.redaction import redact_text
from pauk.sources.github import GitHubClient

from .base import EnrichmentStage

GITHUB_HOSTS = {"github.com", "www.github.com"}

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
    return bool(login) and not BOT_LOGIN.search(login) and (account_type or "User") == "User"


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


def _canonical_repo_id(repo: Repository) -> str:
    """github_{owner}_{name} from the fetched payload, not from the cited URL.

    A repository renamed on GitHub (or cited in a different letter case)
    yields a row keyed by the cited URL but carrying the canonical html_url.
    Without re-keying, two rows would share one URL and violate the
    Repository.url uniqueness constraint at publish time.
    """
    if repo.owner_login and repo.name:
        return f"github_{repo.owner_login.lower()}_{repo.name.lower()}"
    return repo.id


class RepositoriesStage(EnrichmentStage):
    name = "repositories"
    progress_label = "Repositories: retrieving metadata and README status from GitHub"

    def _harvest_accounts(self, client: GitHubClient, repo: Repository, owner: str,
                          name: str, owner_type: str | None,
                          profiles: dict[str, GitHubProfile]) -> None:
        """Collect the people behind a repository into github_profiles.

        These are the candidates an author is later matched against: the
        owner and everyone credited with a commit, each carrying the emails
        and names their commits reveal. Organizations and bots are skipped —
        neither is a person anyone can be matched to.

        Failures here are swallowed. Contributor and commit lists are an
        extra on top of the repository itself, and an empty or forbidden
        list (GitHub answers 403 on repositories it has not analysed) must
        not cost the metadata already fetched.
        """
        try:
            contributors = client.contributors(owner, name)
            identities = _git_identities(client.commits(owner, name, COMMIT_PAGES))
        except Exception:
            return

        roles: dict[str, str] = {}
        if repo.owner_login and _is_person(repo.owner_login, owner_type):
            roles[repo.owner_login] = "owner"
        for contributor in contributors:
            login = contributor.get("login") or ""
            if _is_person(login, contributor.get("type")):
                roles.setdefault(login, "contributor")

        repo.contributors = sorted(roles)
        for login in sorted(roles):
            profile_id = f"github_{login.lower()}"
            emails, commit_names = identities.get(login, (set(), set()))
            try:
                payload = client.get_user(login)
            except Exception:
                payload = {}
            self.raw.append("github_user", payload, {"login": login})
            profile_email = (payload.get("email") or "").strip().lower()
            if profile_email and NOREPLY_EMAIL not in profile_email:
                emails = emails | {profile_email}
            known = profiles.get(profile_id)
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

    def _row_in_scope(self, row: RepoLink) -> bool:
        if self.selection is None:
            return True
        if self.selection.entity in {"repo_links", "publications"}:
            return row.publication_id in self.selection.ids
        return self.selection.entity == "repositories"

    def _pending_repository_ids(
        self, rows: list[RepoLink], repositories: dict[str, Repository],
    ) -> set[str]:
        pending: set[str] = set()
        for row in rows:
            if not self._row_in_scope(row):
                continue
            for link in row.links:
                parsed = urlparse(link.url.rstrip("/"))
                parts = parsed.path.strip("/").split("/")
                if parsed.netloc.lower() not in GITHUB_HOSTS or len(parts) != 2:
                    continue
                repo_id = f"github_{parts[0].lower()}_{parts[1].lower()}"
                if (self.selection is not None and self.selection.entity == "repositories"
                        and repo_id not in self.selection.ids):
                    continue
                repo = repositories.get(repo_id)
                if repo is None or self.needs_attempt(repo.processing.get(self.name)):
                    pending.add(repo_id)
        return pending

    def run(self) -> dict[str, int]:
        rows = list(self.prepared.read_models("repo_links", RepoLink))
        repositories = {
            repository.id: repository for repository in self.prepared.read_models("repositories", Repository)
        }
        profiles = {profile.id: profile for profile in self.prepared.read_models("github_profiles", GitHubProfile)}
        client = GitHubClient(self.config.request_timeout, self.config.github_token)
        changed = 0
        attempted_repo_ids: set[str] = set()
        progress = self.progress_bar(
            total=len(self._pending_repository_ids(rows, repositories)), unit="repository")
        for row in rows:
            if not self._row_in_scope(row):
                continue
            for link in row.links:
                url = link.url.rstrip("/")
                parsed = urlparse(url)
                parts = parsed.path.strip("/").split("/")
                if parsed.netloc.lower() not in GITHUB_HOSTS or len(parts) != 2:
                    continue
                owner, name = parts
                repo_id = f"github_{owner.lower()}_{name.lower()}"
                if (
                    self.selection is not None
                    and self.selection.entity == "repositories"
                    and repo_id not in self.selection.ids
                ):
                    continue
                repo = repositories.get(repo_id)
                if repo is not None:
                    if row.publication_id not in repo.publication_ids:
                        repo.publication_ids.append(row.publication_id)
                    if url not in repo.cited_urls:
                        repo.cited_urls.append(url)
                    state = repo.processing.get(self.name)
                    if not self.needs_attempt(state):
                        continue
                else:
                    repo = Repository(id=repo_id, url=url, name=name,
                                      publication_ids=[row.publication_id], cited_urls=[url])
                    repositories[repo_id] = repo
                    state = None
                # One repository can be mentioned by many publications. Its
                # publication IDs are collected above, but the GitHub API must
                # be called at most once per enrichment run (especially with
                # --force, which otherwise retries every mention).
                if repo_id in attempted_repo_ids:
                    continue
                attempted_repo_ids.add(repo_id)
                try:
                    payload = client.get_repository(owner, name)
                    self.raw.append("github", payload, {"repository": url})
                    repo.url = payload.get("html_url") or url
                    repo.name = payload.get("name") or name
                    # Survives renames and owner transfers — the dedup stage
                    # uses it to recognise rows created before a rename.
                    repo.github_id = payload.get("id")
                    repo.description = payload.get("description")
                    repo.stars_num = payload.get("stargazers_count")
                    repo.has_readme = client.has_readme(owner, name)
                    owner_data = payload.get("owner") or {}
                    repo.owner_login = owner_data.get("login")
                    repo.access_date = date.today()
                    if repo.owner_login:
                        profile_id = f"github_{repo.owner_login.lower()}"
                        # `or ""` and not a .get() default: the API serves
                        # explicit nulls, which .get(key, "") passes through.
                        profiles[profile_id] = GitHubProfile(
                            id=profile_id, login=repo.owner_login,
                            name=owner_data.get("name"), html_url=owner_data.get("html_url"),
                            type=(owner_data.get("type") or "").lower() or None,
                        )
                    self._harvest_accounts(client, repo, owner, name,
                                           owner_data.get("type"), profiles)
                    repo.processing[self.name] = ProcessingState(
                        status=ProcessingStatus.COMPLETED,
                        attempts=(state.attempts if state else 0) + 1,
                        finished_at=datetime.now(UTC),
                        result_count=1,
                    )
                except Exception as exc:
                    repo.processing[self.name] = ProcessingState(
                        status=ProcessingStatus.FAILED,
                        attempts=(state.attempts if state else 0) + 1,
                        finished_at=datetime.now(UTC),
                        error=redact_text(exc),
                    )
                progress.update()
                changed += 1
        progress.close()
        # Re-key fetched rows to their canonical identity: a renamed repo (the
        # API redirects the old URL) or a case-variant citation must collapse
        # into one row, otherwise two rows share one canonical URL.
        canonical: dict[str, Repository] = {}
        for repo in repositories.values():
            canonical_id = _canonical_repo_id(repo)
            winner = canonical.get(canonical_id)
            if winner is None:
                repo.id = canonical_id
                canonical[canonical_id] = repo
            else:
                winner.publication_ids = list(dict.fromkeys([
                    *winner.publication_ids, *repo.publication_ids,
                ]))
                winner.cited_urls = list(dict.fromkeys([
                    *winner.cited_urls, *repo.cited_urls,
                ]))
        repositories = canonical

        for repo in repositories.values():
            repo.publication_ids = list(dict.fromkeys(repo.publication_ids))
        self.prepared.write_models("repositories", repositories.values())
        self.prepared.write_models("github_profiles", profiles.values())
        return {"repositories": changed, "github_profiles": len(profiles)}
