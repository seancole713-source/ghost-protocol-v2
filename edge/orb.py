"""gap_and_go_orb@v1: the card's names, entered on a break of the 15-minute opening range.

Operator decision 2026-10-09. The 09:05 card's entries sit 1% above the premarket price, and a
gapper that fades at the open never reaches them (BB, GLND, HOOD, OPCH, ACHR). This experiment
takes the SAME names gap_and_go_sipref would (the card's sipref-eligible rows, top 2 by average
dollar volume) but places nothing before the open: at the first edge tick from 09:45 ET it reads
the 09:30-09:45 ET 1-minute bars and puts the buy-stop just above that opening-range high.

Issued once, at the first tick of 09:45:00-09:47:00 ET (the edge tick runs on the clock at
:x0:05/:x5:05). A missed window is an abstention, never a later issue with a shorter entry window.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from edge import detectors as D, setups as S
from edge.contracts import ContractError, FrozenSpecError, issue_intraday
from edge.ledger import Ledger
from shared.redaction import redact_exc

OR_MINUTES = 15
ISSUE_FROM, ISSUE_UNTIL = (9, 45), (9, 47)


def step(get, ledger: Ledger, *, now: int, clock: Optional[Callable[[], float]] = None) -> Dict[str, Any]:
    from edge import feeds as FD, intraday as I, pipeline as P
    from edge.providers import alpaca as A
    clock = clock or (lambda: now)
    spec, store = P.GAP_ORB, ledger.store
    day = P._et(now).date()
    ds = day.isoformat()
    if not (P._at(day, *ISSUE_FROM) <= now < P._at(day, *ISSUE_UNTIL)):
        return {"status": "outside_issue_window"}
    done = store.get("edge_orb", ds)
    if done:
        return {"status": "already_done", "forecasts": done.get("forecasts", [])}
    try:
        ledger.register(spec, now=now)
    except FrozenSpecError as exc:
        return {"status": "error", "error": f"frozen spec refused: {str(exc)[:160]}"}
    card = store.get("edge_cards", ds)
    if not card:
        store.put("edge_orb", ds, {"day": ds, "at": now, "forecasts": [], "note": "no 09:05 card today"})
        return {"status": "no_card"}
    rows = sorted([r for r in card.get("rows") or [] if r.get("sipref_verdict") == S.ELIGIBLE],
                  key=lambda r: -(r.get("avg_dollars") or 0))
    chosen, rest = rows[:spec.max_per_day], rows[spec.max_per_day:]
    open_ts = P._at(day, 9, 30)
    feed = FD.live_feed(get, store, now=now)
    bars: Dict[str, Any] = {}
    if chosen:
        try:
            bars = A.bars_multi(get, [r["symbol"] for r in chosen], timeframe="1Min",
                                start=P._iso(open_ts), end=P._iso(now), feed=feed)
        except Exception as exc:  # noqa: BLE001 - the next tick inside the window retries
            return {"status": "retry_pending", "error": redact_exc(exc, 200)}
    issued, abstained = [], []
    for r in chosen:
        sym = r["symbol"]
        b = I._bars(bars.get(sym) or [])
        rng = D.opening_range(b, open_ts, OR_MINUTES)
        if rng is None:
            ledger.abstain(experiment_id=spec.experiment_id, symbol=sym, session_date=ds, now=now,
                           reasons=[f"no 09:30-09:45 opening range ({feed.upper()} 1-minute bars: "
                                    f"{sum(1 for x in b if open_ts <= x[0] < open_ts + OR_MINUTES * 60)} "
                                    f"of {OR_MINUTES}, need {OR_MINUTES // 2})"])
            abstained.append(sym)
            continue
        hi, lo = rng
        ev = {"or_high": hi, "or_low": lo, "or_feed": feed, "card_sipref_price": r.get("sipref_price"),
              "card_sipref_source": r.get("sipref_source"), "catalyst": r.get("catalyst"),
              "avg_dollars": r.get("avg_dollars"), "data_as_of": now,
              "last_close": b[-1][4] if b else None}
        try:
            f = issue_intraday(spec, symbol=sym, session_date=day, entry_ref=hi, issued_at=int(clock()),
                               evidence=ev)
            ledger.record(f, now=int(clock()))
        except ContractError as exc:
            ledger.abstain(experiment_id=spec.experiment_id, symbol=sym, session_date=ds, now=now,
                           reasons=[f"not issued: {exc}"])
            abstained.append(sym)
            continue
        issued.append(sym)
    for r in rest:
        ledger.abstain(experiment_id=spec.experiment_id, symbol=r["symbol"], session_date=ds, now=now,
                       reasons=["ranked below the top 2 by average dollar volume"])
    store.put("edge_orb", ds, {"day": ds, "at": now, "forecasts": issued, "abstained": abstained,
                               "feed": feed})
    return {"status": "issued" if issued else "nothing", "forecasts": issued, "abstained": abstained}
