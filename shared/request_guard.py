"""shared/request_guard.py -- client identity + failed-credential lockout.

Stdlib only; the request object is duck-typed (``.headers.get`` and
``.client.host``) so this works with Starlette/FastAPI requests and test fakes.

Client identity (audit U26)
    The web process runs uvicorn with ``--proxy-headers
    --forwarded-allow-ips="*"``; with ``"*"`` uvicorn copies the LEFTMOST
    X-Forwarded-For entry into ``request.client.host``. A client controls every
    entry it sends, and the edge proxy only APPENDS the address it saw, so the
    leftmost entry is attacker-chosen: a spoofed header gave every request a
    fresh rate-limit / login-throttle bucket. :func:`client_ip` instead reads the
    entry written by the last trusted proxy, counted from the RIGHT
    (``GHOST_TRUSTED_PROXY_HOPS``, default 1 = Railway's edge).

Failed-credential lockout (audit U18)
    The admin/cron routes are exempt from the per-IP rate limiter, and the
    OAuth connector secret and the static MCP token were never throttled, so
    each could be guessed online at full request rate. :data:`CREDENTIAL_LOCKOUT`
    counts WRONG presented secrets per client; after ``AUTH_FAILURE_LIMIT``
    (default 10) inside ``AUTH_FAILURE_WINDOW_S`` (default 900 s) every
    credentialed request from that client is refused (429) until the oldest
    failure ages out -- including a correct guess, so the lockout cannot be used
    as an oracle. State is process-local (one web replica); it resets on deploy.
"""
from __future__ import annotations

import collections
import logging
import os
import threading
import time
from typing import Any, Deque, Dict, Optional

LOGGER = logging.getLogger("ghost.request_guard")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def trusted_proxy_hops() -> int:
    """Number of trusted proxies that append to X-Forwarded-For (0 = ignore XFF)."""
    return _env_int("GHOST_TRUSTED_PROXY_HOPS", 1, 0, 10)


def client_ip(request: Any) -> str:
    """Best-effort client address that a client cannot choose for itself.

    With N trusted hops the client address is the N-th entry from the right of
    X-Forwarded-For (each trusted proxy appended exactly one entry). Without the
    header (local dev, tests, direct connections) the socket peer is used.
    """
    hops = trusted_proxy_hops()
    headers = getattr(request, "headers", None)
    if hops and headers is not None:
        try:
            raw = headers.get("x-forwarded-for") or ""
        except Exception:
            raw = ""
        parts = [p.strip() for p in str(raw).split(",") if p.strip()]
        if parts:
            return parts[max(0, len(parts) - hops)]
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return str(host) if host else "unknown"


class FailureLockout:
    """Sliding-window counter of failed credentials per client key."""

    _MAX_KEYS = 8192

    def __init__(self, *, limit_env: str, window_env: str,
                 default_limit: int, default_window_s: int) -> None:
        self._limit_env = limit_env
        self._window_env = window_env
        self._default_limit = default_limit
        self._default_window_s = default_window_s
        self._lock = threading.Lock()
        self._fails: Dict[str, Deque[float]] = collections.defaultdict(collections.deque)

    def _cfg(self) -> tuple[int, int]:
        limit = _env_int(self._limit_env, self._default_limit, 1, 10_000)
        window = _env_int(self._window_env, self._default_window_s, 10, 86_400)
        return limit, window

    def _prune(self, dq: Deque[float], now: float, window: int) -> None:
        cutoff = now - window
        while dq and dq[0] <= cutoff:
            dq.popleft()

    def _evict(self, now: float, window: int, limit: int) -> None:
        """Enforce the key cap. Caller holds the lock.

        SEC-03 (audit): only EMPTY buckets used to be dropped, but a bucket is
        only pruned when its own key fails again, so stale one-failure buckets
        stayed non-empty forever and the map grew past the cap. Expire old
        entries across every key first; if still over the cap, evict the
        least-recently-failed keys, unlocked keys before locked ones.
        """
        for k in list(self._fails.keys()):
            dq = self._fails[k]
            self._prune(dq, now, window)
            if not dq:
                del self._fails[k]
        excess = len(self._fails) - self._MAX_KEYS
        if excess <= 0:
            return
        victims = sorted(self._fails.items(), key=lambda kv: (len(kv[1]) >= limit, kv[1][-1]))
        for k, _dq in victims[:excess]:
            del self._fails[k]

    def retry_after(self, key: str, now: Optional[float] = None) -> int:
        """Seconds until ``key`` may present a credential again (0 = allowed)."""
        limit, window = self._cfg()
        now = time.time() if now is None else now
        with self._lock:
            dq = self._fails.get(key)
            if not dq:
                return 0
            self._prune(dq, now, window)
            if len(dq) < limit:
                return 0
            # Unlocks when enough failures age out to drop below the limit.
            return max(1, int(dq[len(dq) - limit] + window - now) + 1)

    def record_failure(self, key: str, *, kind: str = "",
                       now: Optional[float] = None) -> bool:
        """Count one wrong credential. Returns True when ``key`` is now locked."""
        limit, window = self._cfg()
        now = time.time() if now is None else now
        with self._lock:
            dq = self._fails[key]
            self._prune(dq, now, window)
            dq.append(now)
            locked = len(dq) >= limit
            if len(self._fails) > self._MAX_KEYS:
                self._evict(now, window, limit)
        if locked and len(dq) == limit:
            # Log the transition once; the key and kind are not secrets.
            LOGGER.warning(
                "Credential lockout: %s failed %s attempts in %ss (last kind=%s)",
                key, limit, window, kind or "unknown",
            )
        return locked

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()


CREDENTIAL_LOCKOUT = FailureLockout(
    limit_env="AUTH_FAILURE_LIMIT",
    window_env="AUTH_FAILURE_WINDOW_S",
    default_limit=10,
    default_window_s=900,
)
