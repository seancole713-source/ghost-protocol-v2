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
                    out["report_ts"] = _report_ts(idx)
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
    """
    data = get_earnings_surprise(symbol)
    if not data.get("available"):
        return {"earnings_surprise": 0.0, "earnings_available": False}
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
        }
    except Exception:
        note_suppressed()
        return {"earnings_surprise": 0.0, "earnings_available": False}
