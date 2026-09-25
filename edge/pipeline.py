"""The shadow pipeline: edge running every market day, forecasting without trading.

Why now, before any data is bought: evidence only accrues in calendar time.
Every day this runs is a day of timestamped forecasts, graded outcomes and
miss reviews that no amount of later building can backfill honestly.

Three steps, each idempotent (safe to re-run; a marker stops repeats):
  07:00-09:04 ET  miss_review     the PREVIOUS session's movers vs what that
                                  morning's radar saw -- full market
  09:05-09:28 ET  morning_card    candidates -> signals -> decisions; forecasts
                                  and abstentions RECORDED before the open
  16:20-20:00 ET  resolve_day     grade from consolidated minute bars:
                                  "forecast" and "simulated" records; then the
                                  control arm grades every radar name once
                                  (edge/control.py, docs/control_arm_v1.md)

Why the miss review waits for the next morning: this account's Polygon plan is
not entitled to a session's grouped-daily bar while that session is still the
current day (403), and IS for completed sessions (probe, 2026-09-22: D-1 answered
12,626 tickers). Asking for today at 16:20 would fall back to a 50-name view
every single day.

It runs its OWN experiment, gap_and_go_auto@v1: the operator's Gap-and-Go v1
levels with a keyword catalyst check instead of human-plus-Claude research.
It is a different rule and keeps a different record -- never merged with the
operator's scoreboard.

Free-plan data it uses (Alpaca): movers screener, IEX snapshots (premarket
reference prices -- one exchange, degraded and labelled so), SIP daily/minute
bars older than 15 min, news. Where a price is missing the name is recorded
DATA_UNAVAILABLE -- which is itself evidence for the purchase decision.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import replace
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional

from edge import catalysts as C, detectors as D, health as H, miss_audit as M, setups as S
from edge import universe as U
from edge.contracts import ET, GAP_AND_GO_V1, TERMINAL, issue
from edge.ledger import Ledger
from edge.providers import alpaca as A
from edge.resolver import resolve_execution, resolve_market

GAP_AND_GO_AUTO = replace(
    GAP_AND_GO_V1, name="gap_and_go_auto",
    description="Gap-and-Go v1 levels, fully automated: keyword_v1 catalyst check on Alpaca "
                "news; IEX premarket reference price <=30 min old or no forecast.",
    eligibility={**GAP_AND_GO_V1.eligibility,
                 "catalyst": "keyword_v1 company-specific kind on Alpaca news <24h",
                 "reference_price": "IEX latest trade, current session, <=30 min old",
                 "candidates": "Alpaca movers screener, top 50 gainers"},
)
SPEC = GAP_AND_GO_AUTO
EID = SPEC.experiment_id

# The baseline the critique demanded: identical levels, gap and liquidity, and
# NO catalyst check at all. If gap_and_go_auto cannot beat this, its catalyst
# filter is not adding edge -- it is only reducing the sample.
GAP_BASELINE = replace(
    GAP_AND_GO_V1, name="gap_baseline",
    description="Baseline: Gap-and-Go v1 levels on any liquid +5..40% premarket gapper, "
                "no catalyst or dilution check.",
    eligibility={**{k: v for k, v in GAP_AND_GO_V1.eligibility.items() if k not in ("catalyst", "exclude")},
                 "catalyst": "NOT CHECKED (baseline)",
                 "reference_price": "IEX latest trade, current session, <=30 min old",
                 "candidates": "Alpaca movers screener, top 50 gainers"},
)
BASE_EID = GAP_BASELINE.experiment_id

# The same frozen levels, but the catalyst must be a Claude research claim --
# cited, reviewed by a second pass, and made BEFORE the card. Registered only
# when the research worker is on. It answers: does AI research beat the keyword
# tagger and the no-catalyst baseline?
GAP_VERIFIED = replace(
    GAP_AND_GO_V1, name="gap_and_go_verified",
    description="Gap-and-Go v1 levels; catalyst and dilution judged from reviewed, cited Claude research "
                "made before the card (edge/research_worker.py).",
    eligibility={**GAP_AND_GO_V1.eligibility,
                 "catalyst": "reviewed Claude research claim, company-specific, made before 09:05 ET",
                 "reference_price": "IEX latest trade, current session, <=30 min old",
                 "candidates": "Alpaca movers screener, top 50 gainers"},
)
VERIFIED_EID = GAP_VERIFIED.experiment_id
EXPERIMENTS = (SPEC, GAP_BASELINE, GAP_VERIFIED)

# What each experiment needs to be released LIVE. Shadow records regardless;
# the banner is what a live release would have said, kept beside the record.
REQUIRES = {EID: ["movers", "quotes_iex", "news"], BASE_EID: ["movers", "quotes_iex"]}


def _research():
    from edge import research_worker
    return research_worker


def _et(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=ET)


def _at(day: date, hh: int, mm: int) -> int:
    return int(datetime.combine(day, dtime(hh, mm), tzinfo=ET).timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=ET).isoformat()


def trading_day(day: date, store=None, now: Optional[int] = None) -> bool:
    """Weekdays that are not NYSE holidays (Alpaca's calendar when stored, else a built-in
    NYSE table) and not in EDGE_HOLIDAYS."""
    from edge import calendar as CAL
    return CAL.session(day, store, now)["trading"]


def previous_trading_day(day: date, store=None) -> date:
    d = day - timedelta(days=1)
    while not trading_day(d, store):
        d -= timedelta(days=1)
    return d


EARLY_CLOSE_NOTE = ("early close (13:00 ET): the frozen rule's 15:30 ET time exit falls after the close, "
                    "so no forecasts are issued today rather than bend the rule")


def _daily_stats(bars: List[dict], day: date) -> Dict[str, Optional[float]]:
    """prev_close from the last COMPLETED session before `day`; 20-day averages."""
    done = []
    for b in bars:
        ts = A.iso_to_epoch(b.get("t"))
        if ts is None:
            continue
        if _et(ts).date() < day:
            done.append(b)
    if not done:
        return {"prev_close": None, "avg_shares": None, "avg_dollars": None}
    last20 = done[-20:]
    vols = [float(b.get("v") or 0) for b in last20]
    dollars = [float(b.get("v") or 0) * float(b.get("vw") or b.get("c") or 0) for b in last20]
    return {"prev_close": float(done[-1]["c"]),
            "avg_shares": statistics.mean(vols) if vols else None,
            "avg_dollars": statistics.mean(dollars) if dollars else None}


def _reference(snap: Optional[dict], *, now: int, day: date) -> Dict[str, Any]:
    """A premarket price is usable only if printed THIS session and <=30 min ago."""
    trade = (snap or {}).get("latestTrade") or {}
    ts = A.iso_to_epoch(trade.get("t"))
    px = trade.get("p")
    if ts is None or px is None:
        return {"price": None, "ts": None, "why": "no IEX print"}
    if ts < _at(day, 4, 0):
        return {"price": None, "ts": ts, "why": "last IEX print is from a prior session"}
    if now - ts > 1800:
        return {"price": None, "ts": ts, "why": f"IEX print {int((now - ts) / 60)} min old"}
    return {"price": float(px), "ts": ts, "why": ""}


def _events(items: Optional[List[dict]], symbols: set, now: int) -> Optional[Dict[str, List[C.CatalystEvent]]]:
    if items is None:
        return None
    out: Dict[str, List[C.CatalystEvent]] = {s: [] for s in symbols}
    for n in items:
        pub = A.iso_to_epoch(n.get("created_at"))
        if pub is None:
            continue
        tick = [t.upper() for t in (n.get("symbols") or [])]
        if len(tick) > C.MAX_STORY_TICKERS:
            continue     # a market wrap ("Crude Oil Down...; Thor Shares Gain...") is not a company catalyst
        for sym in set(tick) & symbols:
            out[sym].append(C.make(sym, str(n.get("headline") or ""), source=str(n.get("source") or "alpaca"),
                                   url=str(n.get("url") or ""), published_at=pub, first_seen_at=now,
                                   tickers=tick))
    return {s: C.dedupe(v) for s, v in out.items()}


def morning_card(get, ledger: Ledger, *, now: int, top: int = 50) -> Dict[str, Any]:
    day = _et(now).date()
    store = ledger.store
    if not trading_day(day, store, now):
        return {"status": "market_closed", "day": day.isoformat()}
    if store.get("edge_cards", day.isoformat()):
        return {"status": "already_issued", "day": day.isoformat()}
    if not (_at(day, 9, 5) <= now < _at(day, 9, 28)):
        return {"status": "outside_card_window", "day": day.isoformat()}
    from edge import calendar as CAL
    if CAL.session(day, store, now)["early_close"]:
        store.put("edge_cards", day.isoformat(), {
            "day": day.isoformat(), "issued_at": now, "experiment_id": EID, "status": "early_close",
            "candidates": 0, "priced": 0, "eligible": 0, "forecasts": [], "baseline_forecasts": [],
            "health_banner": EARLY_CLOSE_NOTE, "rows": []})
        return {"status": "early_close", "day": day.isoformat(), "note": EARLY_CLOSE_NOTE}
    research_on = _research().enabled()
    for spec in EXPERIMENTS:
        if spec is GAP_VERIFIED and not research_on:
            continue
        ledger.register(spec, now=now)
    from edge import features as FX, models as MD
    model = MD.current(store)            # (artifact, spec) only if a model QUALIFIED out of sample
    if model is not None:
        ledger.register(model[1], now=now)

    from edge import premarket as PM
    cand = PM.candidates(get, store, day=day, now=now, top=top)   # today's scanned gappers + the screener
    updated = cand["movers_last_updated"]
    syms = sorted(set(cand["symbols"]))
    source_errors: Dict[str, str] = dict(cand["errors"])   # a failed source is named on the card, never "no data"
    try:
        daily = A.bars_multi(get, syms, timeframe="1Day", start=(day - timedelta(days=45)).isoformat())
    except Exception as exc:  # noqa: BLE001 - no prior close, no gap: named, and the card retries
        daily, source_errors["daily_bars"] = {}, f"{type(exc).__name__}: {str(exc)[:160]}"
    from edge import feeds as FD
    feed = FD.live_feed(get, store, now=now)       # IEX on the free plan; SIP once it is paid for
    try:
        snaps = A.snapshots(get, syms, feed=feed)
    except Exception as exc:  # noqa: BLE001 - recorded as missing, never guessed
        snaps, source_errors[f"snapshots_{feed}"] = {}, f"{type(exc).__name__}: {str(exc)[:160]}"
    try:
        items = A.news(get, syms, start=_iso(now - 86_400))
    except Exception as exc:  # noqa: BLE001
        items, source_errors["news"] = None, f"{type(exc).__name__}: {str(exc)[:160]}"
    events = _events(items, set(syms), now)
    sip_pre = {}
    if model is not None:
        # Same bars, same cutoff, same builder as training: no train/serve skew.
        try:
            sip_pre = A.bars_multi(get, syms, timeframe="1Min", start=_iso(_at(day, 4, 0)),
                                   end=_iso(_at(day, *FX.CUTOFF)), feed="sip")
        except Exception as exc:  # noqa: BLE001 - no features -> the model abstains, recorded
            sip_pre, source_errors["sip_premarket_bars"] = {}, f"{type(exc).__name__}: {str(exc)[:160]}"

    rows, eligible = [], []
    for sym in syms:
        st = _daily_stats(daily.get(sym) or [], day)
        ref = _reference(snaps.get(sym), now=now, day=day)
        ev = None if events is None else events.get(sym, [])
        usable = None if ev is None else C.usable_at(ev, sym, issued_at=now)
        signals = {
            "gap": D.gap(st["prev_close"], ref["price"]) if ref["price"] else
                   D.Signal("gap", D.UNKNOWN, evidence={"missing": ref["why"]}),
            "liquidity": D.liquidity(price=ref["price"] or st["prev_close"], avg_shares=st["avg_shares"]),
            "catalyst": S.catalyst_signal(usable),
            "not_dilutive": S.dilution_signal(usable),
        }
        d = S.decide("premarket_continuation", signals)
        b = S.decide("gap_baseline", signals)
        v = None
        if research_on:
            rv = _research().verdict(store, day=day.isoformat(), symbol=sym, issued_at=now)
            vsig = dict(signals)
            nr = rv.get("not_researched")
            vsig["catalyst"] = (D.Signal("catalyst", D.UNKNOWN, evidence={
                                    "missing": f"not researched ({nr})" if nr else "not researched before the card"})
                                if rv["catalyst"] is None else
                                D.Signal("catalyst", D.PASS if rv["catalyst"] else D.FAIL,
                                         evidence={"headline": rv.get("headline"),
                                                   "reasons": [] if rv["catalyst"] else
                                                   ["research found no reviewed company-specific catalyst"]}))
            vsig["not_dilutive"] = (D.Signal("not_dilutive", D.UNKNOWN, evidence={"missing": "not researched"})
                                    if rv["dilutive"] is None else
                                    D.Signal("not_dilutive", D.FAIL if rv["dilutive"] else D.PASS,
                                             evidence={"reasons": ["research found dilution"]} if rv["dilutive"] else {}))
            v = S.decide("premarket_continuation", vsig)
        inputs = {"prev_close": st["prev_close"], "avg_shares": st["avg_shares"], "ref_price": ref["price"],
                  "ref_why": ref["why"],
                  "events": None if usable is None else [
                      {"headline": e.headline, "kind": e.kind, "published_at": e.published_at,
                       "first_seen_at": e.first_seen_at, "url": e.url} for e in usable]}
        model_prob = None
        if model is not None:
            fx = FX.build(day=day, prev_close=st["prev_close"], avg_shares=st["avg_shares"],
                          avg_dollars=st["avg_dollars"], sip_bars=_minute_bars(sip_pre.get(sym) or []),
                          events=usable)
            model_prob = MD.predict(model[0], fx)
            inputs["model_features"] = fx
        row = {"symbol": sym, "verdict": d.verdict, "reasons": d.reasons, "missing": d.missing,
               "inputs": inputs, "model_prob": model_prob,
               "baseline_verdict": b.verdict, "baseline_reasons": b.reasons, "baseline_missing": b.missing,
               "verified_verdict": v.verdict if v else None,
               "research_status": (None if not research_on else
                                   "not_researched" if (rv.get("not_researched") or rv["catalyst"] is None)
                                   else "researched"),
               "verified_reasons": v.reasons if v else [], "verified_missing": v.missing if v else [],
               "ref_price": ref["price"], "ref_ts": ref["ts"], "prev_close": st["prev_close"],
               "avg_dollars": st["avg_dollars"],
               "catalyst": signals["catalyst"].evidence.get("headline")}
        rows.append(row)
        if d.verdict == S.ELIGIBLE:
            eligible.append(row)

    priced = sum(1 for r in rows if r["ref_price"])
    failures = data_failures(source_errors, cand.get("scan") or {}, rows, priced)
    attempts = store.get("edge_card_attempts", day.isoformat()) or {"day": day.isoformat(), "attempts": []}
    if failures:
        # One bad data minute must not cost the day: nothing is stored and nothing is issued, so
        # the next 5-minute tick tries again. From 09:23 ET (the window's last tick) the card is
        # issued with what there is, the failed sources named on it.
        attempts["attempts"] = (attempts["attempts"] + [{"at": now, "failures": failures}])[-20:]
        store.put("edge_card_attempts", day.isoformat(), attempts)
        if now < _at(day, *CARD_LAST_TRY):
            return {"status": "retry_pending", "day": day.isoformat(), "failures": failures,
                    "candidates": len(rows), "priced": priced, "source_errors": source_errors,
                    "note": "card data failed by an error; nothing stored or issued, retried next tick "
                            f"and issued regardless from {CARD_LAST_TRY[0]:02d}:{CARD_LAST_TRY[1]:02d} ET"}

    chosen = _issue_top(ledger, SPEC, rows, eligible, day=day, now=now,
                        verdict_key="verdict", reasons_key="reasons", missing_key="missing", id_key="forecast_id")
    base_eligible = [r for r in rows if r["baseline_verdict"] == S.ELIGIBLE]
    base_chosen = _issue_top(ledger, GAP_BASELINE, rows, base_eligible, day=day, now=now,
                             verdict_key="baseline_verdict", reasons_key="baseline_reasons",
                             missing_key="baseline_missing", id_key="baseline_forecast_id")
    model_chosen = []
    if model is not None and ledger.store.scan("forecasts", experiment_id=model[1].experiment_id,
                                               session_date=day.isoformat()):
        model = None           # recorded by an earlier tick that died before the card; never twice
    if model is not None:
        mspec = model[1]
        gap_ok = [r for r in rows if r["baseline_verdict"] == S.ELIGIBLE and r["model_prob"] is not None
                  and r["model_prob"] >= mspec.min_prob]
        gap_ok.sort(key=lambda r: -r["model_prob"])
        model_chosen = gap_ok[:mspec.max_per_day]
        chosen_syms = {r["symbol"] for r in model_chosen}
        for r in model_chosen:
            f = issue(mspec, symbol=r["symbol"], session_date=day, entry_ref=r["ref_price"], issued_at=now,
                      prob=r["model_prob"], evidence={"model_sha": mspec.eligibility["model_sha"]})
            ledger.record(f, now=now)
            r["model_forecast_id"] = f.forecast_id
        for r in rows:
            if r["symbol"] in chosen_syms:
                continue
            why = (r["baseline_reasons"] or r["baseline_missing"] or
                   (["model could not score it (incomplete features)"] if r["model_prob"] is None else
                    [f"model probability {r['model_prob']:.2f} below {mspec.min_prob:.2f}"]
                    if r["model_prob"] < mspec.min_prob else ["ranked below the top 2 by model probability"]))
            ledger.abstain(experiment_id=mspec.experiment_id, symbol=r["symbol"], session_date=day.isoformat(),
                           reasons=list(why), now=now)
    ver_chosen = []
    if research_on:
        ver_chosen = _issue_top(ledger, GAP_VERIFIED, rows,
                                [r for r in rows if r["verified_verdict"] == S.ELIGIBLE], day=day, now=now,
                                verdict_key="verified_verdict", reasons_key="verified_reasons",
                                missing_key="verified_missing", id_key="verified_forecast_id")

    health = H.assess([
        H.SourceHealth("movers", now if cand["scan"].get("priced") else updated, 900),
        H.SourceHealth("quotes_iex", now if rows else None, 1800, covered=priced, expected=len(rows) or None,
                       min_coverage=0.5),
        H.SourceHealth("news", now if items is not None else None, 1800,
                       error=None if items is not None else "news request failed"),
    ], now)
    blocking = {eid: H.release_allowed(req, health) for eid, req in REQUIRES.items()}
    card = {"day": day.isoformat(), "issued_at": now, "experiment_id": EID,
            "candidates": len(rows), "priced": priced, "eligible": len(eligible),
            "forecasts": [r["symbol"] for r in chosen],
            "trades": _trades(store, chosen),
            "baseline_forecasts": [r["symbol"] for r in base_chosen],
            "verified_forecasts": [r["symbol"] for r in ver_chosen] if research_on else None,
            "model_forecasts": [r["symbol"] for r in model_chosen] if model is not None else None,
            "model_experiment": model[1].experiment_id if model is not None else None,
            "movers_last_updated": updated, "movers_stale": cand["movers_stale"],
            "premarket_scan": cand["scan"], "premarket_scan_top": cand["scan_top"],
            "dropped_non_common": cand["dropped_non_common"],
            "live_feed": feed,
            "coverage_note": f"{priced}/{len(rows)} candidates had a fresh {feed.upper()} premarket price",
            "health": health, "health_banner": H.banner(len(chosen), {EID: blocking[EID]}),
            "source_errors": source_errors, "movers_count": len(syms),
            "degraded": bool(failures), "data_failures": failures,
            "failed_attempts": sum(1 for a in attempts["attempts"] if a.get("at") != now),
            "health_note": "shadow records regardless; the banner is what a LIVE release would say",
            "backtest_note": backtest_note(store),
            "rows": rows}
    from edge import top10 as T10
    t10 = T10.build(card)                     # learning list, never an order; graded after the close
    card["top10"] = [x["symbol"] for x in t10["list"]]
    card["top10_ranked"] = [{k: x.get(k) for k in ("rank", "symbol", "score")} for x in t10["list"]]
    store.put("edge_cards", day.isoformat(), card)
    store.put("edge_top10", day.isoformat(), t10)
    return {"status": "issued", **{k: card[k] for k in (
        "day", "candidates", "priced", "eligible", "forecasts", "baseline_forecasts",
        "coverage_note", "health_banner", "source_errors")}}


CARD_LAST_TRY = (9, 23)      # the window's last 5-minute tick: from here a card with failed data is issued


def data_failures(source_errors: Dict[str, str], scan: Dict[str, Any], rows: List[dict],
                  priced: int) -> List[str]:
    """Why this tick's card data is broken by an ERROR, not merely thin: a failed source, failed
    premarket-scan batches, candidates but no prior close for any, or candidates but no price for
    any. A thin-but-valid card (e.g. 7/26 priced because IEX prints are stale) is not a failure."""
    out = [f"{k}: {v}" for k, v in sorted(source_errors.items())]
    batches = int(scan.get("batch_errors") or 0)
    if batches:
        out.append(f"premarket_scan: {batches} snapshot batch(es) failed")
    if rows and not any(r.get("prev_close") for r in rows):
        out.append(f"no prior close for any of {len(rows)} candidates")
    if rows and not priced:
        out.append(f"no premarket price for any of {len(rows)} candidates")
    return out


def _trades(store, chosen: List[dict]) -> List[Dict[str, Any]]:
    """The phone card's trade lines, copied from the RECORDED forecasts (the ledger's levels)."""
    out = []
    for r in chosen:
        f = store.get("forecasts", r.get("forecast_id")) or {}
        out.append({"symbol": r["symbol"], "ref_price": r.get("ref_price"), "prev_close": r.get("prev_close"),
                    "catalyst": r.get("catalyst"),
                    **{k: f.get(k) for k in ("entry_trigger", "entry_limit", "target", "stop", "shares",
                                             "entry_expiry", "time_exit")}})
    return out


def _issue_top(ledger: Ledger, spec, rows, eligible, *, day: date, now: int, verdict_key: str,
               reasons_key: str, missing_key: str, id_key: str) -> List[dict]:
    """Record the top N eligible by dollar volume; an abstention with reasons for the rest."""
    prior = {f["symbol"]: f for f in ledger.store.scan("forecasts", experiment_id=spec.experiment_id,
                                                         session_date=day.isoformat())}
    if prior:
        # An earlier tick recorded this experiment's forecasts but died before writing the
        # card. Forecasts are immutable: keep them, never issue a second set.
        chosen = [r for r in rows if r["symbol"] in prior]
        for r in chosen:
            r[id_key] = prior[r["symbol"]]["forecast_id"]
    else:
        eligible = sorted(eligible, key=lambda r: -(r["avg_dollars"] or 0))
        chosen = eligible[:spec.max_per_day]
        for r in chosen:
            f = issue(spec, symbol=r["symbol"], session_date=day, entry_ref=r["ref_price"], issued_at=now,
                      evidence={k: r[k] for k in ("prev_close", "avg_dollars", "catalyst", "ref_ts")})
            ledger.record(f, now=now)
            r[id_key] = f.forecast_id
    chosen_syms = {r["symbol"] for r in chosen}
    for r in rows:
        if r["symbol"] in chosen_syms:
            continue
        why = r[reasons_key] or r[missing_key] or ["eligible, ranked below the top 2 by dollar volume"]
        ledger.abstain(experiment_id=spec.experiment_id, symbol=r["symbol"],
                       session_date=day.isoformat(), reasons=list(why), now=now)
    return chosen


def research_candidates(get, store, *, day: date, now: int, n: int) -> List[str]:
    """The movers most likely to reach the card: gap +5..40%, liquid, by dollar volume. Cached per day."""
    cached = store.get("edge_research_queue", day.isoformat())
    if cached:
        return cached["symbols"]
    from edge import premarket as PM
    syms = sorted(set(PM.candidates(get, store, day=day, now=now, top=50)["symbols"]))
    if not syms:
        return []
    daily = A.bars_multi(get, syms, timeframe="1Day", start=(day - timedelta(days=45)).isoformat())
    from edge import feeds as FD
    snaps = A.snapshots(get, syms, feed=FD.live_feed(get, store, now=now))
    ranked = []
    for s in syms:
        st = _daily_stats(daily.get(s) or [], day)
        ref = _reference(snaps.get(s), now=now, day=day)
        g = D.gap(st["prev_close"], ref["price"]) if ref["price"] else None
        liq = D.liquidity(price=ref["price"] or st["prev_close"], avg_shares=st["avg_shares"])
        if g is not None and g.state == D.PASS and liq.state == D.PASS:
            ranked.append((-(st["avg_dollars"] or 0), s))
    out = [s for _, s in sorted(ranked)[:n]]
    # Cache only a NON-empty queue. At 08:30 ET the free IEX feed often has no fresh
    # premarket prints yet; caching that empty answer stopped research for the whole day
    # (2026-09-23). An empty queue is recomputed on the next tick (3 calls per 5 min).
    if out:
        store.put("edge_research_queue", day.isoformat(), {"symbols": out, "at": now,
                                                            "movers": len(syms)})
    return out


def research_step(get, ledger: Ledger, *, day: date, now: int, client=None) -> Dict[str, Any]:
    """One symbol per tick, so a tick stays inside the scheduler's timeout."""
    import os as _os
    rw = _research()
    try:
        n = max(1, min(10, int(_os.getenv("EDGE_RESEARCH_MAX_SYMBOLS", "5"))))
    except ValueError:
        n = 5
    queue = research_candidates(get, ledger.store, day=day, now=now, n=n)
    if not queue:
        return {"status": "no_candidates_yet", "note": "no mover passed gap + liquidity with a fresh IEX "
                                                        "price; retried next tick"}
    for s in queue:
        if not ledger.store.get("edge_research", f"{day.isoformat()}|{s}"):
            import requests as _rq
            return rw.research_symbol(client or rw._client(), ledger.store, symbol=s, day=day.isoformat(), now=now,
                                      http=_rq)
    return {"status": "nothing", "queue": queue}


def _minute_bars(rows: List[dict]) -> List[tuple]:
    out = []
    for b in rows:
        ts = A.iso_to_epoch(b.get("t"))
        if ts is None:
            continue
        out.append((ts, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b.get("v") or 0)))
    return out


def resolve_day(get, ledger: Ledger, *, day: date, now: int) -> Dict[str, Any]:
    if now < _at(day, 16, 20):
        return {"status": "too_early"}
    from edge.contracts import Forecast
    from edge import intraday as I
    fcs = [f for spec in _all_specs(ledger.store, I)
           for f in ledger.store.scan("forecasts", experiment_id=spec.experiment_id, session_date=day.isoformat())]
    pending = [f for f in fcs
               if not (ledger.store.get("outcomes", f"{f['forecast_id']}|simulated") or {}).get("outcome") in TERMINAL]
    if not pending:
        return {"status": "nothing_pending", "forecasts": len(fcs)}
    syms = sorted({f["symbol"] for f in pending})
    bars = A.bars_multi(get, syms, timeframe="1Min", start=_iso(_at(day, 9, 30)), end=_iso(_at(day, 16, 0)))
    settled = {}
    for row in pending:
        f = Forecast(**{k: row[k] for k in Forecast.__dataclass_fields__})
        b = _minute_bars(bars.get(f.symbol) or [])
        m, x = resolve_market(f, b), resolve_execution(f, b)
        ledger.settle(f.forecast_id, m, now=now, record="forecast")
        ledger.settle(f.forecast_id, x, now=now, record="simulated")
        settled[f"{f.experiment_id}:{f.symbol}"] = {"forecast": m.outcome, "simulated": x.outcome,
                                                   "pnl_usd": x.pnl_usd}
    return {"status": "resolved", "settled": settled}


_COMMON = __import__("re").compile(r"^[A-Z]{1,5}$")
MISS_MIN_PRICE, MISS_MIN_DOLLARS = 1.0, 1_000_000.0


def _full_market_moves(get, day: date) -> Optional[List[M.DayMove]]:
    """Every US stock's day, from two Polygon grouped-daily calls.

    Probe-verified 2026-09-22 (12,626 tickers). Noise floor, stated not hidden:
    plain 1-5 letter tickers, price >= $1, day dollar volume >= $1M -- a move in
    a stock nobody could trade $1,000 of is not an opportunity anyone missed.
    """
    from edge.providers import polygon as PG
    try:
        today = {r["T"]: r for r in PG.grouped_daily(get, day) if r.get("T")}
    except Exception:  # noqa: BLE001 - caller falls back and says so
        return None
    if not today:
        return None
    prior, d = {}, day
    for _ in range(6):
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        try:
            prior = {r["T"]: r for r in PG.grouped_daily(get, d) if r.get("T")}
        except Exception:  # noqa: BLE001
            return None
        if prior:
            break
    moves = []
    for sym, t in today.items():
        p = prior.get(sym)
        if not p or not _COMMON.match(sym):
            continue
        close, vol = float(t.get("c") or 0), float(t.get("v") or 0)
        if close < MISS_MIN_PRICE or close * vol < MISS_MIN_DOLLARS:
            continue
        moves.append(M.DayMove(sym, float(p["c"]), float(t["o"]), float(t["h"]), float(t["l"]), close))
    return moves


def _movers_moves(get, day: date, top: int) -> List[M.DayMove]:
    mv = A.movers(get, top=top)
    syms = sorted({str(g["symbol"]).upper() for g in mv.get("gainers") or [] if g.get("symbol")})
    daily = A.bars_multi(get, syms, timeframe="1Day", start=(day - timedelta(days=7)).isoformat(),
                         end=_iso(_at(day, 20, 0)))
    moves = []
    for s in syms:
        bs = daily.get(s) or []
        today = [b for b in bs if A.iso_to_epoch(b.get("t")) and _et(A.iso_to_epoch(b["t"])).date() == day]
        prior = [b for b in bs if A.iso_to_epoch(b.get("t")) and _et(A.iso_to_epoch(b["t"])).date() < day]
        if today and prior:
            t = today[-1]
            moves.append(M.DayMove(s, float(prior[-1]["c"]), float(t["o"]), float(t["h"]), float(t["l"]), float(t["c"])))
    return moves


def miss_review(get, ledger: Ledger, *, day: date, now: int, top: int = 50,
                max_minute_symbols: int = 400) -> Dict[str, Any]:
    store = ledger.store
    if now < _at(day, 16, 20):
        return {"status": "too_early"}
    if store.get("edge_miss", day.isoformat()):
        return {"status": "already_reviewed"}
    card = store.get("edge_cards", day.isoformat()) or {"rows": []}
    moves = _full_market_moves(get, day)
    if moves is not None:
        coverage = (f"full market: {len(moves)} liquid US stocks (Polygon grouped daily; "
                    f"price >= ${MISS_MIN_PRICE:g}, day dollar volume >= ${MISS_MIN_DOLLARS:,.0f})")
    else:
        moves = _movers_moves(get, day, top)
        coverage = "FALLBACK: Alpaca screener top-50 gainers only -- Polygon grouped daily unavailable"
    # Minute bars only where they can change the answer: names that reached +5%.
    hot = sorted({m.symbol for m in moves if m.prev_close > 0 and m.high / m.prev_close >= 1.05})
    if len(hot) > max_minute_symbols:
        coverage += f"; minute-bar ordering checked for {max_minute_symbols} of {len(hot)} movers"
        hot = hot[:max_minute_symbols]
    minute = A.bars_multi(get, hot, timeframe="1Min", start=_iso(_at(day, 9, 30)), end=_iso(_at(day, 16, 0))) if hot else {}
    syms = hot
    # The universe the system could claim that day. A listed common stock the
    # radar never surfaced is a DETECTION failure; only a name outside this set
    # is a coverage gap. Without a snapshot yet, fall back and say so.
    universe = U.symbols_as_of(store, day.isoformat())
    if universe is None:
        universe = set()
        coverage += "; no universe snapshot yet -- 'coverage' means not in the 9am candidate list"
        fallback_universe = True
    else:
        fallback_universe = False
    radar_rows = store.scan("edge_radar", session_date=day.isoformat())
    if fallback_universe:
        universe |= {r["symbol"] for r in card.get("rows") or []} | {r["symbol"] for r in radar_rows}
    radar, data_down, details = review_records(store, day, card, radar_rows)
    catalysts = stored_catalysts(store, day, card, radar_rows)
    rep = M.audit(day.isoformat(), moves, universe=universe, data_down=data_down,
                  catalyst_symbols=set(catalysts),
                  radar=radar, minute_bars={s: _minute_bars(minute.get(s) or []) for s in syms},
                  rth_open_ts=_at(day, 9, 30), alert_deadline_ts=_at(day, 9, 30))
    for row in rep.rows:
        if row.get("opportunity") == "EXECUTABLE":
            row.update(details.get(row["symbol"]) or {"seen_by": []})
            if catalysts.get(row["symbol"]):
                row["stored_catalyst"] = catalysts[row["symbol"]]
    out = {"day": day.isoformat(), "labels_version": M.LABELS_VERSION,
           "movers": rep.movers, "executable": rep.executable,
           "gap_only": rep.gap_only, "unknown_ordering": rep.unknown_ordering, "caught": rep.caught,
           "recall": rep.recall, "caught_by": rep.caught_by,
           "card_recall": rep.recall_of("card"), "radar_recall": rep.recall_of("radar"),
           "card_seen": rep.seen_by.get("card", 0), "radar_seen": rep.seen_by.get("radar", 0),
           "radar_names": len(radar_rows), "card_names": len(card.get("rows") or []),
           "labels": rep.labels, "correct_rejections": rep.correct_rejections,
           "wrong_rejections": rep.wrong_rejections, "rows": rep.rows,
           "coverage_note": coverage}
    store.put("edge_miss", day.isoformat(), out)
    return {"status": "reviewed", **{k: out[k] for k in (
        "labels_version", "movers", "executable", "caught", "recall", "card_recall", "radar_recall",
        "labels", "coverage_note")}}


def _filled(store, forecast_id: str) -> Dict[str, Any]:
    """Did a forecast's entry fill? The paper (actual) record decides when it is graded -- the
    broker is what really happened; the simulated record stands in when there is no paper
    outcome. A broker fill outside the rule is not the rule's fill. None = not graded yet."""
    from edge.contracts import COUNTED
    from edge.ledger import outside_rule_reason
    act = store.get("outcomes", f"{forecast_id}|actual") or {}
    sim = store.get("outcomes", f"{forecast_id}|simulated") or {}
    a, s = act.get("outcome"), sim.get("outcome")
    filled = None
    if a in TERMINAL:
        f = store.get("forecasts", forecast_id) or {}
        filled = a in COUNTED and not outside_rule_reason(store, f, act)
    elif s in TERMINAL:
        filled = s in COUNTED
    return {"filled": filled, "simulated": s, "actual": a}


def _radar_forecasts(item: Dict[str, Any], recorded: Dict[str, List[str]]) -> List[str]:
    """The intraday forecasts on a radar name: its ENTRY_ELIGIBLE evidence, plus any paper
    strategy's forecast recorded for it that day (a later strategy does not re-transition)."""
    ids = [h["evidence"]["forecast_id"] for h in item.get("history") or []
           if (h.get("evidence") or {}).get("forecast_id")]
    return ids + [f for f in recorded.get(item["symbol"], []) if f not in ids]


def review_records(store, day: date, card: Dict[str, Any], radar_rows: List[Dict[str, Any]]):
    """One M.RadarRecord per name, merged from the 9am card AND the intraday radar.

    Returns (records, data_down, details). A name either source forecast is judged on its
    forecasts' fills; otherwise on the most recent evaluation's reason (the radar's, when it
    saw the name -- the audit measures the move after the open, which is the radar's job).
    """
    from edge import intraday as I, radar as R
    paper_eids = {spec.experiment_id for spec in I.PAPER_SPECS}
    recorded: Dict[str, List[str]] = {}
    for f in store.scan("forecasts", session_date=day.isoformat()):
        if f.get("experiment_id") in paper_eids:
            recorded.setdefault(f["symbol"], []).append(f["forecast_id"])
    card_rows = {r["symbol"]: r for r in card.get("rows") or []}
    radar_by = {r["symbol"]: r for r in radar_rows}
    card_ts = card.get("issued_at")
    records, data_down, details = {}, set(), {}
    for sym in sorted(set(card_rows) | set(radar_by)):
        c, it = card_rows.get(sym), radar_by.get(sym)
        seen_by = tuple(src for src, x in (("card", c), ("radar", it)) if x is not None)
        forecasts = []           # (source, forecast_id, delivered_ts, deadline_ts)
        if c is not None and c.get("forecast_id"):
            forecasts.append(("card", c["forecast_id"], card_ts, _at(day, 9, 30)))
        if it is not None:
            for fid in _radar_forecasts(it, recorded):
                f = store.get("forecasts", fid) or {}
                forecasts.append(("radar", fid, f.get("issued_at"), f.get("entry_expiry")))
        fills = {fid: _filled(store, fid) for _src, fid, _d, _dl in forecasts}
        pick = None
        if forecasts:
            def rank(x):
                src, fid, dlv, dl = x
                on_time = dlv is not None and (dl is None or dlv <= dl)
                return (not on_time, {True: 0, None: 1, False: 2}[fills[fid]["filled"]])
            pick = min(forecasts, key=rank)
        radar_evaluated = it is not None and (
            bool(it.get("blocker")) or any(h.get("to") == R.WATCHING for h in it.get("history") or []))
        card_priced = c is not None and c.get("ref_price") is not None
        if not radar_evaluated and not card_priced:
            data_down.add(sym)
        reason = None
        if it is not None:
            item = R.RadarItem.from_row(it)
            last = (it.get("history") or [{}])[-1]
            reason = item.blocker_text() or last.get("reason") or None
        if reason is None and c is not None:
            reason = "; ".join(c.get("reasons") or c.get("missing") or []) or None
        linked = bool((c or {}).get("catalyst")) or (c or {}).get("research_status") == "researched" \
            or bool((it or {}).get("catalyst"))
        seen_ts = [t for t in (card_ts if c is not None else None, (it or {}).get("detected_at")) if t is not None]
        records[sym] = M.RadarRecord(
            first_seen_ts=min(seen_ts) if seen_ts else None,
            first_seen_price=(c or {}).get("ref_price"),
            rejected_reason=None if pick else reason,
            forecast_issued=pick is not None,
            alert_delivered_ts=pick[2] if pick else None,
            alert_deadline_ts=pick[3] if pick else None,
            filled=fills[pick[1]]["filled"] if pick else None,
            catalyst_linked=linked, seen_by=seen_by, forecast_by=pick[0] if pick else None)
        det: Dict[str, Any] = {"seen_by": list(seen_by)}
        if it is not None:
            det.update({"radar_state": it.get("state"), "radar_detected_at": it.get("detected_at"),
                        "radar_last_reason": ((it.get("history") or [{}])[-1]).get("reason")})
        if c is not None:
            det["card_verdict"] = c.get("verdict")
        if reason and not pick:
            det["why"] = reason[:220]
        if forecasts:
            det["forecasts"] = [{"source": src, "forecast_id": fid, **fills[fid]}
                                for src, fid, _d, _dl in forecasts]
        details[sym] = det
    return records, data_down, details


def stored_catalysts(store, day: date, card: Dict[str, Any], radar_rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """{symbol: headline} for every dated company-specific event the store held that day:
    the card's news events, reviewed (non-quarantined) research claims, the radar's links."""
    from edge import research as RS
    out: Dict[str, str] = {}
    for r in card.get("rows") or []:
        for e in (r.get("inputs") or {}).get("events") or []:
            if e.get("kind") in C.COMPANY_SPECIFIC:
                out.setdefault(r["symbol"], str(e.get("headline") or "")[:160])
    for rec in store.scan("edge_research", day=day.isoformat()):
        for cl in rec.get("claims") or []:
            if cl.get("kind") in C.COMPANY_SPECIFIC and cl.get("status") != RS.QUARANTINED:
                out.setdefault(str(rec.get("symbol") or "").upper(), str(cl.get("statement") or "")[:160])
    for it in radar_rows:
        if it.get("catalyst"):
            out.setdefault(it["symbol"], str(it["catalyst"].get("headline") or "")[:160])
    out.pop("", None)
    return out


def run(get, ledger: Ledger, *, now: int, http=None, notifier=None) -> Dict[str, Any]:
    """One scheduler tick. Each window is checked independently -- they overlap
    (the intraday radar runs across the 10:25 reminder and the 10:30 cancels).

    `http` (requests-like) turns on PAPER execution; `notifier` turns on phone
    messages. None leaves the shadow as forecasts only.
    """
    from edge import intraday as I
    day = _et(now).date()
    ds = day.isoformat()
    out: Dict[str, Any] = {"day": ds}
    from edge import calendar as CAL
    sess = CAL.session(day, ledger.store, now)
    if not sess["trading"]:
        return {**out, "status": "market_closed"}
    early = sess["early_close"]          # no forecasts, no intraday, no duty reminders
    all_specs = _all_specs(ledger.store, I)

    def guarded(key, step):
        try:
            out[key] = step()
        except Exception as exc:  # noqa: BLE001 - each step fails alone, errors kept
            out[key] = {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def within(a, b):
        return _at(day, *a) <= now < _at(day, *b)

    if within((6, 0), (7, 0)):
        guarded("universe", lambda: U.step(get, ledger.store, day=ds, now=now))
    if within((7, 0), (9, 5)):
        prev = previous_trading_day(day, ledger.store)
        guarded("miss_review", lambda: miss_review(get, ledger, day=prev, now=now))
        review = ledger.store.get("edge_miss", prev.isoformat())
        text = _notify().misses_text(review) if review else None
        if notifier is not None and text:
            guarded("notify_misses", lambda: _notify().once(notifier, ledger.store, day=ds, kind="misses", text=text))
    if within((8, 30), (9, 0)) and _research().enabled():   # ends 5 min early: a slow call cannot crowd the card
        guarded("research", lambda: research_step(get, ledger, day=day, now=now))
    if within((9, 5), (9, 28)):
        guarded("card", lambda: morning_card(get, ledger, now=now))
        if http is not None:
            guarded("paper_submit", lambda: _paper().submit(http, ledger, day=ds, experiments=EXPERIMENTS))
        card = ledger.store.get("edge_cards", ds)
        if notifier is not None and card:
            guarded("notify_card", lambda: _notify().once(notifier, ledger.store, day=ds, kind="card",
                                                           text=_notify().card_text(card)))
    if within((9, 28), (9, 45)) and not ledger.store.get("edge_cards", ds):
        out["card_alarm"] = {"status": "error", "error": "no shadow card by 09:28 ET (see earlier card errors)"}
    # The duty reminders go out on EVERY regular trading day. The operator trades from his own
    # card (the morning-picks skill), which can hold a name when Ghost's shadow card has none;
    # gating on Ghost's forecasts skipped them on exactly those days. Each text says when to skip.
    if notifier is not None and not early and within((10, 20), (10, 30)):     # >= 2 ticks wide
        guarded("notify_duty", lambda: _notify().once(notifier, ledger.store, day=ds,
                                                       kind="duty_1030", text=_notify().DUTY_1030))
    if notifier is not None and not early and within((15, 20), (15, 30)):
        guarded("notify_duty", lambda: _notify().once(notifier, ledger.store, day=ds,
                                                       kind="duty_1530", text=_notify().DUTY_1530))
    if not early and within((9, 45), (14, 30)):
        guarded("intraday", lambda: I.tick(get, ledger, now=now, http=http))
        if http is not None:
            guarded("paper_submit_intraday", lambda: _paper().submit(http, ledger, day=ds,
                                                                      experiments=I.PAPER_SPECS))
    if http is not None and within((9, 45), (15, 0)):      # intraday entries expire from ~09:50
        guarded("paper_cancel", lambda: _paper().cancel_unfilled_entries(http, ledger, day=ds,
                                                                          experiments=all_specs, now=now))
    if http is not None and within((15, 30), (15, 55)):                        # refused sells retry
        guarded("paper_exit", lambda: _paper().time_exit(http, ledger, day=ds, experiments=all_specs, now=now))
        not_flat = (out.get("paper_exit") or {}).get("not_flat")
        if not_flat:       # its own PROBLEM kind: an earlier "sell refused, retrying" must not swallow it
            out["paper_exit_not_flat"] = {"status": "error",
                                          "error": "NOT FLAT after the time exit: " + "; ".join(not_flat)}
    if within((20, 0), (20, 15)):
        from edge import replay as RP
        guarded("replay", lambda: RP.replay_all(ledger.store))
        if not ledger.store.get("edge_pruned", ds):
            guarded("prune", lambda: prune(ledger.store, now=now))
            ledger.store.put("edge_pruned", ds, {"day": ds, "at": now})
    if within((16, 20), (20, 0)):
        guarded("resolve", lambda: resolve_day(get, ledger, day=day, now=now))
        guarded("radar_close", lambda: I.close_day(ledger, day=day, now=now))
        from edge import scorecard as SC
        guarded("card_graded", lambda: SC.grade_card(get, ledger.store, day=day, now=now))
        # The observe-all control arm (docs/control_arm_v1.md): every radar name graded once.
        from edge import control as CA
        guarded("control", lambda: CA.grade_day(get, ledger.store, day=day, now=now))
        if http is not None:
            guarded("paper_reconcile", lambda: _paper().reconcile(http, ledger, day=ds,
                                                                  experiments=all_specs, now=now))
        if out["resolve"].get("status") == "resolved":
            out["report"] = {spec.experiment_id: ledger.report(spec.experiment_id)
                             for spec in all_specs if ledger.store.get("experiments", spec.experiment_id)}
        # Built from the stored outcomes, not this tick's resolve result: a Telegram failure
        # at 16:20 is retried on later ticks instead of the graded message being lost.
        if notifier is not None and out["resolve"].get("status") in ("resolved", "nothing_pending") \
                and not ledger.store.get("edge_notify", f"{ds}|graded"):
            text = _notify().graded_text(ds, _settled(ledger, ds, all_specs))
            if text:
                guarded("notify_graded", lambda: _notify().once(notifier, ledger.store, day=ds,
                                                                 kind="graded", text=text))
    if notifier is not None:
        _problems(notifier, ledger.store, out, ds=ds, now=now, within=within, guarded=guarded)
    if len(out) == 1:
        out["status"] = "idle"
    return out


RESOLVE_OK = {"resolved", "nothing_pending"}
GRADE_OK = {"graded", "already_graded", "no_card"}     # no card: the 09:28 alarm already said so


def _problems(notifier, store, out: Dict[str, Any], *, ds: str, now: int, within, guarded) -> None:
    """PROBLEM alerts to the phone, so a broken day never reads like a quiet one.

    One message per kind per day (`once`): no card by 09:28 ET; any step that ended in
    "error" between 09:00 and 16:00 ET; grading not done by 17:00 ET. Notification steps
    are left out -- a Telegram failure cannot be reported through Telegram."""
    N = _notify()
    if (out.get("card_alarm") or {}).get("status") == "error":
        guarded("notify_problem_card_alarm", lambda: N.once(notifier, store, day=ds, kind="problem_card_alarm",
                                                             text=N.PROBLEM_NO_CARD))
    if within((9, 0), (16, 0)):
        for step, res in list(out.items()):
            if step == "card_alarm" or step.startswith("notify") or not isinstance(res, dict) \
                    or res.get("status") != "error":
                continue
            guarded(f"notify_problem_{step}",
                    lambda s=step, r=res: N.once(notifier, store, day=ds, kind=f"problem_{s}",
                                                 text=N.problem_text(s, r, now)))
    if within((17, 0), (20, 0)):
        rs, gs = (out.get("resolve") or {}).get("status"), (out.get("card_graded") or {}).get("status")
        if rs not in RESOLVE_OK or gs not in GRADE_OK:
            guarded("notify_problem_grading", lambda: N.once(notifier, store, day=ds, kind="problem_grading",
                                                             text=N.grading_problem_text(rs, gs)))


# Caches only -- never the ledger (forecasts, outcomes, abstentions, cards, experiments), which
# is kept forever. Days to keep, by table. edge_rvol is the bulk (tens of KB per symbol per day)
# and is only read on the day it was written.
RETENTION_DAYS = {"edge_rvol": 3, "edge_view_logged": 14, "edge_universe_progress": 7, "edge_short": 14,
                  "edge_short_errors": 14, "edge_universe": 30, "edge_radar": 90}


def prune(store, *, now: int) -> Dict[str, Any]:
    if not hasattr(store, "prune"):
        return {"status": "nothing"}
    return {"status": "pruned", "deleted": {t: store.prune(t, older_than=now - d * 86_400)
                                            for t, d in RETENTION_DAYS.items()}}


def paper_guard(http, ledger: Ledger, *, now: int) -> Dict[str, Any]:
    """Every minute: cancel an unfilled paper entry the minute its entry window ends. The 5-minute
    tick let WHLR's order live ~5 minutes past its window and fill there (2026-09-23)."""
    from edge import intraday as I
    day = _et(now).date()
    if not trading_day(day, ledger.store, now) or not (_at(day, 9, 45) <= now < _at(day, 15, 0)):
        return {"status": "outside_window"}
    return _paper().cancel_unfilled_entries(http, ledger, day=day.isoformat(),
                                            experiments=_all_specs(ledger.store, I), now=now)


def _settled(ledger: Ledger, day: str, specs) -> Dict[str, Any]:
    out = {}
    for spec in specs:
        for f in ledger.store.scan("forecasts", experiment_id=spec.experiment_id, session_date=day):
            sim = ledger.store.get("outcomes", f"{f['forecast_id']}|simulated") or {}
            if sim.get("outcome") in TERMINAL:
                out[f"{spec.experiment_id}:{f['symbol']}"] = {"simulated": sim["outcome"],
                                                             "pnl_usd": sim.get("pnl_usd")}
    return out


_NEWS = {"issued", "retry_pending","resolved", "graded", "no_movers", "no_candidates_yet", "no_telegram_config", "budget_exhausted",
         "early_close", "reviewed", "error", "complete", "submitted", "sent", "closed", "researched",
         "drift", "consistent",
         "entries_checked", "time_exit", "reconciled"}


def noteworthy(out: Dict[str, Any]) -> bool:
    """True when a tick DID something -- issued, graded, reviewed, sent, or failed."""
    return any(isinstance(v, dict) and v.get("status") in _NEWS for v in out.values())


def _paper():
    from edge import paper
    return paper


def _notify():
    from edge import notify
    return notify


def _all_specs(store, I):
    """Every experiment the shadow runs today, including a qualified model's."""
    from edge import models as MD
    specs = list(EXPERIMENTS) + list(I.INTRADAY_SPECS)
    m = MD.current(store)
    if m is not None:
        specs.append(m[1])
    return tuple(specs)


def backtest_note(store) -> Optional[str]:
    """One line on the card: what the frozen rule's own point-in-time backtest says (audit F09 --
    the card never said the rule it prints is below break-even on history)."""
    try:
        from edge import backtest as BT
        bt = store.get("edge_backtest", BT.BACKTEST_VERSION)
        if not bt:
            rows = [r for r in store.scan("edge_backtest") if (r.get("experiments") or {}).get(EID)]
            bt = max(rows, key=lambda r: r.get("completed_at") or 0) if rows else None
        e = ((bt or {}).get("experiments") or {}).get(EID)
        if not e or not e.get("filled"):
            return None
        win = bt.get("window") or []
        span = f" {win[0]}..{win[1]}" if len(win) == 2 else ""
        mean = (e.get("expectancy_usd") or {}).get("mean")
        return (f"Rule's own backtest{span}: {e['wins']}/{e['filled']} wins ({e['win_rate']:.0%}) vs "
                f"{bt.get('break_even', 0.375):.1%} needed"
                + (f", {mean:+.2f} $/trade" if mean is not None else "") + f" -- {e.get('verdict')}")
    except Exception:  # noqa: BLE001 - a note, never a reason to lose the card
        return None
