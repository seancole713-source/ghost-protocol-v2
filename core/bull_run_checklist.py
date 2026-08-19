"""core/bull_run_checklist.py — evidence-gated bull-run checklist.

Encodes the SPCE/YMM lesson directly: a large price target (e.g. YMM $12, a
+36% move) is NOT justified by one good number. It requires MULTIPLE
independent confirmations — revenue beat, EPS beat, growth acceleration,
profitability, guidance, premarket reaction, volume, and staged breakouts.

This is a deterministic, evidence-gated engine:

  - Each check is a pure function of an input value vs. thresholds.
  - A check is GREEN / VERY GREEN / EXTREME / RED / UNKNOWN (missing data).
  - UNKNOWN is NOT a pass — missing evidence never counts toward the target.
  - The composite is a count of confirmed boxes mapped to a decision band.

Two kinds of evidence:
  - AUTO: revenue/EPS surprise, premarket gap, relative volume, price vs
    breakout levels — fetched from free data sources.
  - OPERATOR: company-specific KPIs (transaction-service growth, fulfilled
    orders, active shippers, profitability, guidance) — these are not in
    standard free feeds and must be supplied explicitly. They are NEVER
    fabricated; if absent they are UNKNOWN and do not count.

Read-only intelligence. Never fires a pick or loosens any gate.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ── Check states ───────────────────────────────────────────────────────────
# A check resolves to one of these. UNKNOWN (missing data) never counts as a pass.
STATE_RED = "red"
STATE_GREEN = "green"
STATE_VERY_GREEN = "very_green"
STATE_EXTREME = "extreme"
STATE_UNKNOWN = "unknown"

# States that count as a "confirmed box" toward the target.
_PASS_STATES = {STATE_GREEN, STATE_VERY_GREEN, STATE_EXTREME}


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        out = float(v)
        return out if out == out and out not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _state_for(value: Optional[float], thresholds: Dict[str, float]) -> str:
    """Map a value to a state using ordered thresholds.

    thresholds maps state -> minimum value (inclusive). States are checked in
    descending order of strength: extreme, very_green, green, red. A value
    below the green threshold is RED; a missing value is UNKNOWN.
    """
    if value is None:
        return STATE_UNKNOWN
    # Descending strength order.
    order = [STATE_EXTREME, STATE_VERY_GREEN, STATE_GREEN]
    for st in order:
        if st in thresholds and value >= thresholds[st]:
            return st
    return STATE_RED


# ── Generic checklist ──────────────────────────────────────────────────────

def evaluate_check(
    *,
    key: str,
    label: str,
    value: Optional[float],
    thresholds: Dict[str, float],
    note: str = "",
) -> Dict[str, Any]:
    """Evaluate one check. Returns {key, label, value, state, passed, note}."""
    state = _state_for(value, thresholds)
    return {
        "key": key,
        "label": label,
        "value": value,
        "state": state,
        "passed": state in _PASS_STATES,
        "note": note,
    }


def evaluate_checklist(checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate a list of check results into a summary.

    Returns {confirmed, total, unknown, checks, decision, decision_label}.
    """
    confirmed = sum(1 for c in checks if c.get("passed"))
    total = len(checks)
    unknown = sum(1 for c in checks if c.get("state") == STATE_UNKNOWN)
    decision, label = _decision(confirmed, total)
    return {
        "confirmed": confirmed,
        "total": total,
        "unknown": unknown,
        "decision": decision,
        "decision_label": label,
        "checks": checks,
    }


def _decision(confirmed: int, total: int) -> tuple:
    """Map confirmed-box count to a decision band (operator spec).

    - 8+ confirmed → strong bullish setup (target becomes realistic).
    - 5-7 → moderate (a lower zone is more realistic).
    - 0-4 → do not assume the target.
    """
    if confirmed >= 8:
        return "strong", "Strong bullish setup — target is a realistic bull case"
    if confirmed >= 5:
        return "moderate", "Moderately bullish — a lower zone is more realistic"
    return "weak", "Do not assume the target — insufficient confirmation"


# ── YMM $12 preset ─────────────────────────────────────────────────────────

# Thresholds are the operator's own numbers from the YMM $12 checklist.
# Each maps state -> minimum value (inclusive). Missing value = UNKNOWN.
YMM_12_CHECKS = [
    {
        "key": "revenue_beat",
        "label": "Revenue beat",
        "thresholds": {"green": 470.0, "very_green": 480.0},
        "note": "Expected ~$463M. GREEN $470M+, VERY GREEN $480M+, RED <$455M.",
    },
    {
        "key": "eps_beat",
        "label": "EPS beat",
        "thresholds": {"green": 0.20, "very_green": 0.22},
        "note": "Expected ~$0.19. GREEN $0.20-0.21, VERY GREEN $0.22+, RED <$0.18.",
    },
    {
        "key": "transaction_growth",
        "label": "Transaction-service growth",
        "thresholds": {"green": 30.0, "very_green": 35.0, "extreme": 40.0},
        "note": "YoY %. GREEN >30%, VERY GREEN >35-40%, EXTREME >40%.",
    },
    {
        "key": "order_growth",
        "label": "Order growth",
        "thresholds": {"green": 10.0, "very_green": 15.0, "extreme": 20.0},
        "note": "YoY %. GREEN >10%, VERY GREEN >15%, EXTREME >20%.",
    },
    {
        "key": "shipper_growth",
        "label": "Shipper growth",
        "thresholds": {"green": 10.0, "very_green": 15.0},
        "note": "YoY %. GREEN >10%, VERY GREEN >15%.",
    },
    {
        "key": "profitability",
        "label": "Profitability improves",
        "thresholds": {"green": 1.0},
        "note": "1 = adjusted net income grows YoY / margin improves; 0 = deteriorates.",
    },
    {
        "key": "guidance",
        "label": "Guidance",
        "thresholds": {"green": 1.0, "very_green": 2.0, "extreme": 3.0},
        "note": "1=maintains, 2=raises, 3=raises + accelerating volume/revenue.",
    },
    {
        "key": "premarket_gap",
        "label": "Premarket reaction",
        "thresholds": {"green": 3.0, "very_green": 5.0, "extreme": 10.0},
        "note": "Gap %. GREEN +3-5%, VERY GREEN +5-10%, EXTREME +10-15%. +20%+ = chase risk.",
    },
    {
        "key": "relative_volume",
        "label": "Relative volume",
        "thresholds": {"green": 2.0, "very_green": 3.0, "extreme": 5.0},
        "note": "2x interesting, 3x strong, 5x+ major confirmation. Must accompany price advance.",
    },
    {
        "key": "breakout_950",
        "label": "$9.50 breakout",
        "thresholds": {"green": 9.50},
        "note": "Price clears $9.50 with volume.",
    },
    {
        "key": "breakout_1000",
        "label": "$10 breakout",
        "thresholds": {"green": 10.0},
        "note": "Price clears $10.",
    },
    {
        "key": "breakout_1100",
        "label": "$11 breakout",
        "thresholds": {"green": 11.0},
        "note": "Price clears $11 with accelerating volume.",
    },
]


def build_ymm_12_checklist(values: Optional[Dict[str, Optional[float]]] = None) -> Dict[str, Any]:
    """Evaluate the YMM $12 bull-run checklist against supplied values.

    `values` maps check key -> numeric value. Missing keys are UNKNOWN (do not
    count). This is pure and deterministic — no I/O, no fabrication.
    """
    v = values or {}
    checks = []
    for spec in YMM_12_CHECKS:
        checks.append(evaluate_check(
            key=spec["key"],
            label=spec["label"],
            value=_f(v.get(spec["key"])),
            thresholds=spec["thresholds"],
            note=spec["note"],
        ))
    summary = evaluate_checklist(checks)
    summary["symbol"] = "YMM"
    summary["target"] = 12.0
    summary["target_label"] = "$12"
    summary["disclaimer"] = (
        "This is a scenario checklist, not a guarantee. A large move requires "
        "multiple independent confirmations; missing evidence never counts "
        "toward the target."
    )
    return summary


# ── Auto-fetch layer (free data only) ──────────────────────────────────────

def auto_fill_ymm_12(symbol: str = "YMM") -> Dict[str, Any]:
    """Auto-populate the auto-computable checks from free data sources.

    Fills: revenue_beat, eps_beat (from earnings surprise), premarket_gap,
    relative_volume, and breakout levels (from live price). The company-specific
    KPIs (transaction/order/shipper growth, profitability, guidance) are left
    UNKNOWN — they must be supplied by the operator and are never fabricated.
    """
    values: Dict[str, Optional[float]] = {}

    # Earnings: revenue + EPS actual vs expected.
    try:
        from core.earnings_surprise import get_earnings_surprise
        earn = get_earnings_surprise(symbol)
        if earn.get("available"):
            if earn.get("revenue_actual") is not None:
                values["revenue_beat"] = earn["revenue_actual"]
            if earn.get("eps_actual") is not None:
                values["eps_beat"] = earn["eps_actual"]
    except Exception:
        pass

    # Premarket gap + live price (for breakout levels).
    try:
        from core.prices import get_extended_session, get_intraday_session
        sess = get_extended_session(symbol) or {}
        if str(sess.get("session") or "").lower() == "premarket":
            values["premarket_gap"] = _f(sess.get("gap_pct"))
        intra = get_intraday_session(symbol) or {}
        price = _f(intra.get("price"))
        if price:
            # Breakout checks: the price itself is the value vs the level.
            values["breakout_950"] = price
            values["breakout_1000"] = price
            values["breakout_1100"] = price
    except Exception:
        pass

    # Relative volume: from the squeeze radar if the symbol is an active pick.
    try:
        from core.squeeze_monitor import get_squeeze_picks
        board = get_squeeze_picks() or {}
        picks = board.get("picks") or []
        pick = next((p for p in picks if (p.get("symbol") or "").upper() == symbol.upper()), None)
        if pick and pick.get("rvol") is not None:
            values["relative_volume"] = _f(pick.get("rvol"))
    except Exception:
        pass

    return build_ymm_12_checklist(values)
