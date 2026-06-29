"""Super Ghost Top Pick Evidence Gate (PR #105).

Top Picks must be earned, not displayed because a card looks bullish. This gate
centralizes the evidence contract so the UI cannot accidentally promote a stock
using only direction win-rate while ignoring precision, calibration, expectancy,
or kill-condition state.

Prediction intelligence only. No auto-trading and no guaranteed returns.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

LOGGER = logging.getLogger("ghost.super_ghost_top_picks")

MIN_COMPLETED = 5
MIN_DIRECTION_WIN_RATE = 0.70
MIN_PRECISION_SCORE = 60.0
MIN_PRECISION_SAMPLES = 5
MIN_PROFIT_FACTOR = 1.05


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _ok_bool(v: Any) -> bool:
    return bool(v) and v is not None


def evaluate_top_pick_gate(symbol: str, *, horizon: int = 5) -> Dict[str, Any]:
    sym = (symbol or "").strip().upper() or "WOLF"
    h = horizon if horizon in (1, 5, 20) else 5
    checks = []
    errors = []

    def add_check(key: str, passed: bool, current: Any, required: Any, reason: str) -> None:
        checks.append({"key": key, "passed": bool(passed), "current": current, "required": required, "reason": reason})

    # Direction proof from Truth Ledger.
    try:
        from core.super_ghost_ledger import get_accuracy, get_if_followed
        acc = get_accuracy(symbol=sym, horizon=h)
        ifp = get_if_followed(symbol=sym, horizon=h)
    except Exception as exc:
        acc = {"ok": False, "error": str(exc)[:120]}
        ifp = {"ok": False, "error": str(exc)[:120]}
        errors.append(str(exc)[:120])

    overall = (acc or {}).get("overall") or {}
    completed = int(overall.get("n") or (acc or {}).get("resolved_at_horizon") or 0)
    wr = _f(overall.get("win_rate"))
    add_check("completed_predictions", completed >= MIN_COMPLETED, completed, f">={MIN_COMPLETED}", "Enough resolved predictions exist to evaluate the symbol.")
    add_check("direction_win_rate", wr is not None and wr >= MIN_DIRECTION_WIN_RATE, wr, f">={MIN_DIRECTION_WIN_RATE}", "Directional win-rate must be proven; correct DOWN calls count when price falls.")

    # Precision proof from Precision Brain.
    try:
        from core.super_ghost_precision import precision_summary
        prec = precision_summary(symbol=sym, horizon=h, limit=20)
    except Exception as exc:
        prec = {"ok": False, "error": str(exc)[:120], "primary_profile": None}
        errors.append(str(exc)[:120])
    pprof = (prec or {}).get("primary_profile") or {}
    pscore = _f(pprof.get("avg_precision_score"))
    psamples = int(pprof.get("sample_count") or 0)
    add_check("precision_samples", psamples >= MIN_PRECISION_SAMPLES, psamples, f">={MIN_PRECISION_SAMPLES}", "Enough precision-scored outcomes exist.")
    add_check("precision_score", pscore is not None and pscore >= MIN_PRECISION_SCORE, pscore, f">={MIN_PRECISION_SCORE}", "Average price precision must be high enough; a WIN alone is not enough.")

    # Calibration readiness: range OR regime slice can satisfy once available.
    try:
        from core.super_ghost_range_calibration import range_calibration_summary
        range_summary = range_calibration_summary(symbol=sym, horizon=h, limit=20)
    except Exception as exc:
        range_summary = {"ok": False, "error": str(exc)[:120], "primary_profile": None}
        errors.append(str(exc)[:120])
    try:
        from core.super_ghost_regime_calibration import regime_calibration_summary
        regime_summary = regime_calibration_summary(symbol=sym, horizon=h, limit=20)
    except Exception as exc:
        regime_summary = {"ok": False, "error": str(exc)[:120], "primary_profile": None}
        errors.append(str(exc)[:120])
    rprof = (range_summary or {}).get("primary_profile") or {}
    gprof = (regime_summary or {}).get("primary_profile") or {}
    range_ready = _ok_bool(rprof.get("available")) or int(rprof.get("sample_count") or 0) >= 5
    regime_ready = _ok_bool(gprof.get("available")) or int(gprof.get("sample_count") or 0) >= 5
    add_check("calibrated_range_available", range_ready or regime_ready, {"range": range_ready, "regime": regime_ready}, "range or regime profile available", "Top Picks requires calibrated target/stop/range evidence, not raw guesses.")

    # If-followed profitability guard.
    pf = _f((ifp or {}).get("profit_factor"))
    net = _f((ifp or {}).get("net_return_pct"))
    followed = int((ifp or {}).get("followed_calls") or 0)
    profitable = followed >= MIN_COMPLETED and ((pf is not None and pf >= MIN_PROFIT_FACTOR) or (net is not None and net > 0))
    add_check("positive_if_followed", profitable, {"followed_calls": followed, "profit_factor": pf, "net_return_pct": net}, f"profit_factor>={MIN_PROFIT_FACTOR} or net_return>0", "Directional calls must show positive evidence if followed; accuracy without expectancy is insufficient.")

    # Global kill-condition guard.
    try:
        from core.prediction import evaluate_kill_conditions
        kill = evaluate_kill_conditions(include_pause=True)
    except Exception as exc:
        kill = {"ok": False, "error": str(exc)[:120], "any_triggered": True}
        errors.append(str(exc)[:120])
    kill_clear = bool(kill.get("ok") is not False and not kill.get("any_triggered") and not ((kill.get("engine_pause") or {}).get("paused")))
    add_check("kill_conditions_clear", kill_clear, {"any_triggered": kill.get("any_triggered"), "engine_pause": (kill.get("engine_pause") or {}).get("paused")}, "clear", "No Top Pick can promote while kill conditions or engine pause are active.")

    passed = all(c["passed"] for c in checks)
    blocking = [c for c in checks if not c["passed"]]
    decision = "ELIGIBLE_FOR_TOP_PICKS" if passed else "LOCKED"
    return {
        "ok": True,
        "symbol": sym,
        "horizon_days": h,
        "decision": decision,
        "eligible": passed,
        "checks": checks,
        "blocking_reasons": blocking,
        "metrics": {
            "completed_predictions": completed,
            "direction_win_rate": wr,
            "precision_score": pscore,
            "precision_samples": psamples,
            "range_calibration_ready": range_ready,
            "regime_calibration_ready": regime_ready,
            "profit_factor": pf,
            "net_return_pct": net,
            "followed_calls": followed,
            "kill_any_triggered": kill.get("any_triggered"),
        },
        "source_status": {
            "accuracy_ok": (acc or {}).get("ok") is not False,
            "precision_ok": (prec or {}).get("ok") is not False,
            "range_ok": (range_summary or {}).get("ok") is not False,
            "regime_ok": (regime_summary or {}).get("ok") is not False,
            "if_followed_ok": (ifp or {}).get("ok") is not False,
            "kill_ok": (kill or {}).get("ok") is not False,
        },
        "errors": errors[:5],
        "disclaimer": "Prediction intelligence only; not financial advice and not auto-trading.",
    }
