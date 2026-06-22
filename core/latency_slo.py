"""
core/latency_slo.py — Request latency SLO tracking middleware.

Tracks p50, p95, p99 latency per route over a rolling 5-minute window.
Exposed via /api/diagnostics and /api/cockpit/context for ops visibility.

P3-5 (audit): latency SLO tracking for observability score.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.latency")

_LOCK = threading.Lock()
# route_path → deque of (timestamp, latency_ms) tuples
_WINDOW: Dict[str, collections.deque] = {}
_WINDOW_SEC = 300  # 5-minute rolling window
_MAX_SAMPLES = 10000  # per-route cap


def record(path: str, latency_ms: float) -> None:
    """Record a request latency sample. Called from middleware."""
    now = time.time()
    with _LOCK:
        if path not in _WINDOW:
            _WINDOW[path] = collections.deque()
        dq = _WINDOW[path]
        dq.append((now, latency_ms))
        # Evict expired
        cutoff = now - _WINDOW_SEC
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        # Cap size
        while len(dq) > _MAX_SAMPLES:
            dq.popleft()


def _percentile(sorted_vals: List[float], pct: float) -> Optional[float]:
    if not sorted_vals:
        return None
    idx = int(len(sorted_vals) * pct / 100.0)
    idx = max(0, min(len(sorted_vals) - 1, idx))
    return sorted_vals[idx]


def route_stats(path: str) -> Dict[str, Any]:
    """p50/p95/p99 + sample count for one route."""
    with _LOCK:
        dq = _WINDOW.get(path)
        if not dq:
            return {"samples": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}
        vals = sorted(v[1] for v in dq)
    return {
        "samples": len(vals),
        "p50_ms": round(_percentile(vals, 50), 1) if vals else None,
        "p95_ms": round(_percentile(vals, 95), 1) if vals else None,
        "p99_ms": round(_percentile(vals, 99), 1) if vals else None,
    }


def all_stats() -> Dict[str, Any]:
    """Aggregate stats for all tracked routes + overall summary."""
    with _LOCK:
        paths = list(_WINDOW.keys())
    per_route = {p: route_stats(p) for p in paths}
    # Overall: pool all samples
    all_vals = []
    with _LOCK:
        for dq in _WINDOW.values():
            all_vals.extend(v[1] for v in dq)
    all_vals.sort()
    return {
        "routes": per_route,
        "overall": {
            "samples": len(all_vals),
            "p50_ms": round(_percentile(all_vals, 50), 1) if all_vals else None,
            "p95_ms": round(_percentile(all_vals, 95), 1) if all_vals else None,
            "p99_ms": round(_percentile(all_vals, 99), 1) if all_vals else None,
        },
        "window_sec": _WINDOW_SEC,
    }


def slowest_routes(limit: int = 5) -> List[Dict[str, Any]]:
    """Top-N slowest routes by p95 latency."""
    stats = [(p, route_stats(p)) for p in _WINDOW.keys()]
    stats.sort(key=lambda x: x[1].get("p95_ms") or 0, reverse=True)
    return [
        {"path": p, **s} for p, s in stats[:limit] if s.get("p95_ms") is not None
    ]
