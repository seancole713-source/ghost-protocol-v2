"""Turns checklist completeness into an honest win probability.

`core.catalyst_checklist` answers "how much of the checklist does this stock
satisfy?". That is not the same question as "how often is Ghost right when the
checklist looks like this?", and printing the first number while implying the
second is exactly the failure this rebuild exists to end.

This module joins them the only way that is defensible: by measuring. It takes
resolved predictions, buckets them by the completeness score they carried *at
issue time*, and reports how often each band actually won -- reported at the
Wilson lower bound so a lucky 3-for-3 never reads as 100%.

Until a band has enough resolved samples it returns no confidence at all. A
card that says "not proven at this level yet" is the correct output for a young
band; a confident-looking number would not be.

Read-only. Nothing here fires a pick or moves a gate.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.precision_gate import wilson_lower_bound


CALIBRATION_VERSION = "checklist_calib_v1"

# Bands are 10 points wide: narrow enough to be informative, wide enough to
# accumulate samples in a reasonable number of weeks.
BAND_WIDTH = 10.0

# Below this many resolved samples in a band, Ghost shows no number. Chosen to
# match the project's existing small-sample floor (V3_MIN_TP_SL_WINS = 15) and
# because a Wilson bound on fewer samples is too wide to be worth printing.
MIN_BAND_SAMPLES = 15


def band_for(score_pct: float) -> Tuple[float, float]:
    """The [low, high) band a completeness score falls into."""
    score = max(0.0, min(100.0, float(score_pct)))
    low = min(90.0, (score // BAND_WIDTH) * BAND_WIDTH)
    return (low, low + BAND_WIDTH)


def band_label(low: float, high: float) -> str:
    return f"{int(low)}-{int(high)}%"


def build_calibration(samples: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the completeness -> realized-win-rate map.

    Each sample needs ``score_pct`` (recorded when the prediction was issued,
    never recomputed later) and ``won`` (bool). Samples missing either are
    ignored rather than guessed at.
    """
    buckets: Dict[Tuple[float, float], List[bool]] = {}
    skipped = 0

    for row in samples or []:
        score = row.get("score_pct")
        won = row.get("won")
        if score is None or won is None or isinstance(score, bool):
            skipped += 1
            continue
        try:
            score = float(score)
        except (TypeError, ValueError):
            skipped += 1
            continue
        buckets.setdefault(band_for(score), []).append(bool(won))

    bands: List[Dict[str, Any]] = []
    for (low, high) in sorted(buckets):
        outcomes = buckets[(low, high)]
        n = len(outcomes)
        wins = sum(1 for o in outcomes if o)
        proven = n >= MIN_BAND_SAMPLES
        bands.append({
            "band": band_label(low, high),
            "low": low,
            "high": high,
            "n": n,
            "wins": wins,
            "raw_rate_pct": round(100.0 * wins / n, 1) if n else None,
            # Wilson lower bound: what Ghost can stand behind, not the flattering figure.
            "proven_rate_pct": round(100.0 * wilson_lower_bound(wins, n), 1) if n else None,
            "proven": proven,
            "samples_needed": max(0, MIN_BAND_SAMPLES - n),
        })

    total = sum(b["n"] for b in bands)
    return {
        "calibration_version": CALIBRATION_VERSION,
        "band_width": BAND_WIDTH,
        "min_band_samples": MIN_BAND_SAMPLES,
        "bands": bands,
        "total_samples": total,
        "skipped_samples": skipped,
        "any_proven": any(b["proven"] for b in bands),
    }


def confidence_for(
    score_pct: float,
    calibration: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """The number the card is allowed to print for this completeness score.

    Returns ``confidence_pct=None`` whenever the band has not earned a number
    yet. Callers must render that as "not proven at this level yet" and must
    never fall back to showing ``score_pct`` in its place.
    """
    low, high = band_for(score_pct)
    label = band_label(low, high)
    result: Dict[str, Any] = {
        "score_pct": round(float(score_pct), 1),
        "band": label,
        "confidence_pct": None,
        "proven": False,
        "n": 0,
        "samples_needed": MIN_BAND_SAMPLES,
        "explanation": None,
    }

    bands = (calibration or {}).get("bands") or []
    match = next((b for b in bands if b["band"] == label), None)

    if match is None:
        result["explanation"] = (
            f"Ghost has never resolved a call with a {label} checklist, so it "
            "cannot say how often that turns out right."
        )
        return result

    result["n"] = match["n"]
    result["samples_needed"] = match["samples_needed"]

    if not match["proven"]:
        result["explanation"] = (
            f"Only {match['n']} finished calls have had a {label} checklist. "
            f"Ghost needs {match['samples_needed']} more before it will put a "
            "number on this."
        )
        return result

    result["confidence_pct"] = match["proven_rate_pct"]
    result["proven"] = True
    result["explanation"] = (
        f"When Ghost's checklist was {label}, the price reached the target "
        f"{match['wins']} times out of {match['n']}."
    )
    return result


def calibration_gap(calibration: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """How far completeness sits from reality, per proven band.

    A positive gap means the checklist is flattering itself. Watching this
    shrink is how you know added boxes are actually earning their place.
    """
    rows: List[Dict[str, Any]] = []
    for band in (calibration or {}).get("bands") or []:
        if not band["proven"] or band["proven_rate_pct"] is None:
            continue
        midpoint = (band["low"] + band["high"]) / 2.0
        rows.append({
            "band": band["band"],
            "checklist_midpoint_pct": midpoint,
            "proven_rate_pct": band["proven_rate_pct"],
            "gap_pct": round(midpoint - band["proven_rate_pct"], 1),
            "n": band["n"],
        })
    return {
        "calibration_version": CALIBRATION_VERSION,
        "rows": rows,
        "note": (
            "Positive gap means the checklist claims more than it delivers. "
            "Adding better boxes should shrink it."
        ),
    }
