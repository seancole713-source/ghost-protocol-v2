"""Promotion is decided by rules written down BEFORE the results exist.

Stages:
  shadow      forecasts + simulated execution (every experiment starts here)
  paper       the same, with real paper-broker fills (the "actual" record)
  proposable  clears every criterion below -> may be PROPOSED to the operator
  live        never reached by code. Only the operator, explicitly, decides
              to trade a rule with money -- and places the orders.

The criteria are hashed; changing them is a new version, visible in every
report, so a bar lowered after a bad month cannot pass silently.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from edge import stats

CRITERIA = {
    "version": "promotion_v1",
    "min_simulated_filled": 100,        # trades, not days
    "min_paper_filled": 30,
    "min_sessions": 40,                 # distinct days -- clustering, not just trade count
    "wilson_low_above_break_even": True,
    "clustered_low_above_break_even": True,
    "paper_expectancy_positive": True,
    "must_beat_baseline": True,         # simulated win rate above the no-catalyst baseline
    "alpha": 0.05,                      # divided by the number of candidate strategies
}
CRITERIA_HASH = hashlib.sha256(json.dumps(CRITERIA, sort_keys=True).encode()).hexdigest()[:16]

# Retirement is written down before the results too (audit 2026-09-25, F09): without it a rule
# that is clearly below break-even can run forever "undecided". Evaluated on the simulated record
# of ONE feed regime (the ledger never pools IEX and SIP). Hashed like the promotion criteria.
RETIREMENT = {
    "version": "retirement_v1",
    "min_simulated_filled": 30,          # never retire on a handful of trades
    "wilson_high_below_break_even": True,  # the WHOLE interval below break-even -> retire
    "paper_expectancy_negative_confirms": True,  # shown beside it, never required
}
RETIREMENT_HASH = hashlib.sha256(json.dumps(RETIREMENT, sort_keys=True).encode()).hexdigest()[:16]


def evaluate(report: Dict[str, Any], *, baseline: Dict[str, Any], sessions: int,
             by_day: Dict[str, List[bool]], n_candidates: int) -> Dict[str, Any]:
    sim = report["records"]["simulated"]
    act = report["records"]["actual"]
    be = report["break_even"]
    unmet = []
    if (sim.get("filled") or 0) < CRITERIA["min_simulated_filled"]:
        unmet.append(f"simulated trades {sim.get('filled') or 0}/{CRITERIA['min_simulated_filled']}")
    if (act.get("filled") or 0) < CRITERIA["min_paper_filled"]:
        unmet.append(f"paper trades {act.get('filled') or 0}/{CRITERIA['min_paper_filled']}")
    if sessions < CRITERIA["min_sessions"]:
        unmet.append(f"sessions {sessions}/{CRITERIA['min_sessions']}")
    alpha = stats.bonferroni_alpha(CRITERIA["alpha"], n_candidates)
    from statistics import NormalDist
    z = NormalDist().inv_cdf(1 - alpha / 2)      # exact two-sided z for the corrected alpha
    n, k = sim.get("filled") or 0, sim.get("wins") or 0
    lo = stats.wilson(k, n, z=z)[0] if n else 0.0
    if not lo > be:
        unmet.append(f"Wilson lower bound {lo:.1%} not above break-even {be:.1%} (alpha {alpha:.4f})")
    clo = stats.clustered_bootstrap_ci(by_day, alpha=alpha)[0] if by_day else None
    if clo is None or not clo > be:
        unmet.append("day-clustered lower bound not above break-even")
    exp = (act.get("expectancy_usd") or {}).get("mean")
    if not (isinstance(exp, (int, float)) and exp > 0):
        unmet.append("paper expectancy not positive")
    bw = baseline["records"]["simulated"].get("win_rate")
    if sim.get("win_rate") is None or bw is None or not sim["win_rate"] > bw:
        unmet.append("does not beat the no-catalyst baseline")
    # "paper" means the rule traded AND the broker agreed; an actual fill alone (e.g. one outside
    # the rule, which the report no longer counts) never advances a stage.
    stage = "proposable" if not unmet else (
        "paper" if (act.get("filled") or 0) and (sim.get("filled") or 0) else "shadow")
    return {"stage": stage, "unmet": unmet, "criteria_version": CRITERIA["version"],
            "criteria_hash": CRITERIA_HASH,
            "live": "never automatic -- requires the operator's explicit decision"}


def retirement(report: Dict[str, Any]) -> Dict[str, Any]:
    """Should this experiment be retired? Only when enough trades show the whole Wilson interval
    of the simulated win rate below break-even. Retiring is a recommendation to the operator, like
    promotion; the code never stops an experiment on its own."""
    sim = (report.get("records") or {}).get("simulated") or {}
    act = (report.get("records") or {}).get("actual") or {}
    n = sim.get("filled") or 0
    be = report.get("break_even")
    ci = sim.get("win_rate_ci") or [None, None]
    out = {"retirement_version": RETIREMENT["version"], "retirement_hash": RETIREMENT_HASH,
           "feed_regime": report.get("feed_regime")}
    if n < RETIREMENT["min_simulated_filled"] or be is None or ci[1] is None:
        return {**out, "retire": False,
                "why": f"too few simulated trades to judge ({n}/{RETIREMENT['min_simulated_filled']})"}
    if ci[1] < be:
        paper = (act.get("expectancy_usd") or {}).get("mean") if isinstance(act.get("expectancy_usd"), dict) else None
        return {**out, "retire": True,
                "why": (f"whole interval below break-even: {ci[0]:.1%}-{ci[1]:.1%} vs {be:.1%} "
                        f"over {n} simulated trades"
                        + (f"; paper expectancy {paper:+.2f}/trade" if paper is not None else ""))}
    return {**out, "retire": False, "why": "the interval reaches break-even; keep collecting"}
