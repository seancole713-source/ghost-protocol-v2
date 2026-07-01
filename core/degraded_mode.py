"""
core/degraded_mode.py — System-wide degraded-mode flag.

When >2 external APIs are circuit-broken, the system enters DEGRADED mode:
  - Cockpit shows a banner
  - /api/health reports degraded status
  - Prediction confidence floor is raised by DEGRADED_CONF_BUMP
  - Squeeze radar interval is doubled

Recovers automatically when APIs come back online (circuit breakers close).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, Any, Optional

LOGGER = logging.getLogger("ghost.degraded")

# Thresholds — env-tunable
_DEGRADED_API_THRESHOLD = max(1, int(os.getenv("DEGRADED_API_THRESHOLD", "2")))
_DEGRADED_CONF_BUMP = float(os.getenv("DEGRADED_CONF_BUMP", "0.05"))
_DEGRADED_CHECK_INTERVAL_S = float(os.getenv("DEGRADED_CHECK_INTERVAL_S", "30"))

_last_check_ts: float = 0.0
_degraded: bool = False
_degraded_since: Optional[float] = None
_degraded_reasons: list = []


def _count_open_circuits() -> int:
    """Count how many circuit breakers are currently open or half-open.
    PR #77: half_open also counts as impaired — a breaker in half_open
    is still effectively unavailable (probes may succeed, but the feed is
    degraded). Previously only "open" was counted, missing the window
    between initial trip and cooldown expiry."""
    try:
        from core.circuit_breaker import all_breaker_status
        statuses = all_breaker_status()
        return sum(1 for s in statuses.values() if s.get("state") in ("open", "half_open"))
    except Exception:
        return 0


def check_degraded() -> Dict[str, Any]:
    """Evaluate degraded mode. Cached for DEGRADED_CHECK_INTERVAL_S seconds."""
    global _last_check_ts, _degraded, _degraded_since, _degraded_reasons
    now = time.time()
    if (now - _last_check_ts) < _DEGRADED_CHECK_INTERVAL_S:
        return _degraded_status()

    _last_check_ts = now
    open_count = _count_open_circuits()
    was_degraded = _degraded

    if open_count > _DEGRADED_API_THRESHOLD:
        if not _degraded:
            _degraded = True
            _degraded_since = now
            LOGGER.warning(
                "DEGRADED MODE ENTERED: %s/%s APIs circuit-broken",
                open_count, 5,
            )
        # Collect reasons — include half_open since _count_open_circuits counts both
        try:
            from core.circuit_breaker import all_breaker_status
            _degraded_reasons = [
                f"{name}: {s.get('state')}" for name, s in all_breaker_status().items()
                if s.get("state") in ("open", "half_open")
            ]
        except Exception:
            _degraded_reasons = [f"{open_count} APIs circuit-broken"]
    else:
        if _degraded:
            LOGGER.info(
                "DEGRADED MODE CLEARED: %s/%s APIs circuit-broken (was degraded for %.0fs)",
                open_count, 5, now - (_degraded_since or now),
            )
        _degraded = False
        _degraded_since = None
        _degraded_reasons = []

    if was_degraded != _degraded:
        LOGGER.info("Degraded mode transition: %s → %s", was_degraded, _degraded)

    return _degraded_status()


def _degraded_status() -> Dict[str, Any]:
    return {
        "degraded": _degraded,
        "since": _degraded_since,
        "reasons": list(_degraded_reasons),
        "open_circuits": _count_open_circuits(),
        "threshold": _DEGRADED_API_THRESHOLD,
        "conf_bump": _DEGRADED_CONF_BUMP if _degraded else 0.0,
    }


def is_degraded() -> bool:
    """Fast check — returns cached state, does not re-evaluate."""
    return _degraded


def degraded_conf_bump() -> float:
    """Extra confidence required when degraded. 0.0 when healthy."""
    return _DEGRADED_CONF_BUMP if _degraded else 0.0


def degraded_squeeze_interval_mult() -> float:
    """Multiplier for squeeze scan interval when degraded. 1.0 when healthy."""
    return 2.0 if _degraded else 1.0
