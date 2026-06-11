"""Ghost product contract — post-falsification honest positioning (Phase 1)."""
from __future__ import annotations

from typing import Any, Dict

CONTRACT_VERSION = "2.0-post-falsification"


def ghost_contract() -> Dict[str, Any]:
    """Single source of truth for investor-facing product copy."""
    return {
        "version": CONTRACT_VERSION,
        "north_star_retired": True,
        "headline": "Selective directional aid + intraday squeeze radar",
        "lanes": {
            "v3_picks": {
                "label": "Today's v3 pick",
                "horizon": "~3-day TP/SL holds",
                "philosophy": "Silent most cycles; fires only when model + gates agree.",
                "accuracy_claim": None,
            },
            "squeeze_radar": {
                "label": "Intraday squeeze radar",
                "horizon": "Same session (CT extended hours)",
                "philosophy": "RVOL + move thresholds; separate Telegram path from v3.",
                "accuracy_claim": None,
            },
        },
        "falsification": {
            "original_claim": "80% win rate on selective high-conviction WOLF picks",
            "status": "abandoned",
            "reason": "Pre-registered gate: N≥30 resolved picks with WR<70% and 95% CI excludes 80%.",
            "replacement": "Track live win rate, expectancy, and Brier on the pick journal — no fixed accuracy marketing.",
        },
        "operator_note": "Silence on v3 lane is expected when regime/objective gates bind. Squeeze lane is independent.",
    }
