"""What Ghost's confidence number has actually been worth.

The operator asked for a ranked list -- "stock x 58%, stock y 63%, stock z
73%" -- so the highest-conviction name is visible at a glance. Ghost already
computes that number for every symbol on every scan, so the list is a display
change and nothing more.

Shipping it raw would be a trap, and the measurement says so. Over 4,424
resolved shadow outcomes the ordering is INVERTED:

    up   fireable (>=0.55)   56.6%          up   weak (<0.50)   61.5%
    down fireable (>=0.55)   55.1%          down weak (<0.50)   65.8%

The names Ghost is most sure about lose more often than the ones it dismisses.
A list sorted by confidence, descending, would steer the reader toward the
worse bet every single day, and it would look exactly like the tool they asked
for while doing it.

So this module does not hide the number and does not rank on it. It attaches
the receipt: for each probability band, the realized win rate of every
resolved shadow outcome that landed in it, with the sample count and a Wilson
lower bound. A reader seeing

    INTC   Ghost 73%  ->  that band has gone 56.3% (n=412, Wilson 51.4%)

can price the confidence themselves. When the calibration is inverted they see
it inverted rather than trusting it.

Bands are finer than shadow_outcomes._bucket_for's three (fireable/near/weak),
which is coarse enough to hide a monotonic break inside the fireable range.

Read-only diagnostics: no gate, no threshold, no ordering authority. Nothing
here can make a symbol fireable or change what Ghost trades.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.confidence_calibration")

CALIBRATION_VERSION = "confidence_calibration_v1"

# Upper-exclusive edges. Deliberately finer than fireable/near/weak: the whole
# question is whether 0.90 beats 0.60, and a single "fireable" bucket spanning
# 0.55-1.00 cannot answer it.
BANDS: Tuple[Tuple[float, float, str], ...] = (
    (0.00, 0.50, "<50%"),
    (0.50, 0.55, "50-55%"),
    (0.55, 0.60, "55-60%"),
    (0.60, 0.70, "60-70%"),
    (0.70, 0.80, "70-80%"),
    (0.80, 0.90, "80-90%"),
    (0.90, 1.01, "90%+"),
)

# Below this a band's rate is noise quoted to one decimal place. Reported
# either way, but flagged, because a 100% band on n=3 is the most misleading
# number this module could print.
MIN_BAND_SAMPLES = 20


def band_for(prob: Optional[float]) -> Optional[str]:
    """The band label a probability falls in, or None if unusable."""
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    if p != p:  # NaN
        return None
    for lo, hi, label in BANDS:
        if lo <= p < hi:
            return label
    return None


def _wilson_low(wins: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    phat = wins / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * ((phat * (1 - phat) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (centre - margin) / denom)


def build_calibration(rows: List[Dict[str, Any]], *, direction: str) -> Dict[str, Any]:
    """Realized win rate per probability band for one direction.

    A row counts only if it RESOLVED. Pending rows have no outcome and
    including them would quietly deflate every rate.
    """
    lane = str(direction or "").upper()
    tally: Dict[str, Dict[str, int]] = {label: {"n": 0, "wins": 0} for _, _, label in BANDS}
    skipped = 0
    for r in rows:
        if str(r.get("direction") or "").upper() != lane:
            continue
        outcome = str(r.get("outcome") or "").upper()
        if outcome not in ("WIN", "LOSS", "EXPIRED"):
            continue
        label = band_for(r.get("up_prob"))
        if label is None:
            skipped += 1
            continue
        tally[label]["n"] += 1
        if outcome == "WIN":
            tally[label]["wins"] += 1

    bands = []
    for _lo, _hi, label in BANDS:
        n = tally[label]["n"]
        wins = tally[label]["wins"]
        if not n:
            continue
        bands.append({
            "band": label,
            "n": n,
            "wins": wins,
            "realized_pct": round(wins / n * 100.0, 1),
            "wilson_low_pct": round(_wilson_low(wins, n) * 100.0, 1),
            "thin": n < MIN_BAND_SAMPLES,
        })

    return {
        "calibration_version": CALIBRATION_VERSION,
        "direction": lane,
        "bands": bands,
        "total_samples": sum(b["n"] for b in bands),
        "unusable_prob_rows": skipped,
        "min_band_samples": MIN_BAND_SAMPLES,
        "monotonic": _is_monotonic(bands),
        "note": (
            "Realized win rate of resolved shadow outcomes per probability "
            "band. Diagnostics only — never a gate, never an ordering."
        ),
    }


def _is_monotonic(bands: List[Dict[str, Any]]) -> Optional[bool]:
    """Does a higher probability actually win more often?

    None when there is not enough to say. False is the finding that matters:
    it means ranking by confidence ranks the wrong way round, and the operator
    must see that before reading a sorted list.
    """
    usable = [b for b in bands if not b["thin"]]
    if len(usable) < 2:
        return None
    rates = [b["realized_pct"] for b in usable]
    return all(b >= a for a, b in zip(rates, rates[1:]))


def annotate(prob: Optional[float], calibration: Dict[str, Any]) -> Dict[str, Any]:
    """Ghost's number for one symbol, with what that band has been worth.

    Never invents a rate. A band with no history returns history=None so the
    caller renders a blank rather than a number nothing backs.
    """
    label = band_for(prob)
    out: Dict[str, Any] = {
        "up_prob": None if prob is None else round(float(prob), 4),
        "band": label,
        "history": None,
    }
    if label is None:
        out["note"] = "no usable probability"
        return out
    for b in calibration.get("bands") or []:
        if b["band"] == label:
            out["history"] = {
                "realized_pct": b["realized_pct"],
                "n": b["n"],
                "wilson_low_pct": b["wilson_low_pct"],
                "thin": b["thin"],
            }
            break
    if out["history"] is None:
        out["note"] = "band has no resolved history yet"
    elif out["history"]["thin"]:
        out["note"] = f"thin sample (n={out['history']['n']}) — treat as noise"
    return out


def calibrated_confidence_report(days: int = 30) -> Dict[str, Any]:
    """Both lanes, calibrated, for the context payload. Fail-soft."""
    out: Dict[str, Any] = {
        "calibration_version": CALIBRATION_VERSION,
        "lookback_days": days,
        "lanes": {},
    }
    try:
        from core.shadow_outcomes import load_shadow_rows

        rows = load_shadow_rows(days=days)
    except Exception as exc:  # noqa: BLE001 - diagnostics, never a gate
        out["error"] = str(exc)[:160]
        return out

    inverted = []
    for lane in ("UP", "DOWN"):
        calib = build_calibration(rows, direction=lane)
        out["lanes"][lane] = calib
        if calib["monotonic"] is False:
            inverted.append(lane)
    if inverted:
        # Stated in the payload, not left for the reader to infer from a table.
        out["warning"] = (
            "INVERTED CALIBRATION in " + ", ".join(inverted) + ": higher "
            "confidence has NOT won more often. Ranking by confidence would "
            "favour the worse bet. Read each number against its own band."
        )
    return out
