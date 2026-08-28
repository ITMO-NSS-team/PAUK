"""What a job is, in types.

Kept beside `pauk.models.processing`, which describes the state of one
prepared *row*. This describes the state of a whole run: who asked for it,
what it is allowed to touch, and how it ended.

The payload is a model per kind rather than a free dict. The panel builds
it from a form, and a form is the one place where a value can be anything
at all — the same reason `pauk.graph.mutations` keeps closed whitelists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from pauk.storage.naming import validate_group


class JobKind(StrEnum):
    """What the worker should do. One entry, one callable — no free text."""

    COLLECT = "collect"
    PUBLISH = "publish"
    DEDUP = "dedup"
    MAP = "map"


class JobState(StrEnum):
    """Where a job is.

    CLAIMED sits between QUEUED and RUNNING on purpose: a worker takes the
    document first and the resource lock second, and between the two the
    job belongs to nobody visible. Without a state of its own it would read
    as running while nothing is running.
    """

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: States a job will never leave.
FINAL = frozenset({JobState.DONE, JobState.FAILED, JobState.CANCELLED})

#: The one resource every write to Neo4j contends for. Publishing,
#: deduplicating and exporting a snapshot all read or rewrite the whole
#: graph, so they take turns rather than interleave.
GRAPH = "graph"


def now() -> datetime:
    """Current time at the precision BSON keeps.

    Mongo stores milliseconds; a plain datetime.now() carries microseconds
    and comes back rounded, so a document read back would differ from the
    one that was written. Same rule as `pauk.graph.overrides._now`.
    """
    moment = datetime.now(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def aware(moment: datetime) -> datetime:
    """A stored time with a timezone on it.

    pymongo hands datetimes back naive, in UTC; comparing one against an
    aware now() raises TypeError instead of answering.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class CollectPayload(BaseModel):
    """One collection run: a single work, or everything in a date range.

    `--works-file` is deliberately absent. It names a path on the machine
    running the pipeline, and a path that arrives from a browser form is a
    way to read files that have nothing to do with this project.
    """

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
    """Rebuild of the map's static files.

    `public` drops personal fields, the way `generate_data --public` does
    for a build that leaves the corporate network.
    """

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

    `resource` is what the job has to hold before it starts. It is derived
    from the kind and the payload rather than chosen by the caller, so two
    jobs that touch the same thing cannot be given different names for it
    and slip past each other.
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
    """What this job has to hold to run.

    Collection runs are scoped to their group and can go on side by side.
    Everything else writes the graph, and those take turns.
    """
    if JobKind(kind) is JobKind.COLLECT:
        return f"group:{payload.group}"
    return GRAPH
