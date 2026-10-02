"""Backtest of a NEW hypothesis: post-reverse-split momentum continuation.

Why: the miss investigations of 2026-09-22 and 09-23 found the week's biggest runners
(JAGX, VSA, TNMG, FTFT) had no news, but had done a reverse split in the prior months and
were already running the day before. A catalyst-gated card cannot catch them. This asks,
on history and before any forward trade, whether that pattern has an edge.

The rule, point-in-time, decided at 10:00 ET:
  universe   common-stock tickers with a REVERSE split executed in the prior 120 days
             (Polygon reference splits, split_to < split_from)
  momentum   prior-day return >= 25% OR 5-day return >= 50%, prior-day dollar volume >= $2M
  confirm    09:30-10:00 volume >= 5x a normal half hour (20-day average daily volume x 30/390),
             last price above the session VWAP and within 2% of the high of day
  order      buy stop at the 10:00 high x 1.002 (limit x 1.01), 20-min entry window,
             +5% target / -3% stop, time exit 15:30 ET -- the intraday specs' levels
A CONTROL runs the identical rule on runners WITHOUT a recent reverse split, so the report
says whether the split matters, not just whether momentum does.

Costs: these are thin, volatile names. Results are stated at 10, 25 and 50 bps a side.
Limits, stated not hidden: a proxy for time-of-day RVOL (not the 10-session curve); SIP
minute bars; the 10:00 decision uses bars that closed by 10:00.

Price basis (v2, the same fix as edge.backtest v8 / EDGE-09): every series is raw as traded --
Polygon grouped daily with adjusted=false, Alpaca minute bars with adjustment=raw -- and a prior
session's close and share volume are restated onto the decision day's basis only for splits
executed after that session and on or before the decision day (edge.backtest.share_factor).
v1 took the prior-day / 5-day returns, the $1 floor and the 20-day share volume from
split-ADJUSTED daily bars (restated for splits executed long afterwards: a later 1-for-10
reverse split made a $0.40 name read $4.00 and cut its average share volume by ten, so the RVOL
proxy against raw minute volume read 10x) while the minute bars stayed raw. v1's record is kept
as it was and is never pooled with v2's. The rule itself is unchanged.
"""
from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from edge import stats
from edge.backtest import (MINUTE_ADJUSTMENT, PRICE_BASIS, PRICE_BASIS_DETAIL, Splits, share_factor,
                           split_index)
from edge.contracts import COUNTED, ET, WIN, ContractError, issue_intraday
from edge.providers import alpaca as A, polygon as PG
from edge.resolver import RESOLVER_VERSION, resolve_execution
from shared.redaction import redact_exc

# v2: one raw-as-traded price basis with point-in-time split restatement (see the module doc).
VERSION = "post_split_momentum_backtest_v2"
LOOKBACK_SPLIT_DAYS, MIN_PRIOR_DAY, MIN_5DAY, MIN_PRIOR_DOLLARS = 120, 25.0, 50.0, 2_000_000.0
RVOL_MIN, NEAR_HIGH = 5.0, 0.98
COSTS = (10.0, 25.0, 50.0)
LIMITS = ["research evidence on past sessions, NOT the forward record",
          "RVOL is a proxy: 09:30-10:00 volume vs 20-day average daily volume x 30/390",
          "costs stated at 10 / 25 / 50 bps a side; thin names can cost more",
          "split list from Polygon reference splits; an unlisted split is a miss, not a pass",
          "prices and volume as traded (raw), restated only for splits executed by the session"]


def _spec():
    from edge.intraday import INTRADAY_CONTINUATION
    return replace(INTRADAY_CONTINUATION, name="post_split_momentum", version=1,
                   description="backtest-only hypothesis: reverse split <=120d + prior-day momentum + "
                               "10:00 volume/VWAP/high-of-day confirmation")


def _at(day: date, hh: int, mm: int) -> int:
    return int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET).timestamp())


def reverse_splits(splits: Splits) -> Dict[str, List[date]]:
    """{ticker: [execution dates]} of the REVERSE splits (share multiplier < 1) in `splits`."""
    out: Dict[str, List[date]] = {}
    for t, rows in (splits or {}).items():
        for ex, mult in rows:
            if mult < 1.0:
                out.setdefault(t, []).append(ex)
    return out


def _signal(bars: List[tuple], day: date, avg_shares: float) -> Optional[Tuple[float, Dict[str, Any]]]:
    """(entry reference = high of day by 10:00, evidence) when the 10:00 confirmation passes."""
    first = [b for b in bars if _at(day, 9, 30) <= b[0] and b[0] + 60 <= _at(day, 10, 0)]
    if len(first) < 10 or not avg_shares:
        return None
    vol = sum(b[5] for b in first)
    rvol = vol / (avg_shares * 30 / 390)
    pv = sum(((b[2] + b[3] + b[4]) / 3) * b[5] for b in first)
    vwap = pv / vol if vol else None
    hod, last = max(b[2] for b in first), first[-1][4]
    ev = {"rvol_proxy": round(rvol, 2), "vwap": round(vwap, 4) if vwap else None, "hod": hod, "last": last}
    if rvol < RVOL_MIN or vwap is None or last <= vwap or last < NEAR_HIGH * hod:
        return None
    return hod, ev


def run(get, store, *, end_day: date, days: int = 60, warmup: int = 6,
        pace_s: Optional[float] = None, sleep=time.sleep) -> Dict[str, Any]:
    if store.get("edge_backtest_postsplit", VERSION):
        return {"status": "already_run"}
    from edge.backtest import Rolling
    from edge.pipeline import trading_day
    pace = float(os.getenv("EDGE_PROBE_POLYGON_PACE_S", "13")) if pace_s is None else pace_s
    session_days: List[date] = []
    d = end_day
    while len(session_days) < days + warmup + 20:
        if trading_day(d):
            session_days.append(d)
        d -= timedelta(days=1)
    session_days.reverse()
    try:
        # The whole split list (forward and reverse): it picks the universe AND restates prior
        # sessions onto each decision day's basis. A partial list would leave names on mixed
        # bases, so no list, no run -- nothing is stored and the next night tries again.
        all_splits = split_index(PG.splits(get, session_days[0] - timedelta(days=LOOKBACK_SPLIT_DAYS),
                                           end_day, sleep=sleep))
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "why": f"split list unavailable: {redact_exc(exc, 160)}"}
    splits = reverse_splits(all_splits)
    spec = _spec()
    rolling, closes, trades, skipped = Rolling(), {}, [], []
    for i, day in enumerate(session_days):
        if i:
            sleep(pace)
        try:
            rows = PG.grouped_daily(get, day, adjusted=False)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"day": day.isoformat(), "why": type(exc).__name__})
            continue
        if not rows:
            continue
        if i >= 20 + warmup:
            try:
                trades.extend(_session(get, day, closes, rolling, splits, spec, all_splits=all_splits))
            except Exception as exc:  # noqa: BLE001
                skipped.append({"day": day.isoformat(), "why": redact_exc(exc, 120)})
        for r in rows:
            t, c = r.get("T"), r.get("c")
            if t and c:
                closes.setdefault(t, []).append((day, float(c), float(r.get("v") or 0)))
                del closes[t][:-7]
        rolling.push(rows, day)
    out = {"version": VERSION, "resolver_version": RESOLVER_VERSION,
           "price_basis": PRICE_BASIS, "price_basis_detail": PRICE_BASIS_DETAIL,
           "window": [session_days[20 + warmup].isoformat(), end_day.isoformat()],
           "limits": LIMITS, "break_even": spec.break_even_win_rate(), "skipped": skipped,
           "reverse_split_tickers": len(splits),
           "splits_in_window": sum(len(v) for v in all_splits.values()), "arms": {}}
    for arm in ("post_split", "control_no_split"):
        rows = [t for t in trades if t["arm"] == arm]
        out["arms"][arm] = {"signals": len(rows), **{f"cost_{int(c)}bps": _summ(rows, c, out["break_even"])
                                                    for c in COSTS}}
    store.put("edge_backtest_postsplit", VERSION, {**out, "trades": trades[:2000], "completed_at": int(time.time())})
    return {"status": "complete", **{k: out[k] for k in ("version", "window", "arms", "price_basis")}}


def _restated(all_splits: Optional[Splits], t: str, hist: List[tuple], day: date) -> Tuple[List[tuple], float]:
    """`hist` [(session, close, shares)] as traded, restated onto `day`'s basis for splits executed
    after each session and on or before `day`; with the factor applied to the latest session."""
    out, f1 = [], 1.0
    for d, c, v in hist:
        f = share_factor(all_splits, t, d, day)
        out.append((d, c / f, v * f))
        f1 = f
    return out, f1


def _session(get, day: date, closes, rolling, splits, spec, *,
             all_splits: Optional[Splits] = None) -> List[Dict[str, Any]]:
    from edge.backtest import _bars
    cands, factors = [], {}
    for t, raw_hist in closes.items():
        if not t.isalpha() or len(t) > 5 or len(raw_hist) < 6:
            continue
        hist, f1 = _restated(all_splits, t, raw_hist, day)
        (_, c1, v1), (_, c0, _v0) = hist[-1], hist[-2]
        c5 = hist[-6][1]
        prior = (c1 / c0 - 1) * 100 if c0 else 0
        five = (c1 / c5 - 1) * 100 if c5 else 0
        if c1 < 1.0 or c1 * v1 < MIN_PRIOR_DOLLARS or not (prior >= MIN_PRIOR_DAY or five >= MIN_5DAY):
            continue
        recent = [x for x in splits.get(t, []) if day - timedelta(days=LOOKBACK_SPLIT_DAYS) <= x < day]
        cands.append((t, "post_split" if recent else "control_no_split", round(prior, 1), round(five, 1)))
        if any(share_factor(all_splits, t, d, day) != 1.0 for d, _c, _v in raw_hist):
            factors[t] = {"raw_prior_close": raw_hist[-1][1], "prior_share_factor": f1}
    if not cands:
        return []
    bars = A.bars_multi(get, [c[0] for c in cands], timeframe="1Min", start=datetime.fromtimestamp(
        _at(day, 9, 30), tz=ET).isoformat(), end=datetime.fromtimestamp(_at(day, 16, 0), tz=ET).isoformat(),
        adjustment=MINUTE_ADJUSTMENT)
    out = []
    for t, arm, prior, five in cands:
        b = _bars(bars.get(t) or [])
        sh, _dol = rolling.avg(t, as_of=day, splits=all_splits)
        sig = _signal(b, day, sh or 0)
        if sig is None:
            continue
        ref, ev = sig
        try:
            f = issue_intraday(spec, symbol=t, session_date=day, entry_ref=ref, issued_at=_at(day, 10, 0))
        except ContractError:
            continue
        res = {f"cost_{int(c)}bps": resolve_execution(f, b, cost_bps_per_side=c) for c in COSTS}
        out.append({"day": day.isoformat(), "symbol": t, "arm": arm, "prior_day_pct": prior, "five_day_pct": five,
                    **ev, **{k: {"simulated": x.outcome, "pnl_usd": x.pnl_usd} for k, x in res.items()},
                    **({"split_restated": factors[t]} if t in factors else {})})
    return out


def _summ(rows: List[Dict[str, Any]], cost: float, break_even: float) -> Dict[str, Any]:
    key = f"cost_{int(cost)}bps"
    filled = [r[key] for r in rows if r[key]["simulated"] in COUNTED]
    n, wins = len(filled), sum(1 for x in filled if x["simulated"] == WIN)
    lo, hi = stats.wilson(wins, n)
    verdict = stats.break_even_verdict(wins, n, break_even)
    return {"filled": n, "wins": wins, "win_rate": wins / n if n else None,
            "wilson_ci": [lo, hi] if n else None,
            "expectancy_usd": stats.expectancy([x["pnl_usd"] for x in filled if x["pnl_usd"] is not None]),
            "verdict": verdict}
