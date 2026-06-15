"""Intraday squeeze drift — alert buy vs live quote (panel refresh)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.squeeze_live_drift")


def first_alert_buy_map(alerts: List[Dict[str, Any]]) -> Dict[str, float]:
    """First Telegram alert buy per symbol this session (chronological)."""
    ordered = sorted(alerts, key=lambda a: int(a.get("alerted_at") or 0))
    out: Dict[str, float] = {}
    for a in ordered:
        sym = (a.get("symbol") or "").upper()
        buy = a.get("buy")
        if not sym or buy is None or sym in out:
            continue
        try:
            out[sym] = float(buy)
        except (TypeError, ValueError):
            continue
    return out


def live_price_map(
    picks: List[Dict[str, Any]],
    leaders: List[Dict[str, Any]],
) -> Dict[str, float]:
    """Best-effort last-scan price by symbol."""
    out: Dict[str, float] = {}
    for src in (leaders or []) + (picks or []):
        sym = (src.get("symbol") or "").upper()
        px = src.get("price")
        if px is None:
            px = src.get("buy")
        if not sym or px is None:
            continue
        try:
            out[sym] = float(px)
        except (TypeError, ValueError):
            continue
    return out


def compute_live_drift(
    alert_buy: float,
    live_price: float,
) -> Optional[Dict[str, Any]]:
    """Gap from frozen alert buy to current quote."""
    if alert_buy <= 0 or live_price <= 0:
        return None
    gap = (live_price - alert_buy) / alert_buy * 100.0
    if gap >= 0.5:
        status, label = "above", "above alert"
    elif gap <= -2.0:
        status, label = "fading", "below alert — fading"
    elif gap < 0:
        status, label = "below", "below alert"
    else:
        status, label = "at", "at alert"
    return {
        "alert_buy": round(alert_buy, 4),
        "live_price": round(live_price, 4),
        "gap_pct": round(gap, 2),
        "gap_label": label,
        "drift_status": status,
    }


def attach_live_drift(
    row: Dict[str, Any],
    *,
    alert_buy: Optional[float],
    live_price: Optional[float],
) -> None:
    """Mutate row with live drift fields when both prices exist."""
    if alert_buy is None or live_price is None:
        return
    drift = compute_live_drift(float(alert_buy), float(live_price))
    if not drift:
        return
    row.update(drift)


def enrich_pick_rows(
    picks: List[Dict[str, Any]],
    alert_history: List[Dict[str, Any]],
    leaders: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Add live_price + gap vs first Telegram alert buy to pick/leader rows."""
    alert_map = first_alert_buy_map(alert_history)
    live_map = live_price_map(picks, leaders)
    out: List[Dict[str, Any]] = []
    for row in picks or []:
        item = dict(row)
        sym = (item.get("symbol") or "").upper()
        live = live_map.get(sym) or item.get("price") or item.get("buy")
        if live is not None:
            try:
                item["live_price"] = round(float(live), 4)
            except (TypeError, ValueError):
                pass
        alert_buy = alert_map.get(sym)
        if alert_buy is not None and item.get("live_price") is not None:
            attach_live_drift(item, alert_buy=alert_buy, live_price=item["live_price"])
        out.append(item)
    return out


def build_live_drift_board(
    alert_history: List[Dict[str, Any]],
    picks: List[Dict[str, Any]],
    leaders: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """One row per alerted symbol: alert buy vs live now (for cockpit summary table)."""
    alert_map = first_alert_buy_map(alert_history)
    live_map = live_price_map(picks, leaders)
    kind_map: Dict[str, str] = {}
    for a in alert_history:
        sym = (a.get("symbol") or "").upper()
        if sym and sym not in kind_map:
            kind_map[sym] = str(a.get("kind") or "")

    rows: List[Dict[str, Any]] = []
    for sym, alert_buy in alert_map.items():
        live = live_map.get(sym)
        if live is None:
            continue
        drift = compute_live_drift(alert_buy, live)
        if not drift:
            continue
        rows.append({"symbol": sym, "kind": kind_map.get(sym), **drift})
    rows.sort(key=lambda r: r.get("gap_pct") or 0)
    return rows


def fetch_live_prices(symbols: List[str]) -> Dict[str, float]:
    """On-demand quotes for daily-log pending rows (API refresh path)."""
    out: Dict[str, float] = {}
    for sym in symbols:
        s = (sym or "").upper().strip()
        if not s or s in out:
            continue
        try:
            from core.prices import get_intraday_session

            sess = get_intraday_session(s)
            px = sess.get("price") if sess else None
            if px and float(px) > 0:
                out[s] = round(float(px), 4)
        except Exception as exc:
            LOGGER.debug("live quote %s: %s", s, str(exc)[:80])
    return out


def enrich_daily_log_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach live drift to unresolved rows (telegram buys only for summary clarity)."""
    pending_telegram = [
        r for r in rows if not r.get("outcome") and r.get("source") == "telegram"
    ]
    syms = list({(r.get("symbol") or "").upper() for r in pending_telegram if r.get("symbol")})
    live_map = fetch_live_prices(syms[:40])
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("outcome"):
            out.append(item)
            continue
        sym = (item.get("symbol") or "").upper()
        buy = item.get("buy")
        live = live_map.get(sym)
        if buy is not None and live is not None:
            attach_live_drift(item, alert_buy=float(buy), live_price=live)
        out.append(item)
    return out
