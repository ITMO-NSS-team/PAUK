"""The process that performs what the panel schedules.

Separate from the panel because a collection run takes hours and a restart
of the web service must not cut one in half. The two share nothing but the
`jobs` collection.

The worker takes no locks of its own. Every function it calls holds what it
touches, which keeps one rule instead of two and covers somebody running
`pauk publish graph` in a terminal as well. A job whose resource turns out
to be busy goes back to the queue rather than failing.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel
from pymongo.database import Database
from pymongo.errors import PyMongoError

from pauk.jobs import locks, store
from pauk.jobs.models import Job, JobKind, parse_payload
from pauk.settings import Settings

logger = logging.getLogger(__name__)

# How long to wait before asking for work again on an empty queue, and how
# often a running job says it is alive. The beat is well inside the lock's
# lease (see locks.LEASE_MINUTES), so a live run never loses its own lock.
POLL_SECONDS = 5.0
BEAT_SECONDS = 60.0


#: Returns True when somebody pressed cancel while the run was under way.
#: Asked between the phases of a pipeline, and by `Report` at every step.
Stop = Callable[[], bool]

#: Told which part of the run is under way, by name and by how many of how
#: many are behind it. Passed alongside `stop` because both are hooks the
#: worker holds and the work itself knows nothing about.
#:
#: May raise `Cancelled`. The work calls it between the parts it is made of,
#: which is exactly where stopping is safe, so the worker answers a cancel
#: from inside it rather than waiting for the whole part to finish.
Report = Callable[..., None]


def _collect(config: Settings, db: Database, payload, stop: Stop,
             report: Report) -> dict[str, int]:
    from pauk.pipeline.runner import PipelineRunner
    from pauk.pipeline.selectors import PeriodSelector, WorkSelector

    selector = (WorkSelector(payload.work_id) if payload.work_id
                else PeriodSelector(payload.date_from, payload.date_to))
    return PipelineRunner(config, payload.group, db).run(selector, on_stage=report)


def _publish(config: Settings, db: Database, payload, stop: Stop,
             report: Report) -> dict[str, int]:
    from pauk.graph.load import load_jsonl_group
    report("выкладка в граф")

    def rows(step: str, done: int, total: int) -> None:
        # The load counts rows, and the page reads the counts as stages
        # ("stage 3 of 10"), so the rows go into the label — the same as a
        # stage's own (see pauk.pipeline.enrich._inside).
        report(f"{step} {done}/{total}" if total else step)

    # The reporter goes in, so the load says how far it has got and can be
    # stopped between two chunks. Given up halfway it leaves the group
    # loaded in part; the next publish finishes it, because every write in
    # there is a MERGE.
    return load_jsonl_group(config, db, payload.group, report=rows)


def _dedup(config: Settings, db: Database, payload, stop: Stop,
           report: Report) -> dict[str, int]:
    from pauk.graph.dedup import run_graph_dedup
    report("склейка дублей по всему графу")
    return run_graph_dedup(config, db)


def _prune(config: Settings, db: Database, payload, stop: Stop,
           report: Report) -> dict[str, int]:
    from pauk.graph import prune
    from pauk.graph.audit import actor_context, audited_client

    report("сверка графа с источником")
    client = audited_client(config, db)
    try:
        # The lock lives in `prune.run`, the way it lives in a publish:
        # a plan and its application have to be one turn, and a run from a
        # terminal takes the same turn as this one.
        with actor_context("etl-pipeline", source="prune"):
            return prune.run(client, db, payload.apply, report=report).counts()
    finally:
        client.close()


def _health(config: Settings, db: Database, payload, stop: Stop,
            report: Report) -> dict[str, int]:
    from pauk.admin import health
    from pauk.graph.audit import audited_client
    from pauk.gui.generate_stats import collect

    report("проверки по графу")
    # Reads only, so no lock is taken. The job still contends for the graph
    # (`resource_for`), which keeps it from measuring a graph that a publish
    # is halfway through rewriting.
    client = audited_client(config, db)
    try:
        stats = collect(client.driver)
    finally:
        client.close()
    health.save(db, stats)
    counted = health.verdict(stats["checks"])
    return {"checks": len(stats["checks"]),
            "checks_failed": counted["fail"], "checks_warned": counted["warn"],
            "checks_broken": counted["error"]}


def _rebuild_map(config: Settings, db: Database, payload, stop: Stop,
                 report: Report) -> dict[str, int]:
    from pauk.gui.rebuild import rebuild_map
    report("пересборка карты")
    return rebuild_map(config, db, public=payload.public, seed=payload.seed)


#: The three phases a pipeline run is made of, in order. Named here because
#: the page draws one segment per phase and has to know how many there are.
PHASES = ("сбор", "публикация", "карта")


class Cancelled(Exception):
    """A job that was asked to stop, and did, at the next step it reported."""


def _pipeline(config: Settings, db: Database, payload, stop: Stop,
              report: Report) -> dict[str, int]:
    """Collect, publish, rebuild the map. One job, three phases.

    Not three queued jobs: publishing names a group, and when the queue is
    filled that group has no rows for the check to accept. As one job the
    order is also guaranteed — nothing else slips in between the collection
    and its publish.

    Each phase takes and releases its own lock, so nothing is held across
    the whole run: a collection can take hours, and holding the graph for
    all of it would stop every other run from touching it.

    Raises:
        Cancelled: Somebody pressed cancel. Checked between the phases and,
            through `report`, at every step inside them — never in the
            middle of one.
    """
    def during(phase: int) -> Report:
        """The worker's reporter, with the phase this run is in attached.

        The parts inside a phase report their own step and know nothing
        about the phase they sit in, so it is added here rather than
        threaded through every one of them.
        """
        def inner(step: str, done: int = 0, total: int = 0) -> None:
            report(step, done, total, phase=phase)
        return inner

    counts = _collect(config, db, payload, stop, during(0))
    if stop():
        raise Cancelled("остановлено после сбора")
    counts |= _publish(config, db, payload, stop, during(1))
    if stop():
        raise Cancelled("остановлено после публикации")
    return counts | _rebuild_map(config, db, payload, stop, during(2))


#: What each kind of job does. A closed table looked up by an enum, so no
#: job can name a callable of its own.
STEPS: dict[JobKind, Callable[[Settings, Database, BaseModel, Stop, Report], dict[str, int]]] = {
    JobKind.COLLECT: _collect,
    JobKind.PUBLISH: _publish,
    JobKind.DEDUP: _dedup,
    JobKind.MAP: _rebuild_map,
    JobKind.PRUNE: _prune,
    JobKind.HEALTH: _health,
    JobKind.PIPELINE: _pipeline,
}


class _Beat:
    """Says a running job is alive while it is busy doing something else.

    The work is one synchronous call that does not come back for hours, so
    nothing renews the lease from inside it. A daemon thread renews the
    heartbeat and the resource lock, and stops when the call returns.

    The lock is renewed as the process, not as the worker's name. The two
    are the same string until somebody passes `--name`, and then they are
    not: the lock was taken by `locks.this_process()` inside the step, and
    a renewal under any other name matches nothing and says nothing. The
    lease would then run out under a long publish and let a second writer
    in, which is the one thing the lock exists to prevent.
    """

    def __init__(self, db: Database, job: Job, owner: str | None = None) -> None:
        self._db, self._job = db, job
        self._owner = owner or locks.this_process()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"beat-{job.id}")

    def _loop(self) -> None:
        while not self._stop.wait(BEAT_SECONDS):
            try:
                store.heartbeat(self._db, self._job.id)
                locks.renew(self._db, self._job.resource, self._owner)
            except PyMongoError as error:
                # One blip must not end the beating. An unhandled error
                # here killed the thread outright, and after that the run
                # kept going in silence: the lease it holds on the graph
                # would expire under it and let a second writer in, which
                # is the one thing the lock exists to prevent.
                logger.warning("job %s could not report in: %s", self._job.id, error)

    def __enter__(self) -> _Beat:
        self._thread.start()
        return self

    def __exit__(self, *_exception) -> None:
        self._stop.set()
        self._thread.join(timeout=BEAT_SECONDS)


@dataclass
class Worker:
    """One process taking jobs off the queue, one at a time.

    Args:
        config: Settings, passed on to whatever the job runs.
        db: Mongo database holding the queue.
        name: How this worker is recorded on the jobs it takes.
        poll_seconds: Wait between empty polls.
    """

    config: Settings
    db: Database
    name: str = ""
    poll_seconds: float = POLL_SECONDS

    def __post_init__(self) -> None:
        self.name = self.name or locks.this_process()
        self._stopping = threading.Event()

    def stop(self) -> None:
        """Finish the job in hand, then leave the loop."""
        self._stopping.set()

    def run_forever(self) -> None:
        """Take jobs until asked to stop.

        SIGINT and SIGTERM ask rather than interrupt, because a publish
        cut in half leaves the graph written and the decision unrecorded.

        Handlers are installed only in the process's own main thread.
        Python refuses to set them elsewhere, and a worker running inside
        somebody else's program has no business taking their SIGINT.
        """
        if threading.current_thread() is threading.main_thread():
            for received in (signal.SIGINT, signal.SIGTERM):
                signal.signal(received, lambda *_: self.stop())
        logger.info("worker %s started", self.name)
        while not self._stopping.is_set():
            if not self.run_once():
                self._stopping.wait(self.poll_seconds)
        logger.info("worker %s stopped", self.name)

    def run_once(self) -> bool:
        """Take one job if there is one.

        Returns:
            True when work was done. False when there was nothing to do, or
            when the job went back because its resource was busy. Either
            way the caller waits before asking again.
        """
        # Jobs left behind by a worker that is gone. Nothing else ever moves
        # them out of "under way": the only process that could was the one
        # that died holding them.
        store.reap_stale(self.db)
        # Jobs waiting on something somebody else holds are passed over,
        # not taken and handed straight back.
        job = store.claim(self.db, self.name, busy=locks.taken(self.db))
        if job is None:
            return False
        if job.cancel_requested:
            # Asked to stop between being queued and being picked up.
            store.cancelled(self.db, job.id)
            logger.info("job %s cancelled before it started", job.id)
            return True
        return self._perform(job)

    def _perform(self, job: Job) -> bool:
        if not store.start(self.db, job.id):
            # Somebody settled it while it was being claimed.
            logger.info("job %s was already settled", job.id)
            return True
        payload = parse_payload(job.kind, job.payload)

        def stop() -> bool:
            current = store.read(self.db, job.id)
            return bool(current and current.cancel_requested)

        def report(step: str, done: int = 0, total: int = 0,
                   phase: int | None = None) -> None:
            store.progress(self.db, job.id, step, done, total, phase)
            # Between two parts of the work nothing is half written, so this
            # is where a cancel can be honoured. Before, the only such seam
            # was between the three phases of a pipeline, and a run stopped
            # during collection kept going through ten more stages — hours
            # after somebody pressed the button.
            if stop():
                raise Cancelled(f"остановлено перед шагом «{step}»")

        try:
            with _Beat(self.db, job):
                result = STEPS[job.kind](self.config, self.db, payload, stop, report)
        except locks.Busy as error:
            # Not a failure. It goes back for whoever gets there next, and
            # this worker waits instead of picking it up again at once.
            logger.info("job %s waits: %s", job.id, error)
            store.requeue(self.db, job.id)
            return False
        except Cancelled as reason:
            logger.info("job %s stopped: %s", job.id, reason)
            store.cancelled(self.db, job.id)
            return True
        except Exception as error:  # the worker outlives one bad job
            logger.exception("job %s failed", job.id)
            store.fail(self.db, job.id, f"{type(error).__name__}: {error}")
            return True
        store.finish(self.db, job.id, result)
        logger.info("job %s done: %s", job.id,
                    ", ".join(f"{k}={v}" for k, v in sorted(result.items())) or "nothing to do")
        return True
