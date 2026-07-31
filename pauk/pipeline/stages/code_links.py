from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from pauk.models import CodeLink, Publication, RepoLink
from pauk.models.processing import ProcessingState, ProcessingStatus
from .base import EnrichmentStage


GITHUB_URL = re.compile(r"https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+", re.IGNORECASE)


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
            if state and state.status not in {ProcessingStatus.NOT_STARTED, ProcessingStatus.FAILED}:
                continue
            # rstrip(".") drops sentence-ending periods the regex captures
            # ("code at https://github.com/org/repo." -> repo name "repo.").
            urls = list(dict.fromkeys(url.rstrip(".") for url in GITHUB_URL.findall(pub.abstract or "")))
            pub.has_code = bool(urls)
            pub.code_url = urls[0] if urls else None
            pub.processing[self.name] = ProcessingState(
                status=ProcessingStatus.COMPLETED if urls else ProcessingStatus.COMPLETED_EMPTY,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(timezone.utc), result_count=len(urls),
            )
            links_by_publication[pub.id] = RepoLink(publication_id=pub.id, links=[
                CodeLink(url=url, host=urlparse(url).netloc, is_relevant=True,
                         llm_confidence=1.0, llm_reason="github_url_in_abstract")
                for url in urls
            ])
            changed += 1
        self.prepared.write_models("publications", publications)
        self.prepared.write_models("repo_links", links_by_publication.values())
        return {"publications": changed, "repo_links": len(links_by_publication)}
