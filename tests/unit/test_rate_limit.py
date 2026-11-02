import math
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from pauk.sources.rate_limit import HostRateLimiter, RateLimit


class _Clock:
    def __init__(self):
        self.now = 100.0
        self.extra_sleep = 0.0
        self.before_wait = None

    def __call__(self):
        return self.now

    def wait(self, timeout=None):
        if timeout is None:
            raise AssertionError("a concurrency slot leaked")
        if self.before_wait is not None:
            callback, self.before_wait = self.before_wait, None
            callback()
        self.now += timeout + self.extra_sleep
        self.extra_sleep = 0.0


class RateLimitTest(unittest.TestCase):
    def test_rejects_invalid_limits(self):
        for rate in (0, -1, math.nan, math.inf):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                RateLimit(rate)
        for capacity in (0, -1, 1.5, True):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                RateLimit(10, capacity)

    def test_stricter_combines_rate_and_optional_concurrency_independently(self):
        cases = (
            (RateLimit(10), RateLimit(5), RateLimit(5)),
            (RateLimit(2, 3), RateLimit(5, 1), RateLimit(2, 1)),
            (RateLimit(2), RateLimit(5, 1), RateLimit(2, 1)),
        )
        for first, second, expected in cases:
            with self.subTest(first=first, second=second):
                self.assertEqual(first.stricter(second), expected)
                self.assertEqual(second.stricter(first), expected)


class HostRateLimiterTest(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.limiter = HostRateLimiter(clock=self.clock)
        self.limit = RateLimit(5, 1)
        self.host = "api.test"
        with self.limiter.slot(self.host, self.limit):
            pass
        condition = self.limiter._hosts[self.host].condition
        self.enterContext(mock.patch.object(condition, "wait", side_effect=self.clock.wait))

    def test_spaces_successive_admissions(self):
        times = []
        for _ in range(3):
            with self.limiter.slot(self.host, self.limit):
                times.append(self.clock())
        self.assertAlmostEqual(times[1] - times[0], 0.2)
        self.assertAlmostEqual(times[2] - times[1], 0.2)

    def test_late_wakeup_does_not_allow_a_catchup_burst(self):
        self.clock.extra_sleep = 0.4
        with self.limiter.slot(self.host, self.limit):
            delayed = self.clock()
        with self.limiter.slot(self.host, self.limit):
            following = self.clock()
        self.assertAlmostEqual(following - delayed, 0.2)

    def test_idle_time_does_not_accumulate_admissions(self):
        self.clock.now += 50
        with self.limiter.slot(self.host, self.limit):
            first = self.clock()
        with self.limiter.slot(self.host, self.limit):
            second = self.clock()
        self.assertAlmostEqual(second - first, 0.2)

    def test_other_hosts_and_unlimited_requests_do_not_wait(self):
        self.limiter.pause(self.host, 10)
        before = self.clock()
        with self.limiter.slot("other.test", self.limit), self.limiter.slot(self.host, None):
            self.assertEqual(self.clock(), before)

    def test_shared_pause_applies_to_existing_waiter(self):
        self.clock.before_wait = lambda: self.limiter.pause(self.host, 3)
        before = self.clock()
        with self.limiter.slot(self.host, self.limit):
            self.assertAlmostEqual(self.clock() - before, 3)

    def test_shorter_pause_does_not_undo_longer_one(self):
        self.limiter.pause(self.host, 3)
        self.limiter.pause(self.host, 1)
        before = self.clock()
        with self.limiter.slot(self.host, self.limit):
            self.assertAlmostEqual(self.clock() - before, 3)

    def test_exception_releases_concurrency_slot(self):
        with self.assertRaises(RuntimeError), self.limiter.slot(self.host, self.limit):
            raise RuntimeError("request failed")
        with self.limiter.slot(self.host, self.limit):
            pass

    def test_profiles_share_the_stricter_rate(self):
        with self.limiter.slot(self.host, RateLimit(2, 1)):
            first = self.clock()
        with self.limiter.slot(self.host, RateLimit(20, 8)):
            second = self.clock()
        self.assertAlmostEqual(second - first, 0.5)

    def test_server_limit_only_reduces_the_configured_rate(self):
        self.limiter.restrict(self.host, requests_per_second=2)
        before = self.clock()
        with self.limiter.slot(self.host, self.limit):
            self.assertAlmostEqual(self.clock() - before, 0.5)
            first = self.clock()
        self.limiter.restrict(self.host, requests_per_second=20)
        with self.limiter.slot(self.host, self.limit):
            self.assertAlmostEqual(self.clock() - first, 0.5)

    def test_invalid_pause_is_rejected(self):
        for delay in (-1, math.nan, math.inf):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                self.limiter.pause(self.host, delay)

    def test_wait_is_bounded_by_the_platform_timeout(self):
        self.limiter.pause(self.host, threading.TIMEOUT_MAX * 2)
        waits = []

        def wait(timeout=None):
            waits.append(timeout)
            self.clock.now += timeout

        with (
            mock.patch.object(self.limiter._hosts[self.host].condition, "wait", side_effect=wait),
            self.limiter.slot(self.host, self.limit),
        ):
            pass
        self.assertTrue(all(delay <= threading.TIMEOUT_MAX for delay in waits))
        self.assertGreaterEqual(len(waits), 2)


class HostConcurrencyTest(unittest.TestCase):
    def test_profile_and_server_updates_use_the_host_condition(self):
        clock = _Clock()
        limiter = HostRateLimiter(clock=clock)
        original = RateLimit(10000, 5)
        with limiter.slot("api.test", original):
            pass
        state = limiter._hosts["api.test"]
        started = [threading.Event(), threading.Event()]
        finished = [threading.Event(), threading.Event()]

        def change_profile():
            started[0].set()
            limiter._state("api.test", RateLimit(2, 5))
            finished[0].set()

        def restrict():
            started[1].set()
            limiter.restrict("api.test", max_concurrent=1)
            finished[1].set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            with state.condition:
                futures = [pool.submit(change_profile), pool.submit(restrict)]
                for event in started:
                    self.assertTrue(event.wait(timeout=1))
                for event in finished:
                    self.assertFalse(event.wait(timeout=0.03))
            for future in futures:
                future.result(timeout=1)

        self.assertEqual(state.limit, RateLimit(2, 1))
        with mock.patch.object(state.condition, "wait", side_effect=clock.wait), limiter.slot("api.test", original):
            self.assertAlmostEqual(clock(), 100.5)

    def test_equal_and_weaker_updates_do_not_strand_a_paused_waiter(self):
        limiter = HostRateLimiter()
        limit = RateLimit(10000, 1)
        waiting = threading.Event()

        def request():
            with limiter.slot("api.test", limit):
                return time.monotonic()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with limiter.slot("api.test", limit):
                state = limiter._hosts["api.test"]
                original_wait = state.condition.wait

                def wait(timeout=None):
                    waiting.set()
                    return original_wait(timeout)

                with mock.patch.object(state.condition, "wait", side_effect=wait):
                    future = pool.submit(request)
                    self.assertTrue(waiting.wait(timeout=1))
                    limiter.restrict("api.test", requests_per_second=10000, max_concurrent=1)
                    limiter._state("api.test", RateLimit(20000, 5))
                    paused_at = time.monotonic()
                    limiter.pause("api.test", 0.05)
            self.assertGreaterEqual(future.result(timeout=1) - paused_at, 0.05)

    def test_request_waits_until_capacity_is_released(self):
        limiter = HostRateLimiter()
        limit = RateLimit(10000, 1)
        started = threading.Event()
        sent = threading.Event()

        def request():
            started.set()
            with limiter.slot("api.test", limit):
                sent.set()
                return "sent"

        with ThreadPoolExecutor(max_workers=1) as pool:
            with limiter.slot("api.test", limit):
                future = pool.submit(request)
                self.assertTrue(started.wait(timeout=1))
                self.assertFalse(sent.wait(timeout=0.03))
            self.assertEqual(future.result(timeout=1), "sent")

    def test_lower_server_concurrency_applies_to_later_requests(self):
        limiter = HostRateLimiter()
        original = RateLimit(10000, 3)
        with limiter.slot("api.test", original):
            pass
        limiter.restrict("api.test", max_concurrent=1)
        limiter.restrict("api.test", requests_per_second=5000)
        limiter.restrict("api.test", max_concurrent=3)
        sent = threading.Event()

        def request():
            with limiter.slot("api.test", original):
                sent.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with limiter.slot("api.test", original):
                future = pool.submit(request)
                self.assertFalse(sent.wait(timeout=0.03))
            future.result(timeout=1)
        self.assertTrue(sent.is_set())

    def test_saturated_host_does_not_block_another_host(self):
        limiter = HostRateLimiter()
        limit = RateLimit(10000, 1)
        with limiter.slot("first.test", limit), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: self._request(limiter, "other.test", limit))
            self.assertEqual(future.result(timeout=1), "sent")

    @staticmethod
    def _request(limiter, host, limit):
        with limiter.slot(host, limit):
            return "sent"
