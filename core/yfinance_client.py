"""
core/yfinance_client.py — Centralized yfinance access with circuit breaker gating.

PR #81: Every yfinance call in the codebase must go through this module.
Direct `import yfinance` / `yf.Ticker(...)` outside this module or tests
is banned. The wrapper:

  1. Checks _yfinance_cb.allow() before every call
  2. Records success/failure into the breaker
  3. Returns None or safe defaults on failure/breaker-open
  4. Sanitizes NaN/Inf from all numeric fields

Usage:
    from core.yfinance_client import yf_ticker, yf_info, yf_history, yf_fast_info, yf_news

    info = yf_info("WOLF")          # -> dict or None
    hist = yf_history("WOLF", "1mo")  # -> DataFrame or None
    fi   = yf_fast_info("WOLF")     # -> fast_info object or None
    news = yf_news("WOLF")          # -> list or None
"""
from __future__ import annotations

import logging
import math as _math
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.yfinance_client")


def _gate() -> bool:
    """Check the yfinance circuit breaker. Returns True if call should proceed."""
    from core.circuit_breaker import _yfinance_cb
    return _yfinance_cb.allow()


def _record_success() -> None:
    from core.circuit_breaker import _yfinance_cb
    _yfinance_cb.record_success()


def _record_failure() -> None:
    from core.circuit_breaker import _yfinance_cb
    _yfinance_cb.record_failure()


def _safe_float(val: Any) -> Optional[float]:
    """Convert to float, rejecting NaN/Inf/None."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if _math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> Optional[int]:
    """Convert to int, rejecting None."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def yf_ticker(symbol: str) -> Optional[Any]:
    """Return a breaker-gated yfinance Ticker object, or None if blocked."""
    if not _gate():
        return None
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        _record_success()
        return tk
    except Exception as e:
        _record_failure()
        LOGGER.debug("yfinance_client ticker %s: %s", symbol, str(e)[:80])
        return None


def yf_info(symbol: str) -> Optional[Dict[str, Any]]:
    """Return yfinance .info dict for a symbol, or None if blocked/failed."""
    if not _gate():
        return None
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
        _record_success()
        return info
    except Exception as e:
        _record_failure()
        LOGGER.debug("yfinance_client info %s: %s", symbol, str(e)[:80])
        return None


def yf_history(symbol: str, period: str = "1mo", interval: str = "1d") -> Optional[Any]:
    """Return yfinance .history() DataFrame, or None if blocked/failed."""
    if not _gate():
        return None
    try:
        import yfinance as yf
        h = yf.Ticker(symbol).history(period=period, interval=interval)
        if h is None or getattr(h, "empty", True):
            return None
        _record_success()
        return h
    except Exception as e:
        _record_failure()
        LOGGER.debug("yfinance_client history %s: %s", symbol, str(e)[:80])
        return None


def yf_fast_info(symbol: str) -> Optional[Any]:
    """Return yfinance .fast_info object, or None if blocked/failed."""
    if not _gate():
        return None
    try:
        import yfinance as yf
        fi = yf.Ticker(symbol).fast_info
        _record_success()
        return fi
    except Exception as e:
        _record_failure()
        LOGGER.debug("yfinance_client fast_info %s: %s", symbol, str(e)[:80])
        return None


def yf_news(symbol: str) -> Optional[List[Dict[str, Any]]]:
    """Return yfinance .news list, or None if blocked/failed."""
    if not _gate():
        return None
    try:
        import yfinance as yf
        news = yf.Ticker(symbol).news or []
        _record_success()
        return news
    except Exception as e:
        _record_failure()
        LOGGER.debug("yfinance_client news %s: %s", symbol, str(e)[:80])
        return None


def yf_earnings_history(symbol: str) -> Optional[Any]:
    """Return yfinance .earnings_history DataFrame, or None if blocked/failed."""
    if not _gate():
        return None
    try:
        import yfinance as yf
        eh = yf.Ticker(symbol).earnings_history
        _record_success()
        return eh
    except Exception as e:
        _record_failure()
        LOGGER.debug("yfinance_client earnings_history %s: %s", symbol, str(e)[:80])
        return None
