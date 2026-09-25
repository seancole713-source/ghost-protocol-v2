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
CROWDED_SHORT_IGNITION = replace(
    GAP_AND_GO_V1, name="crowded_short_ignition", version=1,
    description="v0 hypothesis: expensive borrow + large recent short interest + time-of-day RVOL + acceleration.",
    trigger_mult=1.002, limit_mult=1.01, entry_expiry_et="14:50", time_exit_et="15:30",
    eligibility={**_BASE_ELIG, "requires": list(S.STRATEGIES["crowded_short_ignition"].required),
                 "short_data": "FINRA consolidated short interest + iBorrowDesk borrow (IBKR, unofficial)"},
)
SHORT_INTEREST_IGNITION = replace(
    GAP_AND_GO_V1, name="short_interest_ignition", version=1,
    description="v0 hypothesis: heavily shorted (days to cover >= 5, report <= 20 days old) + time-of-day "
                "RVOL + acceleration. No borrow data: NOT a crowded-short claim.",
    trigger_mult=1.002, limit_mult=1.01, entry_expiry_et="14:50", time_exit_et="15:30",
    eligibility={**_BASE_ELIG, "requires": list(S.STRATEGIES["short_interest_ignition"].required),
                 "short_data": "FINRA consolidated short interest (verified in production 2026-09-22)"},
)
INTRADAY_CONTINUATION_V2 = replace(
    INTRADAY_CONTINUATION, version=2,
    description="v0 hypothesis: intraday_continuation v1 + a dilution / reverse-split veto (rule E5) -- "
                "no offering, ATM, registered direct or split in the last 24h.",
    eligibility={**_BASE_ELIG, "requires": list(S.STRATEGIES["intraday_continuation_v2"].required)},
)
INTRADAY_SPECS = (CATALYST_BREAKOUT, INTRADAY_CONTINUATION, INTRADAY_CONTINUATION_V2,
                  CROWDED_SHORT_IGNITION, SHORT_INTEREST_IGNITION)
# A later version is recorded BESIDE the version it refines, on the same stocks at the same
# moment, so the two records compare like for like: whenever v1 is chosen for a stock, v2
# records its own forecast too if its stricter rule also passes. It is never "the" choice.
_PAIRED = {INTRADAY_CONTINUATION.experiment_id: INTRADAY_CONTINUATION_V2}
_SECONDARY = {v.experiment_id for v in _PAIRED.values()}
# Only these place PAPER orders. A paired later version shares its v1 order: same stock, same
# moment, same levels, so a second bracket would only double the paper exposure (2026-09-25:
# BENF and AESI were each bought twice). Its "actual" record is read from v1's broker order.
PAPER_SPECS = tuple(s for s in INTRADAY_SPECS if s.experiment_id not in _SECONDARY)
_STRATEGY_OF = {CATALYST_BREAKOUT.experiment_id: "catalyst_breakout",
                INTRADAY_CONTINUATION.experiment_id: "intraday_continuation",
                INTRADAY_CONTINUATION_V2.experiment_id: "intraday_continuation_v2",
                CROWDED_SHORT_IGNITION.experiment_id: "crowded_short_ignition",
                SHORT_INTEREST_IGNITION.experiment_id: "short_interest_ignition"}


def _short_data(http, store, syms: List[str], day: date, *, max_borrow_calls: int = 10) -> Dict[str, Dict[str, Any]]:
    """Per-symbol short interest + borrow, cached per day. Anything unfetched stays unknown."""
    from edge.providers import shortdata as SD
    ds = day.isoformat()
    cache = store.get("edge_short", ds) or {}
    if http is None:
        return cache
    need_si = [s for s in syms if s not in cache or "si" not in cache[s]]
    if need_si:
        try:
            rows = SD.fetch_finra_si_for(http, need_si)
        except Exception as exc:  # noqa: BLE001 - recorded, never guessed
            rows = None
            store.put("edge_short_errors", ds, {"finra": f"{type(exc).__name__}: {str(exc)[:120]}"})
        for s in need_si:
            cache.setdefault(s, {})["si"] = (rows or {}).get(s) if rows is not None else None
    calls = 0
    for s in syms:
        if "borrow" in cache.get(s, {}):
            continue
        if calls >= max_borrow_calls:
            break
        try:
            cache.setdefault(s, {})["borrow"] = SD.fetch_borrow(http, s)
        except Exception:  # noqa: BLE001
            cache.setdefault(s, {})["borrow"] = None
        calls += 1
    store.put("edge_short", ds, cache)
    return cache


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


HISTORY_CHUNK = 10      # symbols per 16-day minute-bar request: SIP has far more bars than IEX
HISTORY_MAX_PAGES = 40


def _history(get, store, syms: List[str], day: date, feed: str = "iex") -> Dict[str, Dict[int, List[float]]]:
    """Cumulative volume by minute on `feed`, one value per prior session (up to 10), cached per day.
    Same feed as today's bars, always: RVOL compares like with like.

    Requested HISTORY_CHUNK symbols at a time. One request for ~80 symbols hit the page cap on SIP,
    and the symbols paged last came back short or empty -- kept all day, so they could never reach
    10 sessions of RVOL (audit 2026-09-25). A truncated answer is never cached or used: a truncated
    chunk is re-asked one symbol at a time, and a symbol still truncated is left ABSENT (RVOL
    unknown this tick, never inflated by a short history) and re-asked on the next tick."""
    out, need = {}, []
    for s in syms:
        c = store.get("edge_rvol", f"{day.isoformat()}|{s}")
        if c is not None and c.get("feed", "iex") == feed and c.get("complete", True):
            out[s] = {int(k): v for k, v in c["by_minute"].items()}
        else:
            need.append(s)
    start, end = _iso(_at(day - timedelta(days=16), 9, 30)), _iso(_at(day, 0, 0))

    def fetch(group: List[str]):
        return A.bars_pages(get, group, timeframe="1Min", start=start, end=end, feed=feed,
                            max_pages=HISTORY_MAX_PAGES)

    for i in range(0, len(need), HISTORY_CHUNK):
        chunk = need[i:i + HISTORY_CHUNK]
        rows, complete = fetch(chunk)
        if complete:
            whole = {s: rows.get(s) or [] for s in chunk}
        else:
            A.LOG.warning("rvol history truncated for %s; re-asking one symbol at a time", ",".join(chunk))
            whole = {}
            for s in chunk:
                one, ok = fetch([s])
                if ok:
                    whole[s] = one.get(s) or []
                else:
                    A.LOG.warning("rvol history for %s still truncated; left out this tick (RVOL unknown)", s)
        for s, raw in whole.items():
            hist = _bars(raw)
            days = sorted({datetime.fromtimestamp(t[0], tz=ET).date() for t in hist})[-10:]
            opens = {d.isoformat(): _at(d, 9, 30) for d in days}
            curve = rvol_curve([h for h in hist if datetime.fromtimestamp(h[0], tz=ET).date() in set(days)], opens)
            lists = {m: v for m, v in curve.items()}
            store.put("edge_rvol", f"{day.isoformat()}|{s}", {"by_minute": {str(k): v for k, v in lists.items()},
                                                              "sessions": len(days), "feed": feed,
                                                              "complete": True})
            out[s] = lists
    return out


QUALITY_MIN_PCT, QUALITY_MAX = 5.0, 30


def _quality_lane(get, store, *, now: int, known: set, universe) -> Dict[str, float]:
    """Liquid movers the top-gainers screener never shows. 2026-09-24: the top 50 by % were
    +20-200% sub-$5 names, so NBIS, QMCO, WRBY, TWST (+8-15% on real volume) were never seen.
    The most-active list (by volume) is priced from snapshots; a common stock at $2-$500
    up >= 5% on the day joins the radar. The strategies' own rules still decide everything."""
    from edge import feeds as FD
    from edge import premarket as PM
    try:
        act = A.most_actives(get, top=100)
        syms = [str(a["symbol"]).upper() for a in act["most_actives"] if a.get("symbol")]
        syms = [x for x in syms if x not in known and PM.common_stock(x, universe)]
        snaps = A.snapshots(get, syms, feed=FD.live_feed(get, store, now=now)) if syms else {}
    except Exception:  # noqa: BLE001 - the lane is additive; the gainers screener still runs
        return {}
    out = {}
    for x in syms:
        snap = snaps.get(x) or {}
        px = ((snap.get("latestTrade") or {}).get("p"))
        prev = ((snap.get("prevDailyBar") or {}).get("c"))
        if not px or not prev:
            continue
        pct = (float(px) / float(prev) - 1) * 100
        if pct >= QUALITY_MIN_PCT and 2.0 <= float(px) <= 500.0:
            out[x] = round(pct, 2)
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:QUALITY_MAX])


def _radar(store, day: str, sym: str, now: int, move: float) -> R.RadarItem:
    raw = store.get("edge_radar", f"{day}|{sym}")
    if raw:
        return R.RadarItem.from_row(raw)
    return R.RadarItem(sym, day, now, move)


def _save(store, item: R.RadarItem) -> None:
    store.put("edge_radar", f"{item.session_date}|{item.symbol}", asdict(item))


def tick(get, ledger: Ledger, *, now: int, top: int = 50, http=None) -> Dict[str, Any]:
    day = datetime.fromtimestamp(now, tz=ET).date()
    ds = day.isoformat()
    store = ledger.store
    if not (_at(day, 9, 45) <= now < _at(day, 14, 30)):
        return {"status": "outside_intraday_window"}
    for spec in INTRADAY_SPECS:
        ledger.register(spec, now=now)
    mv = A.movers(get, top=top)
    from edge import premarket as PM, universe as U
    uni = U.symbols_as_of(ledger.store, day.isoformat())
    gainers = {str(g["symbol"]).upper(): float(g.get("percent_change") or 0)
               for g in mv.get("gainers") or []
               if g.get("symbol") and PM.common_stock(str(g["symbol"]), uni)}   # no warrants/rights (E6)
    quality = _quality_lane(get, store, now=now, known=set(gainers), universe=uni)
    gainers.update(quality)
    syms = sorted(gainers)
    if not syms:
        return {"status": "no_movers"}
    from edge import stream as ST
    live = ST.maybe_start()              # None unless EDGE_STREAM_ENABLED
    if live is not None:
        ST.watch(syms)
    from edge import feeds as FD
    feed = FD.live_feed(get, store, now=now)     # IEX on the free plan; SIP once it is paid for
    today = A.bars_multi(get, syms, timeframe="1Min", start=_iso(_at(day, 9, 30)), end=_iso(now), feed=feed)
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
    hist = _history(get, store, syms, day, feed)
    shorts = _short_data(http, store, syms, day)
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
        streamed = live.price(s, now=now) if live is not None else None
        if streamed is not None and streamed["ts"] > last[0] + 60:
            price = streamed["price"]     # fresher than the last polled bar
        minute = int((now - open_ts) // 60) - 1
        cum_now = sum(b[5] for b in bars)
        history_at_minute = hist.get(s, {}).get(minute) or []
        ev = None
        if items is not None:
            evs = [C.make(s, str(n.get("headline") or ""), source=str(n.get("source") or ""), url=str(n.get("url") or ""),
                          published_at=A.iso_to_epoch(n.get("created_at")) or now,
                          first_seen_at=now, tickers=[x.upper() for x in n.get("symbols") or []])
                   for n in items if s in [x.upper() for x in n.get("symbols") or []]
                   and len(n.get("symbols") or []) <= C.MAX_STORY_TICKERS]      # no market wraps (E4)
            ev = C.usable_at(C.dedupe(evs), s, issued_at=now)
        sig = {
            "liquidity": D.liquidity(price=price, avg_shares=(daily.get(s) or {}).get("avg_shares")),
            "catalyst": S.catalyst_signal(ev), "not_dilutive": S.dilution_signal(ev),
            "orb_break": D.orb_break(bars, open_ts, now_ts=now),
            "rvol_tod": D.rvol_time_of_day(cum_now, history_at_minute),   # UNKNOWN under 10 sessions
            "vwap_hold": D.vwap_hold(bars), "acceleration": D.acceleration(bars),
            "crowded_short": _crowded(shorts.get(s) or {}, day),
            "short_interest": _short_interest(shorts.get(s) or {}, day),
        }
        if item.state == R.DETECTED:
            item.transition(R.WATCHING, ts=now)
        if item.catalyst is None and sig["catalyst"].state == D.PASS:
            item.catalyst = {"kind": sig["catalyst"].evidence.get("kind"),
                             "headline": str(sig["catalyst"].evidence.get("headline") or "")[:160], "at": now}
        best, closest = None, None
        for spec in INTRADAY_SPECS:
            if spec.experiment_id in _SECONDARY:
                continue
            d = S.decide(_STRATEGY_OF[spec.experiment_id], sig)
            if d.verdict == S.ELIGIBLE and best is None:
                best = (spec, d)
            # The closest miss: fewest failing + missing inputs; ties keep the spec order.
            if closest is None or len(d.reasons) + len(d.missing) < len(closest.reasons) + len(closest.missing):
                closest = d
        undecided = item.state in (R.WATCHING, R.SETUP_FORMING)     # no forecast on this name yet
        if best is None and closest is not None and undecided:
            item.set_blocker(strategy=closest.strategy, verdict=closest.verdict,
                             reasons=closest.reasons + [f"missing {m}" for m in closest.missing], ts=now)
        if best is None:
            if item.state == R.WATCHING and any(v.state == D.PASS for k, v in sig.items() if k != "liquidity"):
                item.transition(R.SETUP_FORMING, ts=now, strategy=None,
                                evidence={k: v.state for k, v in sig.items()})
            _save(store, item)
            continue
        spec, d = best
        eid = spec.experiment_id

        def _record(sp):
            if counts[sp.experiment_id] >= sp.max_per_day:
                return None
            try:
                fc = issue_intraday(sp, symbol=s, session_date=day, entry_ref=price, issued_at=now,
                                    evidence={"feed": feed, "rvol": sig["rvol_tod"].value,
                                              "catalyst": sig["catalyst"].evidence.get("headline")})
            except Exception:  # noqa: BLE001 - e.g. too late in the session for the entry window
                return None
            if store.get("forecasts", fc.forecast_id):
                return None
            ledger.record(fc, now=now)
            counts[sp.experiment_id] += 1
            issued.append(f"{sp.experiment_id}:{s}")
            return fc

        pair = _PAIRED.get(eid)
        if pair is not None and S.decide(_STRATEGY_OF[pair.experiment_id], sig).verdict == S.ELIGIBLE:
            _record(pair)
        f = _record(spec)
        if f is None:
            if undecided:
                item.set_blocker(strategy=_STRATEGY_OF[eid], verdict=S.ELIGIBLE, ts=now,
                                 reasons=["eligible, but no forecast recorded (daily cap, entry window or already recorded)"])
            _save(store, item)
            continue
        item.blocker = None
        if item.state in (R.WATCHING, R.SETUP_FORMING):
            if item.state == R.WATCHING:
                item.transition(R.SETUP_FORMING, ts=now, strategy=_STRATEGY_OF[eid])
            item.transition(R.ENTRY_ELIGIBLE, ts=now, strategy=_STRATEGY_OF[eid],
                            evidence={"forecast_id": f.forecast_id, "price": price})
        _save(store, item)
    return {"status": "issued" if issued else "watched", "movers": len(syms), "issued": issued,
            "quality_lane": sorted(quality)}


def close_day(ledger: Ledger, *, day: date, now: int) -> Dict[str, Any]:
    """After the close: anything never eligible EXPIRES, with its reason on record."""
    ds = day.isoformat()
    n = 0
    for raw in ledger.store.scan("edge_radar", session_date=ds):
        item = R.RadarItem.from_row(raw)
        if item.state in (R.DETECTED, R.WATCHING, R.SETUP_FORMING, R.DATA_UNAVAILABLE):
            item.transition(R.EXPIRED, ts=now, reason=expiry_reason(item))
            _save(ledger.store, item)
            n += 1
    return {"status": "closed" if n else "nothing", "expired": n}


EXPIRY_BASE = "session ended without an eligible setup"


def expiry_reason(item: R.RadarItem) -> str:
    """The EXPIRED reason carries the last thing that blocked the name, not one line for all."""
    last = item.history[-1] if item.history else {}
    if item.state == R.DATA_UNAVAILABLE and last.get("reason"):
        return f"{EXPIRY_BASE}; last blocker: data unavailable ({last['reason']})"
    why = item.blocker_text()
    return f"{EXPIRY_BASE}; last blocker: {why}" if why else EXPIRY_BASE


def _crowded(rec: Dict[str, Any], day: date) -> D.Signal:
    from edge.providers import shortdata as SD
    si, bw = rec.get("si"), rec.get("borrow")
    return D.crowded_short(
        borrow_fee_pct=(bw or {}).get("borrow_fee_pct"), available_shares=(bw or {}).get("available_shares"),
        short_interest_pct_float=None, days_to_cover=(si or {}).get("days_to_cover"),
        short_interest_age_days=SD.si_age_days((si or {}).get("settlement_date"), day) if si else None)


def _short_interest(rec: Dict[str, Any], day: date) -> D.Signal:
    from edge.providers import shortdata as SD
    si = rec.get("si")
    return D.high_short_interest(
        days_to_cover=(si or {}).get("days_to_cover"),
        short_interest_age_days=SD.si_age_days((si or {}).get("settlement_date"), day) if si else None)
