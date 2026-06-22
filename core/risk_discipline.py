"""Operator risk rules: 1% sizing, portfolio exit alerts, daily loss lock, action labels.

Env (defaults assume $25k account, 1% risk/trade, $250 daily stop):
  GHOST_ACCOUNT_SIZE          — 25000
  GHOST_RISK_PCT_PER_TRADE    — 1.0  (max $ loss if stop hits)
  GHOST_DAILY_LOSS_LIMIT_USD  — 250  (0 = derive from account × 1%)
  GHOST_DAILY_MAX_LOSSES      — 3    (stop new fires after N LOSS resolves today CT)
  GHOST_PORTFOLIO_EXIT_PCT    — 25   (Telegram EXIT if position down this % vs cost)
  GHOST_OPEN_BUFFER_MIN       — 30   (block new fires first N min after 9:30 ET — optional)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.db import db_conn

LOGGER = logging.getLogger("ghost.risk")

_GHOST_SYMBOL_PATTERNS = ("ZZE2E", "STOCK GHOST", "GHOST", "ZZ", "TEST")


def _env_float(name: str, default: float, lo: float = 0.0, hi: float = 1e9) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(lo, min(hi, float(raw)))
    except Exception:
        return default


def _env_int(name: str, default: int, lo: int = 0, hi: int = 1_000_000) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except Exception:
        return default


def risk_settings() -> Dict[str, Any]:
    account = _env_float("GHOST_ACCOUNT_SIZE", 25000.0, 100.0)
    risk_pct = _env_float("GHOST_RISK_PCT_PER_TRADE", 1.0, 0.1, 10.0)
    daily_limit = _env_float("GHOST_DAILY_LOSS_LIMIT_USD", 0.0, 0.0)
    if daily_limit <= 0:
        daily_limit = round(account * risk_pct / 100.0, 2)
    return {
        "account_size_usd": round(account, 2),
        "risk_pct_per_trade": risk_pct,
        "max_loss_per_trade_usd": round(account * risk_pct / 100.0, 2),
        "daily_loss_limit_usd": round(daily_limit, 2),
        "daily_max_losses": _env_int("GHOST_DAILY_MAX_LOSSES", 3, 1, 20),
        "portfolio_exit_pct": _env_float("GHOST_PORTFOLIO_EXIT_PCT", 25.0, 1.0, 99.0),
        "open_buffer_min": _env_int("GHOST_OPEN_BUFFER_MIN", 30, 0, 120),
    }


def _ct_day_start_ts() -> int:
    import pytz

    tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def _ghost_state_get(key: str) -> Optional[str]:
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            cur.execute("SELECT val FROM ghost_state WHERE key=%s", (key,))
            row = cur.fetchone()
            return str(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def _ghost_state_set(key: str, val: str) -> None:
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
        cur.execute(
            "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
            (key, val),
        )


def pick_action_tier(confidence: float, ghost_score: Optional[float] = None) -> str:
    """Official pick action label (entries only — not shorts)."""
    c = float(confidence or 0)
    gs = float(ghost_score) if ghost_score is not None else None
    if c >= 0.90 and (gs is None or gs >= 80):
        return "SUPER BUY"
    if c >= 0.75:
        return "BUY NOW"
    return "BUY"


def bias_label_from_score(score: float) -> str:
    """Informational gauge copy — never implies an official pick."""
    s = float(score or 0)
    if s >= 80:
        return "strong bullish bias"
    if s >= 60:
        return "mild bullish bias"
    if s >= 40:
        return "neutral bias"
    if s >= 20:
        return "mild bearish bias"
    return "strong bearish bias"


def trade_action_from_context(
    *,
    has_official_pick: bool,
    pick_confidence: Optional[float] = None,
    ghost_score: Optional[float] = None,
    gates_blocked: bool = True,
    engine_paused: bool = False,
    daily_locked: bool = False,
) -> Dict[str, str]:
    """Separate composite bias from actionable trade state."""
    if has_official_pick and pick_confidence:
        tier = pick_action_tier(pick_confidence, ghost_score)
        return {
            "trade_action": tier,
            "trade_note": "Official pick — use pre-set stop; size to 1% account risk.",
        }
    if daily_locked:
        return {
            "trade_action": "NO TRADE — DAILY LOCK",
            "trade_note": "Daily loss limit hit — no new fires until tomorrow CT.",
        }
    if engine_paused:
        return {
            "trade_action": "NO TRADE — COOLDOWN",
            "trade_note": "Engine paused after loss streak — wait for auto-resume.",
        }
    if gates_blocked:
        bl = bias_label_from_score(float(ghost_score or 50))
        return {
            "trade_action": "NO TRADE",
            "trade_note": f"Ghost Score is {bl} only — no setup cleared the gates.",
        }
    return {
        "trade_action": "NO TRADE",
        "trade_note": "No high-conviction setup today.",
    }


def position_sizing_plan(
    entry: float,
    stop: float,
    *,
    confidence: Optional[float] = None,
    account_size: Optional[float] = None,
    risk_pct: Optional[float] = None,
    win_rate: Optional[float] = None,
    avg_win_pct: Optional[float] = None,
    avg_loss_pct: Optional[float] = None,
    open_positions: int = 0,
) -> Dict[str, Any]:
    """Position sizing with Kelly criterion integration (Pillar 8).

    When win_rate, avg_win_pct, and avg_loss_pct are provided, computes
    the mathematically optimal Kelly fraction and blends it with the
    fixed-percentage risk model. Also applies portfolio heat scaling
    when multiple positions are open.
    """
    cfg = risk_settings()
    account = float(account_size if account_size is not None else cfg["account_size_usd"])
    rp = float(risk_pct if risk_pct is not None else cfg["risk_pct_per_trade"])
    max_loss = round(account * rp / 100.0, 2)
    entry_f = float(entry or 0)
    stop_f = float(stop or 0)
    if entry_f <= 0 or stop_f <= 0:
        return {
            "ok": False,
            "error": "entry and stop required",
            "account_size_usd": account,
            "risk_pct_per_trade": rp,
            "max_loss_usd": max_loss,
        }
    stop_dist_pct = abs(entry_f - stop_f) / entry_f * 100.0
    if stop_dist_pct <= 0:
        return {"ok": False, "error": "stop distance zero", "max_loss_usd": max_loss}

    # P8 (audit): Kelly criterion — mathematically optimal fraction when stats available
    kelly_frac = None
    kelly_note = None
    if win_rate is not None and avg_win_pct is not None and avg_loss_pct is not None:
        try:
            from core.kelly_sizing import kelly_fraction as _kf
            kelly_frac = _kf(win_rate, avg_win_pct, avg_loss_pct)
            if kelly_frac > 0:
                # Blend: use Kelly fraction as a multiplier on the fixed risk pct
                rp = min(rp, kelly_frac * 100.0)  # Kelly fraction → percentage
                max_loss = round(account * rp / 100.0, 2)
                kelly_note = f"Kelly f*={kelly_frac:.4f} → risk {rp:.2f}%"
            else:
                kelly_note = f"Kelly f*={kelly_frac:.4f} (no edge) — using fixed {rp:.2f}%"
        except Exception:
            pass

    # P8 (audit): portfolio heat scaling — reduce size as more positions open
    heat_mult = 1.0
    if open_positions > 1:
        try:
            from core.kelly_sizing import portfolio_heat_scale as _phs
            heat_mult = _phs(open_positions)
            if heat_mult < 1.0:
                max_loss = round(max_loss * heat_mult, 2)
                rp = round(rp * heat_mult, 2)
        except Exception:
            pass

    notional = max_loss / (stop_dist_pct / 100.0)
    shares = int(notional / entry_f) if entry_f > 0 else 0
    if shares < 1:
        shares = 1
    actual_notional = round(shares * entry_f, 2)
    actual_loss = round(actual_notional * stop_dist_pct / 100.0, 2)
    return {
        "ok": True,
        "account_size_usd": account,
        "risk_pct_per_trade": rp,
        "max_loss_usd": max_loss,
        "stop_distance_pct": round(stop_dist_pct, 3),
        "suggested_shares": shares,
        "suggested_notional_usd": actual_notional,
        "estimated_loss_at_stop_usd": actual_loss,
        "pick_action": pick_action_tier(confidence or 0.75) if confidence else None,
        "kelly_fraction": kelly_frac,
        "kelly_note": kelly_note,
        "portfolio_heat_mult": heat_mult if heat_mult < 1.0 else None,
        "open_positions": open_positions,
    }


def _today_resolved_stats() -> Dict[str, Any]:
    day_start = _ct_day_start_ts()
    trades: List[Dict[str, Any]] = []
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT outcome, pnl_pct FROM predictions "
                "WHERE symbol='WOLF' AND resolved_at >= %s AND outcome IN ('WIN','LOSS') "
                "ORDER BY resolved_at ASC",
                (day_start,),
            )
            for outcome, pnl in cur.fetchall():
                trades.append({"outcome": outcome, "pnl_pct": float(pnl or 0)})
    except Exception as e:
        LOGGER.warning("daily stats failed: %s", str(e)[:80])
    cfg = risk_settings()
    from core.pnl import realized_pnl

    pnl = realized_pnl(trades, bankroll=cfg["account_size_usd"], stake_fraction=1.0)
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    return {
        "date_start_ts": day_start,
        "trades": len(trades),
        "losses": losses,
        "realized_pnl_usd": float(pnl.get("realized_pnl_usd") or 0),
        "wins": int(pnl.get("wins") or 0),
    }


def daily_loss_lock_state() -> Dict[str, Any]:
    """Whether new picks are blocked for the rest of the CT day."""
    cfg = risk_settings()
    stats = _today_resolved_stats()
    import pytz

    tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    today = datetime.now(tz).strftime("%Y-%m-%d")
    pnl = float(stats["realized_pnl_usd"])
    limit = float(cfg["daily_loss_limit_usd"])
    loss_count = int(stats["losses"])
    max_losses = int(cfg["daily_max_losses"])
    breach_pnl = pnl <= -limit if limit > 0 else False
    breach_count = loss_count >= max_losses if max_losses > 0 else False
    should_lock = breach_pnl or breach_count
    reason_parts = []
    if breach_pnl:
        reason_parts.append(f"daily P&L ${pnl:.2f} vs limit -${limit:.2f}")
    if breach_count:
        reason_parts.append(f"{loss_count} losses today (max {max_losses})")
    return {
        "locked": bool(should_lock),
        "should_lock": should_lock,
        "date_ct": today,
        "realized_pnl_usd": round(pnl, 2),
        "daily_loss_limit_usd": limit,
        "losses_today": loss_count,
        "daily_max_losses": max_losses,
        "reason": "; ".join(reason_parts) if reason_parts else "",
    }


def refresh_daily_loss_lock(*, notify: bool = True) -> Dict[str, Any]:
    """Persist lock state; Telegram once when lock newly engages."""
    st = daily_loss_lock_state()
    today = st["date_ct"]
    prev_date = _ghost_state_get("daily_loss_lock_date")
    prev_alerted = _ghost_state_get("daily_loss_lock_alerted") == today
    if st["should_lock"]:
        _ghost_state_set("daily_loss_lock_active", "1")
        _ghost_state_set("daily_loss_lock_date", today)
        st["locked"] = True
        if notify and not prev_alerted and prev_date != today:
            try:
                from core.telegram import send_risk_discipline_alert

                send_risk_discipline_alert(
                    "DAILY LOSS LOCK",
                    "No new Ghost fires until tomorrow (CT).\n"
                    + (st.get("reason") or "Daily risk limit reached."),
                )
                _ghost_state_set("daily_loss_lock_alerted", today)
            except Exception as e:
                LOGGER.warning("daily lock alert failed: %s", str(e)[:80])
    elif _ghost_state_get("daily_loss_lock_date") != today:
        _ghost_state_set("daily_loss_lock_active", "0")
        st["locked"] = False
    return st


def is_daily_loss_locked() -> bool:
    return bool(daily_loss_lock_state().get("should_lock"))


def in_open_buffer_window() -> Tuple[bool, str]:
    """Optional: no new fires in first N minutes after US cash open (9:30 ET)."""
    from core.market_hours import in_open_buffer_window_et

    buf = risk_settings()["open_buffer_min"]
    return in_open_buffer_window_et(buf)


def combined_trading_block() -> Dict[str, Any]:
    """All reasons new picks may be suppressed."""
    from core.prediction import engine_pause_state

    pause = engine_pause_state()
    daily = daily_loss_lock_state()
    buf, buf_reason = in_open_buffer_window()
    blocked = bool(pause.get("paused")) or bool(daily.get("locked") or daily.get("should_lock")) or buf
    reasons = []
    if pause.get("paused"):
        reasons.append("engine cooldown: " + str(pause.get("reason") or "paused"))
    if daily.get("locked") or daily.get("should_lock"):
        reasons.append("daily loss lock: " + str(daily.get("reason") or "limit"))
    if buf:
        reasons.append(buf_reason)
    return {
        "blocked": blocked,
        "reasons": reasons,
        "engine_pause": pause,
        "daily_loss_lock": daily,
        "open_buffer": {"active": buf, "reason": buf_reason},
        "settings": risk_settings(),
    }


def _is_ghost_symbol(sym: str) -> bool:
    up = str(sym or "").strip().upper()
    return any(up.startswith(p) or up == p for p in _GHOST_SYMBOL_PATTERNS)


def _portfolio_positions() -> List[Dict[str, Any]]:
    positions = []
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, symbol, quantity, buy_price, manual_price FROM user_portfolio ORDER BY id DESC"
            )
            rows = cur.fetchall()
        for r in rows:
            sym, qty, bp = str(r[1]).upper(), float(r[2]), float(r[3])
            if _is_ghost_symbol(sym):
                continue
            manual = float(r[4]) if r[4] is not None else None
            live = manual
            if live is None:
                try:
                    from core.prices import get_price

                    live = get_price(sym, "stock")
                except Exception:
                    live = None
            cost = qty * bp
            val = qty * live if live else None
            glp = ((val - cost) / cost * 100.0) if val is not None and cost > 0 else None
            positions.append(
                {
                    "id": r[0],
                    "symbol": sym,
                    "quantity": qty,
                    "buy_price": bp,
                    "live_price": live,
                    "gain_loss_pct": round(glp, 2) if glp is not None else None,
                }
            )
    except Exception as e:
        LOGGER.warning("portfolio read failed: %s", str(e)[:80])
    return positions


def check_portfolio_exit_alerts(*, notify: bool = True) -> List[Dict[str, Any]]:
    """Alert when any portfolio row is down >= portfolio_exit_pct vs cost."""
    cfg = risk_settings()
    threshold = -float(cfg["portfolio_exit_pct"])
    import pytz

    tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    today = datetime.now(tz).strftime("%Y-%m-%d")
    alerted_raw = _ghost_state_get("portfolio_exit_alerted") or "{}"
    try:
        alerted = json.loads(alerted_raw)
    except Exception:
        alerted = {}
    if not isinstance(alerted, dict):
        alerted = {}
    if alerted.get("_date") != today:
        alerted = {"_date": today}
    hits = []
    # Match portfolio API: one row per symbol (largest lot) so Cash App dupes
    # do not spam identical AMC alerts.
    by_symbol: Dict[str, Dict[str, Any]] = {}
    for pos in _portfolio_positions():
        sym = str(pos["symbol"]).upper()
        prev = by_symbol.get(sym)
        if prev is None or float(pos["quantity"]) > float(prev["quantity"]):
            by_symbol[sym] = pos
    for pos in by_symbol.values():
        glp = pos.get("gain_loss_pct")
        if glp is None or glp > threshold:
            continue
        key = str(pos["symbol"]).upper()
        if alerted.get(key):
            continue
        hits.append(pos)
        if notify:
            try:
                from core.telegram import send_risk_discipline_alert

                send_risk_discipline_alert(
                    "PORTFOLIO EXIT — " + pos["symbol"],
                    f"Position down {glp:.1f}% (limit −{cfg['portfolio_exit_pct']:.0f}%).\n"
                    f"Qty {pos['quantity']} @ ${pos['buy_price']:.2f} · "
                    f"mark ${pos.get('live_price') or 0:.2f}\n"
                    "Rule: cut losses — do not hope. Review stop / trim / exit.",
                )
            except Exception as e:
                LOGGER.warning("portfolio exit alert failed: %s", str(e)[:80])
        alerted[key] = today
    if hits:
        _ghost_state_set("portfolio_exit_alerted", json.dumps(alerted))
    return hits


def run_risk_discipline_cycle(*, notify: bool = True) -> Dict[str, Any]:
    """Scheduler hook: refresh daily lock + portfolio exit scans."""
    daily = refresh_daily_loss_lock(notify=notify)
    exits = check_portfolio_exit_alerts(notify=notify)
    return {"daily_loss_lock": daily, "portfolio_exit_alerts": len(exits), "exits": exits}
