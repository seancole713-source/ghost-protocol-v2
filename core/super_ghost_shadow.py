"""Super Ghost Shadow Model Runner (PR #97).

Runs multiple specialist prediction brains in parallel, stores their shadow
predictions, and later scores them against resolved Truth Ledger outcomes.

This is the bridge from "learning from mistakes" to "competing models":
production Ghost remains in control, while shadow models quietly produce their
own direction/confidence/target ideas. When outcomes resolve, Ghost learns which
specialist brains worked under which conditions.

No auto-trading. No auto-promotion. Shadow evidence only.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.super_ghost_shadow")

HORIZON_DAYS = 5
MIN_PROFILE_SAMPLES = 5


def _now() -> int:
    return int(time.time())


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        out = float(v)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _jsonb(v: Any) -> str:
    return json.dumps(v, default=str)


def _coerce_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


def ensure_shadow_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_shadow_predictions (
            id SERIAL PRIMARY KEY,
            parent_ledger_id INT NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            created_at BIGINT NOT NULL,
            model_id VARCHAR(80) NOT NULL,
            model_family VARCHAR(60) NOT NULL,
            horizon_days INT NOT NULL DEFAULT 5,
            direction VARCHAR(10) NOT NULL,
            confidence FLOAT,
            reference_price FLOAT,
            target_price FLOAT,
            stop_loss FLOAT,
            reason TEXT,
            feature_snapshot_json JSONB,
            prediction_json JSONB,
            realized_price FLOAT,
            return_pct FLOAT,
            signed_return_pct FLOAT,
            correct BOOLEAN,
            resolved_at BIGINT,
            UNIQUE(parent_ledger_id, model_id, horizon_days)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_shadow_model_profiles (
            id SERIAL PRIMARY KEY,
            model_id VARCHAR(80) NOT NULL,
            model_family VARCHAR(60) NOT NULL,
            horizon_days INT NOT NULL,
            sample_count INT NOT NULL,
            actionable_count INT NOT NULL,
            wins INT NOT NULL,
            losses INT NOT NULL,
            win_rate FLOAT,
            false_positive_rate FLOAT,
            avg_signed_return_pct FLOAT,
            net_return_pct FLOAT,
            profit_factor FLOAT,
            max_drawdown_pct FLOAT,
            best_regime VARCHAR(40),
            worst_regime VARCHAR(40),
            calibration_error FLOAT,
            status VARCHAR(32),
            updated_at BIGINT NOT NULL,
            payload_json JSONB,
            UNIQUE(model_id, horizon_days)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sg_shadow_parent ON super_ghost_shadow_predictions(parent_ledger_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sg_shadow_symbol ON super_ghost_shadow_predictions(symbol, created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sg_shadow_profiles ON super_ghost_shadow_model_profiles(horizon_days, win_rate DESC)")


@dataclass(frozen=True)
class ShadowModel:
    model_id: str
    model_family: str
    description: str
    fn: Callable[[Dict[str, Any]], Dict[str, Any]]


def _items(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(i.get("key")): i for i in (report.get("checklist") or []) if isinstance(i, dict)}


def _score_for(report: Dict[str, Any], keys: Iterable[str]) -> Tuple[float, int, List[Dict[str, Any]]]:
    by = _items(report)
    total = 0.0
    weight_sum = 0.0
    used: List[Dict[str, Any]] = []
    for k in keys:
        item = by.get(k)
        if not item or not item.get("available"):
            continue
        score = _f(item.get("score"))
        if score is None:
            continue
        weight = _f(item.get("weight")) or 1.0
        total += score * weight
        weight_sum += 2.0 * weight
        used.append({"key": k, "score": score, "weight": weight, "evidence": item.get("evidence")})
    edge = total / weight_sum if weight_sum else 0.0
    return edge, len(used), used


def _decision_from_edge(edge: float, used: int, *, threshold: float = 0.14) -> Tuple[str, float]:
    if used <= 0:
        return "HOLD", 0.50
    conf = round(_clamp(0.50 + abs(edge) * 0.70, 0.50, 0.90), 3)
    if edge >= threshold:
        return "UP", conf
    if edge <= -threshold:
        return "DOWN", conf
    return "HOLD", min(conf, 0.58)


def _risk(report: Dict[str, Any]) -> Dict[str, Any]:
    return dict(report.get("risk_plan") or {})


def _base_shadow(model_id: str, model_family: str, direction: str, confidence: float, report: Dict[str, Any], *, reason: str, drivers: List[Dict[str, Any]]) -> Dict[str, Any]:
    risk = _risk(report)
    return {
        "model_id": model_id,
        "model_family": model_family,
        "symbol": (report.get("symbol") or "").upper(),
        "direction": direction,
        "confidence": confidence,
        "reference_price": _f(risk.get("entry")),
        "target_price": _f(risk.get("target_price")),
        "stop_loss": _f(risk.get("stop_loss")),
        "reason": reason,
        "feature_snapshot": {"drivers": drivers, "source_engine": report.get("engine")},
        "prediction": {"direction": direction, "confidence": confidence, "reason": reason, "drivers": drivers[:5]},
    }


def technical_shadow(report: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("perf_30d", "range_52w", "avg_volume", "rvol", "relative_strength", "moving_averages", "support_resistance")
    edge, used, drivers = _score_for(report, keys)
    direction, conf = _decision_from_edge(edge, used, threshold=0.13)
    return _base_shadow("technical_shadow_v1", "technical", direction, conf, report, reason=f"Technical edge {edge:+.3f} from {used} price/volume features.", drivers=drivers)


def news_shadow(report: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("news_catalysts", "guidance")
    edge, used, drivers = _score_for(report, keys)
    direction, conf = _decision_from_edge(edge, used, threshold=0.12)
    return _base_shadow("news_shadow_v1", "news", direction, conf, report, reason=f"News/guidance edge {edge:+.3f} from {used} features.", drivers=drivers)


def fundamental_shadow(report: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("eps", "revenue_growth", "guidance", "insider_trading", "institutional_ownership", "analyst_ratings")
    edge, used, drivers = _score_for(report, keys)
    direction, conf = _decision_from_edge(edge, used, threshold=0.12)
    return _base_shadow("fundamental_shadow_v1", "fundamental", direction, conf, report, reason=f"Fundamental edge {edge:+.3f} from {used} features.", drivers=drivers)


def macro_shadow(report: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("spx", "nasdaq", "sector", "vix", "fed_cpi")
    edge, used, drivers = _score_for(report, keys)
    direction, conf = _decision_from_edge(edge, used, threshold=0.11)
    return _base_shadow("macro_shadow_v1", "macro", direction, conf, report, reason=f"Macro/market edge {edge:+.3f} from {used} features.", drivers=drivers)


def regime_shadow(report: Dict[str, Any]) -> Dict[str, Any]:
    pred = report.get("prediction") or {}
    regime = report.get("market_regime") or {}
    direction = str(pred.get("direction") or "HOLD").upper()
    risk_state = str(regime.get("risk_state") or "").lower()
    conf = _f(pred.get("confidence")) or 0.50
    reason = f"Regime state {risk_state or 'unknown'} assessed against production direction {direction}."
    if direction == "UP" and risk_state == "risk_off":
        direction = "HOLD"
        conf = 0.52
        reason += " Skipped long because tape is risk-off."
    elif direction == "DOWN" and risk_state == "risk_on":
        direction = "HOLD"
        conf = 0.52
        reason += " Skipped short because tape is risk-on."
    return _base_shadow("regime_shadow_v1", "regime", direction, round(_clamp(conf, 0.50, 0.90), 3), report, reason=reason, drivers=[{"risk_state": risk_state, "regime": regime.get("label")}])


def learning_adjusted_shadow(report: Dict[str, Any]) -> Dict[str, Any]:
    pred = report.get("prediction") or {}
    learn = report.get("learning_adjustment") or {}
    direction = str(pred.get("direction") or "HOLD").upper()
    conf = _f(pred.get("confidence")) or 0.50
    reason = "Learning profile cold-start; mirrors production direction."
    if learn.get("available"):
        conf = _clamp(conf + (_f(learn.get("confidence_delta")) or 0.0), 0.50, 0.92)
        scope = str(learn.get("scope") or "symbol")
        reason = (f"Applies bounded Learning Brain adjustment ({scope} evidence, "
                  f"{int(learn.get('sample_count') or 0)} resolved) from outcomes.")
        if learn.get("status") == "dampen":
            direction = "HOLD"
            conf = 0.52
            reason += " Dampen profile blocks action."
        elif direction in ("UP", "DOWN") and conf < 0.55:
            # PR #162: the brain's judgment must be VISIBLE on the scoreboard.
            # Shadow scoring keys on direction only (confidence/target tweaks
            # are invisible to _correct/_signed), so a learning brain that
            # never changes direction reads byte-identical to its input
            # forever. When the evidence-adjusted confidence is below a
            # coin-flip-plus-noise floor, skip the pick — that's the lesson
            # ("this class of marginal call hasn't been paying") expressed in
            # the one dimension the scoreboard can measure.
            direction = "HOLD"
            reason += (f" Skipped: evidence-adjusted confidence {conf:.3f} < 0.55 "
                       "floor — marginal picks like this haven't paid.")
            conf = 0.52
    out = _base_shadow("learning_adjusted_shadow_v1", "learning", direction, round(conf, 3), report, reason=reason, drivers=[learn])
    if learn.get("new_target_price") is not None:
        out["target_price"] = _f(learn.get("new_target_price"))
    return out


def contrarian_shadow(report: Dict[str, Any]) -> Dict[str, Any]:
    """Inverse-Ghost (PR #132): bets against production on every committed call.

    Tests the operator hypothesis that a persistently wrong predictor is a
    tradable contrarian signal. Under the 5-day sign resolution this brain's
    accuracy is exactly the complement of production's on committed calls, so
    its profile answers the question directly: if production is truly
    anti-skilled (not just unlucky), contrarian win_rate rises above chance
    over enough samples. HOLD stays HOLD — no production conviction means
    nothing to invert. Shadow-only, like every other brain here.
    """
    pred = report.get("prediction") or {}
    direction = str(pred.get("direction") or "HOLD").upper()
    conf = _clamp(_f(pred.get("confidence")) or 0.50, 0.50, 0.90)
    inverted = {"UP": "DOWN", "DOWN": "UP"}.get(direction, "HOLD")
    if inverted == "HOLD":
        reason = "Production holds — contrarian has nothing to invert."
        conf = 0.50
    else:
        reason = f"Inverts production {direction} (conf {conf:.2f}) — anti-signal hypothesis."
    out = _base_shadow(
        "contrarian_shadow_v1", "contrarian", inverted, round(conf, 3), report,
        reason=reason,
        drivers=[{"production_direction": direction, "production_confidence": conf}],
    )
    # The risk plan's target/stop were built for the production direction —
    # mirror them around entry so the inverted trade carries sane geometry.
    entry, tgt, stp = out.get("reference_price"), out.get("target_price"), out.get("stop_loss")
    if inverted != "HOLD" and entry and tgt and stp:
        out["target_price"] = round(entry - (tgt - entry), 6)
        out["stop_loss"] = round(entry - (stp - entry), 6)
    return out


def news_event_shadow(report: Dict[str, Any]) -> Dict[str, Any]:
    """news_shadow_v2 (PR #134) — structured-event news brain.

    Reads typed, deduplicated, point-in-time events (core.news_events) instead
    of v1's thin checklist sentiment. v1 stays registered and FROZEN as the
    baseline: same model_id + new logic would contaminate its ledger profile,
    so this ships as a new versioned id and must beat v1 on resolved outcomes.

    Guardrails honored here: decision uses only events with asof_ts <= now
    (point-in-time by query construction); a dead feed reports "news
    unavailable" and HOLDs rather than reading silence as bullish.
    """
    sym = (report.get("symbol") or "").upper()
    try:
        from core.news_events import news_available, recent_events_for_symbol
        available = news_available()
        events = recent_events_for_symbol(sym) if available else []
    except Exception as exc:
        available, events = False, []
        LOGGER.debug("news_event_shadow %s: %s", sym, str(exc)[:100])
    direction, conf = "HOLD", 0.50
    if not available:
        reason = "News unavailable (feed dead or never ingested) — refusing to treat silence as signal."
    elif not events:
        reason = "News feed live; no material events for this symbol in the last 7 days."
    else:
        bull = [e for e in events if e.get("direction_hint") == "bullish"]
        bear = [e for e in events if e.get("direction_hint") == "bearish"]
        def _wt(evs):
            return sum(float(e.get("materiality") or 0) * float(e.get("source_reliability") or 0.6)
                       for e in evs)
        bw, brw = _wt(bull), _wt(bear)
        top = events[0]  # ordered by materiality desc
        if abs(bw - brw) < 0.35 or float(top.get("materiality") or 0) < 0.6:
            reason = (f"Mixed/weak event tape (bull {bw:.2f} vs bear {brw:.2f}); "
                      f"no committed call.")
        else:
            direction = "UP" if bw > brw else "DOWN"
            edge = abs(bw - brw)
            conf = round(_clamp(0.52 + min(edge, 1.4) * 0.10, 0.52, 0.68), 3)
            reason = (f"{'Bullish' if direction == 'UP' else 'Bearish'} event edge "
                      f"{edge:.2f} led by {top.get('event_type')} "
                      f"(materiality {top.get('materiality')}, {top.get('confirmation_status')}).")
    drivers = [{"available": available, "events": [
        {k: e.get(k) for k in ("event_type", "direction_hint", "materiality",
                               "confirmation_status", "asof_ts")} for e in events[:5]]}]
    return _base_shadow("news_shadow_v2", "news", direction, conf, report,
                        reason=reason, drivers=drivers)


def momentum_shadow(report: Dict[str, Any]) -> Dict[str, Any]:
    """Trend/breakout brain (PR #151) — Ghost's 'other way of thinking'.

    Production Ghost is a short-term mean-reversion predictor (2%/3-day). This
    brain is the complement: it leans UP when a symbol is in a confirmed bullish
    run — breaking to new highs, above rising moving averages, trending (ADX),
    with volume and a strong 20-day return. This is the lens that would have
    'seen' the ODD-style +80% climb the base engine is blind to.

    Confidence-capped at 0.70 and it only commits on a strong stack (>=4 of 6
    momentum signals). Shadow-only: its profile decides whether trend-following
    actually pays forward, or is just hindsight bias.
    """
    sym = (report.get("symbol") or "").upper()
    try:
        from core.momentum import compute_momentum
        m = compute_momentum(sym)
    except Exception as exc:
        m = {"available": False, "reason": str(exc)[:80]}
    direction, conf = "HOLD", 0.50
    if not m.get("available"):
        reason = f"Momentum unavailable: {m.get('reason', 'no data')}."
    else:
        score = int(m.get("score") or 0)
        if score >= 4:
            direction = "UP"
            conf = round(_clamp(0.50 + score * 0.033, 0.52, 0.70), 3)
            fired = [k for k, v in (m.get("signals") or {}).items() if v]
            reason = (f"Confirmed bullish run: {score}/6 momentum signals "
                      f"({', '.join(fired)}); +{m.get('ret_20d_pct')}% 20d, "
                      f"ADX {m.get('adx')}. Ride-the-trend lean, shadow-capped.")
        else:
            reason = (f"No confirmed run ({score}/6 momentum signals); "
                      f"not a trend to ride.")
    return _base_shadow("momentum_shadow_v1", "momentum", direction, conf, report,
                        reason=reason, drivers=[m])




def momentum_shadow_v2(report: Dict[str, Any]) -> Dict[str, Any]:
    """Trend-following v2 (PR #153) — richer, still shadow-only.

    v1 is frozen as a baseline. v2 adds relative strength, pullback/extension
    discrimination, and regime-like trend penalties. It must earn promotion via
    resolved shadow profiles before any real firing path can use it.
    """
    sym = (report.get("symbol") or "").upper()
    try:
        from core.momentum import compute_momentum_v2
        m = compute_momentum_v2(sym)
    except Exception as exc:
        m = {"available": False, "version": "v2", "reason": str(exc)[:80]}
    direction, conf = "HOLD", 0.50
    if not m.get("available"):
        reason = f"Momentum v2 unavailable: {m.get('reason', 'no data')}."
    else:
        score = int(m.get("score") or 0)
        raw = int(m.get("raw_score") or score)
        penalties = [k for k, v in (m.get("penalties") or {}).items() if v]
        setup = m.get("setup") or "hold"
        if score >= 6 and setup == "trend_continuation":
            direction = "UP"
            # Confidence capped below production fire threshold; profile earns trust later.
            conf = round(_clamp(0.52 + score * 0.028, 0.54, 0.69), 3)
            fired = [k for k, v in (m.get("signals") or {}).items() if v]
            reason = (f"Trend-following v2 continuation: score {score}/8 "
                      f"(raw {raw}, setup {setup}; {', '.join(fired)}); "
                      f"20d {m.get('ret_20d_pct')}%, rel20 {m.get('relative_strength_20d')}, "
                      f"ADX {m.get('adx')}. Shadow-only.")
        elif score >= 5:
            reason = (f"Momentum v2 watchlist only: score {score}/8 setup {setup}; "
                      f"penalties={penalties or 'none'}; wait for forward proof.")
        else:
            reason = (f"No v2 trend entry ({score}/8 after penalties; raw {raw}); "
                      f"penalties={penalties or 'none'}.")
    return _base_shadow("momentum_shadow_v2", "momentum", direction, conf, report,
                        reason=reason, drivers=[m])

def seasonal_shadow(report: Dict[str, Any]) -> Dict[str, Any]:
    """Calendar-seasonality brain (PR #133).

    Leans with this symbol's own ~4-year record for the current 5-day calendar
    window (core.seasonality). Deliberately humble: n<=4 yearly windows is thin
    evidence, so confidence is hard-capped at 0.65 and the brain commits only
    when the seasonal excess is >=2.5% with >=75% year-over-year consistency.
    Its shadow profile — not this heuristic — decides whether calendar
    seasonality ever deserves more weight.
    """
    sym = (report.get("symbol") or "").upper()
    try:
        from core.seasonality import seasonal_window_stats
        stats = seasonal_window_stats(sym) or {}
    except Exception as exc:
        stats = {"available": False, "reason": str(exc)[:80]}
    direction, conf = "HOLD", 0.50
    if not stats.get("available"):
        reason = f"Seasonality unavailable: {stats.get('reason', 'no data')}."
    else:
        lean = stats.get("lean")
        cons = _f(stats.get("consistency")) or 0.0
        excess = _f(stats.get("excess_pct")) or 0.0
        if lean in ("UP", "DOWN") and cons >= 0.75:
            direction = lean
            conf = round(_clamp(0.52 + min(abs(excess), 12.0) / 100.0, 0.52, 0.65), 3)
            reason = (
                f"{stats['n_years']}-yr calendar window: avg {stats['avg_window_pct']:+.1f}% "
                f"vs baseline {stats['baseline_pct']:+.1f}% (excess {excess:+.1f}%, "
                f"consistency {int(cons * 100)}%). Confidence capped — n={stats['n_years']} is thin."
            )
        else:
            reason = (
                f"No committed seasonal edge for this window "
                f"(excess {excess:+.1f}%, consistency {int(cons * 100)}%)."
            )
    return _base_shadow("seasonal_shadow_v1", "seasonal", direction, conf, report,
                        reason=reason, drivers=[stats])


def ensemble_shadow(report: Dict[str, Any]) -> Dict[str, Any]:
    votes = [technical_shadow(report), news_shadow(report), fundamental_shadow(report), macro_shadow(report), regime_shadow(report), learning_adjusted_shadow(report)]
    up = sum(1 for v in votes if v["direction"] == "UP")
    down = sum(1 for v in votes if v["direction"] == "DOWN")
    hold = len(votes) - up - down
    direction = "HOLD"
    if up >= 3 and up > down:
        direction = "UP"
    elif down >= 3 and down > up:
        direction = "DOWN"
    conf = round(_clamp(0.50 + abs(up - down) / max(len(votes), 1) * 0.35, 0.50, 0.88), 3)
    return _base_shadow("ensemble_shadow_v1", "ensemble", direction, conf, report, reason=f"Specialist vote UP={up}, DOWN={down}, HOLD={hold}.", drivers=[{"model_id": v["model_id"], "direction": v["direction"], "confidence": v["confidence"]} for v in votes])


SHADOW_MODELS: Tuple[ShadowModel, ...] = (
    ShadowModel("technical_shadow_v1", "technical", "Price action, volume, trend and support/resistance specialist.", technical_shadow),
    ShadowModel("news_shadow_v1", "news", "News catalyst and guidance specialist.", news_shadow),
    ShadowModel("fundamental_shadow_v1", "fundamental", "EPS, revenue, guidance, ownership and analyst specialist.", fundamental_shadow),
    ShadowModel("macro_shadow_v1", "macro", "Broad market, sector, VIX and Fed/CPI specialist.", macro_shadow),
    ShadowModel("regime_shadow_v1", "regime", "Regime risk-on/risk-off specialist.", regime_shadow),
    ShadowModel("learning_adjusted_shadow_v1", "learning", "Learning Brain adjusted production specialist.", learning_adjusted_shadow),
    ShadowModel("ensemble_shadow_v1", "ensemble", "Committee vote across all specialist shadows.", ensemble_shadow),
    ShadowModel("contrarian_shadow_v1", "contrarian", "Inverse-Ghost: bets against every committed production call (anti-signal hypothesis).", contrarian_shadow),
    ShadowModel("seasonal_shadow_v1", "seasonal", "Calendar-seasonality lean from the symbol's own ~4-year record for the current 5-day window.", seasonal_shadow),
    ShadowModel("news_shadow_v2", "news", "Structured-event news brain: typed, deduplicated, point-in-time events (v1 frozen as baseline).", news_event_shadow),
    ShadowModel("momentum_shadow_v1", "momentum", "Trend/breakout brain: leans UP on confirmed multi-week bullish runs (the ODD-style climb the base engine is blind to).", momentum_shadow),
    ShadowModel("momentum_shadow_v2", "momentum", "Trend-following v2: multi-timeframe run detection with relative-strength, pullback, extension, and regime penalties; shadow-only until proven.", momentum_shadow_v2),
)


def shadow_manifest() -> List[Dict[str, str]]:
    return [{"model_id": m.model_id, "model_family": m.model_family, "description": m.description} for m in SHADOW_MODELS]


def run_shadow_models(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for model in SHADOW_MODELS:
        try:
            pred = model.fn(report)
            pred.setdefault("model_id", model.model_id)
            pred.setdefault("model_family", model.model_family)
            out.append(pred)
        except Exception as exc:
            LOGGER.warning("shadow model %s failed: %s", model.model_id, str(exc)[:120])
            out.append({
                "model_id": model.model_id,
                "model_family": model.model_family,
                "symbol": (report.get("symbol") or "").upper(),
                "direction": "HOLD",
                "confidence": 0.50,
                "reason": f"Shadow model failed safely: {str(exc)[:100]}",
                "feature_snapshot": {},
                "prediction": {"error": str(exc)[:120]},
            })
    return out


def store_shadow_predictions(cur, parent_ledger_id: int, report: Dict[str, Any]) -> Dict[str, Any]:
    ensure_shadow_tables(cur)
    preds = run_shadow_models(report)
    created = int(report.get("ts") or _now())
    count = 0
    for p in preds:
        cur.execute(
            """
            INSERT INTO super_ghost_shadow_predictions (
                parent_ledger_id, symbol, created_at, model_id, model_family, horizon_days,
                direction, confidence, reference_price, target_price, stop_loss, reason,
                feature_snapshot_json, prediction_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
            ON CONFLICT(parent_ledger_id, model_id, horizon_days) DO UPDATE SET
                direction=EXCLUDED.direction,
                confidence=EXCLUDED.confidence,
                reference_price=EXCLUDED.reference_price,
                target_price=EXCLUDED.target_price,
                stop_loss=EXCLUDED.stop_loss,
                reason=EXCLUDED.reason,
                feature_snapshot_json=EXCLUDED.feature_snapshot_json,
                prediction_json=EXCLUDED.prediction_json
            """,
            (
                parent_ledger_id, p.get("symbol"), created, p.get("model_id"), p.get("model_family"), HORIZON_DAYS,
                p.get("direction") or "HOLD", _f(p.get("confidence")), _f(p.get("reference_price")),
                _f(p.get("target_price")), _f(p.get("stop_loss")), p.get("reason"),
                _jsonb(p.get("feature_snapshot")), _jsonb(p.get("prediction")),
            ),
        )
        count += 1
    return {"ok": True, "stored": count, "models": [p.get("model_id") for p in preds]}


def _correct(direction: str, ret_pct: Optional[float]) -> Optional[bool]:
    if ret_pct is None:
        return None
    d = (direction or "").upper()
    if d == "UP":
        return ret_pct > 0
    if d == "DOWN":
        return ret_pct < 0
    return abs(ret_pct) < 3.0


def _signed(direction: str, ret_pct: Optional[float]) -> Optional[float]:
    if ret_pct is None:
        return None
    d = (direction or "").upper()
    if d == "UP":
        return float(ret_pct)
    if d == "DOWN":
        return -float(ret_pct)
    return 0.0


def resolve_shadow_predictions(*, symbol: Optional[str] = None, limit: int = 1000) -> Dict[str, Any]:
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_shadow_tables(cur)
            where = "sp.resolved_at IS NULL AND p.resolved_5d_at IS NOT NULL AND p.price_5d IS NOT NULL AND p.return_5d_pct IS NOT NULL"
            params: List[Any] = []
            if symbol:
                where += " AND sp.symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT sp.id, sp.direction, p.price_5d, p.return_5d_pct
                FROM super_ghost_shadow_predictions sp
                JOIN super_ghost_predictions p ON p.id = sp.parent_ledger_id
                WHERE {where}
                ORDER BY sp.created_at ASC
                LIMIT %s
                """,
                params + [max(1, min(5000, int(limit)))],
            )
            rows = cur.fetchall()
            now = _now()
            updated = 0
            for sid, direction, price, ret in rows:
                retf = _f(ret)
                cur.execute(
                    """
                    UPDATE super_ghost_shadow_predictions
                    SET realized_price=%s, return_pct=%s, signed_return_pct=%s, correct=%s, resolved_at=%s
                    WHERE id=%s
                    """,
                    (price, ret, _signed(direction, retf), _correct(direction, retf), now, sid),
                )
                updated += 1
            profiles = refresh_shadow_profiles(cur)
        return {"ok": True, "resolved": updated, "profiles_updated": profiles}
    except Exception as exc:
        LOGGER.warning("resolve_shadow_predictions: %s", str(exc)[:160])
        return {"ok": False, "error": str(exc)[:160], "resolved": 0}


def refresh_shadow_profiles(cur) -> int:
    cur.execute(
        """
        SELECT model_id, model_family, horizon_days,
               COUNT(*) AS sample_count,
               SUM(CASE WHEN direction IN ('UP','DOWN') THEN 1 ELSE 0 END) AS actionable_count,
               SUM(CASE WHEN correct = TRUE AND direction IN ('UP','DOWN') THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN correct = FALSE AND direction IN ('UP','DOWN') THEN 1 ELSE 0 END) AS losses,
               AVG(CASE WHEN direction IN ('UP','DOWN') THEN signed_return_pct ELSE NULL END) AS avg_signed,
               SUM(CASE WHEN direction IN ('UP','DOWN') THEN signed_return_pct ELSE 0 END) AS net_return,
               AVG(CASE WHEN direction IN ('UP','DOWN') AND correct IS NOT NULL
                             AND confidence IS NOT NULL
                        THEN POWER(confidence - (CASE WHEN correct THEN 1.0 ELSE 0.0 END), 2)
                        ELSE NULL END) AS brier
        FROM super_ghost_shadow_predictions
        WHERE resolved_at IS NOT NULL
        GROUP BY model_id, model_family, horizon_days
        """
    )
    now = _now()
    count = 0
    for model_id, family, horizon, n, actionable, wins, losses, avg_signed, net_return, brier in cur.fetchall():
        actionable = int(actionable or 0)
        wins = int(wins or 0)
        losses = int(losses or 0)
        win_rate = wins / actionable if actionable else None
        fpr = losses / actionable if actionable else None
        # PR #163: Brier on the brain's own confidence — the scoreboard was
        # direction-only, so confidence-quality differences between brains
        # (e.g. the learning brain's whole output channel) were unmeasurable.
        brier = round(float(brier), 4) if brier is not None else None
        # Simplified profile metrics; PR98 can add regime breakdowns/calibration.
        status = "cold_start" if int(n or 0) < MIN_PROFILE_SAMPLES else ("promising" if win_rate is not None and win_rate >= 0.60 else "watch")
        cur.execute(
            """
            INSERT INTO super_ghost_shadow_model_profiles (
                model_id, model_family, horizon_days, sample_count, actionable_count,
                wins, losses, win_rate, false_positive_rate, avg_signed_return_pct,
                net_return_pct, profit_factor, max_drawdown_pct, best_regime, worst_regime,
                calibration_error, status, updated_at, payload_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT(model_id, horizon_days) DO UPDATE SET
                sample_count=EXCLUDED.sample_count,
                actionable_count=EXCLUDED.actionable_count,
                wins=EXCLUDED.wins,
                losses=EXCLUDED.losses,
                win_rate=EXCLUDED.win_rate,
                false_positive_rate=EXCLUDED.false_positive_rate,
                avg_signed_return_pct=EXCLUDED.avg_signed_return_pct,
                net_return_pct=EXCLUDED.net_return_pct,
                calibration_error=EXCLUDED.calibration_error,
                status=EXCLUDED.status,
                updated_at=EXCLUDED.updated_at,
                payload_json=EXCLUDED.payload_json
            """,
            (
                model_id, family, horizon, int(n or 0), actionable, wins, losses, win_rate, fpr,
                avg_signed, net_return, None, 0.0, None, None, brier, status, now,
                _jsonb({"note": "Shadow model profile; no auto-promotion. "
                                "calibration_error = Brier on the brain's own confidence (PR #163)."}),
            ),
        )
        count += 1
    return count


def shadow_summary(*, symbol: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_shadow_tables(cur)
            where = "1=1"
            params: List[Any] = []
            if symbol:
                where += " AND symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT parent_ledger_id, symbol, created_at, model_id, model_family, horizon_days,
                       direction, confidence, reference_price, target_price, stop_loss, reason,
                       realized_price, return_pct, signed_return_pct, correct, resolved_at
                FROM super_ghost_shadow_predictions
                WHERE {where}
                ORDER BY created_at DESC, parent_ledger_id DESC
                LIMIT %s
                """,
                params + [max(1, min(500, int(limit)))],
            )
            rows = cur.fetchall()
        return {"ok": True, "symbol": (symbol or "ALL").upper(), "manifest": shadow_manifest(), "count": len(rows), "rows": [
            {"parent_ledger_id": r[0], "symbol": r[1], "created_at": r[2], "model_id": r[3], "model_family": r[4], "horizon_days": r[5], "direction": r[6], "confidence": r[7], "reference_price": r[8], "target_price": r[9], "stop_loss": r[10], "reason": r[11], "realized_price": r[12], "return_pct": r[13], "signed_return_pct": r[14], "correct": r[15], "resolved_at": r[16]}
            for r in rows
        ]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "manifest": shadow_manifest(), "rows": []}


def shadow_model_profiles() -> Dict[str, Any]:
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_shadow_tables(cur)
            cur.execute(
                """
                SELECT model_id, model_family, horizon_days, sample_count, actionable_count,
                       wins, losses, win_rate, false_positive_rate, avg_signed_return_pct,
                       net_return_pct, profit_factor, max_drawdown_pct, best_regime, worst_regime,
                       calibration_error, status, updated_at, payload_json
                FROM super_ghost_shadow_model_profiles
                ORDER BY horizon_days, win_rate DESC NULLS LAST, sample_count DESC
                """
            )
            rows = cur.fetchall()
        return {"ok": True, "profiles": [
            {"model_id": r[0], "model_family": r[1], "horizon_days": r[2], "sample_count": r[3], "actionable_count": r[4], "wins": r[5], "losses": r[6], "win_rate": r[7], "false_positive_rate": r[8], "avg_signed_return_pct": r[9], "net_return_pct": r[10], "profit_factor": r[11], "max_drawdown_pct": r[12], "best_regime": r[13], "worst_regime": r[14], "calibration_error": r[15], "status": r[16], "updated_at": r[17], "payload": _coerce_json(r[18])}
            for r in rows
        ], "manifest": shadow_manifest()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "profiles": [], "manifest": shadow_manifest()}
