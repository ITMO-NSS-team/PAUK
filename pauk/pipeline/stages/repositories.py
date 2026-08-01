from datetime import date, datetime, timezone
from urllib.parse import urlparse

from pauk.models import GitHubProfile, RepoLink, Repository
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.sources.github import GitHubClient

from .base import EnrichmentStage


GITHUB_HOSTS = {"github.com", "www.github.com"}


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

    def _row_in_scope(self, row: RepoLink) -> bool:
        if self.selection is None:
            return True
        if self.selection.entity in {"repo_links", "publications"}:
            return row.publication_id in self.selection.ids
        return self.selection.entity == "repositories"

    def run(self) -> dict[str, int]:
        rows = list(self.prepared.read_models("repo_links", RepoLink))
        repositories = {
            repository.id: repository
            for repository in self.prepared.read_models("repositories", Repository)
        }
        profiles = {
            profile.id: profile
            for profile in self.prepared.read_models("github_profiles", GitHubProfile)
        }
        client = GitHubClient(self.config.request_timeout, self.config.github_token)
        changed = 0
        attempted_repo_ids: set[str] = set()
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
                if self.selection is not None and self.selection.entity == "repositories" and repo_id not in self.selection.ids:
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
                    repo.description = payload.get("description")
                    repo.stars_num = payload.get("stargazers_count")
                    repo.has_readme = False
                    repo.owner_login = (payload.get("owner") or {}).get("login")
                    repo.access_date = date.today()
                    owner_data = payload.get("owner") or {}
                    if repo.owner_login:
                        profile_id = f"github_{repo.owner_login.lower()}"
                        # `or ""` and not a .get() default: the API serves
                        # explicit nulls, which .get(key, "") passes through.
                        profiles[profile_id] = GitHubProfile(
                            id=profile_id, login=repo.owner_login,
                            name=owner_data.get("name"), html_url=owner_data.get("html_url"),
                            type=(owner_data.get("type") or "").lower() or None,
                        )
                    repo.processing[self.name] = ProcessingState(
                        status=ProcessingStatus.COMPLETED,
                        attempts=(state.attempts if state else 0) + 1,
                        finished_at=datetime.now(timezone.utc), result_count=1,
                    )
                except Exception as exc:
                    repo.processing[self.name] = ProcessingState(
                        status=ProcessingStatus.FAILED,
                        attempts=(state.attempts if state else 0) + 1,
                        finished_at=datetime.now(timezone.utc), error=str(exc),
                    )
                changed += 1
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
