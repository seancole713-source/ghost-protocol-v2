"""The daily Top 10: every priced card candidate ranked by setup QUALITY, not by raw % gain.

Operator request, 2026-09-24: "see the full market and make the best 10 predictions each
day". Ten a day is a LEARNING list, not a trade quota: no order is ever placed from it.
Each is graded after the close exactly like the card counterfactuals (frozen Gap-and-Go
levels, same resolver), so within weeks the scorecard can say whether a quality score
picks better stocks than the rest -- about 50 graded calls a week instead of about 5.

The score uses only what the card knows at issue time, all point-in-time:
  gap vs the prior close   a 5-25% move is the sweet spot; over 40% is exhausted
  dated company news       FDA / earnings / guidance / M&A beat contract / index / analyst
  dilution, reverse split  an offering or a split is a sell signal, never a reason to buy
  liquidity                3-month average dollar volume
  price                    $2-$500, as rule E2
Premarket relative volume is NOT in it: the free IEX feed cannot measure it honestly
before the open. Weights are fixed and versioned (METHOD); changing them is a new METHOD.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from edge import catalysts as C

METHOD = "top10_v1"
SIZE = 10
STRONG = frozenset({C.FDA, C.EARNINGS, C.GUIDANCE, C.MNA})
MEDIUM = frozenset({C.CONTRACT, C.INDEX, C.ANALYST})


def _gap_pct(row: Dict[str, Any]) -> Optional[float]:
    ref, prev = row.get("ref_price"), row.get("prev_close")
    if not ref or not prev:
        return None
    return (float(ref) / float(prev) - 1) * 100


def score(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """{"score", "reasons", "gap_pct", "catalyst_kind"} for a priced gainer; None otherwise."""
    gap = _gap_pct(row)
    if gap is None or gap <= 0:
        return None
    pts, why = 0, []
    if 5 <= gap <= 25:
        pts, _ = pts + 30, why.append(f"gap +{gap:.1f}% (5-25% sweet spot) +30")
    elif 25 < gap <= 40:
        pts, _ = pts + 15, why.append(f"gap +{gap:.1f}% (large) +15")
    elif gap > 40:
        pts, _ = pts - 20, why.append(f"gap +{gap:.1f}% (over 40%, exhausted) -20")
    elif gap >= 3:
        pts, _ = pts + 10, why.append(f"gap +{gap:.1f}% (small) +10")
    else:
        why.append(f"gap +{gap:.1f}% (under 3%) +0")
    kinds = {e.get("kind") for e in ((row.get("inputs") or {}).get("events") or [])}
    kind = None
    if C.OFFERING in kinds:
        pts, _ = pts - 40, why.append("offering / dilution in the last 24h -40")
    if C.REVERSE_SPLIT in kinds:
        pts, _ = pts - 25, why.append("reverse split -25")
    if kinds & STRONG:
        kind = sorted(kinds & STRONG)[0]
        pts, _ = pts + 30, why.append(f"company news: {kind} +30")
    elif kinds & MEDIUM:
        kind = sorted(kinds & MEDIUM)[0]
        pts, _ = pts + 20, why.append(f"company news: {kind} +20")
    else:
        why.append("no dated company news +0")
    dollars = float(row.get("avg_dollars") or 0)
    if dollars >= 20e6:
        pts, _ = pts + 20, why.append(f"liquid (${dollars / 1e6:.0f}M/day) +20")
    elif dollars >= 5e6:
        pts, _ = pts + 10, why.append(f"tradeable (${dollars / 1e6:.1f}M/day) +10")
    else:
        pts, _ = pts - 10, why.append(f"thin (${dollars / 1e6:.1f}M/day) -10")
    price = float(row["ref_price"])
    if 2 <= price <= 500:
        pts, _ = pts + 10, why.append("price in $2-$500 +10")
    else:
        pts, _ = pts - 30, why.append(f"price ${price:.2f} outside $2-$500 -30")
    return {"score": pts, "reasons": why, "gap_pct": round(gap, 2), "catalyst_kind": kind}


def build(card: Dict[str, Any]) -> Dict[str, Any]:
    """The day's ranked list from a card's rows, ties broken by dollar volume."""
    ranked = []
    for r in card.get("rows") or []:
        s = score(r)
        if s is not None:
            ranked.append({"symbol": r["symbol"], "ref_price": r.get("ref_price"),
                           "avg_dollars": r.get("avg_dollars"), "catalyst": r.get("catalyst"), **s})
    ranked.sort(key=lambda x: (-x["score"], -(x.get("avg_dollars") or 0)))
    top = [{"rank": i + 1, **x} for i, x in enumerate(ranked[:SIZE])]
    return {"day": card.get("day"), "issued_at": card.get("issued_at"), "method": METHOD,
            "considered": len(ranked), "candidates": len(card.get("rows") or []), "list": top,
            "note": ("a learning list, never a trade: graded after the close at the frozen Gap-and-Go "
                     "levels; only priced gainers can be ranked")}


def with_outcomes(store, day: str) -> Optional[Dict[str, Any]]:
    """The stored list, each entry joined to its after-close counterfactual grade if graded."""
    t = store.get("edge_top10", day)
    if not t:
        return None
    graded = store.get("edge_card_outcomes", day)
    by_sym = {o["symbol"]: o for o in (graded or {}).get("rows") or []}
    out = dict(t)
    # outcome = the frictionless forecast grade; execution = the order as written, with costs (U12).
    out["list"] = [{**x, "outcome": (by_sym.get(x["symbol"]) or {}).get("outcome"),
                    "execution": (by_sym.get(x["symbol"]) or {}).get("execution")} for x in t.get("list") or []]
    out["graded"] = graded is not None
    return out


def latest_day(store) -> Optional[str]:
    rows = store.scan("edge_top10")
    return max((r["day"] for r in rows if r.get("day")), default=None)
