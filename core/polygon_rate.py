"""Process-wide request budget for core Polygon calls.

Production logged ~20 ``Polygon <SYM>: HTTP 429 ... exceeded the maximum
requests per minute`` lines every hour: the hourly core scan reaches the
Polygon daily-bar fallback for many symbols at once (when Alpaca is itself
rate-limited) and the free tier allows 5 requests a minute. Every 429 was a
wasted round trip before the next fallback.

Two guards, shared by every core Polygon caller in the process:
  * a token bucket of ``POLYGON_MAX_RPM`` requests per minute (default 5, the
    free tier; 0 disables the bucket). ``try_acquire()`` never blocks: no
    token means "skip Polygon now, use the next fallback".
  * after an HTTP 429, Polygon is skipped until the start of the next
    wall-clock minute (``note_rate_limited()``), whatever the bucket says.

The edge/ package has its own paced Polygon client and is not gated here.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

LOGGER = logging.getLogger("ghost.polygon_rate")

DEFAULT_MAX_RPM = 5


def max_rpm() -> int:
    try:
        return max(0, int(os.getenv("POLYGON_MAX_RPM", str(DEFAULT_MAX_RPM))))
    except ValueError:
        return DEFAULT_MAX_RPM


class PolygonBudget:
    """Thread-safe token bucket + 429 cooldown. ``clock`` is injectable for tests."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: Optional[float] = None
        self._last: Optional[float] = None
        self._cooldown_until = 0.0
        self.skipped = 0
        self.rate_limited = 0

    def reset(self) -> None:
        with self._lock:
            self._tokens = None
            self._last = None
            self._cooldown_until = 0.0
            self.skipped = 0
            self.rate_limited = 0

    def cooling_down(self, now: Optional[float] = None) -> bool:
        now = self._clock() if now is None else now
        with self._lock:
            return now < self._cooldown_until

    def try_acquire(self) -> bool:
        """Take one request token. False = skip Polygon for now (no wait)."""
        now = self._clock()
        rpm = max_rpm()
        with self._lock:
            if now < self._cooldown_until:
                self.skipped += 1
                return False
            if rpm <= 0:
                return True
            if self._tokens is None or self._last is None:
                self._tokens, self._last = float(rpm), now
            self._tokens = min(float(rpm), self._tokens + (now - self._last) * rpm / 60.0)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            self.skipped += 1
            return False

    def note_rate_limited(self) -> bool:
        """Record an HTTP 429: skip Polygon for the rest of this minute.

        Returns True when this 429 opened a new cooldown (callers log once per
        cooldown instead of once per symbol)."""
        now = self._clock()
        until = (int(now // 60) + 1) * 60.0
        with self._lock:
            self.rate_limited += 1
            fresh = now >= self._cooldown_until
            self._cooldown_until = max(self._cooldown_until, until)
            self._tokens = 0.0
            self._last = now
        return fresh


BUDGET = PolygonBudget()


def try_acquire() -> bool:
    return BUDGET.try_acquire()


def note_rate_limited(source: str = "") -> None:
    if BUDGET.note_rate_limited():
        LOGGER.info(
            "Polygon HTTP 429%s: skipping Polygon until the next minute (POLYGON_MAX_RPM=%s)",
            f" ({source})" if source else "", max_rpm(),
        )
