"""Super Ghost Prediction Truth Ledger (PR #84).

Every Super Ghost prediction is logged here — including NO EDGE / WATCH ONLY,
because skipping a bad setup is itself a judgeable decision. A resolver later
scores each prediction against realized price at 1, 5, and 20 trading-day
horizons, producing the honest accuracy + "if-followed" performance that turns
Ghost from "smart analysis" into a measurable prediction product.

Design mirrors core/squeeze_outcomes.py and core/performance_log.py:
- Non-destructive CREATE TABLE IF NOT EXISTS.
- JSONB columns for the rich checklist / drivers / ai brief / risk plan.
- Best-effort, never raises into callers; logging failures degrade silently.
- Resolution reads realized OHLC via the same price path used elsewhere.

Nothing here trades. It records and grades predictions for a human.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.super_ghost_ledger")

# Horizons (in trading days) at which a prediction is scored.
HORIZONS = (1, 5, 20)
# A prediction is eligible to resolve at a horizon once this many calendar days
# have elapsed (trading-day approximation: ~7 calendar days per 5 trading days).
_CAL_DAYS_PER_HORIZON = {1: 1, 5: 7, 20: 28}

# Directional actions that count as an actionable "call" for if-followed math.
_DIRECTIONAL_ACTIONS = ("HIGH-CONVICTION", "WATCHLIST")


def ledger_enabled() -> bool:
    return os.getenv("SUPER_GHOST_LEDGER", "1").strip().lower() in ("1", "true", "yes", "on")


def _now() -> int:
    return int(time.time())


def _jsonb(val: Any) -> Optional[str]:
    if val is None:
        return None
    try:
        return json.dumps(val, default=str)
    except Exception:
        return None


def _coerce_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        out = float(v)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def ensure_ledger_table(cur) -> None:
    """Create the ledger table + indexes. Safe to call repeatedly."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_predictions (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            created_at BIGINT NOT NULL,
            engine VARCHAR(48),
            reference_price FLOAT,
            direction VARCHAR(10) NOT NULL,
            action TEXT,
            confidence FLOAT,
            conviction_score FLOAT,
            edge_score FLOAT,
            quality_score FLOAT,
            accuracy_grade VARCHAR(4),
            data_quality FLOAT,
            critical_data_quality FLOAT,
            checklist_coverage INT,
            regime_label VARCHAR(40),
            regime_risk_state VARCHAR(20),
            regime_multiplier FLOAT,
            stop_loss FLOAT,
            target_price FLOAT,
            risk_reward FLOAT,
            checklist_json JSONB,
            top_drivers_json JSONB,
            risk_plan_json JSONB,
            ai_brief_json JSONB,
            market_regime_json JSONB,
            -- resolution (1d / 5d / 20d)
            price_1d FLOAT, return_1d_pct FLOAT, correct_1d BOOLEAN, resolved_1d_at BIGINT,
            price_5d FLOAT, return_5d_pct FLOAT, correct_5d BOOLEAN, resolved_5d_at BIGINT,
            price_20d FLOAT, return_20d_pct FLOAT, correct_20d BOOLEAN, resolved_20d_at BIGINT,
            hit_target BOOLEAN,
            hit_stop BOOLEAN,
            max_favorable_pct FLOAT,
            max_adverse_pct FLOAT,
            fully_resolved BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sg_pred_symbol_time ON super_ghost_predictions (symbol, created_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sg_pred_unresolved ON super_ghost_predictions (created_at) WHERE fully_resolved = FALSE"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_sg_pred_grade ON super_ghost_predictions (accuracy_grade)"
    )


def _extract_row(report: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a Super Ghost report into ledger columns."""
    pred = report.get("prediction") or {}
    regime = report.get("market_regime") or {}
    coverage = report.get("coverage") or {}
    risk_plan = report.get("risk_plan") or {}
    return {
        "symbol": (report.get("symbol") or "").upper(),
        "engine": report.get("engine"),
        "reference_price": _f(risk_plan.get("entry")) or _f((report.get("ai_brain") or {}).get("reference_price")),
        "direction": pred.get("direction") or "HOLD",
        "action": pred.get("action"),
        "confidence": _f(pred.get("confidence")),
        "conviction_score": _f(pred.get("conviction_score")),
        "edge_score": _f(pred.get("edge_score")),
        "quality_score": _f(pred.get("quality_score")),
        "accuracy_grade": pred.get("accuracy_grade"),
        "data_quality": _f(pred.get("data_quality")),
        "critical_data_quality": _f(pred.get("critical_data_quality")),
        "checklist_coverage": int(coverage.get("available") or 0),
        "regime_label": regime.get("label"),
        "regime_risk_state": regime.get("risk_state"),
        "regime_multiplier": _f(regime.get("conviction_multiplier")),
        "stop_loss": _f(risk_plan.get("stop_loss")),
        "target_price": _f(risk_plan.get("target_price")),
        "risk_reward": _f(risk_plan.get("risk_reward_ratio")),
        "checklist_json": report.get("checklist"),
        "top_drivers_json": report.get("top_drivers"),
        "risk_plan_json": risk_plan,
        "ai_brief_json": report.get("ai_brief"),
        "market_regime_json": regime,
    }


def log_prediction(report: Dict[str, Any], *, created_at: Optional[int] = None) -> Optional[int]:
    """Persist one Super Ghost prediction. Returns row id or None.

    Logs every prediction including HOLD / NO EDGE so skip-accuracy is tracked.
    Never raises into the caller.
    """
    if not ledger_enabled():
        return None
    if not isinstance(report, dict) or not report.get("ok"):
        return None
    row = _extract_row(report)
    if not row["symbol"]:
        return None
    ts = int(created_at or report.get("ts") or _now())
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ledger_table(cur)
            cur.execute(
                """
                INSERT INTO super_ghost_predictions (
                    symbol, created_at, engine, reference_price, direction, action,
                    confidence, conviction_score, edge_score, quality_score, accuracy_grade,
                    data_quality, critical_data_quality, checklist_coverage,
                    regime_label, regime_risk_state, regime_multiplier,
                    stop_loss, target_price, risk_reward,
                    checklist_json, top_drivers_json, risk_plan_json, ai_brief_json, market_regime_json
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,
                    %s,%s,%s,
                    %s,%s,%s,
                    %s,%s,%s,
                    %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb
                ) RETURNING id
                """,
                (
                    row["symbol"], ts, row["engine"], row["reference_price"], row["direction"], row["action"],
                    row["confidence"], row["conviction_score"], row["edge_score"], row["quality_score"], row["accuracy_grade"],
                    row["data_quality"], row["critical_data_quality"], row["checklist_coverage"],
                    row["regime_label"], row["regime_risk_state"], row["regime_multiplier"],
                    row["stop_loss"], row["target_price"], row["risk_reward"],
                    _jsonb(row["checklist_json"]), _jsonb(row["top_drivers_json"]),
                    _jsonb(row["risk_plan_json"]), _jsonb(row["ai_brief_json"]), _jsonb(row["market_regime_json"]),
                ),
            )
            r = cur.fetchone()
            ledger_id = int(r[0]) if r else None
            if ledger_id:
                try:
                    from core.super_ghost_feature_store import persist_feature_snapshot
                    persist_feature_snapshot(cur, report, ledger_id=ledger_id, prediction_ts=ts)
                except Exception as store_exc:
                    LOGGER.warning("log_prediction feature_store %s: %s", row.get("symbol"), str(store_exc)[:120])
                try:
                    from core.super_ghost_memory import log_prediction_memory
                    log_prediction_memory(cur, ledger_id, report)
                except Exception as mem_exc:
                    LOGGER.warning("log_prediction memory %s: %s", row.get("symbol"), str(mem_exc)[:120])
                try:
                    from core.super_ghost_shadow import store_shadow_predictions
                    store_shadow_predictions(cur, ledger_id, report)
                except Exception as shadow_exc:
                    LOGGER.warning("log_prediction shadow %s: %s", row.get("symbol"), str(shadow_exc)[:120])
            return ledger_id
    except Exception as exc:
        LOGGER.warning("log_prediction %s: %s", row.get("symbol"), str(exc)[:160])
        return None


def _direction_correct(direction: str, ret_pct: Optional[float]) -> Optional[bool]:
    """Whether a prediction direction matched realized return.

    For HOLD/NO-EDGE: 'correct' means the move stayed small (abs return < 3%),
    i.e. skipping was the right call.
    """
    if ret_pct is None:
        return None
    d = (direction or "").upper()
    if d == "UP":
        return ret_pct > 0
    if d == "DOWN":
        return ret_pct < 0
    # HOLD / anything non-directional: small move => skip was correct.
    return abs(ret_pct) < 3.0


def _ohlc_series(symbol: str, period: str = "3mo") -> List[Dict[str, Any]]:
    """Realized daily OHLC bars, newest last. Best-effort via signal_engine."""
    try:
        from core.signal_engine import _fetch_ohlcv

        bars = _fetch_ohlcv(symbol.upper(), "stock", period=period) or []
        out = []
        for b in bars:
            o, h, l, c = b.get("open"), b.get("high"), b.get("low"), b.get("close")
            ts = b.get("ts")
            if None in (o, h, l, c):
                continue
            out.append({
                "ts": ts,
                "open": float(o), "high": float(h), "low": float(l), "close": float(c),
            })
        return out
    except Exception as exc:
        LOGGER.debug("_ohlc_series %s: %s", symbol, str(exc)[:80])
        return []


def _bar_epoch(bar: Dict[str, Any]) -> Optional[int]:
    ts = bar.get("ts")
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    s = str(ts)
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _bars_after(series: List[Dict[str, Any]], created_at: int) -> List[Dict[str, Any]]:
    """Trading-day bars strictly after the prediction timestamp, in order."""
    out = []
    for b in series:
        e = _bar_epoch(b)
        if e is None:
            continue
        # Use noon-UTC tolerance so a same-day prediction doesn't count its own bar.
        if e > created_at + 6 * 3600:
            out.append(b)
    return out


def _resolve_one(row: Dict[str, Any], series: List[Dict[str, Any]], now: int) -> Dict[str, Any]:
    """Compute horizon outcomes for a single prediction row. Pure function."""
    created_at = int(row["created_at"])
    ref = _f(row.get("reference_price"))
    direction = row.get("direction") or "HOLD"
    target = _f(row.get("target_price"))
    stop = _f(row.get("stop_loss"))
    fwd = _bars_after(series, created_at)
    updates: Dict[str, Any] = {}

    # If we have no reference price, anchor to the first forward bar's open.
    if ref is None and fwd:
        ref = _f(fwd[0].get("open"))

    # Max favorable / adverse across the longest available window (cap 20 td).
    if ref and ref > 0 and fwd:
        window = fwd[: max(HORIZONS)]
        highs = [b["high"] for b in window]
        lows = [b["low"] for b in window]
        if highs and lows:
            updates["max_favorable_pct"] = round((max(highs) - ref) / ref * 100.0, 3)
            updates["max_adverse_pct"] = round((min(lows) - ref) / ref * 100.0, 3)
            hit_target = bool(target and max(highs) >= target)
            hit_stop = bool(stop and min(lows) <= stop)
            updates["hit_target"] = hit_target
            updates["hit_stop"] = hit_stop

    elapsed_days = (now - created_at) / 86400.0
    resolved_horizons = 0
    for h in HORIZONS:
        need_cal_days = _CAL_DAYS_PER_HORIZON[h]
        # Already resolved?
        if row.get(f"resolved_{h}d_at"):
            resolved_horizons += 1
            continue
        if len(fwd) >= h:
            bar = fwd[h - 1]
            px = _f(bar.get("close"))
            if px is not None and ref and ref > 0:
                ret = round((px - ref) / ref * 100.0, 3)
                updates[f"price_{h}d"] = round(px, 4)
                updates[f"return_{h}d_pct"] = ret
                updates[f"correct_{h}d"] = _direction_correct(direction, ret)
                updates[f"resolved_{h}d_at"] = now
                resolved_horizons += 1
        elif elapsed_days >= need_cal_days + 5:
            # Enough wall-clock time elapsed but bars unavailable (e.g. halted/
            # delisted). Mark resolved as indeterminate so it stops blocking.
            updates[f"resolved_{h}d_at"] = now

    # Fully resolved once the longest horizon has a resolution timestamp.
    if updates.get(f"resolved_{max(HORIZONS)}d_at") or row.get(f"resolved_{max(HORIZONS)}d_at"):
        updates["fully_resolved"] = True
    return updates


def resolve_predictions(*, limit: int = 200, now: Optional[int] = None) -> Dict[str, Any]:
    """Resolve unresolved predictions against realized prices. Returns summary."""
    if not ledger_enabled():
        return {"ok": True, "enabled": False, "resolved": 0, "updated": 0}
    now = int(now or _now())
    updated = 0
    horizons_filled = 0
    cols = (
        "id, symbol, created_at, reference_price, direction, target_price, stop_loss, "
        "resolved_1d_at, resolved_5d_at, resolved_20d_at"
    )
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ledger_table(cur)
            cur.execute(
                f"""
                SELECT {cols} FROM super_ghost_predictions
                WHERE fully_resolved = FALSE
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (max(1, min(1000, int(limit))),),
            )
            rows = cur.fetchall()
            # Group symbols so we fetch each price series once.
            by_symbol: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                rec = {
                    "id": r[0], "symbol": r[1], "created_at": r[2], "reference_price": r[3],
                    "direction": r[4], "target_price": r[5], "stop_loss": r[6],
                    "resolved_1d_at": r[7], "resolved_5d_at": r[8], "resolved_20d_at": r[9],
                }
                by_symbol.setdefault((r[1] or "").upper(), []).append(rec)

            for sym, recs in by_symbol.items():
                series = _ohlc_series(sym, period="6mo")
                if not series:
                    continue
                for rec in recs:
                    updates = _resolve_one(rec, series, now)
                    if not updates:
                        continue
                    set_clauses = []
                    params: List[Any] = []
                    for k, v in updates.items():
                        set_clauses.append(f"{k} = %s")
                        params.append(v)
                        if k.startswith("resolved_") and k.endswith("d_at"):
                            horizons_filled += 1
                    params.append(rec["id"])
                    cur.execute(
                        f"UPDATE super_ghost_predictions SET {', '.join(set_clauses)} WHERE id = %s",
                        params,
                    )
                    updated += 1
    except Exception as exc:
        LOGGER.warning("resolve_predictions: %s", str(exc)[:160])
        return {"ok": False, "error": str(exc)[:160], "resolved": 0, "updated": 0}
    if updated:
        LOGGER.info("[SuperGhostLedger] updated %s rows (%s horizon fills)", updated, horizons_filled)
    return {"ok": True, "enabled": True, "updated": updated, "horizons_filled": horizons_filled}


def _row_to_dict(r: Tuple, cols: List[str]) -> Dict[str, Any]:
    d = dict(zip(cols, r))
    for k in ("checklist_json", "top_drivers_json", "risk_plan_json", "ai_brief_json", "market_regime_json"):
        if k in d:
            d[k] = _coerce_json(d[k])
    return d


_HISTORY_COLS = [
    "id", "symbol", "created_at", "engine", "reference_price", "direction", "action",
    "confidence", "conviction_score", "edge_score", "quality_score", "accuracy_grade",
    "data_quality", "critical_data_quality", "checklist_coverage",
    "regime_label", "regime_risk_state", "regime_multiplier",
    "stop_loss", "target_price", "risk_reward",
    "price_1d", "return_1d_pct", "correct_1d", "resolved_1d_at",
    "price_5d", "return_5d_pct", "correct_5d", "resolved_5d_at",
    "price_20d", "return_20d_pct", "correct_20d", "resolved_20d_at",
    "hit_target", "hit_stop", "max_favorable_pct", "max_adverse_pct", "fully_resolved",
]


def get_history(*, symbol: Optional[str] = None, limit: int = 100, include_payload: bool = False) -> Dict[str, Any]:
    """Recent logged predictions (newest first)."""
    if not ledger_enabled():
        return {"ok": True, "enabled": False, "rows": []}
    lim = max(1, min(500, int(limit)))
    cols = list(_HISTORY_COLS)
    if include_payload:
        cols = cols + ["checklist_json", "top_drivers_json", "risk_plan_json", "ai_brief_json", "market_regime_json"]
    select = ", ".join(cols)
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ledger_table(cur)
            if symbol:
                cur.execute(
                    f"SELECT {select} FROM super_ghost_predictions WHERE symbol = %s ORDER BY created_at DESC LIMIT %s",
                    ((symbol or "").upper(), lim),
                )
            else:
                cur.execute(
                    f"SELECT {select} FROM super_ghost_predictions ORDER BY created_at DESC LIMIT %s",
                    (lim,),
                )
            raw = cur.fetchall()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "rows": []}
    rows = [_row_to_dict(r, cols) for r in raw]
    return {"ok": True, "enabled": True, "count": len(rows), "rows": rows}


def _wilson_low(wins: int, n: int) -> Optional[float]:
    """Wilson score lower bound (95%) — honest small-sample win rate floor."""
    if n <= 0:
        return None
    z = 1.96
    phat = wins / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return round((centre - margin) / denom, 4)


def get_accuracy(*, symbol: Optional[str] = None, horizon: int = 5) -> Dict[str, Any]:
    """Resolved-accuracy stats overall and by confidence tier / grade / regime."""
    if not ledger_enabled():
        return {"ok": True, "enabled": False}
    if horizon not in HORIZONS:
        horizon = 5
    correct_col = f"correct_{horizon}d"
    ret_col = f"return_{horizon}d_pct"
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ledger_table(cur)
            where = f"{correct_col} IS NOT NULL"
            params: List[Any] = []
            if symbol:
                where += " AND symbol = %s"
                params.append((symbol or "").upper())
            cur.execute(
                f"""
                SELECT direction, action, accuracy_grade, confidence, conviction_score,
                       regime_risk_state, {correct_col}, {ret_col}
                FROM super_ghost_predictions WHERE {where}
                """,
                params,
            )
            raw = cur.fetchall()
            # totals (logged regardless of resolution)
            cur.execute(
                "SELECT COUNT(*) FROM super_ghost_predictions" + (" WHERE symbol = %s" if symbol else ""),
                ([(symbol or "").upper()] if symbol else []),
            )
            total_logged = int(cur.fetchone()[0])
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}

    def _bucket() -> Dict[str, Any]:
        return {"n": 0, "wins": 0, "returns": []}

    overall = _bucket()
    by_grade: Dict[str, Dict[str, Any]] = {}
    by_conf: Dict[str, Dict[str, Any]] = {}
    by_dir: Dict[str, Dict[str, Any]] = {}
    by_regime: Dict[str, Dict[str, Any]] = {}

    def _conf_tier(c: Optional[float]) -> str:
        if c is None:
            return "unknown"
        if c >= 0.80:
            return "80-100%"
        if c >= 0.70:
            return "70-80%"
        if c >= 0.60:
            return "60-70%"
        return "<60%"

    for direction, action, grade, conf, conv, regime_state, correct, ret in raw:
        for bucket in (overall,):
            bucket["n"] += 1
            if correct:
                bucket["wins"] += 1
            if ret is not None:
                bucket["returns"].append(float(ret))
        for key, store in ((grade or "?", by_grade), (_conf_tier(_f(conf)), by_conf), (direction or "?", by_dir), (regime_state or "?", by_regime)):
            b = store.setdefault(key, _bucket())
            b["n"] += 1
            if correct:
                b["wins"] += 1
            if ret is not None:
                b["returns"].append(float(ret))

    def _summ(b: Dict[str, Any]) -> Dict[str, Any]:
        n = b["n"]
        wins = b["wins"]
        rets = b["returns"]
        return {
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4) if n else None,
            "win_rate_wilson_low": _wilson_low(wins, n),
            "avg_return_pct": round(sum(rets) / len(rets), 3) if rets else None,
        }

    return {
        "ok": True,
        "enabled": True,
        "symbol": (symbol or "ALL").upper(),
        "horizon_days": horizon,
        "total_logged": total_logged,
        "resolved_at_horizon": overall["n"],
        "overall": _summ(overall),
        "by_grade": {k: _summ(v) for k, v in sorted(by_grade.items())},
        "by_confidence_tier": {k: _summ(v) for k, v in sorted(by_conf.items())},
        "by_direction": {k: _summ(v) for k, v in sorted(by_dir.items())},
        "by_regime": {k: _summ(v) for k, v in sorted(by_regime.items())},
        "note": (
            "Direction correct: UP=positive return, DOWN=negative, HOLD/NO-EDGE=move stayed under 3%. "
            "win_rate_wilson_low is a 95% small-sample floor; trust it over raw win_rate at low N."
        ),
    }


def get_if_followed(*, symbol: Optional[str] = None, horizon: int = 5) -> Dict[str, Any]:
    """'If a human followed Ghost's directional calls' performance — no trading."""
    if not ledger_enabled():
        return {"ok": True, "enabled": False}
    if horizon not in HORIZONS:
        horizon = 5
    ret_col = f"return_{horizon}d_pct"
    correct_col = f"correct_{horizon}d"
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ledger_table(cur)
            where = f"{ret_col} IS NOT NULL AND direction IN ('UP','DOWN')"
            params: List[Any] = []
            if symbol:
                where += " AND symbol = %s"
                params.append((symbol or "").upper())
            cur.execute(
                f"""
                SELECT direction, action, accuracy_grade, {ret_col}, {correct_col}
                FROM super_ghost_predictions WHERE {where}
                ORDER BY created_at ASC
                """,
                params,
            )
            raw = cur.fetchall()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}

    trades = []
    for direction, action, grade, ret, correct in raw:
        r = float(ret)
        # A SHORT (DOWN) "followed" return is the inverse of price return.
        signed = r if direction == "UP" else -r
        trades.append({"direction": direction, "grade": grade, "return_pct": round(signed, 3), "correct": bool(correct)})

    n = len(trades)
    wins = [t for t in trades if t["return_pct"] > 0]
    losses = [t for t in trades if t["return_pct"] <= 0]
    gross_win = sum(t["return_pct"] for t in wins)
    gross_loss = abs(sum(t["return_pct"] for t in losses))
    cumulative = 0.0
    equity = [0.0]
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += t["return_pct"]
        equity.append(round(cumulative, 3))
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)

    high_conv = [t for t in trades if (t.get("grade") or "") in ("A+", "A", "B+")]

    def _wr(ts: List[Dict[str, Any]]) -> Optional[float]:
        if not ts:
            return None
        return round(sum(1 for x in ts if x["return_pct"] > 0) / len(ts), 4)

    return {
        "ok": True,
        "enabled": True,
        "symbol": (symbol or "ALL").upper(),
        "horizon_days": horizon,
        "followed_calls": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "avg_win_pct": round(gross_win / len(wins), 3) if wins else None,
        "avg_loss_pct": round(-gross_loss / len(losses), 3) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (None if not wins else float("inf")),
        "net_return_pct": round(cumulative, 3),
        "max_drawdown_pct": round(max_dd, 3),
        "equity_curve": equity,
        "high_conviction_only": {
            "followed_calls": len(high_conv),
            "win_rate": _wr(high_conv),
            "net_return_pct": round(sum(t["return_pct"] for t in high_conv), 3) if high_conv else None,
        },
        "note": (
            "Equal-weight, no leverage, no costs. DOWN calls scored as shorts (inverse price move). "
            "This is a measurement of Ghost's directional calls, NOT a trade recommendation."
        ),
    }


def run_resolver_job() -> Dict[str, Any]:
    """Scheduler hook: resolve pending predictions, then update learning brain.

    PR #93: Learning is tied to truth. The brain only learns after outcomes have
    been resolved, so every adjustment traces back to a real ledger result.
    """
    try:
        out = resolve_predictions(limit=500)
        try:
            from core.super_ghost_learning import learn_from_ledger
            out["learning"] = learn_from_ledger(limit=500)
        except Exception as learn_exc:
            out["learning"] = {"ok": False, "error": str(learn_exc)[:120]}
        try:
            from core.super_ghost_lab import run_lab
            out["lab"] = run_lab(limit=1000, persist=True)
        except Exception as lab_exc:
            out["lab"] = {"ok": False, "error": str(lab_exc)[:120]}
        try:
            from core.super_ghost_memory import score_features_from_ledger
            out["feature_memory"] = score_features_from_ledger(limit=2000)
        except Exception as mem_exc:
            out["feature_memory"] = {"ok": False, "error": str(mem_exc)[:120]}
        try:
            from core.super_ghost_shadow import resolve_shadow_predictions
            out["shadow_models"] = resolve_shadow_predictions(limit=2000)
        except Exception as shadow_exc:
            out["shadow_models"] = {"ok": False, "error": str(shadow_exc)[:120]}
        try:
            from core.super_ghost_promotion import run_promotion_review
            out["promotion_gate"] = run_promotion_review(persist=True)
        except Exception as promo_exc:
            out["promotion_gate"] = {"ok": False, "error": str(promo_exc)[:120]}
        return out
    except Exception as exc:
        LOGGER.warning("run_resolver_job: %s", str(exc)[:120])
        return {"ok": False, "error": str(exc)[:120]}
