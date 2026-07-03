"""My Picks — the user's personal, server-persisted watchlist.

Any symbol the user is personally invested in can be saved here and tracked
in its own console tab. Each pick gets the same full-system read as any
other symbol: live price, the latest Super Ghost ledger row (direction /
action / grade / reference / target / stop), and any active engine
prediction. Server-side persistence means the list is identical on every
device (the console's sidebar "prediction pool" is localStorage-only and
does not survive browsers).

Auth: personal investment info — every route is gated behind the same
require_portfolio_auth as the portfolio (admin cookie / MCP token / OAuth).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.my_picks")

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,7}$")
MAX_PICKS = 30

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS user_my_picks (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL UNIQUE,
    note TEXT DEFAULT '',
    added_at BIGINT NOT NULL
)
"""


def clean_symbol(raw: Any) -> Optional[str]:
    """Uppercased, validated ticker or None."""
    sym = str(raw or "").strip().upper()
    return sym if _SYMBOL_RE.match(sym) else None


def ensure_table(cur) -> None:
    cur.execute(_CREATE_TABLE)


def list_symbols(cur) -> List[Dict[str, Any]]:
    ensure_table(cur)
    cur.execute("SELECT symbol, note, added_at FROM user_my_picks ORDER BY added_at ASC, symbol ASC")
    return [{"symbol": r[0], "note": r[1] or "", "added_at": int(r[2] or 0)} for r in cur.fetchall()]


def add_symbol(cur, raw_symbol: Any, note: str = "") -> Dict[str, Any]:
    sym = clean_symbol(raw_symbol)
    if not sym:
        return {"ok": False, "error": "invalid symbol"}
    ensure_table(cur)
    cur.execute("SELECT COUNT(*) FROM user_my_picks")
    if int(cur.fetchone()[0] or 0) >= MAX_PICKS:
        return {"ok": False, "error": f"pick limit reached ({MAX_PICKS})"}
    cur.execute(
        "INSERT INTO user_my_picks (symbol, note, added_at) VALUES (%s,%s,%s) "
        "ON CONFLICT (symbol) DO NOTHING",
        (sym, str(note or "")[:200], int(time.time())),
    )
    return {"ok": True, "symbol": sym, "added": bool(cur.rowcount)}


def remove_symbol(cur, raw_symbol: Any) -> Dict[str, Any]:
    sym = clean_symbol(raw_symbol)
    if not sym:
        return {"ok": False, "error": "invalid symbol"}
    ensure_table(cur)
    cur.execute("DELETE FROM user_my_picks WHERE symbol=%s", (sym,))
    return {"ok": True, "symbol": sym, "removed": bool(cur.rowcount)}


def _latest_ledger_row(cur, sym: str) -> Optional[Dict[str, Any]]:
    """Most recent Super Ghost ledger read for the symbol (cheap DB row)."""
    try:
        cur.execute(
            "SELECT created_at, direction, action, confidence, accuracy_grade, "
            "reference_price, target_price, stop_loss, regime_label "
            "FROM super_ghost_predictions WHERE symbol=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (sym,),
        )
        r = cur.fetchone()
    except Exception:
        return None
    if not r:
        return None
    return {
        "created_at": int(r[0] or 0),
        "direction": r[1],
        "action": r[2],
        "confidence": float(r[3]) if r[3] is not None else None,
        "grade": r[4],
        "reference_price": float(r[5]) if r[5] is not None else None,
        "target_price": float(r[6]) if r[6] is not None else None,
        "stop_loss": float(r[7]) if r[7] is not None else None,
        "regime": r[8],
    }


def _active_prediction(cur, sym: str, now: int) -> Optional[Dict[str, Any]]:
    """Open engine pick for the symbol, if any (highest confidence)."""
    try:
        cur.execute(
            "SELECT direction, confidence, entry_price, target_price, stop_price, expires_at "
            "FROM predictions WHERE symbol=%s AND outcome IS NULL AND expires_at > %s "
            "ORDER BY confidence DESC LIMIT 1",
            (sym, now),
        )
        r = cur.fetchone()
    except Exception:
        return None
    if not r:
        return None
    return {
        "direction": r[0],
        "confidence": float(r[1]) if r[1] is not None else None,
        "entry_price": float(r[2]) if r[2] is not None else None,
        "target_price": float(r[3]) if r[3] is not None else None,
        "stop_price": float(r[4]) if r[4] is not None else None,
        "expires_at": int(r[5] or 0),
    }


def build_my_picks_payload() -> Dict[str, Any]:
    """Full payload: every saved pick with its system summary.

    Deliberately cheap — DB rows + best-effort spot prices only. The heavy
    per-symbol Super Ghost brains stay on-demand: the console's full-report
    view fetches them when the user opens a pick.
    """
    from core.db import db_conn

    now = int(time.time())
    picks: List[Dict[str, Any]] = []
    with db_conn() as conn:
        cur = conn.cursor()
        rows = list_symbols(cur)
        for row in rows:
            sym = row["symbol"]
            entry: Dict[str, Any] = dict(row)
            entry["ledger"] = _latest_ledger_row(cur, sym)
            entry["active_pick"] = _active_prediction(cur, sym, now)
            picks.append(entry)
    # Spot prices outside the DB transaction (network, best-effort).
    for entry in picks:
        try:
            from core.prices import get_price
            p = get_price(entry["symbol"], "stock")
            entry["live_price"] = round(float(p), 4) if p else None
        except Exception:
            entry["live_price"] = None
    return {"ok": True, "count": len(picks), "picks": picks, "ts": now}
