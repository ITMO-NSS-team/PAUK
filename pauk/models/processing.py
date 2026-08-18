from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ProcessingStatus(StrEnum):
    NOT_STARTED = "not_started"
    COMPLETED = "completed"
    COMPLETED_EMPTY = "completed_empty"
    NOT_APPLICABLE = "not_applicable"
    FAILED = "failed"


class ProcessingState(BaseModel):
    status: ProcessingStatus = ProcessingStatus.NOT_STARTED
    # The identifier used for this attempt.  A completed result for one DOI,
    # ORCID, or email must not suppress a later request for a different one.
    request_key: str | None = None
    # Some sources use a multi-step lookup; this records the next or final
    # route without multiplying one source into several processing keys.
    phase: str | None = None
    attempts: int = 0
    finished_at: datetime | None = None
    error: str | None = None
    result_count: int | None = None

