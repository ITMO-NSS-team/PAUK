from datetime import datetime, timezone

from pauk.models import Publication
from pauk.models.processing import ProcessingState, ProcessingStatus
from .base import EnrichmentStage


class PdfStage(EnrichmentStage):
    """Records whether OpenAlex supplied a PDF URL; downloading is optional."""

    name = "pdf"

    def run(self) -> dict[str, int]:
        publications = list(self.prepared.read_models("publications", Publication))
        changed = 0
        for publication in publications:
            if self.selection is not None and (
                self.selection.entity != "publications" or publication.id not in self.selection.ids
            ):
                continue
            state = publication.processing.get(self.name)
            if state and state.status not in {ProcessingStatus.NOT_STARTED, ProcessingStatus.FAILED}:
                continue
            publication.processing[self.name] = ProcessingState(
                status=ProcessingStatus.COMPLETED if publication.pdf_url else ProcessingStatus.NOT_APPLICABLE,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(timezone.utc), result_count=int(bool(publication.pdf_url)),
            )
            changed += 1
        self.prepared.write_models("publications", publications)
        return {"publications": changed}
