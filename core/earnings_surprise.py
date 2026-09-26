"""core/earnings_surprise.py — per-symbol earnings surprise (actual vs expected).

Feeds core/catalyst_freshness.score_earnings_surprise with real data so the
Squeeze Hunter can distinguish a genuine earnings surprise from a stale or
already-priced catalyst.

Best-effort, free data only (yfinance earnings_history + income statement).
Failures degrade to unavailable rather than raising. Read-only intelligence —
never fires a pick or loosens any gate.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.quiet import note_suppressed

LOGGER = logging.getLogger("ghost.earnings_surprise")

_CACHE_TTL_S = int(__import__("os").getenv("EARNINGS_SURPRISE_CACHE_TTL_S", "3600"))
_cache: Dict[str, tuple] = {}

# yfinance's earnings_history is indexed by the FISCAL QUARTER END, not by the
# day the quarter was reported (audit U19). Treating the quarter end as the
# publication time let a result count as "known" weeks before it was public.
# The real report day comes from the earnings calendar when it answers; when it
# does not, the latest a US filer may publish -- 90 days after the quarter (the
# slowest 10-K deadline) -- is used, which can only make a result later, never
# earlier, than it really was.
_MAX_REPORT_LAG_S = 90 * 86400
REPORT_TS_BASIS_CALENDAR = "earnings_calendar"
REPORT_TS_BASIS_BOUND = "quarter_end_plus_max_filing_lag"

# The Hunter reads a 1-14 day window. A surprise older than this is an
# already-priced catalyst, not a trigger (audit U19: there was no recency gate,
# so a three-month-old beat kept feeding the trigger score all quarter).
TRIGGER_MAX_REPORT_AGE_S = int(
    __import__("os").getenv("EARNINGS_SURPRISE_MAX_AGE_S", str(14 * 86400))
)


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        out = float(v)
        return out if out == out and out not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _report_ts(idx: Any) -> Optional[int]:
    """Best-effort epoch seconds for an earnings_history index label.

    yfinance indexes earnings_history by the report date (a pandas Timestamp
    in modern versions, occasionally a plain date-ish string). Returns None
    rather than guessing when the label can't be parsed -- an unparseable
    date must not silently pass as "known now".
    """
    try:
        as_ts = getattr(idx, "timestamp", None)
        if callable(as_ts):
            value = as_ts()
            if isinstance(value, (int, float)) and math.isfinite(value):
                return int(value)
    except Exception:
        pass
    try:
        return int(
            datetime.fromisoformat(str(idx)[:10]).replace(tzinfo=timezone.utc).timestamp()
        )
    except Exception:
        return None


def _calendar_report_ts(tk: Any, quarter_end_ts: Optional[int], *,
                        now_ts: Optional[int] = None) -> Optional[int]:
    """Day the quarter ending ``quarter_end_ts`` was reported, from the
    earnings calendar: the first dated row after the quarter end that carries a
    reported EPS. None when the calendar is unavailable or has no such row."""
    if quarter_end_ts is None:
        return None
    now = int(time.time() if now_ts is None else now_ts)
    try:
        cal = tk.get_earnings_dates(limit=12)
    except Exception as exc:
        LOGGER.debug("earnings calendar: %s", str(exc)[:80])
        return None
    if cal is None or getattr(cal, "empty", True):
        return None
    best: Optional[int] = None
    try:
        for idx, row in cal.iterrows():
            ts = _report_ts(idx)
            if ts is None or ts <= quarter_end_ts or ts > now:
                continue
            if _f(row.get("Reported EPS")) is None:
                continue
            if best is None or ts < best:
                best = ts
    except Exception as exc:
        LOGGER.debug("earnings calendar rows: %s", str(exc)[:80])
        return None
    return best


def _latest_quarter_earnings(symbol: str) -> Dict[str, Any]:
    """Latest quarter EPS estimate/actual + revenue from yfinance."""
    out: Dict[str, Any] = {
        "available": False,
        "eps_actual": None,
        "eps_expected": None,
        "eps_surprise_pct": None,
        "revenue_actual": None,
        "revenue_expected": None,
        "quarter": None,
    }
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol.upper())
        # EPS history: epsEstimate, epsActual, surprisePercent.
        try:
            eh = tk.earnings_history
            if eh is not None and not eh.empty:
                # Most recent quarter is the last row.
                last = eh.iloc[-1]
                est = _f(last.get("epsEstimate"))
                act = _f(last.get("epsActual"))
                if est is not None or act is not None:
                    out["eps_expected"] = est
                    out["eps_actual"] = act
                    out["available"] = True
                    idx = eh.index[-1]
                    out["quarter"] = str(idx)
                    quarter_end_ts = _report_ts(idx)
                    out["quarter_end_ts"] = quarter_end_ts
                    report_ts = _calendar_report_ts(tk, quarter_end_ts)
                    if report_ts is not None:
                        out["report_ts"] = report_ts
                        out["report_ts_basis"] = REPORT_TS_BASIS_CALENDAR
                    elif quarter_end_ts is not None:
                        out["report_ts"] = quarter_end_ts + _MAX_REPORT_LAG_S
                        out["report_ts_basis"] = REPORT_TS_BASIS_BOUND
                    else:
                        out["report_ts"] = None
                        out["report_ts_basis"] = None
                    if est is not None and act is not None and est != 0:
                        out["eps_surprise_pct"] = round((act - est) / abs(est) * 100.0, 2)
        except Exception as exc:
            LOGGER.debug("earnings_history %s: %s", symbol, str(exc)[:80])

        # Revenue from income statement (quarterly).
        try:
            inc = tk.quarterly_income_stmt if hasattr(tk, "quarterly_income_stmt") else None
            if inc is None or (hasattr(inc, "empty") and inc.empty):
                inc = tk.quarterly_financials if hasattr(tk, "quarterly_financials") else None
            if inc is not None and hasattr(inc, "empty") and not inc.empty:
                for label in ("Total Revenue", "TotalRevenue", "Revenue"):
                    if label in inc.index:
                        row = inc.loc[label]
                        if len(row) > 0:
                            out["revenue_actual"] = _f(row.iloc[0])
                            break
        except Exception as exc:
            LOGGER.debug("income_stmt %s: %s", symbol, str(exc)[:80])
    except Exception as exc:
        LOGGER.debug("earnings surprise %s: %s", symbol, str(exc)[:80])
    return out


def get_earnings_surprise(symbol: str, *, asof_ts: Optional[int] = None) -> Dict[str, Any]:
    """Cached per-symbol earnings surprise (actual vs expected).

    yfinance only ever answers "what's the most recent quarter right now" --
    there is no historical as-of mode to query. ``asof_ts`` does not change
    what gets fetched; it is a post-fetch honesty gate: if the report yfinance
    currently has on file was published after ``asof_ts``, that fact was not
    yet knowable at that decision time and this returns unavailable rather
    than laundering a present-day read as something known in the past.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"available": False}
    hit = _cache.get(sym)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        out = dict(hit[1])
    else:
        out = _latest_quarter_earnings(sym)
        _cache[sym] = (time.time(), out)
    if asof_ts is not None and out.get("available"):
        report_ts = out.get("report_ts")
        if report_ts is None or report_ts > int(asof_ts):
            return {**out, "available": False, "reason": "report_ts_after_or_unknown_asof"}
    return out


def earnings_surprise_to_trigger(symbol: str) -> Dict[str, Any]:
    """Map a symbol's earnings surprise to a 0-100 trigger score.

    Uses core.catalyst_freshness.score_earnings_surprise so the relative
    surprise (not absolute sign) drives the score.

    Recency gate (audit U19): only a report dated by the earnings calendar and
    at most TRIGGER_MAX_REPORT_AGE_S old is a trigger. A stale report, or one
    whose publication day is not known, is unavailable -- never a neutral or
    positive score.

    Revenue surprise stays unavailable: no free source here gives a revenue
    CONSENSUS, and revenue_actual alone cannot form a surprise.
    """
    data = get_earnings_surprise(symbol)
    if not data.get("available"):
        return {"earnings_surprise": 0.0, "earnings_available": False}
    report_ts = data.get("report_ts")
    if data.get("report_ts_basis") != REPORT_TS_BASIS_CALENDAR or report_ts is None:
        return {"earnings_surprise": 0.0, "earnings_available": False,
                "reason": "report_date_unknown", "quarter": data.get("quarter")}
    age_s = int(time.time()) - int(report_ts)
    if age_s < 0 or age_s > TRIGGER_MAX_REPORT_AGE_S:
        return {"earnings_surprise": 0.0, "earnings_available": False,
                "reason": "report_not_recent", "report_age_days": round(age_s / 86400, 1),
                "quarter": data.get("quarter")}
    try:
        from core.catalyst_freshness import score_earnings_surprise
        scored = score_earnings_surprise(
            eps_actual=data.get("eps_actual"),
            eps_expected=data.get("eps_expected"),
            revenue_actual=data.get("revenue_actual"),
        )
        return {
            "earnings_surprise": scored.get("score", 0.0),
            "earnings_available": True,
            "eps_surprise_pct": scored.get("eps_surprise_pct"),
            "revenue_surprise_pct": scored.get("revenue_surprise_pct"),
            "quarter": data.get("quarter"),
            "report_ts": report_ts,
            "report_age_days": round(age_s / 86400, 1),
        }
    except Exception:
        note_suppressed()
        return {"earnings_surprise": 0.0, "earnings_available": False}
