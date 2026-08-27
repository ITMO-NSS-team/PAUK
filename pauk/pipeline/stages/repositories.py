from datetime import UTC, date, datetime
from urllib.parse import urlparse

from pauk.models import GitHubProfile, RepoLink, Repository
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.redaction import redact_text
from pauk.sources.github import GitHubClient

from .base import EnrichmentStage

GITHUB_HOSTS = {"github.com", "www.github.com"}

def _payload_date(value: str | None) -> date | None:
    """GitHub timestamps are ISO-8601 with a `Z`, which date.fromisoformat
    rejects before Python 3.11 and which carries a time we don't keep."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _github_owner_name(url: str | None) -> tuple[str, str] | None:
    """(owner, name) for a github.com URL of exactly two path segments.

    Anything else — a gist, a subdirectory link, another host — is not a
    repository this stage can fetch.
    """
    parsed = urlparse((url or "").rstrip("/"))
    parts = parsed.path.strip("/").split("/")
    if parsed.netloc.lower() not in GITHUB_HOSTS or len(parts) != 2:
        return None
    return parts[0], parts[1]


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

    def _row_in_scope(self, row: RepoLink) -> bool:
        if self.selection is None:
            return True
        if self.selection.entity in {"repo_links", "publications"}:
            return row.publication_id in self.selection.ids
        return self.selection.entity == "repositories"

    def _enrich_repository(self, client: GitHubClient, repo: Repository, owner: str,
                           name: str, source_url: str, profiles: dict[str, GitHubProfile],
                           state: ProcessingState | None) -> None:
        """One repository's metadata, its README status and its owner's
        profile stub. The people behind it are a separate stage — see
        repo_people.py for why.

        `source_url` is the URL this repository was reached by — the cited one
        when a publication led here, its own otherwise. It is what the raw
        store records, and the fallback when the payload carries no html_url.
        """
        try:
            payload = client.get_repository(owner, name)
            self.raw.append("github", payload, {"repository": source_url})
            repo.url = payload.get("html_url") or source_url
            repo.name = payload.get("name") or name
            # Survives renames and owner transfers — the dedup stage
            # uses it to recognise rows created before a rename.
            repo.github_id = payload.get("id")
            repo.description = payload.get("description")
            repo.stars_num = payload.get("stargazers_count")
            repo.topics = payload.get("topics") or []
            repo.language = payload.get("language")
            repo.forks_num = payload.get("forks_count")
            repo.archived = payload.get("archived")
            repo.is_fork = payload.get("fork")
            # `pushed_at` is the last commit; `updated_at` also moves on
            # a star or a description edit, which says nothing about
            # whether the code is still alive.
            repo.last_updated = _payload_date(payload.get("pushed_at"))
            repo.license = (payload.get("license") or {}).get("spdx_id")
            repo.has_readme = client.has_readme(owner, name)
            owner_data = payload.get("owner") or {}
            repo.owner_login = owner_data.get("login")
            repo.access_date = date.today()
            if repo.owner_login:
                profile_id = f"github_{repo.owner_login.lower()}"
                known = profiles.get(profile_id)
                if known is None:
                    known = GitHubProfile(id=profile_id, login=repo.owner_login)
                    profiles[profile_id] = known
                # The nested owner object carries a login, a type and
                # a URL, never a name or a location. Writing a fresh
                # profile from it would drop the emails and commit
                # names an earlier repository revealed about the same
                # person. `or ""` and not a .get() default: the API
                # serves explicit nulls, which .get(key, "") passes on.
                known.html_url = owner_data.get("html_url") or known.html_url
                known.type = (owner_data.get("type") or "").lower() or known.type
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

    def _pending_repository_ids(
        self, rows: list[RepoLink], repositories: dict[str, Repository],
    ) -> set[str]:
        pending: set[str] = set()
        for row in rows:
            if not self._row_in_scope(row):
                continue
            for link in row.links:
                parsed = _github_owner_name(link.url)
                if parsed is None:
                    continue
                repo_id = f"github_{parsed[0].lower()}_{parsed[1].lower()}"
                if not self.in_scope("repositories", repo_id):
                    continue
                repo = repositories.get(repo_id)
                if repo is None or self.needs_attempt(repo.processing.get(self.name)):
                    pending.add(repo_id)
        pending |= set(self._unlinked_repositories(repositories))
        return pending

    def _unlinked_repositories(self, repositories: dict[str, Repository]) -> dict[str, Repository]:
        """Rows still needing an attempt, keyed by the id their own URL gives.

        A curated import writes the Repository row straight into the
        collection with no link behind it, so a work list built only from
        repo_links can never reach it again.

        Keyed by the URL-derived id, not `repo.id`, because those two differ
        once a row has been re-keyed to its canonical identity: the link pass
        works from the cited URL, and without a shared key a forced run would
        fetch such a row twice.
        """
        found: dict[str, Repository] = {}
        for repo in repositories.values():
            parsed = _github_owner_name(repo.url)
            if parsed is None or not self.in_scope("repositories", repo.id):
                continue
            if not self.needs_attempt(repo.processing.get(self.name)):
                continue
            found[f"github_{parsed[0].lower()}_{parsed[1].lower()}"] = repo
        return found

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
                parsed = _github_owner_name(url)
                if parsed is None:
                    continue
                owner, name = parsed
                repo_id = f"github_{owner.lower()}_{name.lower()}"
                if not self.in_scope("repositories", repo_id):
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
                self._enrich_repository(client, repo, owner, name, url, profiles, state)
                progress.update()
                changed += 1

        # Second pass: rows the links never reach. The loop above is what
        # *discovers* repositories, this is what keeps already-known ones
        # enriched — including the curated rows that arrived without a link.
        for url_id, repo in sorted(self._unlinked_repositories(repositories).items()):
            if url_id in attempted_repo_ids:
                continue
            attempted_repo_ids.add(url_id)
            owner, name = _github_owner_name(repo.url)
            self._enrich_repository(client, repo, owner, name, repo.url,
                                    profiles, repo.processing.get(self.name))
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
