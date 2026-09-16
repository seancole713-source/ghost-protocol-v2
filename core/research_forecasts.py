"""Read existing immutable daily shadow forecasts without creating live picks."""
from __future__ import annotations

import time


def research_forecasts(*, limit: int = 50, now: int | None = None) -> dict:
    from core.db import db_conn

    limit = max(1, min(200, int(limit)))
    now = int(time.time()) if now is None else int(now)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FILTER (WHERE expires_at > %s), "
            "COUNT(*) FILTER (WHERE expires_at <= %s) "
            "FROM ghost_shadow_outcomes WHERE outcome IS NULL",
            (now, now),
        )
        counts = cur.fetchone() or (0, 0)
        cur.execute(
            "SELECT id, symbol, trade_date, eval_ts, direction, entry_price, "
            "target_price, stop_price, expires_at, model_prob, hold_bars, "
            "model_sha256, skip_code FROM ghost_shadow_outcomes "
            "WHERE outcome IS NULL AND expires_at > %s "
            "ORDER BY eval_ts DESC, symbol, id DESC LIMIT %s",
            (now, limit),
        )
        keys = ("id", "symbol", "session_date", "issued_at", "direction", "entry_reference",
                "target_reference", "stop_reference", "expires_at", "model_score",
                "hold_bars", "model_sha256", "release_blocker")
        forecasts = [dict(zip(keys, row)) for row in cur.fetchall()]
    for forecast in forecasts:
        forecast["trading_eligible"] = False
        forecast["probability_status"] = "unproven_model_score"
    return {
        "ok": True,
        "read_only": True,
        "as_of": now,
        "source": "ghost_shadow_outcomes",
        "status": "collecting_outcomes" if forecasts else "no_open_research_forecasts",
        "forecasts": forecasts,
        "count": len(forecasts),
        "total_open": int(counts[0]),
        "awaiting_resolution": int(counts[1]),
        "has_more": int(counts[0]) > len(forecasts),
        "trading_eligible": False,
        "accuracy_proven": False,
        "issuance_contract": "completed_daily_bar_post_close_v1",
        "note": (
            "Existing post-close research forecasts, not approved picks or new intraday "
            "predictions. Prices are frozen historical references, not executable entries. "
            "A model score is not a demonstrated win rate. No issuance or trading gates "
            "are bypassed by this read-only view."
        ),
    }
