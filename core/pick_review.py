"""Re-evaluate open picks each scan — withdraw or supersede when the model changes mind."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

LOGGER = logging.getLogger("ghost.pick_review")

_TRANSIENT_SKIPS = frozenset({
    "no_price", "intraday_data", "no_v3_model", "v3_engine_error", "excluded",
})


def open_pick_review_enabled() -> bool:
    return os.getenv("GHOST_OPEN_PICK_REVIEW", "1").strip().lower() in ("1", "true", "yes", "on")


def withdraw_notify_enabled() -> bool:
    return os.getenv("GHOST_WITHDRAW_NOTIFY", "1").strip().lower() in ("1", "true", "yes", "on")


def _withdraw_min_age_s() -> int:
    try:
        return max(0, int(os.getenv("GHOST_WITHDRAW_MIN_AGE_MIN", "15"))) * 60
    except Exception:
        return 15 * 60


def _supersede_entry_pct() -> float:
    try:
        return max(0.1, float(os.getenv("GHOST_SUPERSEDE_ENTRY_PCT", "1.0")))
    except Exception:
        return 1.0


def _supersede_conf_delta() -> float:
    try:
        return max(0.005, float(os.getenv("GHOST_SUPERSEDE_CONF_DELTA", "0.03")))
    except Exception:
        return 0.03


def _withdraw_skip_codes() -> Set[str]:
    raw = os.getenv(
        "GHOST_WITHDRAW_SKIPS",
        "v3_regime_gate,v3_prob_low,v3_meta_gate,below_confidence_floor,sell_blocked,v3_no_signal",
    )
    return {s.strip() for s in (raw or "").split(",") if s.strip()}


def _eval_by_symbol(symbol_evals: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for ev in symbol_evals:
        sym = (ev.get("symbol") or "").strip().upper()
        if sym:
            out[sym] = ev
    return out


def _pick_for_symbol(all_picks: Sequence[Dict[str, Any]], symbol: str) -> Optional[Dict[str, Any]]:
    sym = symbol.strip().upper()
    for p in all_picks:
        if (p.get("symbol") or "").strip().upper() == sym:
            return p
    return None


def _should_supersede(
    open_conf: float,
    open_entry: float,
    new_pick: Dict[str, Any],
) -> Tuple[bool, str]:
    """True when a fresh candidate materially differs from the open pick."""
    new_conf = float(new_pick.get("confidence") or 0)
    new_entry = float(new_pick.get("entry_price") or 0)
    if open_entry <= 0 or new_entry <= 0:
        return False, ""
    entry_chg_pct = abs(new_entry - open_entry) / open_entry * 100.0
    conf_delta = new_conf - open_conf
    conf_thr = _supersede_conf_delta()
    if conf_delta <= -conf_thr:
        return True, "confidence_dropped"
    if entry_chg_pct >= _supersede_entry_pct():
        return True, "levels_updated"
    if conf_delta >= conf_thr:
        return True, "confidence_improved"
    return False, ""


def _withdraw_reason(
    ev: Dict[str, Any],
    all_picks: Sequence[Dict[str, Any]],
    open_row: Tuple[Any, ...],
) -> Optional[str]:
    """Return withdraw reason code, or None to keep the open pick."""
    _pid, symbol, direction, confidence, entry, target, stop, predicted_at = open_row
    sym = (symbol or "").strip().upper()
    new_pick = _pick_for_symbol(all_picks, sym)
    fired = bool(ev.get("fired"))

    if fired and new_pick:
        ok, reason = _should_supersede(float(confidence or 0), float(entry or 0), new_pick)
        return reason if ok else None

    if fired:
        return None

    skip = (ev.get("skip_code") or "").strip()
    if skip in _TRANSIENT_SKIPS:
        return None
    if skip in _withdraw_skip_codes() or skip.startswith("objective"):
        return skip or "model_withdrawn"

    up = ev.get("up_prob")
    min_p = ev.get("min_win_proba")
    if up is not None and min_p is not None and float(up) < float(min_p):
        return "model_no_longer_bullish"
    return None


def _apply_withdraw(
    cur,
    pred_id: int,
    symbol: str,
    direction: str,
    entry: float,
    target: float,
    stop: float,
    reason: str,
    now_ts: int,
) -> Optional[Dict[str, Any]]:
    from core.pnl import resolution_exit
    from core.prices import get_price

    try:
        from core.performance_log import record_pick_resolution
    except Exception:
        record_pick_resolution = None  # type: ignore

    market = get_price(symbol, "stock")
    exit_price, pnl_pct = resolution_exit(
        "WITHDRAWN",
        direction or "UP",
        float(entry),
        float(target),
        float(stop),
        float(market) if market else float(entry),
    )
    cur.execute(
        "UPDATE predictions SET outcome=%s, exit_price=%s, pnl_pct=%s, resolved_at=%s "
        "WHERE id=%s AND outcome IS NULL",
        ("WITHDRAWN", exit_price, pnl_pct, now_ts, pred_id),
    )
    if cur.rowcount != 1:
        return None

    # Store withdraw reason in scores JSON for audit (best-effort).
    try:
        cur.execute("SELECT scores FROM predictions WHERE id=%s", (pred_id,))
        row = cur.fetchone()
        scores = {}
        if row and row[0]:
            scores = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
        if not isinstance(scores, dict):
            scores = {}
        scores["withdraw"] = {"reason": reason, "ts": now_ts}
        cur.execute("UPDATE predictions SET scores=%s WHERE id=%s", (json.dumps(scores), pred_id))
    except Exception:
        pass

    if record_pick_resolution:
        try:
            record_pick_resolution(
                pred_id,
                symbol,
                "WITHDRAWN",
                exit_price=exit_price,
                pnl_pct=pnl_pct,
                source="pick_review",
            )
        except Exception:
            pass

    LOGGER.info(
        "WITHDRAW %s id=%s reason=%s pnl=%.2f%%",
        symbol, pred_id, reason, pnl_pct,
    )
    return {
        "id": pred_id,
        "symbol": symbol,
        "reason": reason,
        "exit_price": exit_price,
        "pnl_pct": pnl_pct,
        "direction": direction,
        "entry_price": entry,
    }


def review_open_picks(
    cur,
    symbol_evals: Sequence[Dict[str, Any]],
    all_picks: Sequence[Dict[str, Any]],
    now_ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Withdraw open picks the latest scan no longer supports. Returns withdrawn rows."""
    if not open_pick_review_enabled():
        return []

    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    min_age = _withdraw_min_age_s()
    eval_map = _eval_by_symbol(symbol_evals)
    withdrawn: List[Dict[str, Any]] = []

    cur.execute(
        """
        SELECT id, symbol, direction, confidence, entry_price, target_price, stop_price, predicted_at
        FROM predictions
        WHERE outcome IS NULL AND expires_at > %s
        """,
        (now_ts,),
    )
    open_rows = cur.fetchall()

    for row in open_rows:
        sym = (row[1] or "").strip().upper()
        ev = eval_map.get(sym)
        if not ev:
            continue
        predicted_at = int(row[7] or 0)
        if min_age > 0 and predicted_at and (now_ts - predicted_at) < min_age:
            continue
        reason = _withdraw_reason(ev, all_picks, row)
        if not reason:
            continue
        result = _apply_withdraw(
            cur,
            int(row[0]),
            sym,
            row[2],
            float(row[4] or 0),
            float(row[5] or 0),
            float(row[6] or 0),
            reason,
            now_ts,
        )
        if result:
            withdrawn.append(result)

    return withdrawn


def notify_withdrawals(withdrawn: Sequence[Dict[str, Any]]) -> None:
    if not withdrawn or not withdraw_notify_enabled():
        return
    try:
        from core.telegram import send_pick_withdrawn
    except Exception:
        return
    for w in withdrawn:
        try:
            send_pick_withdrawn(
                w.get("symbol") or "WOLF",
                w.get("reason") or "model_withdrawn",
                float(w.get("entry_price") or 0),
                float(w.get("exit_price") or 0),
                float(w.get("pnl_pct") or 0),
            )
        except Exception as exc:
            LOGGER.warning("withdraw notify failed: %s", str(exc)[:80])
