"""Daily point-in-time options snapshots — the forward clock on options evidence.

The 2026-07-16 edge-hunt falsified all three feature levers derivable from
free historical data (geometry, SEC fundamentals, momentum). Options metrics
are the strongest untested information source, but historical options data is
paywalled — it cannot be backtested for free. What CAN start for free is
forward accumulation: snapshot the chain once per trading day so that in a
few weeks there is honest, point-in-time options history to test on the
sweep harness (and to justify — or kill — a paid-data purchase with evidence).

One row per (symbol, snap_date). Fetches are breaker-gated via
core.yfinance_client.yf_ticker; a symbol with no chain records
available=false rather than being skipped silently.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.options_snapshots")

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS ghost_options_snapshots (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    snap_date TEXT NOT NULL,
    ts BIGINT NOT NULL,
    nearest_expiry TEXT,
    underlying DOUBLE PRECISION,
    call_volume BIGINT,
    put_volume BIGINT,
    call_oi BIGINT,
    put_oi BIGINT,
    pcr_volume DOUBLE PRECISION,
    pcr_oi DOUBLE PRECISION,
    atm_iv_call DOUBLE PRECISION,
    atm_iv_put DOUBLE PRECISION,
    available BOOLEAN DEFAULT FALSE,
    UNIQUE(symbol, snap_date)
)
"""

# Snapshot window: late RTH so the day's chain volume is meaningful but the
# chain is still live (CT session; market closes 15:00 CT).
SNAP_WINDOW_START_HOUR_CT = 13
SNAP_WINDOW_END_HOUR_CT = 15
STATE_KEY_LAST_DATE = "options_snap_last_date"


def _ct_now():
    from zoneinfo import ZoneInfo
    from datetime import datetime
    return datetime.now(ZoneInfo("America/Chicago"))


def _ct_today() -> str:
    return _ct_now().strftime("%Y-%m-%d")


def compute_chain_metrics(calls, puts, underlying: Optional[float]) -> Dict[str, Any]:
    """Pure math over a fetched chain — testable without network."""
    def _col_sum(df, col) -> float:
        try:
            if df is None or df.empty or col not in df:
                return 0.0
            return float(df[col].fillna(0).sum())
        except Exception:
            return 0.0

    def _atm_iv(df) -> Optional[float]:
        try:
            if df is None or df.empty or underlying is None or "strike" not in df:
                return None
            row = df.iloc[(df["strike"] - float(underlying)).abs().argsort()[:1]]
            iv = float(row["impliedVolatility"].iloc[0])
            return round(iv, 4) if 0.0 < iv < 10.0 else None
        except Exception:
            return None

    cv = _col_sum(calls, "volume")
    pv = _col_sum(puts, "volume")
    coi = _col_sum(calls, "openInterest")
    poi = _col_sum(puts, "openInterest")
    return {
        "call_volume": int(cv),
        "put_volume": int(pv),
        "call_oi": int(coi),
        "put_oi": int(poi),
        "pcr_volume": round(pv / cv, 4) if cv > 0 else None,
        "pcr_oi": round(poi / coi, 4) if coi > 0 else None,
        "atm_iv_call": _atm_iv(calls),
        "atm_iv_put": _atm_iv(puts),
        "available": bool(cv > 0 or pv > 0 or coi > 0 or poi > 0),
    }


def snapshot_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch one symbol's nearest-expiry chain metrics. None = fetch blocked
    or failed (breaker open, network error); an EMPTY chain is a valid row."""
    sym = (symbol or "").upper()
    if not sym:
        return None
    try:
        from core.yfinance_client import yf_ticker
        t = yf_ticker(sym)
        if t is None:
            return None
        expirations = getattr(t, "options", None)
        base = {"symbol": sym, "snap_date": _ct_today(), "ts": int(time.time())}
        if not expirations:
            return {**base, "nearest_expiry": None, "underlying": None,
                    **compute_chain_metrics(None, None, None)}
        near = expirations[0]
        chain = t.option_chain(near)
        underlying = None
        try:
            underlying = float(t.fast_info["last_price"])
        except Exception:
            underlying = None
        return {**base, "nearest_expiry": str(near), "underlying": underlying,
                **compute_chain_metrics(chain.calls, chain.puts, underlying)}
    except Exception as exc:
        LOGGER.debug("options snapshot %s failed: %s", sym, str(exc)[:80])
        return None


def record_snapshots(symbols: Optional[List[str]] = None, *,
                     delay_s: float = 1.0, max_symbols: int = 150) -> Dict[str, Any]:
    """Snapshot the watchlist and upsert one row per (symbol, snap_date)."""
    from core.db import db_conn
    if symbols is None:
        from config.symbols import OFFICIAL_WATCHLIST
        symbols = list(OFFICIAL_WATCHLIST)
    symbols = [s.upper() for s in symbols][: max(1, int(max_symbols))]
    stored = 0
    empty = 0
    failed = 0
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(TABLE_DDL)
    for i, sym in enumerate(symbols):
        snap = snapshot_symbol(sym)
        if snap is None:
            failed += 1
        else:
            if not snap["available"]:
                empty += 1
            try:
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        INSERT INTO ghost_options_snapshots
                            (symbol, snap_date, ts, nearest_expiry, underlying,
                             call_volume, put_volume, call_oi, put_oi,
                             pcr_volume, pcr_oi, atm_iv_call, atm_iv_put, available)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(symbol, snap_date) DO UPDATE SET
                            ts=EXCLUDED.ts, nearest_expiry=EXCLUDED.nearest_expiry,
                            underlying=EXCLUDED.underlying,
                            call_volume=EXCLUDED.call_volume, put_volume=EXCLUDED.put_volume,
                            call_oi=EXCLUDED.call_oi, put_oi=EXCLUDED.put_oi,
                            pcr_volume=EXCLUDED.pcr_volume, pcr_oi=EXCLUDED.pcr_oi,
                            atm_iv_call=EXCLUDED.atm_iv_call, atm_iv_put=EXCLUDED.atm_iv_put,
                            available=EXCLUDED.available
                        """,
                        (snap["symbol"], snap["snap_date"], snap["ts"],
                         snap["nearest_expiry"], snap["underlying"],
                         snap["call_volume"], snap["put_volume"],
                         snap["call_oi"], snap["put_oi"],
                         snap["pcr_volume"], snap["pcr_oi"],
                         snap["atm_iv_call"], snap["atm_iv_put"], snap["available"]),
                    )
                stored += 1
            except Exception as exc:
                LOGGER.warning("options snapshot store %s: %s", sym, str(exc)[:80])
                failed += 1
        if delay_s > 0 and i + 1 < len(symbols):
            time.sleep(delay_s)
    out = {"ok": True, "requested": len(symbols), "stored": stored,
           "empty_chain": empty, "failed": failed, "snap_date": _ct_today()}
    LOGGER.info("options snapshots: %s", out)
    return out


def get_snapshots(symbol: Optional[str] = None, *, days: int = 30,
                  limit: int = 5000) -> Dict[str, Any]:
    """Read-only snapshot history."""
    from core.db import db_conn
    cutoff = int(time.time()) - max(1, min(365, int(days))) * 86400
    rows: List[Dict[str, Any]] = []
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            q = ("SELECT symbol, snap_date, ts, nearest_expiry, underlying, "
                 "call_volume, put_volume, call_oi, put_oi, pcr_volume, pcr_oi, "
                 "atm_iv_call, atm_iv_put, available "
                 "FROM ghost_options_snapshots WHERE ts >= %s")
            params: List[Any] = [cutoff]
            if symbol:
                q += " AND symbol = %s"
                params.append(symbol.upper())
            q += " ORDER BY ts DESC LIMIT %s"
            params.append(max(1, min(20000, int(limit))))
            cur.execute(q, tuple(params))
            cols = ("symbol", "snap_date", "ts", "nearest_expiry", "underlying",
                    "call_volume", "put_volume", "call_oi", "put_oi",
                    "pcr_volume", "pcr_oi", "atm_iv_call", "atm_iv_put", "available")
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}
    return {"ok": True, "days": int(days), "count": len(rows), "rows": rows}


def run_options_snapshot_job() -> Dict[str, Any]:
    """Scheduler entry — self-gating: CT weekday, snapshot window, once/day."""
    now = _ct_now()
    if now.weekday() >= 5:
        return {"ok": True, "skipped": "weekend"}
    if not (SNAP_WINDOW_START_HOUR_CT <= now.hour < SNAP_WINDOW_END_HOUR_CT):
        return {"ok": True, "skipped": "outside_snapshot_window"}
    today = _ct_today()
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key=%s",
                        (STATE_KEY_LAST_DATE,))
            row = cur.fetchone()
            if row and row[0] == today:
                return {"ok": True, "skipped": "already_ran_today"}
            # Claim the day BEFORE the multi-minute fetch loop so an
            # overlapping scheduler tick cannot double-run.
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (STATE_KEY_LAST_DATE, today),
            )
    except Exception as exc:
        return {"ok": False, "error": "state check failed: " + str(exc)[:120]}
    return record_snapshots()
