"""WOLF options flow probe (Phase 2) — best-effort from yfinance chain."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

LOGGER = logging.getLogger("ghost.options")


def probe_options_flow(symbol: str = "WOLF") -> Dict[str, Any]:
    sym = (symbol or "WOLF").upper()
    out: Dict[str, Any] = {
        "ok": False,
        "symbol": sym,
        "available": False,
        "put_call_volume_ratio": None,
        "total_call_volume": None,
        "total_put_volume": None,
        "note": "Best-effort; thin chains may be empty.",
    }
    try:
        import yfinance as yf

        t = yf.Ticker(sym)
        expirations = getattr(t, "options", None)
        if not expirations:
            out["note"] = "No listed options expirations"
            out["ok"] = True
            return out
        near = expirations[0]
        chain = t.option_chain(near)
        calls = chain.calls
        puts = chain.puts
        cv = float(calls["volume"].fillna(0).sum()) if calls is not None and not calls.empty else 0.0
        pv = float(puts["volume"].fillna(0).sum()) if puts is not None and not puts.empty else 0.0
        pcr = round(pv / cv, 3) if cv > 0 else None
        out.update({
            "ok": True,
            "available": cv > 0 or pv > 0,
            "nearest_expiry": near,
            "put_call_volume_ratio": pcr,
            "total_call_volume": int(cv),
            "total_put_volume": int(pv),
            "skew_hint": "elevated_puts" if pcr and pcr > 1.2 else (
                "elevated_calls" if pcr and pcr < 0.7 else "balanced"
            ),
        })
    except Exception as exc:
        LOGGER.debug("options probe %s: %s", sym, exc)
        out["error"] = str(exc)[:160]
        out["ok"] = True
    return out
