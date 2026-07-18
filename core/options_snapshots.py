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
import re
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


# OCC option symbol: UNDERLYING(1-6) + YYMMDD + C|P + strike*1000 (8 digits).
_OCC_RE = re.compile(r"^[A-Z]+(\d{6})([CP])(\d{8})$")
_ALPACA_OPTIONS_BASE = "https://data.alpaca.markets/v1beta1/options/snapshots"
# Front-month window: liquid flow lives in the near expiries; bound the fetch.
_OPT_EXPIRY_WINDOW_DAYS = 45


def aggregate_alpaca_options(snapshots: Dict[str, Any]) -> Dict[str, Any]:
    """Pure aggregation of Alpaca option snapshots -> put/call volume + PCR.

    Parses OCC contract symbols; sums daily volume by side. No network, no
    IV/OI (Alpaca snapshots omit both) — PCR-volume is the reliable flow
    signal we accrue. Testable without a live call."""
    cv = pv = 0
    for sym, snap in (snapshots or {}).items():
        m = _OCC_RE.match(sym)
        if not m:
            continue
        side = m.group(2)
        vol = 0
        try:
            vol = int((snap.get("dailyBar") or {}).get("v") or 0)
        except Exception:
            vol = 0
        if side == "C":
            cv += vol
        else:
            pv += vol
    return {
        "call_volume": cv,
        "put_volume": pv,
        "call_oi": None,
        "put_oi": None,
        "pcr_volume": round(pv / cv, 4) if cv > 0 else None,
        "pcr_oi": None,
        "atm_iv_call": None,   # deferred: needs a BS solver (no IV in Alpaca snapshot)
        "atm_iv_put": None,
        "available": bool(cv > 0 or pv > 0),
    }


def _alpaca_options_snapshot(symbol: str) -> Optional[Dict[str, Any]]:
    """One breaker-gated Alpaca options-snapshot fetch for the front-month
    window. None = blocked/failed (caller falls back)."""
    import os
    from datetime import timedelta
    from core.circuit_breaker import _alpaca_cb
    if not _alpaca_cb.allow():
        return None
    key = os.getenv("ALPACA_KEY_ID", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    if not (key and sec):
        return None
    try:
        import requests
        today = _ct_now().date()
        params = {
            "limit": 1000,
            "expiration_date_gte": today.isoformat(),
            "expiration_date_lte": (today + timedelta(days=_OPT_EXPIRY_WINDOW_DAYS)).isoformat(),
        }
        r = requests.get(
            f"{_ALPACA_OPTIONS_BASE}/{symbol.upper()}",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
            params=params, timeout=15,
        )
        if r.status_code != 200:
            # 404/no-options is a valid "no chain", not a breaker failure.
            if r.status_code in (403, 404):
                _alpaca_cb.record_success()
                return {}
            _alpaca_cb.record_failure()
            return None
        _alpaca_cb.record_success()
        return (r.json() or {}).get("snapshots") or {}
    except Exception as exc:
        _alpaca_cb.record_failure()
        LOGGER.debug("alpaca options %s: %s", symbol, str(exc)[:80])
        return None


def snapshot_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch one symbol's front-month options flow. None = fetch blocked/failed
    (caller counts it, never silently skips); an EMPTY chain is a valid row.

    Primary source is Alpaca options snapshots (reliable, breaker-managed, no
    yfinance rate-limit that capped the first run at 3 symbols). yfinance is a
    last-resort fallback that also yields ATM IV when it is reachable."""
    sym = (symbol or "").upper()
    if not sym:
        return None
    base = {"symbol": sym, "snap_date": _ct_today(), "ts": int(time.time())}
    snaps = _alpaca_options_snapshot(sym)
    if snaps is not None:
        from core.prices import get_stock_price
        underlying = None
        try:
            underlying = float(get_stock_price(sym) or 0) or None
        except Exception:
            underlying = None
        return {**base, "nearest_expiry": None, "underlying": underlying,
                **aggregate_alpaca_options(snaps)}
    # Alpaca blocked (breaker/creds) — best-effort yfinance fallback.
    try:
        from core.yfinance_client import yf_ticker
        t = yf_ticker(sym)
        if t is None:
            return None
        expirations = getattr(t, "options", None)
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
                     delay_s: float = 0.5, max_symbols: int = 150) -> Dict[str, Any]:
    """Snapshot the watchlist and upsert one row per (symbol, snap_date).

    Primary source is Alpaca options snapshots (one request per symbol, no
    yfinance rate-limit), so the full watchlist accrues in a single run —
    unlike the yfinance-only v1 which the breaker capped at ~3 symbols/day.
    If the Alpaca breaker opens mid-run we stop early and report the remainder
    as skipped_breaker (its cooldown outlasts the rest of the loop)."""
    from core.db import db_conn
    if symbols is None:
        from config.symbols import OFFICIAL_WATCHLIST
        symbols = list(OFFICIAL_WATCHLIST)
    symbols = [s.upper() for s in symbols][: max(1, int(max_symbols))]
    stored = 0
    empty = 0
    failed = 0
    skipped_breaker = 0
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(TABLE_DDL)
    for i, sym in enumerate(symbols):
        try:
            from core.circuit_breaker import _alpaca_cb
            from core.yfinance_client import _gate
            # Only stop early if BOTH sources are blocked — Alpaca is primary,
            # yfinance is the fallback.
            if not _alpaca_cb.allow() and not _gate():
                skipped_breaker = len(symbols) - i
                LOGGER.warning("options snapshots: both option sources blocked at "
                               "symbol %d/%d — stopping early", i + 1, len(symbols))
                break
        except Exception:
            pass
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
           "empty_chain": empty, "failed": failed,
           "skipped_breaker": skipped_breaker, "snap_date": _ct_today()}
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


_CLAIM_STALE_S = 1800  # a "running" claim older than this is a crashed run


def _write_claim(status: str, stored: int = 0) -> None:
    import json as _json
    from core.db import db_conn
    val = _json.dumps({"date": _ct_today(), "status": status,
                       "stored": int(stored), "ts": int(time.time())})
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
            (STATE_KEY_LAST_DATE, val),
        )


def run_options_snapshot_job() -> Dict[str, Any]:
    """Scheduler entry — self-gating: CT weekday, snapshot window, once/day.

    A day is only marked done when the run STORED something. A run that
    stored 0 (breaker open, provider outage) leaves the day re-claimable on
    the next tick inside the window — the 2026-07-16 manual run stored 0/100
    behind an open breaker, and a claim-on-start design would have silently
    lost the whole day. A 'running' claim guards against double-runs and
    goes stale after 30 minutes (crashed run)."""
    import json as _json
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
        claim = None
        if row and row[0]:
            try:
                claim = _json.loads(row[0])
            except Exception:
                # Legacy plain-date value (pre status-claim): treat as done.
                if row[0] == today:
                    return {"ok": True, "skipped": "already_ran_today"}
        if isinstance(claim, dict) and claim.get("date") == today:
            if claim.get("status") == "done" and int(claim.get("stored") or 0) > 0:
                return {"ok": True, "skipped": "already_ran_today"}
            if (claim.get("status") == "running"
                    and int(time.time()) - int(claim.get("ts") or 0) < _CLAIM_STALE_S):
                return {"ok": True, "skipped": "run_in_progress"}
            # else: failed (stored=0) or stale running claim — retry the day.
        _write_claim("running")
    except Exception as exc:
        return {"ok": False, "error": "state check failed: " + str(exc)[:120]}
    out = record_snapshots()
    try:
        _write_claim("done", stored=int(out.get("stored") or 0))
    except Exception:
        LOGGER.warning("options snapshots: done-claim write failed")
    return out
