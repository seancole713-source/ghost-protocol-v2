"""Historical test of the FROZEN Gap-and-Go rule -- research evidence, not proof.

The rule (docs/gap_and_go_v1.md) was frozen before this ran, so this is a
test of a fixed rule on past sessions, not a fit. Each decision uses only what
was knowable at 09:10 ET that day:

  reference price  the last premarket minute bar that began before 09:10 ET
                   (consolidated SIP bars, which include extended hours), no
                   older than 30 min -- else DATA_UNAVAILABLE
  previous close   the prior session's grouped-daily close
  liquidity        20-day averages from PRIOR sessions only
  catalysts        news published before 09:10 ET, through the same keyword
                   model the live shadow uses

Outcomes come from the same resolver and costs as the live shadow, for both
gap_and_go_auto and gap_baseline, so the backtest and the forward record speak
the same language.

Limits, printed on every report:
  * candidates are pre-screened with that day's OPEN (>= +1%) to keep the
    minute-bar fetch bounded. A name +5% at 09:10 that opened below +1% is
    missed -- a small OPTIMISTIC bias (those are fades).
  * historical news has no first-seen time; publication time stands in for it
    (optimistic: live receipt is later).
  * 10 bps per side is an assumed cost, not a measured one.
  * this is not the forward ledger and never enters it: the ledger refuses
    anything recorded after its window opened, by design.

Data: Polygon grouped daily (one paced call per session) + Alpaca SIP minute
bars and news (multi-symbol, far higher rate limit). Runs overnight.
"""
from __future__ import annotations

import os
import statistics
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from edge import catalysts as C, detectors as D, features as FX, setups as S, stats
from edge.contracts import COUNTED, ET, WIN, issue
from edge.pipeline import GAP_AND_GO_AUTO, GAP_BASELINE, previous_trading_day, trading_day
from edge.providers import alpaca as A, polygon as PG
from edge.resolver import resolve_execution, resolve_market

# v3: v2's historical news query had no `end`, so Alpaca paged back from NOW and the capped
# pages held no news from the session studied -- the catalyst rule saw almost none (1 forecast
# in 58 sessions). The no-catalyst baseline was unaffected. v2's record is kept as it was.
# v4: the resolver no longer fills a stop-limit on a bar that ran through the trigger and past the
# limit (it waits for price to return to the limit), matching what the paper broker did on day 1.
BACKTEST_VERSION = "gap_and_go_backtest_v4"
LIMITS = [
    "research evidence on past sessions, NOT the forward record",
    "candidates pre-screened by that day's open >= +1% (small optimistic bias: misses 9:10 gappers that faded before the open)",
    "historical news uses publication time as first-seen (optimistic)",
    "10 bps per side assumed cost",
    "catalyst check is keyword_v1, same as the live shadow",
]


def _at(day: date, hh: int, mm: int) -> int:
    return int(datetime.combine(day, dtime(hh, mm), tzinfo=ET).timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=ET).isoformat()


class Rolling:
    """Per-ticker 20-session history of (volume, dollar volume) from PRIOR days."""

    def __init__(self, n: int = 20) -> None:
        self.n, self.h = n, {}

    def push(self, rows: List[dict]) -> None:
        for r in rows:
            t = r.get("T")
            if not t:
                continue
            v = float(r.get("v") or 0)
            px = float(r.get("vw") or r.get("c") or 0)
            q = self.h.setdefault(t, [])
            q.append((v, v * px))
            if len(q) > self.n:
                del q[0]

    def avg(self, t: str, *, min_days: int = 10) -> Tuple[Optional[float], Optional[float]]:
        q = self.h.get(t) or []
        if len(q) < min_days:
            return None, None
        return statistics.mean(x for x, _ in q), statistics.mean(y for _, y in q)


def _bars(rows: List[dict]) -> List[tuple]:
    out = []
    for b in rows:
        ts = A.iso_to_epoch(b.get("t"))
        if ts is not None:
            out.append((ts, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b.get("v") or 0)))
    return sorted(out)


def _premarket_ref(bars: List[tuple], day: date) -> Tuple[Optional[float], Optional[int], str]:
    cutoff, floor = _at(day, 9, 10), _at(day, 8, 40)
    pre = [b for b in bars if _at(day, 4, 0) <= b[0] and b[0] + 60 <= cutoff]
    if not pre:
        return None, None, "no premarket bar before 09:10"
    last = pre[-1]
    if last[0] < floor:
        return None, last[0], "last premarket bar older than 30 min at 09:10"
    return last[4], last[0] + 60, ""


def session(get, day: date, prev_rows: List[dict], today_rows: List[dict], rolling: Rolling, *,
            max_symbols: int = 400, cost_bps: float = 10.0) -> Dict[str, Any]:
    """Decide and resolve one past session. Pure given its inputs + two Alpaca calls."""
    prev_close = {r["T"]: float(r["c"]) for r in prev_rows if r.get("T") and r.get("c")}
    cands = []
    for r in today_rows:
        t = r.get("T")
        if not t or t not in prev_close or not t.isalpha() or len(t) > 5:
            continue
        pc, op = prev_close[t], float(r.get("o") or 0)
        if pc <= 0 or not (1.01 <= op / pc <= 1.5) or not (2.0 <= pc <= 500.0):
            continue
        sh, dol = rolling.avg(t)
        if dol is None or dol < 5_000_000:
            continue
        cands.append((dol, t, sh))
    cands.sort(reverse=True)
    cands = cands[:max_symbols]
    syms = [t for _, t, _ in cands]
    if not syms:
        return {"day": day.isoformat(), "candidates": 0, "results": []}
    minute = A.bars_multi(get, syms, timeframe="1Min", start=_iso(_at(day, 4, 0)), end=_iso(_at(day, 16, 0)))
    news = A.news(get, syms, start=_iso(_at(day, 9, 10) - 86_400), end=_iso(_at(day, 9, 10)), limit=50)
    issued_at = _at(day, 9, 10)
    events: Dict[str, List[C.CatalystEvent]] = {s: [] for s in syms}
    for n in news:
        pub = A.iso_to_epoch(n.get("created_at"))
        if pub is None or pub > issued_at:
            continue            # published after the card: not knowable at 09:10
        tick = [x.upper() for x in (n.get("symbols") or [])]
        for s in set(tick) & set(syms):
            events[s].append(C.make(s, str(n.get("headline") or ""), source=str(n.get("source") or ""),
                                    url=str(n.get("url") or ""), published_at=pub, first_seen_at=pub,
                                    tickers=tick))
    rows = []
    for dol, t, sh in cands:
        b = _bars(minute.get(t) or [])
        ref, ref_ts, why = _premarket_ref(b, day)
        ev = C.usable_at(C.dedupe(events[t]), t, issued_at=issued_at)
        sig = {
            "gap": D.gap(prev_close[t], ref) if ref else D.Signal("gap", D.UNKNOWN, evidence={"missing": why}),
            "liquidity": D.liquidity(price=ref or prev_close[t], avg_shares=sh),
            "catalyst": S.catalyst_signal(ev), "not_dilutive": S.dilution_signal(ev),
        }
        fx = FX.build(day=day, prev_close=prev_close[t], avg_shares=sh, avg_dollars=dol, sip_bars=b, events=ev)
        rows.append({"symbol": t, "avg_dollars": dol, "ref": ref, "bars": b, "features": fx,
                     "main": S.decide("premarket_continuation", sig).verdict,
                     "base": S.decide("gap_baseline", sig).verdict,
                     "gap_ok": sig["gap"].state == D.PASS, "liquid": sig["liquidity"].state == D.PASS})
    results = []
    for spec, key in ((GAP_AND_GO_AUTO, "main"), (GAP_BASELINE, "base")):
        chosen = sorted([r for r in rows if r[key] == S.ELIGIBLE], key=lambda r: -r["avg_dollars"])[:spec.max_per_day]
        for r in chosen:
            f = issue(spec, symbol=r["symbol"], session_date=day, entry_ref=r["ref"], issued_at=issued_at)
            rth = [x for x in r["bars"] if x[0] >= _at(day, 9, 30)]
            m, x = resolve_market(f, rth), resolve_execution(f, rth, cost_bps_per_side=cost_bps)
            results.append({"experiment": spec.experiment_id, "symbol": r["symbol"], "ref": r["ref"],
                            "forecast": m.outcome, "simulated": x.outcome, "pnl_usd": x.pnl_usd,
                            "ambiguous": x.ambiguous})
    # The model dataset: EVERY priced, gap-qualified, liquid candidate -- not just the
    # ones a rule chose -- with its point-in-time features and its market outcome
    # under the same frozen levels. This is what a model is trained and judged on.
    dataset = []
    for r in rows:
        if not (r["ref"] and r["gap_ok"] and r["liquid"] and FX.complete(r["features"])):
            continue
        f = issue(GAP_AND_GO_AUTO, symbol=r["symbol"], session_date=day, entry_ref=r["ref"], issued_at=issued_at)
        rth = [x for x in r["bars"] if x[0] >= _at(day, 9, 30)]
        m = resolve_market(f, rth)
        x = resolve_execution(f, rth, cost_bps_per_side=cost_bps)
        dataset.append({"day": day.isoformat(), "symbol": r["symbol"], "features": r["features"],
                        "avg_dollars": r["avg_dollars"], "market": m.outcome, "simulated": x.outcome,
                        "pnl_usd": x.pnl_usd, "catalyst_ok": r["main"] == S.ELIGIBLE})
    priced = sum(1 for r in rows if r["ref"])
    return {"day": day.isoformat(), "candidates": len(rows), "priced": priced, "results": results,
            "dataset": dataset}


def summarize(sessions: List[Dict[str, Any]], break_even: float) -> Dict[str, Any]:
    out: Dict[str, Any] = {"version": BACKTEST_VERSION, "sessions": len(sessions), "limits": LIMITS,
                           "break_even": break_even, "experiments": {}}
    for eid in (GAP_AND_GO_AUTO.experiment_id, GAP_BASELINE.experiment_id):
        rows = [r for s in sessions for r in s["results"] if r["experiment"] == eid]
        filled = [r for r in rows if r["simulated"] in COUNTED]
        wins = sum(1 for r in filled if r["simulated"] == WIN)
        n = len(filled)
        by_day: Dict[str, List[bool]] = {}
        for s in sessions:
            for r in s["results"]:
                if r["experiment"] == eid and r["simulated"] in COUNTED:
                    by_day.setdefault(s["day"], []).append(r["simulated"] == WIN)
        lo, hi = stats.wilson(wins, n)
        clo, chi = stats.clustered_bootstrap_ci(by_day) if by_day else (None, None)
        pnls = [r["pnl_usd"] for r in filled if r["pnl_usd"] is not None]
        verdict = "no filled trades"
        if n:
            if lo > break_even:
                verdict = "above break-even across the whole interval"
            elif hi < break_even:
                verdict = "below break-even across the whole interval"
            else:
                verdict = "undecided: the interval straddles break-even"
        out["experiments"][eid] = {
            "forecasts": len(rows), "filled": n, "no_fill": sum(1 for r in rows if r["simulated"] == "NO_FILL"),
            "wins": wins, "win_rate": wins / n if n else None, "wilson_ci": [lo, hi] if n else None,
            "clustered_ci": [clo, chi] if n else None, "expectancy_usd": stats.expectancy(pnls),
            "forecast_record_wins": sum(1 for r in rows if r["forecast"] == WIN),
            "ambiguous_losses": sum(1 for r in filled if r["ambiguous"]),
            "verdict": verdict,
        }
    return out


def run(get, store, *, end_day: date, days: int = 60, warmup: int = 20,
        pace_s: Optional[float] = None, sleep=time.sleep) -> Dict[str, Any]:
    """Whole backtest in one pass; results stored under edge_backtest/<version>."""
    if store.get("edge_backtest", BACKTEST_VERSION):
        return {"status": "already_run"}
    pace = float(os.getenv("EDGE_PROBE_POLYGON_PACE_S", "13")) if pace_s is None else pace_s
    sessions_days: List[date] = []
    d = end_day
    while len(sessions_days) < days + warmup + 1:
        if trading_day(d):
            sessions_days.append(d)
        d -= timedelta(days=1)
    sessions_days.reverse()
    rolling, prev_rows, results, skipped = Rolling(), None, [], []
    for i, day in enumerate(sessions_days):
        if i:
            sleep(pace)
        try:
            rows = PG.grouped_daily(get, day)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"day": day.isoformat(), "why": type(exc).__name__})
            continue
        if not rows:            # an unlisted holiday
            skipped.append({"day": day.isoformat(), "why": "no bars (holiday)"})
            continue
        if prev_rows is not None and i > warmup:
            try:
                results.append(session(get, day, prev_rows, rows, rolling))
            except Exception as exc:  # noqa: BLE001
                skipped.append({"day": day.isoformat(), "why": f"{type(exc).__name__}: {str(exc)[:80]}"})
        rolling.push(rows)
        prev_rows = rows
    summary = summarize(results, GAP_AND_GO_AUTO.break_even_win_rate())
    dataset = [row for sess in results for row in sess.get("dataset") or []]
    summary["dataset_rows"] = len(dataset)
    try:
        from edge import models as MD
        summary["model"] = MD.train_and_register(store, dataset)
    except Exception as exc:  # noqa: BLE001 - a failed model never blocks the backtest record
        summary["model"] = {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    summary.update({"window": [results[0]["day"], results[-1]["day"]] if results else None,
                    "skipped": skipped, "completed_at": int(time.time())})
    store.put("edge_backtest", BACKTEST_VERSION, {**summary, "sessions_detail": [
        {k: v for k, v in sess.items() if k != "dataset"} for sess in results]})
    store.put("edge_dataset", BACKTEST_VERSION, {"rows": dataset, "features": list(FX.FEATURES)})
    return {"status": "complete", **{k: summary[k] for k in ("sessions", "window", "experiments",
                                                              "dataset_rows")},
            "model": {k: v for k, v in (summary.get("model") or {}).items() if k != "evaluation"}}
