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
                "label": "v3 pick lane",
                "horizon": "~3-day TP/SL holds",
                "philosophy": "Research-grade gate chain — silent most cycles; live journal tracks edge, not marketing accuracy.",
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
        "contract_70": {
            "claim": "70%+ Wilson-proven win rate on the up_prob≥0.70 bucket",
            "status": "UNPROVEN_AT_CURRENT_DATA",
            "preregistered_at": "2026-07-16",
            "basis": (
                "Three offline feature/geometry levers measured null on the "
                "out-of-time harness; live ranking flat across confidence "
                "buckets; 0 family-corrected qualifying slices."
            ),
            "verdict_endpoint": "/api/ghost/contract/70-verdict",
            "note": (
                "Pre-registered falsification and revival criteria live in "
                "core/contract_70_verdict.py. Gates stay strict regardless — "
                "this block changes the claim, never the firing behavior."
            ),
        },
        "operator_note": "Silence on v3 lane is expected when regime/objective gates bind. Squeeze lane is independent.",
    }
