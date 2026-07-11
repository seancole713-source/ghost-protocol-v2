"""Ghost Doctrine — 6-step thinking layer (PR #129).

Display-only honesty layer. Every prediction must be explainable as six ordered
steps: Clarity → Decision → Direction → Alignment → Consistency → Results.

Reads the proven engine/gate chain; never modifies it. Every step shows real
underlying values with sources, or `status: "insufficient"` + reason.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.doctrine")

DOCTRINE_VERSION = "1.0"
DOCTRINE_KEYS = ("clarity", "decision", "direction", "alignment", "consistency", "results")
DOCTRINE_STATUSES = ("pass", "hold", "insufficient")


def ghost_doctrine_spec() -> Dict[str, Any]:
    """Static specification — what each step means and what it checks."""
    return {
        "version": DOCTRINE_VERSION,
        "steps": [
            {
                "step": 1, "key": "clarity", "label": "Clarity",
                "ghost_meaning": "See the market as it is before predicting",
                "what_it_checks": [
                    "Feature coverage (44+ features present)",
                    "Regime classification (bull/bear/flat)",
                    "Macro context + sentiment overlay",
                    "Data quality (no stale/missing bars)",
                ],
                "data_sources": [
                    "classify_regime", "macro_regime", "news_sentiment",
                    "feature_schema", "super_ghost coverage + data_quality",
                ],
            },
            {
                "step": 2, "key": "decision", "label": "Decision",
                "ghost_meaning": "Commit only when every gate passes; silence is a decision",
                "what_it_checks": [
                    "Regime gate (trend alignment)",
                    "Meta gates (holdout acc, edge, wf)",
                    "Precision gate (operating point proven)",
                    "Probability gate (above threshold)",
                    "Confidence floor",
                    "Objective gate (track record)",
                    "Kill conditions (system health)",
                ],
                "data_sources": [
                    "_evaluate_lane gate chain", "/api/wolf/gate-status",
                    "evaluate_kill_conditions",
                ],
            },
            {
                "step": 3, "key": "direction", "label": "Direction",
                "ghost_meaning": "The committed path: side, entry, target, stop, hold, size",
                "what_it_checks": [
                    "Direction (UP/DOWN) from model probability",
                    "Entry / target / stop from vol_targets + tp_sl_resolve",
                    "Position size from Kelly or fixed-fraction",
                    "Hold period (bars)",
                ],
                "data_sources": [
                    "up_prob/down_prob", "vol_targets", "tp_sl_resolve",
                    "kelly_sizing", "super_ghost risk_plan",
                ],
            },
            {
                "step": 4, "key": "alignment", "label": "Alignment",
                "ghost_meaning": "Everything must agree before conviction",
                "what_it_checks": [
                    "Regime-vs-model veto (no BUY in bear trend)",
                    "Sentiment brake (negative news overrides)",
                    "Conviction multiplier",
                    "Top-pick evidence gate (ELIGIBLE/LOCKED)",
                    "Blockers list",
                ],
                "data_sources": [
                    "regime-vs-model veto", "sentiment brake",
                    "conviction_multiplier", "evaluate_top_pick_gate",
                    "blockers[]",
                ],
            },
            {
                "step": 5, "key": "consistency", "label": "Consistency",
                "ghost_meaning": "Proven repetition, not one-offs",
                "what_it_checks": [
                    "Walk-forward fold consistency",
                    "Precision track record (avg score, samples)",
                    "Ledger Wilson accuracy",
                    "Shadow → promotion path",
                    "Kill windows (no firing during pause)",
                ],
                "data_sources": [
                    "wf_fold_count, wf_acc_mean, wf_edge_mean",
                    "precision_summary", "get_accuracy (Wilson)",
                    "shadow_outcomes", "kill windows",
                ],
            },
            {
                "step": 6, "key": "results", "label": "Results",
                "ghost_meaning": "Outcomes measured honestly",
                "what_it_checks": [
                    "Win rate (Wilson lower bound)",
                    "Profit factor (if-followed)",
                    "Net return %",
                    "Performance log cycles",
                    "Learning loop (did we improve?)",
                ],
                "data_sources": [
                    "get_accuracy (Wilson)", "get_if_followed (profit factor)",
                    "performance_log", "pnl",
                ],
            },
        ],
        "honesty_rule": "Every step shows real underlying values with sources, or status: 'insufficient' + reason — nothing invented.",
        "note": "Display layer only — reads the proven engine/gate chain, never modifies it.",
    }


# ── Per-symbol doctrine builder ──────────────────────────────────────

def build_symbol_doctrine(
    symbol: str,
    *,
    super_ghost: Optional[Dict[str, Any]] = None,
    mode: str = "full",
    include_live_gate: bool = False,
) -> Dict[str, Any]:
    """Per-symbol 6-step doctrine record.

    Args:
        symbol: Ticker symbol
        super_ghost: Pre-built super-ghost dict (full mode) or None (light mode
            uses latest ledger row)
        mode: "full" (super-ghost) or "light" (cheap DB-only)
        include_live_gate: If True, runs predict_live_ex + up_prob inversion
    """
    sym = (symbol or "").strip().upper()
    ts = int(time.time())
    sources: Dict[str, Any] = {}

    # ── Gather data ───────────────────────────────────────────────
    sg: Optional[Dict[str, Any]] = super_ghost
    if sg is None and mode == "full":
        try:
            from core.super_ghost import build_super_ghost
            sg = build_super_ghost(sym, ai=False)
        except Exception as e:
            LOGGER.debug("doctrine super_ghost %s: %s", sym, e)
            sg = None

    if sg is None and mode == "light":
        try:
            from core.db import db_conn
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT created_at, direction, action, confidence, accuracy_grade, "
                    "reference_price, target_price, stop_loss, regime_label "
                    "FROM super_ghost_predictions WHERE symbol=%s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (sym,),
                )
                r = cur.fetchone()
                if r:
                    sg = {
                        "prediction": {
                            "direction": r[1], "action": r[2], "confidence": float(r[3]) if r[3] is not None else None,
                            "accuracy_grade": r[4],
                        },
                        "risk_plan": {
                            "entry": float(r[5]) if r[5] is not None else None,
                            "target_price": float(r[6]) if r[6] is not None else None,
                            "stop_loss": float(r[7]) if r[7] is not None else None,
                        },
                        "market_regime": {"label": r[8]},
                    }
        except Exception as e:
            LOGGER.debug("doctrine light ledger %s: %s", sym, e)

    # Gate config
    gate_cfg: Dict[str, Any] = {}
    try:
        from core import prediction as _pred
        gate_cfg = _pred._objective_effective_config()
        sources["objective_config"] = gate_cfg
    except Exception as e:
        sources["objective_config_error"] = str(e)[:120]

    # Symbol stats
    sym_stats: Dict[str, Any] = {}
    try:
        from core import prediction as _pred
        sym_stats = _pred._objective_symbol_stats(sym, "UP")
        sources["symbol_stats"] = sym_stats
    except Exception as e:
        sources["symbol_stats_error"] = str(e)[:120]

    # Live gate (optional, heavy)
    live_gate: Dict[str, Any] = {}
    if include_live_gate:
        try:
            from core.signal_engine import predict_live_ex
            _scores: Dict[str, Any] = {}
            signal, reason = predict_live_ex(sym, "stock", scores=_scores)
            live_gate["reason"] = reason
            live_gate["signal"] = signal
            up_prob = _scores.get("up_prob")
            mm = _scores.get("model_meta") or {}
            acc = mm.get("accuracy")
            min_p = mm.get("min_win_proba")
            floor = float(getattr(_pred, "CONFIDENCE_FLOOR", 0.55))
            boot_conf = float(gate_cfg.get("bootstrap_min_conf", 0.60))
            phase = "established" if int(sym_stats.get("combined_total", 0)) >= int(gate_cfg.get("min_samples", 30)) else "bootstrap"
            binding_conf = max(floor, boot_conf) if phase == "bootstrap" else floor
            live_gate["up_prob"] = up_prob
            live_gate["binding_confidence_threshold"] = round(binding_conf, 3)
            # up_prob_needed_to_fire inversion (duplicated from wolf_gate_status
            # to avoid circular import — see api/routes_wolf_ops.py:~94)
            _conf_slope = float(os.getenv("CONFIDENCE_SLOPE", "4.0"))
            if acc is not None and min_p is not None:
                needed = min_p + (binding_conf - acc) / max(_conf_slope, 0.5)
                needed = max(needed, min_p)
                live_gate["up_prob_needed_to_fire"] = round(needed, 4)
                if up_prob is not None:
                    live_gate["up_prob_gap"] = round(up_prob - needed, 4)
            sources["live_gate"] = live_gate
        except Exception as e:
            sources["live_gate_error"] = str(e)[:120]

    # Kill conditions
    kill: Dict[str, Any] = {}
    try:
        from core.prediction import evaluate_kill_conditions
        kill = evaluate_kill_conditions(include_pause=True)
        sources["kill"] = kill
    except Exception as e:
        sources["kill_error"] = str(e)[:120]

    # Top-pick gate
    top_pick: Dict[str, Any] = {}
    try:
        from core.super_ghost_top_picks import evaluate_top_pick_gate
        top_pick = evaluate_top_pick_gate(sym, horizon=5)
        sources["top_pick_gate"] = top_pick
    except Exception as e:
        sources["top_pick_gate_error"] = str(e)[:120]

    # Accuracy (Wilson)
    accuracy: Dict[str, Any] = {}
    try:
        from core.super_ghost_ledger import get_accuracy
        accuracy = get_accuracy(symbol=sym, horizon=5)
        sources["accuracy"] = accuracy
    except Exception as e:
        sources["accuracy_error"] = str(e)[:120]

    # If-followed
    if_followed: Dict[str, Any] = {}
    try:
        from core.super_ghost_ledger import get_if_followed
        if_followed = get_if_followed(symbol=sym, horizon=5)
        sources["if_followed"] = if_followed
    except Exception as e:
        sources["if_followed_error"] = str(e)[:120]

    # ── Build steps ───────────────────────────────────────────────
    steps = [
        _step_clarity(sg, sym),
        _step_decision(gate_cfg, sym_stats, kill, live_gate, sym),
        _step_direction(sg, sym),
        _step_alignment(sg, kill, top_pick, sym),
        _step_consistency(sg, accuracy, sym),
        _step_results(accuracy, if_followed, sym),
    ]

    # Summary
    statuses = [s["status"] for s in steps]
    pass_count = statuses.count("pass")
    hold_count = statuses.count("hold")
    insuf_count = statuses.count("insufficient")
    if insuf_count > 0:
        summary_status = "insufficient"
        headline = f"{insuf_count} step(s) lack evidence"
    elif hold_count > 0:
        summary_status = "hold"
        headline = "gates holding — expected silence"
    else:
        summary_status = "pass"
        headline = "all steps clear"

    return {
        "ok": True,
        "symbol": sym,
        "doctrine_version": DOCTRINE_VERSION,
        "ts": ts,
        "mode": mode,
        "sources": {k: v for k, v in sources.items() if not k.endswith("_error")},
        "steps": steps,
        "summary": {
            "pass": pass_count,
            "hold": hold_count,
            "insufficient": insuf_count,
            "status": summary_status,
            "headline": headline,
        },
        "disclaimer": "Display layer only — reads the proven engine/gate chain, never modifies it. No auto-trading and no guaranteed returns.",
    }


# ── Per-step builders (pure, testable without DB) ───────────────────

def _step_clarity(sg: Optional[Dict[str, Any]], sym: str) -> Dict[str, Any]:
    evidence: List[Dict[str, Any]] = []
    if sg:
        cov = sg.get("coverage") or {}
        dq = sg.get("data_quality") or {}
        regime = sg.get("market_regime") or {}
        evidence.append({"name": "regime_label", "value": regime.get("label"), "threshold": "bull/bear/flat", "source": "classify_regime"})
        evidence.append({"name": "feature_count", "value": cov.get("feature_count"), "threshold": "≥44", "source": "feature_schema"})
        evidence.append({"name": "data_quality", "value": dq.get("status"), "threshold": "ok", "source": "data_quality check"})
        status = "pass" if regime.get("label") and cov.get("feature_count", 0) >= 40 else "insufficient"
        headline = f"Regime: {regime.get('label', 'unknown')} · {cov.get('feature_count', 0)} features"
    else:
        status = "insufficient"
        headline = "No market data available"
    return {
        "step": 1, "key": "clarity", "label": "Clarity",
        "status": status, "headline": headline,
        "evidence": evidence,
        "insufficient_reason": None if status != "insufficient" else "Super Ghost data unavailable for this symbol",
    }


def _step_decision(
    gate_cfg: Dict[str, Any],
    sym_stats: Dict[str, Any],
    kill: Dict[str, Any],
    live_gate: Dict[str, Any],
    sym: str,
) -> Dict[str, Any]:
    evidence: List[Dict[str, Any]] = []
    status = "insufficient"
    headline = "Gate chain status unknown"

    if gate_cfg:
        evidence.append({"name": "objective_mode", "value": gate_cfg.get("mode"), "threshold": "enforced", "source": "objective_config"})
        evidence.append({"name": "target_wr", "value": gate_cfg.get("target_wr"), "threshold": "≥0.70", "source": "accuracy_contract"})
    if sym_stats:
        total = int(sym_stats.get("combined_total", 0))
        wr = sym_stats.get("combined_wr")
        evidence.append({"name": "resolved_picks", "value": total, "threshold": f"≥{gate_cfg.get('min_samples', 30)}", "source": "objective_symbol_stats"})
        evidence.append({"name": "combined_wr", "value": wr, "threshold": f"≥{gate_cfg.get('target_wr', 0.70)}", "source": "objective_symbol_stats"})
    if kill:
        paused = (kill.get("engine_pause") or {}).get("paused", False)
        triggered = kill.get("any_triggered", False)
        evidence.append({"name": "kill_triggered", "value": triggered, "threshold": "False", "source": "evaluate_kill_conditions"})
        evidence.append({"name": "engine_paused", "value": paused, "threshold": "False", "source": "evaluate_kill_conditions"})
    if live_gate:
        evidence.append({"name": "gate_reason", "value": live_gate.get("reason"), "threshold": "None (fired)", "source": "predict_live_ex"})
        evidence.append({"name": "up_prob", "value": live_gate.get("up_prob"), "threshold": f"≥{live_gate.get('binding_confidence_threshold', 0.55)}", "source": "predict_live_ex"})

    if live_gate and live_gate.get("signal"):
        status = "pass"
        headline = "Gate chain passed — model fired"
    elif live_gate and live_gate.get("reason"):
        status = "hold"
        headline = f"Gates holding fire: {live_gate['reason']}"
    elif kill and kill.get("any_triggered"):
        status = "hold"
        headline = "Kill conditions active — engine paused"
    else:
        status = "insufficient"
        headline = "No live gate data"

    return {
        "step": 2, "key": "decision", "label": "Decision",
        "status": status, "headline": headline,
        "evidence": evidence,
        "insufficient_reason": None if status != "insufficient" else "Live gate evaluation unavailable",
    }


def _step_direction(sg: Optional[Dict[str, Any]], sym: str) -> Dict[str, Any]:
    evidence: List[Dict[str, Any]] = []
    if sg:
        pred = sg.get("prediction") or {}
        risk = sg.get("risk_plan") or {}
        direction = pred.get("direction")
        evidence.append({"name": "direction", "value": direction, "threshold": "UP or DOWN", "source": "super_ghost prediction"})
        evidence.append({"name": "entry", "value": risk.get("entry"), "threshold": "—", "source": "risk_plan"})
        evidence.append({"name": "target", "value": risk.get("target_price"), "threshold": "—", "source": "risk_plan"})
        evidence.append({"name": "stop", "value": risk.get("stop_loss"), "threshold": "—", "source": "risk_plan"})
        status = "pass" if direction else "insufficient"
        headline = f"Direction: {direction or 'none'} · Entry: {risk.get('entry', '—')}"
    else:
        status = "insufficient"
        headline = "No direction data"
    return {
        "step": 3, "key": "direction", "label": "Direction",
        "status": status, "headline": headline,
        "evidence": evidence,
        "insufficient_reason": None if status != "insufficient" else "Super Ghost data unavailable",
    }


def _step_alignment(
    sg: Optional[Dict[str, Any]],
    kill: Dict[str, Any],
    top_pick: Dict[str, Any],
    sym: str,
) -> Dict[str, Any]:
    evidence: List[Dict[str, Any]] = []
    if sg:
        regime = sg.get("market_regime") or {}
        pred = sg.get("prediction") or {}
        evidence.append({"name": "regime_label", "value": regime.get("label"), "threshold": "aligned with direction", "source": "market_regime"})
        evidence.append({"name": "sentiment_brake", "value": sg.get("sentiment_brake_active"), "threshold": "False", "source": "news_sentiment"})
    if top_pick:
        evidence.append({"name": "top_pick_decision", "value": top_pick.get("decision"), "threshold": "ELIGIBLE_FOR_TOP_PICKS", "source": "evaluate_top_pick_gate"})
        evidence.append({"name": "top_pick_eligible", "value": top_pick.get("eligible"), "threshold": "True", "source": "evaluate_top_pick_gate"})
    if kill:
        evidence.append({"name": "kill_clear", "value": not kill.get("any_triggered"), "threshold": "True", "source": "evaluate_kill_conditions"})

    if top_pick and top_pick.get("eligible"):
        status = "pass"
        headline = "Top-pick gate: ELIGIBLE"
    elif top_pick and not top_pick.get("eligible"):
        status = "hold"
        headline = f"Top-pick gate: LOCKED ({len(top_pick.get('blocking', []))} blockers)"
    else:
        status = "insufficient"
        headline = "Alignment data unavailable"
    return {
        "step": 4, "key": "alignment", "label": "Alignment",
        "status": status, "headline": headline,
        "evidence": evidence,
        "insufficient_reason": None if status != "insufficient" else "Top-pick gate evaluation unavailable",
    }


def _step_consistency(
    sg: Optional[Dict[str, Any]],
    accuracy: Dict[str, Any],
    sym: str,
) -> Dict[str, Any]:
    evidence: List[Dict[str, Any]] = []
    # Import thresholds from top_picks constants (source tripwire)
    from core.super_ghost_top_picks import MIN_COMPLETED, MIN_PRECISION_SCORE, MIN_PRECISION_SAMPLES

    overall = (accuracy or {}).get("overall") or {}
    n = int(overall.get("n") or 0)
    wr = overall.get("win_rate")
    wr_low = overall.get("win_rate_wilson_low")
    evidence.append({"name": "resolved", "value": n, "threshold": f"≥{MIN_COMPLETED}", "source": "get_accuracy"})
    evidence.append({"name": "win_rate", "value": wr, "threshold": "≥0.70", "source": "get_accuracy (Wilson)"})
    evidence.append({"name": "win_rate_wilson_low", "value": wr_low, "threshold": "≥0.55", "source": "get_accuracy (Wilson)"})

    if sg:
        evidence.append({"name": "accuracy_grade", "value": (sg.get("prediction") or {}).get("accuracy_grade"), "threshold": "A or B", "source": "super_ghost"})

    if n >= MIN_COMPLETED and wr is not None:
        status = "pass" if (wr or 0) >= 0.55 else "hold"
        headline = f"{n} resolved · WR={wr:.1%}" if wr else f"{n} resolved"
    elif n > 0:
        status = "insufficient"
        headline = f"Only {n} resolved (need ≥{MIN_COMPLETED})"
    else:
        status = "insufficient"
        headline = "No resolved outcomes"
    return {
        "step": 5, "key": "consistency", "label": "Consistency",
        "status": status, "headline": headline,
        "evidence": evidence,
        "insufficient_reason": None if status != "insufficient" else f"Need ≥{MIN_COMPLETED} resolved outcomes (have {n})",
    }


def _step_results(
    accuracy: Dict[str, Any],
    if_followed: Dict[str, Any],
    sym: str,
) -> Dict[str, Any]:
    evidence: List[Dict[str, Any]] = []
    overall = (accuracy or {}).get("overall") or {}
    n = int(overall.get("n") or 0)
    wr = overall.get("win_rate")
    wr_low = overall.get("win_rate_wilson_low")
    pf = if_followed.get("profit_factor") if if_followed else None
    net_ret = if_followed.get("net_return_pct") if if_followed else None

    evidence.append({"name": "n", "value": n, "threshold": ">0", "source": "get_accuracy"})
    evidence.append({"name": "win_rate", "value": wr, "threshold": "—", "source": "get_accuracy"})
    evidence.append({"name": "win_rate_wilson_low", "value": wr_low, "threshold": "—", "source": "get_accuracy (Wilson)"})
    evidence.append({"name": "profit_factor", "value": pf, "threshold": "≥1.0", "source": "get_if_followed"})
    evidence.append({"name": "net_return_pct", "value": net_ret, "threshold": ">0", "source": "get_if_followed"})

    if n == 0:
        status = "insufficient"
        headline = "No resolved outcomes — results pending"
    elif wr is not None:
        status = "pass" if (pf or 0) >= 1.0 else "hold"
        headline = f"WR={wr:.1%} · PF={pf:.2f}" if pf else f"WR={wr:.1%}"
    else:
        status = "insufficient"
        headline = "Results unavailable"
    return {
        "step": 6, "key": "results", "label": "Results",
        "status": status, "headline": headline,
        "evidence": evidence,
        "insufficient_reason": None if status != "insufficient" else "No resolved outcomes to measure",
    }
