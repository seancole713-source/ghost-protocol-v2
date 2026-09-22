"""Shared vocabulary for provider adapters and the readiness probe.

Every adapter is written to its vendor's PUBLIC documentation and has not been
exercised against the live API from the build machine (its egress proxy blocks
market-data hosts). The probe is how each one gets verified: it runs inside
production, with production's keys, and reports what actually answered.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Optional

import requests

OK = "OK"                       # answered with usable data
EMPTY = "EMPTY"                 # answered, but with nothing in it
NOT_AUTHORIZED = "NOT_AUTHORIZED"   # 401/403: the key's PLAN does not include this
NO_KEY = "NO_KEY"               # nothing configured to ask with
RATE_LIMITED = "RATE_LIMITED"   # 429: exists, but the plan's budget ran out
ERROR = "ERROR"                 # anything else: 5xx, timeout, bad payload
UNVERIFIED = "UNVERIFIED"       # adapter exists, probe not run yet

# Capability names. A strategy declares which it needs; the probe says which exist.
UNIVERSE = "universe.reference"
DAILY_ALL = "bars.daily.full_market"
SNAPSHOT_ALL = "snapshot.full_market"
MOVERS = "movers.full_market"
MINUTE_BARS = "bars.minute"
QUOTE_SIP = "quotes.realtime.sip"
QUOTE_IEX = "quotes.realtime.iex"
SPLITS = "corporate.splits"
DIVIDENDS = "corporate.dividends"
NEWS = "news.realtime"
FILINGS = "filings.8k"
BORROW = "borrow.fee_and_availability"
SHORT_VOLUME = "short.volume.daily"
SHORT_INTEREST = "short.interest.biweekly"


@dataclass
class Probe:
    capability: str
    provider: str
    status: str
    http_status: Optional[int] = None
    latency_ms: Optional[int] = None
    rows: Optional[int] = None
    newest_ts: Optional[int] = None     # freshest timestamp seen in the sample
    note: str = ""
    checked_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


HttpGet = Callable[..., Any]   # requests.get-compatible


def classify(code: Optional[int]) -> str:
    if code is None:
        return ERROR
    if code in (401, 403):
        return NOT_AUTHORIZED
    if code == 429:
        return RATE_LIMITED
    if 200 <= code < 300:
        return OK
    return ERROR


def timed_get(get: HttpGet, url: str, **kw) -> tuple:
    """(response or None, latency_ms, error_text)."""
    t0 = time.monotonic()
    try:
        r = get(url, timeout=kw.pop("timeout", 12), **kw)
        return r, int((time.monotonic() - t0) * 1000), ""
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        return None, int((time.monotonic() - t0) * 1000), type(exc).__name__


def default_get() -> HttpGet:
    return requests.get
