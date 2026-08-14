from datetime import UTC, datetime

from pauk.models import Publication
from pauk.models.processing import ProcessingState, ProcessingStatus

from .base import EnrichmentStage


class PdfStage(EnrichmentStage):
    """Records whether OpenAlex supplied a PDF URL; downloading is optional."""

    name = "pdf"
    progress_label = "Publications: checking PDF availability"

    def run(self) -> dict[str, int]:
        publications = list(self.prepared.read_models("publications", Publication))
        candidates = [
            publication for publication in publications
            if self.selected("publications", publication.id)
            and self.needs_attempt(publication.processing.get(self.name))
        ]
        changed = 0
        for publication in self.progress(candidates, total=len(candidates)):
            state = publication.processing.get(self.name)
            publication.processing[self.name] = ProcessingState(
                status=ProcessingStatus.COMPLETED if publication.pdf_url else ProcessingStatus.NOT_APPLICABLE,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(UTC), result_count=int(bool(publication.pdf_url)),
            )
            changed += 1
        self.prepared.write_models("publications", publications)
        return {"publications": changed}
