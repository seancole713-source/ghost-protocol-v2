"""Production access-log filters for peaceful operator logs.

Uvicorn logs every successful request by default. That is useful for debugging,
but stale console tabs can keep calling deprecated selected-symbol evidence
endpoints after a deploy and drown Railway logs in harmless 200 OK lines. This
filter suppresses ONLY successful GET access logs for those noisy evidence reads.
Errors, writes, health, version, active picks, squeeze, and the new bundled
snapshot endpoint still log normally.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple


_NOISY_SUCCESS_GET_PREFIXES = (
    "/api/wolf/super-ghost?",                 # old full report selected-symbol read
    "/api/wolf/super-ghost/history",
    "/api/wolf/super-ghost/accuracy",
    "/api/wolf/super-ghost/if-followed",
    "/api/wolf/super-ghost/top-pick-gate",
    "/api/wolf/super-ghost/learning",
    "/api/wolf/super-ghost/precision",
    "/api/wolf/super-ghost/range-calibration",
    "/api/wolf/super-ghost/regime-calibration",
    "/api/wolf/super-ghost/lab",
    "/api/wolf/super-ghost/feature-profile",
    "/api/wolf/super-ghost/shadow",
    "/api/wolf/super-ghost/promotion",
    "/api/wolf/super-ghost/feature-store/audit",
    "/api/wolf/super-ghost/data-brain",
    "/api/ghost/doctrine/",
)


def _extract_access(record: logging.LogRecord) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Best-effort extraction from Uvicorn access LogRecord.

    Uvicorn's access logger typically emits args as:
      (client_addr, method, full_path, http_version, status_code)
    but we keep this defensive so a logging package change fails open (logs).
    """
    args: Any = getattr(record, "args", None)
    method = path = None
    status = None
    if isinstance(args, tuple) and len(args) >= 5:
        method = str(args[1] or "").upper()
        path = str(args[2] or "")
        try:
            status = int(args[4])
        except Exception:
            status = None
    return method, path, status


class PeacefulAccessFilter(logging.Filter):
    """Suppress harmless stale-console evidence reads, keep important logs.

    Returns False only for successful GETs to old noisy evidence endpoints.
    That means 4xx/5xx, POSTs, health/version, picks/squeeze, and the new
    /super-ghost/snapshot endpoint remain visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - called by logging
        method, path, status = _extract_access(record)
        if method != "GET" or status is None or status >= 400:
            return True
        return not any(path.startswith(prefix) for prefix in _NOISY_SUCCESS_GET_PREFIXES)
