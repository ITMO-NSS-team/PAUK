from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ProcessingStatus(StrEnum):
    NOT_STARTED = "not_started"
    COMPLETED = "completed"
    COMPLETED_EMPTY = "completed_empty"
    NOT_APPLICABLE = "not_applicable"
    FAILED = "failed"


class ClassificationStatus(StrEnum):
    PENDING = "pending"
    CLASSIFIED = "classified"
    FAILED = "failed"


class ProcessingState(BaseModel):
    status: ProcessingStatus = ProcessingStatus.NOT_STARTED
    # The identifier used for this attempt.  A completed result for one DOI,
    # ORCID, or email must not suppress a later request for a different one.
    request_key: str | None = None
    attempts: int = 0
    finished_at: datetime | None = None
    error: str | None = None
    result_count: int | None = None

