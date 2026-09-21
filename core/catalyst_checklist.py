"""Transparent, evidence-gated checklist behind every Ghost confidence number.

Ghost's old confidence came from an opaque model score that stopped
discriminating above ~0.55 (setups scored 56%, 65% and 78% all realized ~56%).
An opaque score cannot be debugged: when it is wrong nobody can say which part
was wrong.

This module replaces it with an explicit checklist. Every box is a named,
sourced fact that a person can read. The raw score it produces is
*checklist completeness*, NOT a probability -- turning completeness into an
honest win probability is `core.checklist_calibration`, which measures what
actually happened at each completeness band.

Three honesty rules are structural here, not conventions:

1. UNKNOWN is never a pass. Missing evidence lowers the score; it is never
   quietly dropped from the denominator (that is how a stock with one known
   fact would otherwise score 100%).
2. Correlated boxes cannot stack. A revenue beat, an EPS beat and a margin
   improvement are usually one fact wearing three hats, so boxes live in
   groups and each group contributes at most its own single share.
3. Vetoes only ever subtract. A veto blocks a call outright; it can never add
   confidence.

Read-only scoring. This module never fires a pick and never moves a gate.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


CHECKLIST_VERSION = "catalyst_v1"

# DERIVED, never hardcoded. This was `HOLD_BARS = 3` with a comment claiming it
# matched the trained lane. Production runs V3_LABEL_HOLD_BARS=5, so it did not
# — and checklist_ledger.validate_outcome_contract() fails closed on exactly
# that divergence, which is the first statement of record_snapshot(). Every
# checklist snapshot therefore raised, the shadow writer swallowed it per row
# ("one bad row must not stop the rest"), and the entire checklist lane
# recorded ZERO snapshots with no health signal. Confirmed live 2026-09-04:
# all four calibration cohorts read total_samples=0.
#
# The guard was right; the constant was the bug. Reading the same source the
# resolver reads makes that divergence structurally impossible rather than
# merely detected. Snapshots already written under a different horizon stay
# separated by cohort identity, because outcome_contract embeds this value.
#
# Captured at import: the process must restart to pick up an env change, and
# on Railway an env change restarts the container anyway.
from core.tp_sl_resolve import label_hold_bars as _label_hold_bars

HOLD_BARS = _label_hold_bars()

UP = "UP"
DOWN = "DOWN"
DIRECTIONS = (UP, DOWN)

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"

# Box kinds -------------------------------------------------------------
# directional : sign carries meaning. Positive readings favour UP, negative
#               favour DOWN, so the same box reads both sides of the market.
# magnitude   : bigger is more supportive whichever way the call points
#               (short interest fuels a squeeze up or a collapse down).
# boolean     : plain true/false.
_DIRECTIONAL = "directional"
_MAGNITUDE = "magnitude"
_BOOLEAN = "boolean"


def _num(value: Any) -> Optional[float]:
    """Coerce to float, treating bools and junk as absent rather than 0.0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return out


# ---------------------------------------------------------------------------
# The checklist. Groups exist to stop correlated evidence from stacking.
# `label` is written for a non-trader: it is the sentence shown on the card.
# ---------------------------------------------------------------------------
_GROUPS: Tuple[Dict[str, Any], ...] = (
    {"key": "catalyst", "label": "Something happened to move it"},
    {"key": "fundamentals", "label": "The business itself"},
    {"key": "positioning", "label": "Who is betting against it"},
    {"key": "confirmation", "label": "The market is agreeing"},
    {"key": "context", "label": "The rest of the market"},
)

_BOXES: Tuple[Dict[str, Any], ...] = (
    # -- catalyst ----------------------------------------------------------
    {
        "key": "earnings_surprise",
        "group": "catalyst",
        "kind": _DIRECTIONAL,
        "signal": "earnings_surprise_pct",
        "threshold": 2.0,
        "label_up": "Beat its earnings expectations",
        "label_down": "Missed its earnings expectations",
    },
    {
        "key": "guidance_direction",
        "group": "catalyst",
        "kind": _DIRECTIONAL,
        "signal": "guidance_direction",
        "threshold": 0.5,
        "label_up": "Told investors to expect better ahead",
        "label_down": "Told investors to expect worse ahead",
    },
    {
        "key": "material_news",
        "group": "catalyst",
        "kind": _DIRECTIONAL,
        "signal": "news_sentiment",
        "threshold": 0.25,
        "label_up": "Recent news is good",
        "label_down": "Recent news is bad",
    },
    {
        "key": "leadership_change",
        "group": "catalyst",
        "kind": _DIRECTIONAL,
        "signal": "leadership_change_sentiment",
        "threshold": 0.15,
        "label_up": "A recent leadership change looks like good news",
        "label_down": "A recent leadership change looks like bad news",
    },
    # -- fundamentals ------------------------------------------------------
    {
        "key": "revenue_trend",
        "group": "fundamentals",
        "kind": _DIRECTIONAL,
        "signal": "revenue_growth_pct",
        "threshold": 0.0,
        "label_up": "Selling more than it was last quarter",
        "label_down": "Selling less than it was last quarter",
    },
    {
        "key": "margin_trend",
        "group": "fundamentals",
        "kind": _DIRECTIONAL,
        "signal": "margin_change_pct",
        "threshold": 0.0,
        "label_up": "Keeping more of each dollar it earns",
        "label_down": "Keeping less of each dollar it earns",
    },
    {
        "key": "profit_trend",
        "group": "fundamentals",
        "kind": _DIRECTIONAL,
        "signal": "net_income_growth_pct",
        "threshold": 0.0,
        "label_up": "Profits are growing",
        "label_down": "Profits are shrinking",
    },
    # -- positioning -------------------------------------------------------
    {
        "key": "short_float",
        "group": "positioning",
        "kind": _MAGNITUDE,
        "signal": "short_float_pct",
        "threshold": 15.0,
        "label_up": "A lot of people are betting against it",
        "label_down": "A lot of people are betting against it",
    },
    {
        "key": "days_to_cover",
        "group": "positioning",
        "kind": _MAGNITUDE,
        "signal": "days_to_cover",
        "threshold": 3.0,
        "label_up": "Those bets would take days to unwind",
        "label_down": "Those bets would take days to unwind",
    },
    {
        "key": "borrow_pressure",
        "group": "positioning",
        "kind": _MAGNITUDE,
        "signal": "borrow_fee_pct",
        "threshold": 10.0,
        "label_up": "Betting against it has become expensive",
        "label_down": "Betting against it has become expensive",
    },
    # -- confirmation ------------------------------------------------------
    {
        "key": "relative_volume",
        "group": "confirmation",
        "kind": _MAGNITUDE,
        "signal": "relative_volume",
        "threshold": 2.0,
        "label_up": "Far more people trading it than usual",
        "label_down": "Far more people trading it than usual",
    },
    {
        "key": "premarket_move",
        "group": "confirmation",
        "kind": _DIRECTIONAL,
        "signal": "premarket_gap_pct",
        "threshold": 1.0,
        "label_up": "Already moving up before the bell",
        "label_down": "Already moving down before the bell",
    },
    {
        "key": "trend_agreement",
        "group": "confirmation",
        "kind": _DIRECTIONAL,
        "signal": "trend_slope_pct",
        "threshold": 0.0,
        "label_up": "Its recent direction is upward",
        "label_down": "Its recent direction is downward",
    },
    # -- context -----------------------------------------------------------
    {
        "key": "sector_agreement",
        "group": "context",
        "kind": _DIRECTIONAL,
        "signal": "sector_move_pct",
        "threshold": 0.0,
        "label_up": "Similar companies are moving up too",
        "label_down": "Similar companies are moving down too",
    },
    {
        "key": "market_not_fighting",
        "group": "context",
        "kind": _DIRECTIONAL,
        "signal": "market_move_pct",
        "threshold": 0.0,
        "label_up": "The overall market is not pushing against it",
        "label_down": "The overall market is not pushing against it",
    },
)

# Vetoes can only ever block. They never add to the score.
_VETOES: Tuple[Dict[str, Any], ...] = (
    {
        "key": "already_ran",
        "signal": "move_from_base_pct",
        "limit": 20.0,
        "label": "It already jumped this much before Ghost looked",
        "reason": "Buying something that has already run is how Ghost lost DOMO.",
    },
    {
        "key": "earnings_inside_window",
        "signal": "earnings_days_away",
        "limit": float(HOLD_BARS),
        "mode": "below",
        "label": "Earnings land inside the watch window",
        "reason": "An earnings report mid-window makes the call a coin flip.",
    },
    {
        "key": "thin_liquidity",
        "signal": "avg_dollar_volume",
        "limit": 1_000_000.0,
        "mode": "below",
        "label": "Barely anyone trades it",
        "reason": "Too thin to get in or out of at the prices shown.",
    },
)

_GROUP_KEYS = tuple(g["key"] for g in _GROUPS)
_BOXES_BY_GROUP: Dict[str, Tuple[Dict[str, Any], ...]] = {
    key: tuple(b for b in _BOXES if b["group"] == key) for key in _GROUP_KEYS
}


def _evaluate_box(box: Dict[str, Any], direction: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    """One box -> pass / fail / unknown. Absent evidence is UNKNOWN, never pass."""
    raw = evidence.get(box["signal"])
    value = _num(raw)
    kind = box["kind"]

    if kind == _BOOLEAN:
        if raw is None:
            state = UNKNOWN
        else:
            state = PASS if bool(raw) else FAIL
    elif value is None:
        state = UNKNOWN
    elif kind == _MAGNITUDE:
        state = PASS if value >= box["threshold"] else FAIL
    else:  # directional -- sign carries the meaning, so it reads both sides
        threshold = box["threshold"]
        if direction == UP:
            # A zero threshold still requires a strictly positive sign; neutral
            # cannot support both UP and DOWN.
            state = PASS if (value > 0.0 if threshold == 0 else value >= threshold) else FAIL
        else:
            state = PASS if (value < 0.0 if threshold == 0 else value <= -threshold) else FAIL

    return {
        "key": box["key"],
        "group": box["group"],
        "label": box["label_up"] if direction == UP else box["label_down"],
        "state": state,
        "value": value if kind != _BOOLEAN else raw,
        "signal": box["signal"],
    }


def _evaluate_veto(veto: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    """A veto trips only on evidence. Unknown never trips and never clears."""
    value = _num(evidence.get(veto["signal"]))
    below = veto.get("mode") == "below"
    if value is None:
        tripped, state = False, UNKNOWN
    elif below:
        tripped = value < veto["limit"]
        state = FAIL if tripped else PASS
    else:
        tripped = value > veto["limit"]
        state = FAIL if tripped else PASS
    return {
        "key": veto["key"],
        "label": veto["label"],
        "reason": veto["reason"],
        "state": state,
        "tripped": tripped,
        "value": value,
    }


def evaluate_checklist(
    symbol: str,
    direction: str,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Score `symbol` for a `direction` call against the checklist.

    Returns completeness -- explicitly NOT a win probability. Feed
    ``score_pct`` to `core.checklist_calibration` to get an honest confidence.
    """
    direction = (direction or UP).upper()
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")
    evidence = dict(evidence or {})

    boxes = [_evaluate_box(box, direction, evidence) for box in _BOXES]
    by_key = {b["key"]: b for b in boxes}

    # Each group contributes at most its own equal share, so three correlated
    # boxes inside one group cannot out-vote three independent groups.
    groups: List[Dict[str, Any]] = []
    for meta in _GROUPS:
        members = [by_key[b["key"]] for b in _BOXES_BY_GROUP[meta["key"]]]
        passed = sum(1 for m in members if m["state"] == PASS)
        known = sum(1 for m in members if m["state"] != UNKNOWN)
        fraction = (passed / len(members)) if members else 0.0
        groups.append({
            "key": meta["key"],
            "label": meta["label"],
            "passed": passed,
            "total": len(members),
            "known": known,
            "fraction": round(fraction, 4),
            "confirmed": bool(members) and passed == len(members),
            "boxes": members,
        })

    # Denominator is every group, always. Unknown evidence therefore lowers the
    # score instead of being silently excluded from it.
    score = sum(g["fraction"] for g in groups) / len(groups) if groups else 0.0
    score_pct = round(score * 100.0, 1)

    # Two numbers Ghost must never flatten into one. `evidence_coverage_pct`
    # is how much of the picture is verified at all; `direction_strength_pct`
    # is, of only what is verified, how much confirms this direction. A
    # checklist that is 92% bullish on 43% coverage is a real, useful signal
    # -- and collapsing it into a single score is what hid the YMM
    # DATA_UNAVAILABLE problem in the first place.
    total_boxes_ct = len(boxes)
    known_boxes_ct = sum(1 for b in boxes if b["state"] != UNKNOWN)
    passed_boxes_ct = sum(1 for b in boxes if b["state"] == PASS)
    evidence_coverage_pct = round(100.0 * known_boxes_ct / total_boxes_ct, 1) if total_boxes_ct else 0.0
    direction_strength_pct = (
        round(100.0 * passed_boxes_ct / known_boxes_ct, 1) if known_boxes_ct else None
    )

    vetoes = [_evaluate_veto(v, evidence) for v in _VETOES]
    tripped = [v for v in vetoes if v["tripped"]]

    total_boxes = total_boxes_ct
    known_boxes = known_boxes_ct

    return {
        "symbol": (symbol or "").upper(),
        "direction": direction,
        "checklist_version": CHECKLIST_VERSION,
        "hold_bars": HOLD_BARS,
        "score_pct": score_pct,
        "score_is_probability": False,
        "score_meaning": (
            "How much of Ghost's checklist this stock satisfies. It is not a "
            "chance of being right until it has been calibrated against what "
            "actually happened."
        ),
        "evidence_coverage_pct": evidence_coverage_pct,
        "evidence_coverage_meaning": (
            "How much of the checklist Ghost could actually verify, regardless "
            "of which way it points."
        ),
        "direction_strength_pct": direction_strength_pct,
        "direction_strength_meaning": (
            "Of only the boxes Ghost could verify, how many point this way. "
            "A high number on low coverage means the lean looks strong but is "
            "thinly evidenced -- read both numbers together, never one alone."
        ),
        "boxes_passed": sum(1 for b in boxes if b["state"] == PASS),
        "boxes_total": total_boxes,
        "boxes_known": known_boxes,
        "boxes_unknown": total_boxes - known_boxes,
        "groups_confirmed": sum(1 for g in groups if g["confirmed"]),
        "groups_total": len(groups),
        "groups": groups,
        "vetoes": vetoes,
        "blocked": bool(tripped),
        "blocked_by": [v["key"] for v in tripped],
        "block_reason": tripped[0]["reason"] if tripped else None,
    }


def checklist_spec() -> Dict[str, Any]:
    """Static description of every box -- for the UI and for tests."""
    return {
        "checklist_version": CHECKLIST_VERSION,
        "hold_bars": HOLD_BARS,
        "groups": [
            {
                "key": meta["key"],
                "label": meta["label"],
                "boxes": [
                    {
                        "key": b["key"],
                        "signal": b["signal"],
                        "kind": b["kind"],
                        "label_up": b["label_up"],
                        "label_down": b["label_down"],
                    }
                    for b in _BOXES_BY_GROUP[meta["key"]]
                ],
            }
            for meta in _GROUPS
        ],
        "vetoes": [
            {"key": v["key"], "signal": v["signal"], "label": v["label"], "reason": v["reason"]}
            for v in _VETOES
        ],
    }
