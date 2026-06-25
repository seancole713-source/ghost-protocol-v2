"""
core/circuit_breaker.py — Generic sliding-window circuit breaker for external APIs.

Prevents cascading waste when an external API is persistently failing.
Each breaker tracks failures in a sliding window; after threshold consecutive
failures the circuit opens (requests skip immediately). After cooldown, the
circuit goes half-open (allows a probe request). If the probe succeeds, the
circuit closes; if it fails, the cooldown resets.

Usage:
    cb = CircuitBreaker("yfinance", failure_threshold=5, cooldown_seconds=600)
    if not cb.allow():
        return None  # circuit open, skip
    try:
        result = call_api()
        cb.record_success()
        return result
    except Exception:
        cb.record_failure()
        return None
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

LOGGER = logging.getLogger("ghost.circuit_breaker")


@dataclass
class CircuitBreaker:
    """Sliding-window circuit breaker for an external API."""

    name: str
    failure_threshold: int = 5       # consecutive failures to open circuit
    cooldown_seconds: int = 300      # how long circuit stays open
    half_open_max: int = 2           # max probe requests in half-open state
    rate_limit_window_s: int = 60    # window for call-rate tracking
    rate_limit_max_calls: int = 20   # max calls in window before auto-open

    _failure_count: int = field(default=0, init=False)
    _circuit_open_until: float = field(default=0.0, init=False)
    _half_open_probes: int = field(default=0, init=False)
    _last_failure_ts: float = field(default=0.0, init=False)
    _total_failures: int = field(default=0, init=False)
    _total_successes: int = field(default=0, init=False)
    _call_timestamps: list = field(default_factory=list, init=False)  # rate-limit tracking

    @property
    def state(self) -> str:
        """Current breaker state: closed, open, or half_open."""
        now = time.time()
        if self._circuit_open_until and now < self._circuit_open_until:
            if self._half_open_probes < self.half_open_max:
                return "half_open"
            return "open"
        return "closed"

    def allow(self) -> bool:
        """Return True if the request should proceed. False = circuit open, skip."""
        now = time.time()
        if self._circuit_open_until and now < self._circuit_open_until:
            if self._half_open_probes < self.half_open_max:
                self._half_open_probes += 1
                LOGGER.debug("CB %s: half-open probe %s/%s", self.name,
                             self._half_open_probes, self.half_open_max)
                return True
            return False
        # Circuit closed or cooldown expired — reset
        if self._circuit_open_until and now >= self._circuit_open_until:
            self._circuit_open_until = 0.0
            self._failure_count = 0
            self._half_open_probes = 0
        # Rate-limit tracking: if we're making too many calls, auto-open
        if self.rate_limit_max_calls > 0:
            cutoff = now - self.rate_limit_window_s
            self._call_timestamps = [t for t in self._call_timestamps if t > cutoff]
            if len(self._call_timestamps) >= self.rate_limit_max_calls:
                self._circuit_open_until = now + self.cooldown_seconds
                self._half_open_probes = 0
                LOGGER.warning(
                    "CB %s: %s calls in %ss — rate-limit circuit OPEN for %ss",
                    self.name, len(self._call_timestamps), self.rate_limit_window_s,
                    self.cooldown_seconds,
                )
                return False
        self._call_timestamps.append(now)
        return True

    def record_success(self) -> None:
        """Call after a successful API response."""
        self._total_successes += 1
        if self.state == "half_open":
            LOGGER.info("CB %s: half-open probe SUCCESS — circuit CLOSED", self.name)
            self._circuit_open_until = 0.0
            self._failure_count = 0
            self._half_open_probes = 0
        else:
            self._failure_count = 0

    def record_failure(self) -> None:
        """Call after a failed API call."""
        now = time.time()
        self._total_failures += 1
        self._last_failure_ts = now
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            was_already_open = bool(self._circuit_open_until and now < self._circuit_open_until)
            self._circuit_open_until = now + self.cooldown_seconds
            # Only reset half-open probes on initial trip. If the circuit was
            # already open, probes are exhausted — don't grant more free probes.
            if not was_already_open:
                self._half_open_probes = 0
            LOGGER.warning(
                "CB %s: %s consecutive failures — circuit OPEN for %ss",
                self.name, self._failure_count, self.cooldown_seconds,
            )

    def status(self) -> dict:
        """Read-only status for diagnostics."""
        now = time.time()
        cutoff = now - self.rate_limit_window_s
        recent_calls = sum(1 for t in self._call_timestamps if t > cutoff)
        return {
            "name": self.name,
            "state": self.state,
            "failure_count": self._failure_count,
            "threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "circuit_open_until": self._circuit_open_until or None,
            "total_successes": self._total_successes,
            "total_failures": self._total_failures,
            "last_failure_ts": self._last_failure_ts or None,
            "recent_calls": recent_calls,
            "rate_limit_max_calls": self.rate_limit_max_calls,
        }

    def reset(self) -> None:
        """Force-close the circuit breaker. Use for manual recovery."""
        self._circuit_open_until = 0.0
        self._failure_count = 0
        self._half_open_probes = 0
        self._call_timestamps = []
        LOGGER.info("CB %s: manually RESET — circuit CLOSED", self.name)


# Pre-instantiated breakers for Ghost's external APIs.
# Created at module load so all call sites share the same breaker state.

_yfinance_cb = CircuitBreaker(
    name="yfinance",
    failure_threshold=int(__import__("os").getenv("CB_YFINANCE_THRESHOLD", "5")),
    cooldown_seconds=int(__import__("os").getenv("CB_YFINANCE_COOLDOWN_S", "600")),
    rate_limit_window_s=int(__import__("os").getenv("CB_YFINANCE_RATE_WINDOW_S", "60")),
    rate_limit_max_calls=int(__import__("os").getenv("CB_YFINANCE_RATE_MAX_CALLS", "15")),
)

_finnhub_cb = CircuitBreaker(
    name="finnhub",
    failure_threshold=int(__import__("os").getenv("CB_FINNHUB_THRESHOLD", "5")),
    cooldown_seconds=int(__import__("os").getenv("CB_FINNHUB_COOLDOWN_S", "300")),
)

_polygon_cb = CircuitBreaker(
    name="polygon",
    failure_threshold=int(__import__("os").getenv("CB_POLYGON_THRESHOLD", "5")),
    cooldown_seconds=int(__import__("os").getenv("CB_POLYGON_COOLDOWN_S", "300")),
)

_alpaca_cb = CircuitBreaker(
    name="alpaca",
    failure_threshold=int(__import__("os").getenv("CB_ALPACA_THRESHOLD", "5")),
    cooldown_seconds=int(__import__("os").getenv("CB_ALPACA_COOLDOWN_S", "300")),
    rate_limit_window_s=int(__import__("os").getenv("CB_ALPACA_RATE_WINDOW_S", "60")),
    rate_limit_max_calls=int(__import__("os").getenv("CB_ALPACA_RATE_MAX_CALLS", "30")),
)

_anthropic_cb = CircuitBreaker(
    name="anthropic",
    failure_threshold=int(__import__("os").getenv("CB_ANTHROPIC_THRESHOLD", "3")),
    cooldown_seconds=int(__import__("os").getenv("CB_ANTHROPIC_COOLDOWN_S", "600")),
)


def all_breaker_status() -> dict:
    """Status of all circuit breakers for /api/diagnostics."""
    return {
        b.name: b.status()
        for b in (_yfinance_cb, _finnhub_cb, _polygon_cb, _alpaca_cb, _anthropic_cb)
    }


def reset_all_breakers() -> dict:
    """Force-close all circuit breakers. Admin recovery tool."""
    for b in (_yfinance_cb, _finnhub_cb, _polygon_cb, _alpaca_cb, _anthropic_cb):
        b.reset()
    return {"ok": True, "message": "All circuit breakers reset to closed"}
