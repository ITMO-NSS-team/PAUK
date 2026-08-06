from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from pauk.models import CodeLink, Publication, RepoLink
from pauk.models.processing import ProcessingState, ProcessingStatus
from .base import EnrichmentStage


GITHUB_URL = re.compile(r"https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+", re.IGNORECASE)

# Work types that are a deposit of something rather than a paper about it.
DEPOSIT_TYPES = {"software", "dataset"}
# How GitHub titles the snapshot it archives on Zenodo for a release:
# "asl/BandageNG: Continuous build". The owner/name part carries no spaces,
# which keeps titles like "A/B testing: results" out.
REPOSITORY_ARCHIVE = re.compile(r"^([\w.-]+)/([\w.-]+):\s")


def _canonical_github_url(url: str) -> str:
    """Store www.github.com links under GitHub's canonical host."""
    parsed = urlparse(url)
    if parsed.netloc.lower() == "www.github.com":
        return parsed._replace(netloc="github.com").geturl()
    return url


def _archived_repository_url(publication: Publication) -> str | None:
    """The repository a software or dataset deposit is an archive of.

    Zenodo mints a DOI for every GitHub release, and OpenAlex indexes each
    one as a work of its own — which is why "asl/BandageNG: Continuous
    build" sits in the graph looking like a paper. The repository it
    archives is named in the title, so the deposit can point at it instead
    of standing alone.
    """
    if publication.type not in DEPOSIT_TYPES:
        return None
    match = REPOSITORY_ARCHIVE.match(publication.title or "")
    if not match:
        return None
    return f"https://github.com/{match.group(1)}/{match.group(2)}"


class CodeLinksStage(EnrichmentStage):
    name = "code_links"

    def run(self) -> dict[str, int]:
        publications = list(self.prepared.read_models("publications", Publication))
        links_by_publication = {
            row.publication_id: row
            for row in self.prepared.read_models("repo_links", RepoLink)
        }
        changed = 0
        for pub in publications:
            if self.selection is not None and (
                self.selection.entity != "publications" or pub.id not in self.selection.ids
            ):
                continue
            state = pub.processing.get(self.name)
            if not self.needs_attempt(state):
                continue
            # rstrip(".") drops sentence-ending periods the regex captures
            # ("code at https://github.com/org/repo." -> repo name "repo.");
            # ".git" is a clone-URL suffix, never part of a repo name.
            archived = _archived_repository_url(pub)
            urls = list(dict.fromkeys(
                _canonical_github_url(url.rstrip(".").removesuffix(".git"))
                for url in ([archived] if archived else []) + GITHUB_URL.findall(pub.abstract or "")
            ))
            pub.has_code = bool(urls)
            pub.code_url = urls[0] if urls else None
            pub.processing[self.name] = ProcessingState(
                status=ProcessingStatus.COMPLETED if urls else ProcessingStatus.COMPLETED_EMPTY,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(timezone.utc), result_count=len(urls),
            )
            links_by_publication[pub.id] = RepoLink(publication_id=pub.id, links=[
                CodeLink(url=url, host=urlparse(url).netloc, is_relevant=True, llm_confidence=1.0,
                         llm_reason=("repository_archived_by_this_deposit" if url == archived
                                     else "github_url_in_abstract"))
                for url in urls
            ])
            changed += 1
        self.prepared.write_models("publications", publications)
        self.prepared.write_models("repo_links", links_by_publication.values())
        return {"publications": changed, "repo_links": len(links_by_publication)}
