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

2026-10-05: IEX priced 42 of 3,251 names at the card, and the day's real gappers
(PCVX +50% on Phase 3 data, ALEC on a Genentech licence, RXO, PTC) never reached
the card at all. The free plan does serve SIP bars older than 15 minutes, so on
the IEX feed the scan also reads full-market 5-minute bars ending 16 minutes ago
and adds every name it can price that way (source "sip_delayed"; the price is
that bar's close, timestamped at the bar's end, and must still be <= 30 min old).
That is discovery: it puts the name in front of the card, research and the
reports. The frozen experiments still price their entry from IEX only.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, Optional

from edge.contracts import ET
from shared.redaction import redact_exc

COMMON = re.compile(r"^[A-Z]{1,5}$")
MIN_PRICE, MAX_PRICE, MIN_PRIOR_DOLLARS = 2.0, 500.0, 2_000_000.0
BATCH, CACHE_S, MAX_AGE_S = 100, 240, 1800
MIN_GAP, MAX_GAP = 3.0, 100.0
SIP_DELAY_S = 16 * 60          # the free plan serves SIP data older than 15 minutes
SIP_FRAME, SIP_FRAME_S, SIP_BATCH, SIP_MAX_PAGES = "5Min", 300, 400, 20


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


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=ET).isoformat()


def sip_delayed(get, syms, *, day: date, now: int) -> Dict[str, Any]:
    """{"prices": {sym: (close, bar_end_ts, recent_volume)}, "batch_errors", "truncated"}: the latest
    full-market (SIP) 5-minute bar of this session that the free plan may read (ending >= 16 min ago)
    and is still <= 30 min old. Only the last 15 minutes the plan may read are asked for (one small
    page per batch); recent_volume is their volume. A failed or truncated batch is counted, never
    guessed."""
    from edge.providers import alpaca as A
    session_start, end = _at(day, 4, 0), now - SIP_DELAY_S
    start = max(session_start, end - (MAX_AGE_S - SIP_DELAY_S))
    prices: Dict[str, Any] = {}
    errors = truncated = 0
    if end <= session_start:
        return {"prices": prices, "batch_errors": 0, "truncated": 0}
    for i in range(0, len(syms), SIP_BATCH):
        chunk = syms[i:i + SIP_BATCH]
        try:
            bars, complete = A.bars_pages(get, chunk, timeframe=SIP_FRAME, start=_iso(start),
                                          end=_iso(end), feed="sip", max_pages=SIP_MAX_PAGES)
        except Exception:  # noqa: BLE001 - one failed batch never sinks the scan; it is counted
            errors += 1
            continue
        truncated += 0 if complete else 1
        for s, rows in (bars or {}).items():
            rows = [r for r in rows or [] if A.iso_to_epoch(r.get("t")) is not None and r.get("c") is not None]
            if not rows:
                continue
            last = max(rows, key=lambda r: A.iso_to_epoch(r["t"]))
            ts = min(A.iso_to_epoch(last["t"]) + SIP_FRAME_S, end)
            if ts < session_start or now - ts > MAX_AGE_S:
                continue
            vol = sum(float(r.get("v") or 0) for r in rows if A.iso_to_epoch(r["t"]) >= session_start)
            prices[s] = (float(last["c"]), ts, vol)
    return {"prices": prices, "batch_errors": errors, "truncated": truncated}


def scan(get, store, *, day: date, now: int, top: int = 50) -> Dict[str, Any]:
    """{"gainers": [{"symbol", "gap_pct", "price", "ts"}...], "scanned", "priced", "at"} -- cached 4 min."""
    from edge.providers import alpaca as A
    ds = day.isoformat()
    if store is not None:
        cached = store.get("edge_pm_scan", ds)
        # A scan with failed batches is never reused: the card retries it on its next tick.
        if cached and not cached.get("batch_errors") and now - int(cached.get("at") or 0) < CACHE_S:
            return cached
    base = _base(get, store, day)
    universe = None
    if store is not None:
        from edge import universe as U
        universe = U.symbols_as_of(store, ds)
    syms = sorted(s for s in base if common_stock(s, universe))
    from edge import feeds as FD
    feed = FD.live_feed(get, store, now=now)       # IEX on the free plan; SIP once it is paid for
    session_start, rows, priced, quoted, errors = _at(day, 4, 0), [], 0, 0, 0
    for i in range(0, len(syms), BATCH):
        chunk = syms[i:i + BATCH]
        try:
            snaps = A.snapshots(get, chunk, feed=feed)
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
                rows.append({"symbol": s, "gap_pct": round(gap, 2), "price": float(px), "ts": ts, "source": feed})
    sip = {"prices": {}, "batch_errors": 0, "truncated": 0}
    if feed != "sip":
        # Discovery only (see the module note): names IEX cannot see, priced from 15-min-delayed SIP.
        have = {r["symbol"] for r in rows}
        sip = sip_delayed(get, syms, day=day, now=now)
        for s, (px, ts, vol) in sip["prices"].items():
            if s in have:
                continue
            gap = (px / base[s] - 1) * 100
            if MIN_GAP <= gap <= MAX_GAP:
                rows.append({"symbol": s, "gap_pct": round(gap, 2), "price": px, "ts": ts,
                             "source": "sip_delayed", "recent_volume": vol})
    rows.sort(key=lambda r: -r["gap_pct"])
    out = {"day": ds, "at": now, "scanned": len(syms), "priced": priced, "quoted": quoted, "batch_errors": errors,
           "sip_delayed_priced": len(sip["prices"]), "sip_delayed_batch_errors": sip["batch_errors"],
           "sip_delayed_truncated": sip["truncated"],
           "gainers": rows[:top], "feed": feed,
           "source": f"{feed.upper()} snapshots of the prior session's liquid common stocks"
                     + ("" if feed == "sip" else ", plus 15-min-delayed SIP 5-minute bars")}
    if store is not None:
        store.put("edge_pm_scan", ds, out)
        # Evidence for the SIP decision: how much of the market the free IEX feed can see,
        # by a fresh TRADE (what prices a gap) vs a fresh bid/ask QUOTE, through the morning.
        cov = store.get("edge_pm_coverage", ds) or {"day": ds, "samples": []}
        cov["samples"] = (cov["samples"] + [{"at": now, "feed": feed, "scanned": len(syms), "fresh_trade": priced,
                                             "fresh_quote": quoted, "batch_errors": errors,
                                             "sip_delayed_priced": len(sip["prices"])}])[-100:]
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
        sc, notes["premarket_scan"] = {"gainers": [], "scanned": 0, "priced": 0}, redact_exc(exc, 160)
    mv, updated = {}, None
    try:
        mv = A.movers(get, top=top)
        updated = A.iso_to_epoch(mv.get("last_updated"))
    except Exception as exc:  # noqa: BLE001
        notes["movers"] = redact_exc(exc, 160)
    screener = [str(g["symbol"]).upper() for g in mv.get("gainers") or [] if g.get("symbol")]
    stale = updated is None or now - updated > 900
    ordered, seen = [], set()
    for s in [g["symbol"] for g in sc["gainers"]] + screener:
        if s not in seen and common_stock(s, universe):
            seen.add(s)
            ordered.append(s)
    return {"symbols": ordered[:top], "movers_last_updated": updated, "movers_stale": stale,
            "scan": {k: sc.get(k) for k in ("scanned", "priced", "quoted", "batch_errors", "sip_delayed_priced")},
            "scan_top": sc["gainers"][:10],
            "sip_ref": {g["symbol"]: {"price": g["price"], "ts": g["ts"]}
                        for g in sc["gainers"] if g.get("source") == "sip_delayed"},
            "dropped_non_common": sorted(set(screener) - seen - {
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
        return B.Probe("movers.premarket_scan", "alpaca_iex", B.ERROR, note=redact_exc(exc, 160))
    top = ", ".join(f"{g['symbol']} {g['gap_pct']:+.1f}%"
                    + (" (delayed SIP)" if g.get("source") == "sip_delayed" else "") for g in out["gainers"])
    sip_n = out.get("sip_delayed_priced") or 0
    sip_err = out.get("sip_delayed_batch_errors") or 0
    return B.Probe("movers.premarket_scan", "alpaca_iex", B.OK if (out["priced"] or sip_n) else B.EMPTY,
                   rows=out["priced"] + sip_n,
                   note=f"scanned {out['scanned']}, {out['priced']} with a fresh current-session IEX print, "
                        f"{out['quoted']} with a fresh quote, {sip_n} priced from 15-min-delayed SIP"
                        f"{' (SIP batch errors ' + str(sip_err) + ')' if sip_err else ''}"
                        f"{', batch errors ' + str(out['batch_errors']) if out['batch_errors'] else ''}; top: {top or 'none'}")
