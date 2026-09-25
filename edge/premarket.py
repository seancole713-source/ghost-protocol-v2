"""Today's premarket gainers, computed from the market itself.

First live card, 2026-09-23 09:05 ET: Alpaca's movers screener still served the
PRIOR session's list before the open (health banner "movers: STALE"), so the
card screened yesterday's names -- JAGX, RAIN, GRML -- while today's real
gappers (WOR on an earnings beat, IONQ) never reached it.

This scan asks the market directly:
  1. the prior session's liquid common stocks, from ONE Polygon grouped-daily
     call (completed sessions are on the free plan): price $2-$500 and a day's
     dollar volume >= $2M -- a pre-filter only; the card's own E2/E3 checks
     still apply to every name;
  2. an IEX snapshot of each, 100 symbols per request (free plan);
  3. the gap of every name with a CURRENT-SESSION print no older than 30 min,
     vs the prior close.
It is cached for 4 minutes, so the research window and the card share it.

Limits, stated not hidden: IEX is one exchange, so thinly traded names may
have no premarket print yet (they are skipped, never guessed), and a
full-market consolidated (SIP) view needs the paid feed.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, Optional

from edge.contracts import ET

COMMON = re.compile(r"^[A-Z]{1,5}$")
MIN_PRICE, MAX_PRICE, MIN_PRIOR_DOLLARS = 2.0, 500.0, 2_000_000.0
BATCH, CACHE_S, MAX_AGE_S = 100, 240, 1800
MIN_GAP, MAX_GAP = 3.0, 100.0


def common_stock(sym: str, universe: Optional[set] = None) -> bool:
    """Common stock only (rule E6). The universe snapshot (Polygon type CS/ADRC) is
    authoritative; without it, 5-letter Nasdaq symbols ending W/R/U/Z (warrants,
    rights, units) and dotted classes like .WS/.RT are excluded."""
    s = str(sym or "").upper()
    if universe is not None:
        return s in universe
    if not COMMON.match(s):
        return False
    return not (len(s) == 5 and s[-1] in "WRUZ")


def _at(day: date, hh: int, mm: int) -> int:
    return int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET).timestamp())


def _base(get, store, day: date) -> Dict[str, float]:
    """{symbol: prior close} for the prior session's liquid common stocks, cached per day."""
    ds = day.isoformat()
    cached = store.get("edge_pm_base", ds) if store is not None else None
    if cached:
        return cached["prev_close"]
    from edge.pipeline import previous_trading_day
    from edge.providers import polygon as PG
    prev = previous_trading_day(day, store)
    out: Dict[str, float] = {}
    for r in PG.grouped_daily(get, prev):
        sym, c, v = str(r.get("T") or ""), float(r.get("c") or 0), float(r.get("v") or 0)
        if COMMON.match(sym) and MIN_PRICE <= c <= MAX_PRICE and c * v >= MIN_PRIOR_DOLLARS:
            out[sym] = c
    if store is not None and out:
        store.put("edge_pm_base", ds, {"day": ds, "prior_session": prev.isoformat(), "prev_close": out})
    return out


def scan(get, store, *, day: date, now: int, top: int = 50) -> Dict[str, Any]:
    """{"gainers": [{"symbol", "gap_pct", "price", "ts"}...], "scanned", "priced", "at"} -- cached 4 min."""
    from edge.providers import alpaca as A
    ds = day.isoformat()
    if store is not None:
        cached = store.get("edge_pm_scan", ds)
        if cached and now - int(cached.get("at") or 0) < CACHE_S:
            return cached
    base = _base(get, store, day)
    universe = None
    if store is not None:
        from edge import universe as U
        universe = U.symbols_as_of(store, ds)
    syms = sorted(s for s in base if common_stock(s, universe))
    session_start, rows, priced, quoted, errors = _at(day, 4, 0), [], 0, 0, 0
    for i in range(0, len(syms), BATCH):
        chunk = syms[i:i + BATCH]
        try:
            snaps = A.snapshots(get, chunk, feed="iex")
        except Exception:  # noqa: BLE001 - one failed batch never sinks the scan; it is counted
            errors += 1
            continue
        for s in chunk:
            snap = (snaps or {}).get(s) or {}
            q = snap.get("latestQuote") or {}
            qts = A.iso_to_epoch(q.get("t"))
            if (qts is not None and qts >= session_start and now - qts <= MAX_AGE_S
                    and (float(q.get("bp") or 0) > 0 or float(q.get("ap") or 0) > 0)):
                quoted += 1          # coverage evidence only: a quote never prices a gap here
            t = snap.get("latestTrade") or {}
            ts, px = A.iso_to_epoch(t.get("t")), t.get("p")
            if ts is None or px is None or ts < session_start or now - ts > MAX_AGE_S:
                continue
            priced += 1
            gap = (float(px) / base[s] - 1) * 100
            if MIN_GAP <= gap <= MAX_GAP:
                rows.append({"symbol": s, "gap_pct": round(gap, 2), "price": float(px), "ts": ts})
    rows.sort(key=lambda r: -r["gap_pct"])
    out = {"day": ds, "at": now, "scanned": len(syms), "priced": priced, "quoted": quoted, "batch_errors": errors,
           "gainers": rows[:top], "source": "IEX snapshots of the prior session's liquid common stocks"}
    if store is not None:
        store.put("edge_pm_scan", ds, out)
        # Evidence for the SIP decision: how much of the market the free IEX feed can see,
        # by a fresh TRADE (what prices a gap) vs a fresh bid/ask QUOTE, through the morning.
        cov = store.get("edge_pm_coverage", ds) or {"day": ds, "samples": []}
        cov["samples"] = (cov["samples"] + [{"at": now, "scanned": len(syms), "fresh_trade": priced,
                                             "fresh_quote": quoted, "batch_errors": errors}])[-100:]
        store.put("edge_pm_coverage", ds, cov)
    return out


def candidates(get, store, *, day: date, now: int, top: int = 50) -> Dict[str, Any]:
    """The card's candidate list: today's scanned gainers first (fresh), then the movers
    screener -- common stock only, deduplicated. Says which source was stale or failed."""
    from edge.providers import alpaca as A
    universe = None
    if store is not None:
        from edge import universe as U
        universe = U.symbols_as_of(store, day.isoformat())
    notes: Dict[str, str] = {}
    try:
        sc = scan(get, store, day=day, now=now, top=top)
    except Exception as exc:  # noqa: BLE001 - the screener still answers; the card names the failure
        sc, notes["premarket_scan"] = {"gainers": [], "scanned": 0, "priced": 0}, f"{type(exc).__name__}: {str(exc)[:120]}"
    mv, updated = {}, None
    try:
        mv = A.movers(get, top=top)
        updated = A.iso_to_epoch(mv.get("last_updated"))
    except Exception as exc:  # noqa: BLE001
        notes["movers"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    screener = [str(g["symbol"]).upper() for g in mv.get("gainers") or [] if g.get("symbol")]
    stale = updated is None or now - updated > 900
    ordered, seen = [], set()
    for s in [g["symbol"] for g in sc["gainers"]] + screener:
        if s not in seen and common_stock(s, universe):
            seen.add(s)
            ordered.append(s)
    return {"symbols": ordered[:top], "movers_last_updated": updated, "movers_stale": stale,
            "scan": {k: sc.get(k) for k in ("scanned", "priced", "quoted", "batch_errors")},
            "scan_top": sc["gainers"][:10], "dropped_non_common": sorted(set(screener) - seen - {
                g["symbol"] for g in sc["gainers"]})[:20],
            "errors": notes}


def probe(get=None):
    """Proves the scan end to end with live data: one prior-session call, the IEX snapshots,
    and today's top gappers (during the session: today's gainers so far)."""
    import time
    from edge.providers import base as B
    get = get or B.default_get()
    now = int(time.time())
    try:
        out = scan(get, None, day=B.et_today(), now=now, top=5)
    except Exception as exc:  # noqa: BLE001
        return B.Probe("movers.premarket_scan", "alpaca_iex", B.ERROR, note=f"{type(exc).__name__}: {str(exc)[:120]}")
    top = ", ".join(f"{g['symbol']} {g['gap_pct']:+.1f}%" for g in out["gainers"])
    return B.Probe("movers.premarket_scan", "alpaca_iex", B.OK if out["priced"] else B.EMPTY, rows=out["priced"],
                   note=f"scanned {out['scanned']}, {out['priced']} with a fresh current-session print, "
                        f"{out['quoted']} with a fresh quote"
                        f"{', batch errors ' + str(out['batch_errors']) if out['batch_errors'] else ''}; top: {top or 'none'}")
