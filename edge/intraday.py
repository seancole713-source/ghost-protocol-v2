"""The intraday radar: strategies that act DURING the session, in shadow.

Every 5 minutes, 09:45-14:30 ET, it watches the market's top movers and, for
each, keeps a radar record with its state history for the whole day. Two
strategies are evaluated, each its own experiment and each an UNVALIDATED v0
hypothesis:

  catalyst_breakout      dated company catalyst, not dilutive, liquid, a 1-min
                         CLOSE above the 5-min opening range, time-of-day RVOL
  intraday_continuation  liquid, last 5 closes above VWAP, accelerating,
                         time-of-day RVOL

When one turns ELIGIBLE a forecast is recorded at once; its window opens the
next minute and its entry expires 20 minutes later. At most 2 per strategy per
day, one per symbol.

Data honesty. Real-time bars on the free plan are IEX only -- one exchange's
trades. RVOL is therefore measured IEX-against-IEX: today's IEX volume by this
minute vs the median IEX volume by this minute over the prior 10 sessions. Like
with like, but a thin view; every forecast carries feed="iex" in its evidence,
and outcomes are graded later on consolidated (SIP) bars.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, replace
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional

from edge import catalysts as C, detectors as D, radar as R, setups as S
from edge.contracts import ET, GAP_AND_GO_V1, issue_intraday
from edge.ledger import Ledger
from edge.providers import alpaca as A

_BASE_ELIG = {"candidates": "Alpaca movers screener, top 50 gainers, every 5 min 09:45-14:30 ET",
              "feed": "IEX real-time bars (degraded); graded on SIP", "entry_window_min": 20,
              "status": "UNVALIDATED v0 hypothesis"}
CATALYST_BREAKOUT = replace(
    GAP_AND_GO_V1, name="catalyst_breakout", version=1,
    description="v0 hypothesis: catalyst + opening-range close break + time-of-day RVOL >= 2x.",
    trigger_mult=1.002, limit_mult=1.01, entry_expiry_et="14:50", time_exit_et="15:30",
    eligibility={**_BASE_ELIG, "requires": list(S.STRATEGIES["catalyst_breakout"].required)},
)
INTRADAY_CONTINUATION = replace(
    GAP_AND_GO_V1, name="intraday_continuation", version=1,
    description="v0 hypothesis: VWAP hold + acceleration + time-of-day RVOL >= 2x, no catalyst needed.",
    trigger_mult=1.002, limit_mult=1.01, entry_expiry_et="14:50", time_exit_et="15:30",
    eligibility={**_BASE_ELIG, "requires": list(S.STRATEGIES["intraday_continuation"].required)},
)
INTRADAY_SPECS = (CATALYST_BREAKOUT, INTRADAY_CONTINUATION)
_STRATEGY_OF = {CATALYST_BREAKOUT.experiment_id: "catalyst_breakout",
                INTRADAY_CONTINUATION.experiment_id: "intraday_continuation"}


def _at(day: date, hh: int, mm: int) -> int:
    return int(datetime.combine(day, dtime(hh, mm), tzinfo=ET).timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=ET).isoformat()


def _bars(rows: List[dict]) -> List[tuple]:
    out = []
    for b in rows or []:
        t = A.iso_to_epoch(b.get("t"))
        if t is not None:
            out.append((t, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b.get("v") or 0)))
    return sorted(out)


def rvol_curve(history: List[tuple], day_open: Dict[str, int]) -> Dict[int, List[float]]:
    """{minute_index: [cumulative volume at that minute, one per prior session]}."""
    per_day: Dict[str, Dict[int, float]] = {}
    for t, _o, _h, _l, _c, v in history:
        d = datetime.fromtimestamp(t, tz=ET).date().isoformat()
        if d not in day_open:
            continue
        m = (t - day_open[d]) // 60
        if 0 <= m < 390:
            per_day.setdefault(d, {})[int(m)] = per_day.get(d, {}).get(int(m), 0.0) + v
    curve: Dict[int, List[float]] = {}
    for d, mins in per_day.items():
        cum = 0.0
        for m in range(390):
            cum += mins.get(m, 0.0)
            curve.setdefault(m, []).append(cum)
    return curve


def _history(get, store, syms: List[str], day: date) -> Dict[str, Dict[int, List[float]]]:
    """IEX cumulative volume by minute, one value per prior session (up to 10), cached per day."""
    out, need = {}, []
    for s in syms:
        c = store.get("edge_rvol", f"{day.isoformat()}|{s}")
        if c is not None:
            out[s] = {int(k): v for k, v in c["by_minute"].items()}
        else:
            need.append(s)
    if need:
        start = day - timedelta(days=16)
        rows = A.bars_multi(get, need, timeframe="1Min", start=_iso(_at(start, 9, 30)),
                            end=_iso(_at(day, 0, 0)), feed="iex")
        for s in need:
            hist = _bars(rows.get(s) or [])
            days = sorted({datetime.fromtimestamp(t[0], tz=ET).date() for t in hist})[-10:]
            opens = {d.isoformat(): _at(d, 9, 30) for d in days}
            curve = rvol_curve([h for h in hist if datetime.fromtimestamp(h[0], tz=ET).date() in set(days)], opens)
            lists = {m: v for m, v in curve.items()}
            store.put("edge_rvol", f"{day.isoformat()}|{s}", {"by_minute": {str(k): v for k, v in lists.items()},
                                                              "sessions": len(days), "feed": "iex"})
            out[s] = lists
    return out


def _radar(store, day: str, sym: str, now: int, move: float) -> R.RadarItem:
    raw = store.get("edge_radar", f"{day}|{sym}")
    if raw:
        item = R.RadarItem(raw["symbol"], raw["session_date"], raw["detected_at"], raw["detected_move_pct"],
                           raw.get("strategy"), raw["state"], raw.get("history") or [])
        return item
    return R.RadarItem(sym, day, now, move)


def _save(store, item: R.RadarItem) -> None:
    store.put("edge_radar", f"{item.session_date}|{item.symbol}", asdict(item))


def tick(get, ledger: Ledger, *, now: int, top: int = 50) -> Dict[str, Any]:
    day = datetime.fromtimestamp(now, tz=ET).date()
    ds = day.isoformat()
    store = ledger.store
    if not (_at(day, 9, 45) <= now < _at(day, 14, 30)):
        return {"status": "outside_intraday_window"}
    for spec in INTRADAY_SPECS:
        ledger.register(spec, now=now)
    mv = A.movers(get, top=top)
    gainers = {str(g["symbol"]).upper(): float(g.get("percent_change") or 0)
               for g in mv.get("gainers") or [] if g.get("symbol")}
    syms = sorted(gainers)
    if not syms:
        return {"status": "no_movers"}
    today = A.bars_multi(get, syms, timeframe="1Min", start=_iso(_at(day, 9, 30)), end=_iso(now), feed="iex")
    daily_key = f"{ds}"
    daily = store.get("edge_intraday_daily", daily_key) or {}
    missing = [s for s in syms if s not in daily]
    if missing:
        rows = A.bars_multi(get, missing, timeframe="1Day", start=(day - timedelta(days=45)).isoformat())
        for s in missing:
            done = [b for b in rows.get(s) or []
                    if A.iso_to_epoch(b.get("t")) and datetime.fromtimestamp(A.iso_to_epoch(b["t"]), tz=ET).date() < day]
            vols = [float(b.get("v") or 0) for b in done[-20:]]
            daily[s] = {"avg_shares": statistics.mean(vols) if vols else None,
                        "prev_close": float(done[-1]["c"]) if done else None}
        store.put("edge_intraday_daily", daily_key, daily)
    hist = _history(get, store, syms, day)
    try:
        items = A.news(get, syms, start=_iso(now - 86_400))
    except Exception:  # noqa: BLE001 - recorded as unknown, never guessed
        items = None

    issued: List[str] = []
    counts = {spec.experiment_id: len(store.scan("forecasts", experiment_id=spec.experiment_id, session_date=ds))
              for spec in INTRADAY_SPECS}
    open_ts = _at(day, 9, 30)
    for s in syms:
        bars = _bars(today.get(s) or [])
        item = _radar(store, ds, s, now, gainers[s])
        if item.state in (R.CLOSED, R.REJECTED):
            continue
        last = bars[-1] if bars else None
        if last is None or now - (last[0] + 60) > 300:
            if item.state in (R.DETECTED, R.WATCHING, R.SETUP_FORMING):
                item.transition(R.DATA_UNAVAILABLE, ts=now, reason="no IEX print in the last 5 minutes")
            _save(store, item)
            continue
        if item.state == R.DATA_UNAVAILABLE:
            item.transition(R.WATCHING, ts=now, reason="IEX prints resumed")
        price = last[4]
        minute = int((now - open_ts) // 60) - 1
        cum_now = sum(b[5] for b in bars)
        history_at_minute = hist.get(s, {}).get(minute) or []
        ev = None
        if items is not None:
            evs = [C.make(s, str(n.get("headline") or ""), source=str(n.get("source") or ""), url=str(n.get("url") or ""),
                          published_at=A.iso_to_epoch(n.get("created_at")) or now,
                          first_seen_at=now, tickers=[x.upper() for x in n.get("symbols") or []])
                   for n in items if s in [x.upper() for x in n.get("symbols") or []]]
            ev = C.usable_at(C.dedupe(evs), s, issued_at=now)
        sig = {
            "liquidity": D.liquidity(price=price, avg_shares=(daily.get(s) or {}).get("avg_shares")),
            "catalyst": S.catalyst_signal(ev), "not_dilutive": S.dilution_signal(ev),
            "orb_break": D.orb_break(bars, open_ts, now_ts=now),
            "rvol_tod": D.rvol_time_of_day(cum_now, history_at_minute),   # UNKNOWN under 10 sessions
            "vwap_hold": D.vwap_hold(bars), "acceleration": D.acceleration(bars),
        }
        if item.state == R.DETECTED:
            item.transition(R.WATCHING, ts=now)
        best = None
        for spec in INTRADAY_SPECS:
            d = S.decide(_STRATEGY_OF[spec.experiment_id], sig)
            if d.verdict == S.ELIGIBLE and best is None:
                best = (spec, d)
        if best is None:
            if item.state == R.WATCHING and any(v.state == D.PASS for k, v in sig.items() if k != "liquidity"):
                item.transition(R.SETUP_FORMING, ts=now, strategy=None,
                                evidence={k: v.state for k, v in sig.items()})
            _save(store, item)
            continue
        spec, d = best
        eid = spec.experiment_id
        if counts[eid] >= spec.max_per_day:
            _save(store, item)
            continue
        try:
            f = issue_intraday(spec, symbol=s, session_date=day, entry_ref=price, issued_at=now,
                               evidence={"feed": "iex", "rvol": sig["rvol_tod"].value,
                                         "catalyst": sig["catalyst"].evidence.get("headline")})
        except Exception:  # noqa: BLE001 - e.g. too late in the session for the entry window
            _save(store, item)
            continue
        if store.get("forecasts", f.forecast_id):
            _save(store, item)
            continue
        ledger.record(f, now=now)
        counts[eid] += 1
        issued.append(f"{eid}:{s}")
        if item.state in (R.WATCHING, R.SETUP_FORMING):
            if item.state == R.WATCHING:
                item.transition(R.SETUP_FORMING, ts=now, strategy=_STRATEGY_OF[eid])
            item.transition(R.ENTRY_ELIGIBLE, ts=now, strategy=_STRATEGY_OF[eid],
                            evidence={"forecast_id": f.forecast_id, "price": price})
        _save(store, item)
    return {"status": "issued" if issued else "watched", "movers": len(syms), "issued": issued}


def close_day(ledger: Ledger, *, day: date, now: int) -> Dict[str, Any]:
    """After the close: anything never eligible EXPIRES, with its reason on record."""
    ds = day.isoformat()
    n = 0
    for raw in ledger.store.scan("edge_radar", session_date=ds):
        item = R.RadarItem(raw["symbol"], raw["session_date"], raw["detected_at"], raw["detected_move_pct"],
                           raw.get("strategy"), raw["state"], raw.get("history") or [])
        if item.state in (R.DETECTED, R.WATCHING, R.SETUP_FORMING, R.DATA_UNAVAILABLE):
            item.transition(R.EXPIRED, ts=now, reason="session ended without an eligible setup")
            _save(ledger.store, item)
            n += 1
    return {"status": "closed" if n else "nothing", "expired": n}
