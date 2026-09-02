from collections import defaultdict
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


def _url_repo_id(url: str | None) -> str | None:
    """`github_{owner}_{name}` for a repository URL, or None if it is not one.

    The id a row is reached by, as opposed to `_canonical_repo_id`, which is
    the identity the fetched payload gives it. Both passes of the stage key
    their work by this, so it lives in one place.
    """
    parsed = _github_owner_name(url)
    return f"github_{parsed[0].lower()}_{parsed[1].lower()}" if parsed else None


def _fold_into(winner: Repository, loser: Repository) -> None:
    """Fold one row of a repository into another row of the same repository.

    Only what a row accumulates from the outside moves: the publications that
    cited it and the URLs they cited it by. Everything else is either fetched
    (and the winner is the one that was fetched) or derived from the payload.
    """
    winner.publication_ids = list(dict.fromkeys([
        *winner.publication_ids, *loser.publication_ids,
    ]))
    winner.cited_urls = list(dict.fromkeys([
        *winner.cited_urls, *loser.cited_urls,
    ]))


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
        unlinked: dict[str, list[Repository]],
    ) -> set[str]:
        pending: set[str] = set()
        for row in rows:
            if not self._row_in_scope(row):
                continue
            for link in row.links:
                repo_id = _url_repo_id(link.url)
                if repo_id is None or not self.in_scope("repositories", repo_id):
                    continue
                repo = repositories.get(repo_id)
                if repo is None or self.needs_attempt(repo.processing.get(self.name)):
                    pending.add(repo_id)
        pending |= set(unlinked)
        return pending

    def _unlinked_repositories(
        self, repositories: dict[str, Repository],
    ) -> dict[str, list[Repository]]:
        """Rows still needing an attempt, grouped by the id their own URL gives.

        A curated import writes the Repository row straight into the
        collection with no link behind it, so a work list built only from
        repo_links can never reach it again.

        Keyed by the URL-derived id, not `repo.id`, because those two differ
        once a row has been re-keyed to its canonical identity: the link pass
        works from the cited URL, and without a shared key a forced run would
        fetch such a row twice.

        A key can hold more than one row — a curated import brings its own id
        and the link pass derives one from the cited URL, and both can point
        at the same owner/name. They are one repository, so they are grouped
        rather than overwritten: `run()` fetches the group once and folds the
        rest into what it fetched. Overwriting would starve the same row on
        every run, since rows are read in a stable order.
        """
        found: dict[str, list[Repository]] = defaultdict(list)
        for repo in repositories.values():
            url_id = _url_repo_id(repo.url)
            if url_id is None or not self.in_scope("repositories", repo.id):
                continue
            if not self.needs_attempt(repo.processing.get(self.name)):
                continue
            found[url_id].append(repo)
        return dict(found)

    def _fold_duplicates(self, rows: list[Repository],
                         repositories: dict[str, Repository]) -> Repository:
        """The one row of a URL group worth fetching, with the rest folded in.

        The row that has been to the API wins: `github_id` only ever comes
        from a payload, so that row's name and URL are the canonical ones, and
        a `processing` entry for this stage is the attempt history that would
        otherwise be lost. Ties fall back to the id, so the winner does not
        depend on the order rows are read in. The losers leave their id behind
        in `merged_ids`, which is what lets the graph loader resolve a link
        that still points at them.
        """
        def rank(repo: Repository) -> tuple[int, int, str]:
            return (0 if repo.github_id else 1,
                    0 if self.name in repo.processing else 1,
                    repo.id)

        winner, *losers = sorted(rows, key=rank)
        for loser in losers:
            _fold_into(winner, loser)
            winner.merged_ids = list(dict.fromkeys([
                *winner.merged_ids, loser.id, *loser.merged_ids,
            ]))
            repositories.pop(loser.id, None)
        return winner

    def run(self) -> dict[str, int]:
        rows = list(self.prepared.read_models("repo_links", RepoLink))
        repositories = {
            repository.id: repository for repository in self.prepared.read_models("repositories", Repository)
        }
        profiles = {profile.id: profile for profile in self.prepared.read_models("github_profiles", GitHubProfile)}
        client = GitHubClient(self.config.request_timeout, self.config.github_token)
        changed = 0
        attempted_repo_ids: set[str] = set()
        # Taken before the first fetch, because a fetch can rewrite `repo.url`
        # to the canonical one GitHub redirects to. Recomputing this after the
        # link pass would key the same row under its new URL, miss it in
        # `attempted_repo_ids` and fetch it a second time.
        unlinked = self._unlinked_repositories(repositories)
        progress = self.progress_bar(
            total=len(self._pending_repository_ids(rows, repositories, unlinked)),
            unit="repository")
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
                # A row reached by a link can carry a URL of its own that
                # differs from the cited one; the second pass is keyed by
                # that, so claim it here — before the fetch rewrites it.
                attempted_repo_ids.add(_url_repo_id(repo.url) or repo_id)
                self._enrich_repository(client, repo, owner, name, url, profiles, state)
                progress.update()
                changed += 1

        # Second pass: rows the links never reach. The loop above is what
        # *discovers* repositories, this is what keeps already-known ones
        # enriched — including the curated rows that arrived without a link.
        for url_id, group in sorted(unlinked.items()):
            repo = self._fold_duplicates(group, repositories)
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
                # A row folded away earlier can have been keyed by the very id
                # this row is about to take; nothing should list itself as
                # merged away, least of all the graph loader's alias table.
                repo.merged_ids = [m for m in repo.merged_ids if m != canonical_id]
                canonical[canonical_id] = repo
            else:
                _fold_into(winner, repo)
        repositories = canonical

        for repo in repositories.values():
            repo.publication_ids = list(dict.fromkeys(repo.publication_ids))
        self.prepared.write_models("repositories", repositories.values())
        self.prepared.write_models("github_profiles", profiles.values())
        return {"repositories": changed, "github_profiles": len(profiles)}
