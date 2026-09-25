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
"""
from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from edge import stats
from edge.contracts import COUNTED, ET, WIN, ContractError, issue_intraday
from edge.providers import alpaca as A, polygon as PG
from edge.resolver import resolve_execution

VERSION = "post_split_momentum_backtest_v1"
LOOKBACK_SPLIT_DAYS, MIN_PRIOR_DAY, MIN_5DAY, MIN_PRIOR_DOLLARS = 120, 25.0, 50.0, 2_000_000.0
RVOL_MIN, NEAR_HIGH = 5.0, 0.98
COSTS = (10.0, 25.0, 50.0)
LIMITS = ["research evidence on past sessions, NOT the forward record",
          "RVOL is a proxy: 09:30-10:00 volume vs 20-day average daily volume x 30/390",
          "costs stated at 10 / 25 / 50 bps a side; thin names can cost more",
          "split list from Polygon reference splits; an unlisted split is a miss, not a pass"]


def _spec():
    from edge.intraday import INTRADAY_CONTINUATION
    return replace(INTRADAY_CONTINUATION, name="post_split_momentum", version=1,
                   description="backtest-only hypothesis: reverse split <=120d + prior-day momentum + "
                               "10:00 volume/VWAP/high-of-day confirmation")


def _at(day: date, hh: int, mm: int) -> int:
    return int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET).timestamp())


def reverse_splits(get, start: date, end: date, *, sleep=time.sleep) -> Dict[str, List[date]]:
    """{ticker: [execution dates]} of reverse splits executed in [start, end]."""
    out: Dict[str, List[date]] = {}
    url = PG._base_url() + "/v3/reference/splits"
    params: Optional[Dict[str, Any]] = {"execution_date.gte": start.isoformat(),
                                        "execution_date.lte": end.isoformat(), "limit": 1000,
                                        "apiKey": PG._key()}
    for _ in range(20):
        r = PG._get_patiently(get, url, params, sleep=sleep)
        r.raise_for_status()
        p = r.json() or {}
        for s in p.get("results") or []:
            t, frm, to = str(s.get("ticker") or "").upper(), s.get("split_from"), s.get("split_to")
            if t and frm and to and float(to) < float(frm) and s.get("execution_date"):
                out.setdefault(t, []).append(date.fromisoformat(s["execution_date"]))
        nxt = p.get("next_url")
        if not nxt:
            break
        url, params = nxt, {"apiKey": PG._key()}
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
    splits = reverse_splits(get, session_days[0] - timedelta(days=LOOKBACK_SPLIT_DAYS), end_day, sleep=sleep)
    spec = _spec()
    rolling, closes, trades, skipped = Rolling(), {}, [], []
    for i, day in enumerate(session_days):
        if i:
            sleep(pace)
        try:
            rows = PG.grouped_daily(get, day)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"day": day.isoformat(), "why": type(exc).__name__})
            continue
        if not rows:
            continue
        if i >= 20 + warmup:
            try:
                trades.extend(_session(get, day, closes, rolling, splits, spec))
            except Exception as exc:  # noqa: BLE001
                skipped.append({"day": day.isoformat(), "why": f"{type(exc).__name__}: {str(exc)[:80]}"})
        for r in rows:
            t, c = r.get("T"), r.get("c")
            if t and c:
                closes.setdefault(t, []).append((day, float(c), float(r.get("v") or 0)))
                del closes[t][:-7]
        rolling.push(rows)
    out = {"version": VERSION, "window": [session_days[20 + warmup].isoformat(), end_day.isoformat()],
           "limits": LIMITS, "break_even": spec.break_even_win_rate(), "skipped": skipped,
           "reverse_split_tickers": len(splits), "arms": {}}
    for arm in ("post_split", "control_no_split"):
        rows = [t for t in trades if t["arm"] == arm]
        out["arms"][arm] = {"signals": len(rows), **{f"cost_{int(c)}bps": _summ(rows, c, out["break_even"])
                                                    for c in COSTS}}
    store.put("edge_backtest_postsplit", VERSION, {**out, "trades": trades[:2000], "completed_at": int(time.time())})
    return {"status": "complete", **{k: out[k] for k in ("version", "window", "arms")}}


def _session(get, day: date, closes, rolling, splits, spec) -> List[Dict[str, Any]]:
    from edge.backtest import _bars
    cands = []
    for t, hist in closes.items():
        if not t.isalpha() or len(t) > 5 or len(hist) < 6:
            continue
        (_, c1, v1), (_, c0, _v0) = hist[-1], hist[-2]
        c5 = hist[-6][1]
        prior = (c1 / c0 - 1) * 100 if c0 else 0
        five = (c1 / c5 - 1) * 100 if c5 else 0
        if c1 < 1.0 or c1 * v1 < MIN_PRIOR_DOLLARS or not (prior >= MIN_PRIOR_DAY or five >= MIN_5DAY):
            continue
        recent = [x for x in splits.get(t, []) if day - timedelta(days=LOOKBACK_SPLIT_DAYS) <= x < day]
        cands.append((t, "post_split" if recent else "control_no_split", round(prior, 1), round(five, 1)))
    if not cands:
        return []
    bars = A.bars_multi(get, [c[0] for c in cands], timeframe="1Min", start=datetime.fromtimestamp(
        _at(day, 9, 30), tz=ET).isoformat(), end=datetime.fromtimestamp(_at(day, 16, 0), tz=ET).isoformat())
    out = []
    for t, arm, prior, five in cands:
        b = _bars(bars.get(t) or [])
        sh, _dol = rolling.avg(t)
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
                    **ev, **{k: {"simulated": x.outcome, "pnl_usd": x.pnl_usd} for k, x in res.items()}})
    return out


def _summ(rows: List[Dict[str, Any]], cost: float, break_even: float) -> Dict[str, Any]:
    key = f"cost_{int(cost)}bps"
    filled = [r[key] for r in rows if r[key]["simulated"] in COUNTED]
    n, wins = len(filled), sum(1 for x in filled if x["simulated"] == WIN)
    lo, hi = stats.wilson(wins, n)
    verdict = ("no filled trades" if not n else "above break-even across the whole interval" if lo > break_even
               else "below break-even across the whole interval" if hi < break_even
               else "undecided: the interval straddles break-even")
    return {"filled": n, "wins": wins, "win_rate": wins / n if n else None,
            "wilson_ci": [lo, hi] if n else None,
            "expectancy_usd": stats.expectancy([x["pnl_usd"] for x in filled if x["pnl_usd"] is not None]),
            "verdict": verdict}
