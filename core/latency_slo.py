"""
core/latency_slo.py — Request latency SLO tracking middleware.

Tracks p50, p95, p99 latency per route over a rolling 5-minute window.
Exposed via /api/diagnostics and /api/cockpit/context for ops visibility.

P3-5 (audit): latency SLO tracking for observability score.

SEC-01 / OPS-01 (audit): samples are keyed by the matched route TEMPLATE
(``/mcp/{path_token}``), never the raw request path — a raw path can carry a
credential (the MCP path token) and is attacker-chosen, so it would both leak
through the latency endpoint and grow the key set without bound. The key set
is also hard-capped, and every read prunes samples older than the window (by
monotonic age), so a stale sample is never reported as current.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any, Dict, List, Mapping, Optional

LOGGER = logging.getLogger("ghost.latency")

_LOCK = threading.Lock()
# route template → deque of (monotonic_ts, latency_ms) tuples
_WINDOW: Dict[str, collections.deque] = {}
_WINDOW_SEC = 300  # 5-minute rolling window
_MAX_SAMPLES = 10000  # per-route cap
_MAX_ROUTES = 256  # key cap: route templates are code-defined, so this is ample

# Label for requests that matched no route (404 probes, etc.): one bucket, so
# arbitrary attacker paths can neither be echoed back nor grow the key set.
UNMATCHED_ROUTE = "<unmatched>"


def route_label(scope: Mapping[str, Any]) -> str:
    """Bounded, credential-free label for an ASGI request scope.

    Uses the template of the route the router matched (FastAPI stores it as
    ``scope["route"]``); a Starlette ``Mount`` (e.g. ``/static``) is labelled
    by its mount prefix. Anything else collapses to :data:`UNMATCHED_ROUTE`.
    The raw request path is never returned.
    """
    try:
        route = scope.get("route")
        tpl = getattr(route, "path_format", None) or getattr(route, "path", None)
        if isinstance(tpl, str) and tpl.startswith("/"):
            return tpl
        root_path = str(scope.get("root_path") or "")
        app_root = str(scope.get("app_root_path") or "")
        if root_path and root_path != app_root and root_path.startswith(app_root):
            prefix = root_path[len(app_root):]
            if prefix.startswith("/"):
                return prefix + "/{path:path}"
    except Exception:
        pass
    return UNMATCHED_ROUTE


def _prune(dq: collections.deque, now: float) -> None:
    cutoff = now - _WINDOW_SEC
    while dq and dq[0][0] < cutoff:
        dq.popleft()


def _prune_all(now: float) -> None:
    """Drop expired samples on every route and forget empty routes. Caller holds _LOCK."""
    for key in list(_WINDOW.keys()):
        dq = _WINDOW[key]
        _prune(dq, now)
        if not dq:
            del _WINDOW[key]


def record(path: str, latency_ms: float, now: Optional[float] = None) -> None:
    """Record a request latency sample. Called from middleware with a route label."""
    now = time.monotonic() if now is None else now
    with _LOCK:
        dq = _WINDOW.get(path)
        if dq is None:
            if len(_WINDOW) >= _MAX_ROUTES:
                _prune_all(now)
            while len(_WINDOW) >= _MAX_ROUTES:
                # Hard cap: evict the route whose newest sample is oldest.
                stale = min(_WINDOW, key=lambda k: _WINDOW[k][-1][0] if _WINDOW[k] else float("-inf"))
                del _WINDOW[stale]
            dq = _WINDOW[path] = collections.deque()
        dq.append((now, latency_ms))
        _prune(dq, now)
        # Cap size
        while len(dq) > _MAX_SAMPLES:
            dq.popleft()


def _percentile(sorted_vals: List[float], pct: float) -> Optional[float]:
    if not sorted_vals:
        return None
    idx = int(len(sorted_vals) * pct / 100.0)
    idx = max(0, min(len(sorted_vals) - 1, idx))
    return sorted_vals[idx]


def _summary(vals: List[float]) -> Dict[str, Any]:
    return {
        "samples": len(vals),
        "p50_ms": round(_percentile(vals, 50), 1) if vals else None,
        "p95_ms": round(_percentile(vals, 95), 1) if vals else None,
        "p99_ms": round(_percentile(vals, 99), 1) if vals else None,
    }


def _snapshot(now: Optional[float] = None) -> Dict[str, List[float]]:
    """Sorted in-window latencies per route (prunes expired samples first)."""
    now = time.monotonic() if now is None else now
    with _LOCK:
        _prune_all(now)
        return {p: sorted(v[1] for v in dq) for p, dq in _WINDOW.items()}


def route_stats(path: str, now: Optional[float] = None) -> Dict[str, Any]:
    """p50/p95/p99 + sample count for one route."""
    return _summary(_snapshot(now).get(path) or [])


def all_stats(now: Optional[float] = None) -> Dict[str, Any]:
    """Aggregate stats for all tracked routes + overall summary."""
    snap = _snapshot(now)
    all_vals = sorted(v for vals in snap.values() for v in vals)
    return {
        "routes": {p: _summary(vals) for p, vals in snap.items()},
        "overall": _summary(all_vals),
        "window_sec": _WINDOW_SEC,
    }


def slowest_routes(limit: int = 5, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Top-N slowest routes by p95 latency."""
    stats = [(p, _summary(vals)) for p, vals in _snapshot(now).items()]
    stats.sort(key=lambda x: x[1].get("p95_ms") or 0, reverse=True)
    return [
        {"path": p, **s} for p, s in stats[:limit] if s.get("p95_ms") is not None
    ]


def reset_for_tests() -> None:
    with _LOCK:
        _WINDOW.clear()
