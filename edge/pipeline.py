"""The shadow pipeline: edge running every market day, forecasting without trading.

Why now, before any data is bought: evidence only accrues in calendar time.
Every day this runs is a day of timestamped forecasts, graded outcomes and
miss reviews that no amount of later building can backfill honestly.

Three steps, each idempotent (safe to re-run; a marker stops repeats):
  09:05-09:28 ET  morning_card    candidates -> signals -> decisions; forecasts
                                  and abstentions RECORDED before the open
  16:20-20:00 ET  resolve_day     grade from consolidated minute bars:
                                  "forecast" and "simulated" records
  16:20-20:00 ET  miss_review     the day's movers vs what the radar saw

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

from edge import catalysts as C, detectors as D, miss_audit as M, setups as S
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


def _et(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=ET)


def _at(day: date, hh: int, mm: int) -> int:
    return int(datetime.combine(day, dtime(hh, mm), tzinfo=ET).timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=ET).isoformat()


def _holidays() -> set:
    return {x.strip() for x in os.getenv("EDGE_HOLIDAYS", "").split(",") if x.strip()}


def trading_day(day: date) -> bool:
    return day.weekday() < 5 and day.isoformat() not in _holidays()


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
        for sym in set(tick) & symbols:
            out[sym].append(C.make(sym, str(n.get("headline") or ""), source=str(n.get("source") or "alpaca"),
                                   url=str(n.get("url") or ""), published_at=pub, first_seen_at=now,
                                   tickers=tick))
    return {s: C.dedupe(v) for s, v in out.items()}


def morning_card(get, ledger: Ledger, *, now: int, top: int = 50) -> Dict[str, Any]:
    day = _et(now).date()
    store = ledger.store
    if not trading_day(day):
        return {"status": "market_closed", "day": day.isoformat()}
    if store.get("edge_cards", day.isoformat()):
        return {"status": "already_issued", "day": day.isoformat()}
    if not (_at(day, 9, 5) <= now < _at(day, 9, 28)):
        return {"status": "outside_card_window", "day": day.isoformat()}
    ledger.register(SPEC, now=now)

    mv = A.movers(get, top=top)
    updated = A.iso_to_epoch(mv.get("last_updated"))
    gainers = [g for g in mv.get("gainers") or [] if g.get("symbol")]
    syms = sorted({str(g["symbol"]).upper() for g in gainers})
    daily = A.bars_multi(get, syms, timeframe="1Day", start=(day - timedelta(days=45)).isoformat())
    try:
        snaps = A.snapshots(get, syms, feed="iex")
    except Exception:  # noqa: BLE001 - recorded as missing, never guessed
        snaps = {}
    try:
        items = A.news(get, syms, start=_iso(now - 86_400))
    except Exception:  # noqa: BLE001
        items = None
    events = _events(items, set(syms), now)

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
        row = {"symbol": sym, "verdict": d.verdict, "reasons": d.reasons, "missing": d.missing,
               "ref_price": ref["price"], "ref_ts": ref["ts"], "prev_close": st["prev_close"],
               "avg_dollars": st["avg_dollars"],
               "catalyst": signals["catalyst"].evidence.get("headline")}
        rows.append(row)
        if d.verdict == S.ELIGIBLE:
            eligible.append(row)

    eligible.sort(key=lambda r: -(r["avg_dollars"] or 0))
    chosen = eligible[:SPEC.max_per_day]
    chosen_syms = {r["symbol"] for r in chosen}
    for r in chosen:
        f = issue(SPEC, symbol=r["symbol"], session_date=day, entry_ref=r["ref_price"], issued_at=now,
                  evidence={k: r[k] for k in ("prev_close", "avg_dollars", "catalyst", "ref_ts")})
        ledger.record(f, now=now)
        r["forecast_id"] = f.forecast_id
    for r in rows:
        if r["symbol"] in chosen_syms:
            continue
        why = r["reasons"] or r["missing"] or ["eligible, ranked below the top 2 by dollar volume"]
        ledger.abstain(experiment_id=EID, symbol=r["symbol"], session_date=day.isoformat(),
                       reasons=list(why), now=now)

    priced = sum(1 for r in rows if r["ref_price"])
    card = {"day": day.isoformat(), "issued_at": now, "experiment_id": EID,
            "candidates": len(rows), "priced": priced, "eligible": len(eligible),
            "forecasts": [r["symbol"] for r in chosen], "movers_last_updated": updated,
            "coverage_note": f"{priced}/{len(rows)} candidates had a fresh IEX premarket price",
            "rows": rows}
    store.put("edge_cards", day.isoformat(), card)
    return {"status": "issued", **{k: card[k] for k in ("day", "candidates", "priced", "eligible", "forecasts", "coverage_note")}}


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
    fcs = ledger.store.scan("forecasts", experiment_id=EID, session_date=day.isoformat())
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
        settled[f.symbol] = {"forecast": m.outcome, "simulated": x.outcome, "pnl_usd": x.pnl_usd}
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
    radar, universe, data_down = {}, set(), set()
    for r in card.get("rows") or []:
        universe.add(r["symbol"])
        if r["ref_price"] is None:
            data_down.add(r["symbol"])
        radar[r["symbol"]] = M.RadarRecord(
            first_seen_ts=card.get("issued_at"), first_seen_price=r["ref_price"],
            rejected_reason="; ".join(r["reasons"]) or None,
            forecast_issued=bool(r.get("forecast_id")), alert_delivered_ts=card.get("issued_at"))
    rep = M.audit(day.isoformat(), moves, universe=universe, data_down=data_down, catalyst_symbols=set(),
                  radar=radar, minute_bars={s: _minute_bars(minute.get(s) or []) for s in syms},
                  rth_open_ts=_at(day, 9, 30), alert_deadline_ts=_at(day, 9, 30))
    out = {"day": day.isoformat(), "movers": rep.movers, "executable": rep.executable,
           "gap_only": rep.gap_only, "unknown_ordering": rep.unknown_ordering, "caught": rep.caught,
           "recall": rep.recall, "labels": rep.labels, "correct_rejections": rep.correct_rejections,
           "wrong_rejections": rep.wrong_rejections, "rows": rep.rows,
           "coverage_note": coverage}
    store.put("edge_miss", day.isoformat(), out)
    return {"status": "reviewed", **{k: out[k] for k in ("movers", "executable", "caught", "recall", "labels", "coverage_note")}}


def run(get, ledger: Ledger, *, now: int) -> Dict[str, Any]:
    """One scheduler tick. Decides by exchange time what, if anything, is due."""
    day = _et(now).date()
    out: Dict[str, Any] = {"day": day.isoformat()}
    if not trading_day(day):
        return {**out, "status": "market_closed"}
    if _at(day, 9, 5) <= now < _at(day, 9, 28):
        out["card"] = morning_card(get, ledger, now=now)
    elif _at(day, 16, 20) <= now < _at(day, 20, 0):
        # Each step fails on its own: a grading error must not also cost the
        # day's miss review, and every error is kept, never swallowed.
        for key, step in (("resolve", lambda: resolve_day(get, ledger, day=day, now=now)),
                          ("miss_review", lambda: miss_review(get, ledger, day=day, now=now))):
            try:
                out[key] = step()
            except Exception as exc:  # noqa: BLE001
                out[key] = {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        out["report"] = ledger.report(EID) if ledger.store.get("experiments", EID) else None
    else:
        out["status"] = "idle"
    return out


_NEWS = {"issued", "resolved", "reviewed", "error"}


def noteworthy(out: Dict[str, Any]) -> bool:
    """True when a tick DID something -- issued, graded, reviewed, or failed."""
    return any(isinstance(v, dict) and v.get("status") in _NEWS for v in out.values())
