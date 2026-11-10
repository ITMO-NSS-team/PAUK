"""Shared request admission for threads using the same API host."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

LONG_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class RateLimit:
    requests_per_second: float
    max_concurrent: int | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.requests_per_second) or self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive and finite")
        if self.max_concurrent is not None and (
            isinstance(self.max_concurrent, bool)
            or not isinstance(self.max_concurrent, int)
            or self.max_concurrent < 1
        ):
            raise ValueError("max_concurrent must be a positive integer or None")

    def stricter(self, other: RateLimit) -> RateLimit:
        caps = [cap for cap in (self.max_concurrent, other.max_concurrent) if cap is not None]
        return RateLimit(
            min(self.requests_per_second, other.requests_per_second),
            min(caps) if caps else None,
        )


@dataclass
class _HostState:
    limit: RateLimit
    condition: threading.Condition = field(default_factory=threading.Condition)
    next_at: float = 0.0
    paused_until: float = 0.0
    last_started: float | None = None
    in_flight: int = 0


class HostRateLimiter:
    """Share the strictest host limits and server pauses within one process."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hosts: dict[str, _HostState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _tighten(state: _HostState, limit: RateLimit) -> None:
        """Update ceilings while the caller holds state.condition."""
        combined = state.limit.stricter(limit)
        if combined == state.limit:
            return
        state.limit = combined
        if state.last_started is not None:
            state.next_at = max(state.next_at, state.last_started + 1 / combined.requests_per_second)
        state.condition.notify_all()

    def _state(self, host: str, limit: RateLimit) -> _HostState:
        with self._lock:
            state = self._hosts.get(host)
            if state is None:
                state = self._hosts[host] = _HostState(limit)
        with state.condition:
            self._tighten(state, limit)
        return state

    @contextmanager
    def slot(self, host: str, limit: RateLimit | None) -> Iterator[None]:
        """Hold a host's concurrency slot until the request has finished."""
        if limit is None:
            yield
            return
        state = self._state(host, limit)
        with state.condition:
            while True:
                now = self._clock()
                ready_at = max(state.next_at, state.paused_until)
                capacity = state.limit.max_concurrent
                if (capacity is None or state.in_flight < capacity) and now >= ready_at:
                    state.in_flight += 1
                    state.last_started = now
                    state.next_at = now + 1 / state.limit.requests_per_second
                    break
                delay = ready_at - now
                state.condition.wait(timeout=min(delay, threading.TIMEOUT_MAX) if delay > 0 else None)
        try:
            yield
        finally:
            with state.condition:
                state.in_flight -= 1
                state.condition.notify_all()

    def pause(self, host: str, seconds: float) -> None:
        """Delay future admissions, including callers already waiting."""
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("pause must be nonnegative and finite")
        with self._lock:
            state = self._hosts.get(host)
        if state is not None:
            with state.condition:
                paused_until = self._clock() + seconds
                extended = paused_until > state.paused_until
                state.paused_until = max(state.paused_until, paused_until)
                state.condition.notify_all()
            if extended and seconds >= LONG_WAIT_SECONDS:
                logger.warning("pausing requests to %s for %.0fs", host, seconds)

    def restrict(
        self, host: str, *, requests_per_second: float | None = None, max_concurrent: int | None = None
    ) -> None:
        """Lower host ceilings; None leaves the corresponding ceiling unchanged."""
        with self._lock:
            state = self._hosts.get(host)
        if state is not None:
            with state.condition:
                self._tighten(
                    state,
                    RateLimit(
                        requests_per_second if requests_per_second is not None else state.limit.requests_per_second,
                        max_concurrent if max_concurrent is not None else state.limit.max_concurrent,
                    ),
                )
