"""Decision replay: would TODAY'S code have decided a past card the same way?

The frozen spec's hash protects the rule's numbers. It does not protect the
code that applies them -- a refactor of a detector or a catalyst pattern could
silently change which names a frozen rule selects, and every later "same rule"
result would quietly be a different rule. Each card row stores its full
inputs; replay re-decides every stored row with the current code and reports
any verdict that changed. A non-empty drift list means the rule changed in
code, and that is a new version, not a fix.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from edge import catalysts as C, detectors as D, setups as S


def _signals(inp: Dict[str, Any]) -> Dict[str, D.Signal]:
    ref = inp.get("ref_price")
    events = inp.get("events")
    usable = None if events is None else [
        C.CatalystEvent(symbol="X", headline=e["headline"], source="replay", url=e.get("url") or "",
                        published_at=e["published_at"], first_seen_at=e["first_seen_at"],
                        kind=C.classify(e["headline"]))
        for e in events]
    return {
        "gap": D.gap(inp.get("prev_close"), ref) if ref else
               D.Signal("gap", D.UNKNOWN, evidence={"missing": inp.get("ref_why") or "no price"}),
        "liquidity": D.liquidity(price=ref or inp.get("prev_close"), avg_shares=inp.get("avg_shares")),
        "catalyst": S.catalyst_signal(usable),
        "not_dilutive": S.dilution_signal(usable),
    }


def replay_card(card: Dict[str, Any], explained: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Drift = a verdict today's code decides differently with the SAME headline classifier. A row
    decided under an older classifier differs by design (the classifier is versioned and its
    forecasts are a separate cohort, task #65): it goes to `explained`, never to drift."""
    drift = []
    for row in card.get("rows") or []:
        inp = row.get("inputs")
        if inp is None:
            continue                     # a card from before inputs were stored
        cls = C.classifier_of({"classifier_version": row.get("classifier_version")}, card.get("day"))
        sig = _signals(inp)
        for strategy, key in (("premarket_continuation", "verdict"), ("gap_baseline", "baseline_verdict")):
            now = S.decide(strategy, sig).verdict
            if row.get(key) is not None and now != row[key]:
                d = {"day": card.get("day"), "symbol": row["symbol"], "strategy": strategy,
                     "recorded": row[key], "replayed": now}
                if cls == C.CLASSIFIER_VERSION:
                    drift.append(d)
                elif explained is not None:
                    explained.append({**d, "classifier": cls, "current_classifier": C.CLASSIFIER_VERSION})
    return drift


def replay_all(store, *, last_n: int = 30) -> Dict[str, Any]:
    cards = sorted(store.scan("edge_cards"), key=lambda c: c.get("day") or "")[-last_n:]
    explained: List[Dict[str, Any]] = []
    drift = [d for c in cards for d in replay_card(c, explained)]
    checked = sum(1 for c in cards for r in c.get("rows") or [] if r.get("inputs") is not None)
    return {"status": "drift" if drift else "consistent", "cards": len(cards), "rows_checked": checked,
            "drift": drift[:50], "classifier": C.CLASSIFIER_VERSION,
            "explained_by_classifier_change": explained[:50]}
