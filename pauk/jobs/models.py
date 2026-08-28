"""What a job is, in types.

`pauk.models.processing` describes the state of one prepared row; this
describes the state of a whole run.

The payload is a model per kind rather than a free dict, because the panel
builds it from a form.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from pauk.storage.naming import validate_group


class JobKind(StrEnum):
    COLLECT = "collect"
    PUBLISH = "publish"
    DEDUP = "dedup"
    MAP = "map"


class JobState(StrEnum):
    """CLAIMED sits between QUEUED and RUNNING because a worker takes the
    document first and the resource second. Without a state of its own the
    gap between them would read as running while nothing runs."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: States a job will never leave.
FINAL = frozenset({JobState.DONE, JobState.FAILED, JobState.CANCELLED})

#: Publishing, deduplicating and exporting a snapshot all rewrite or read
#: the whole graph, so they take turns.
GRAPH = "graph"


def now() -> datetime:
    """Current time at the precision BSON keeps.

    Mongo stores milliseconds, so a plain datetime.now() comes back rounded
    and a document read back differs from the one written.
    """
    moment = datetime.now(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def aware(moment: datetime) -> datetime:
    """A stored time with a timezone on it. pymongo returns them naive, and
    comparing one against an aware now() raises."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class CollectPayload(BaseModel):
    """`--works-file` is deliberately absent: it names a path on the machine
    running the pipeline, and a path from a browser reads unrelated files."""

    group: str
    work_id: str | None = None
    date_from: str | None = None
    date_to: str | None = None

    @field_validator("group")
    @classmethod
    def _known_group(cls, value: str) -> str:
        return validate_group(value)


class PublishPayload(BaseModel):
    group: str

    @field_validator("group")
    @classmethod
    def _known_group(cls, value: str) -> str:
        return validate_group(value)


class DedupPayload(BaseModel):
    """Nothing to choose: dedup runs over every published group."""


class MapPayload(BaseModel):
    public: bool = False
    seed: int = 42


PAYLOADS: dict[JobKind, type[BaseModel]] = {
    JobKind.COLLECT: CollectPayload,
    JobKind.PUBLISH: PublishPayload,
    JobKind.DEDUP: DedupPayload,
    JobKind.MAP: MapPayload,
}


class Job(BaseModel):
    """One scheduled run, as stored.

    `resource` is derived from the kind and the payload rather than chosen
    by the caller, so two jobs touching the same thing cannot name it
    differently and slip past each other.
    """

    id: str
    kind: JobKind
    payload: dict = Field(default_factory=dict)
    resource: str
    state: JobState = JobState.QUEUED
    actor: str = "unknown"
    worker: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    result: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
    cancel_requested: bool = False

    @property
    def is_final(self) -> bool:
        return self.state in FINAL


def parse_payload(kind: JobKind, payload: dict) -> BaseModel:
    """Check a payload against the model for its kind.

    Raises:
        ValidationError: The payload does not describe this kind of job.
    """
    return PAYLOADS[JobKind(kind)].model_validate(payload or {})


def resource_for(kind: JobKind, payload: BaseModel) -> str:
    """What the job has to hold to run. Collection runs are scoped to their
    group and can go side by side; everything else writes the graph."""
    if JobKind(kind) is JobKind.COLLECT:
        return f"group:{payload.group}"
    return GRAPH
