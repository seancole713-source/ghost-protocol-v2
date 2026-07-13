"""core/daily_report.py — consolidated report + persisted logs.

Everything Ghost did today and why, in one place, so a human can ask "what's
today's report?" and get the full picture — not just the wallet. Composes the
already-built pieces (scan cycles, gate, wallet, Watcher calibration, breakers)
into structured sections PLUS a plain-English narrative that reads out loud.

``build_daily_report`` is read-only aggregation. ``snapshot_daily_report`` is the
append-only notebook writer: it persists the current report into
``ghost_daily_report_logs`` and never mutates predictions, gates, wallet, or
Watcher evidence.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import core.db as _db
from core.quiet import note_suppressed

LOGGER = logging.getLogger("ghost.daily_report")

# The snapshot job appends every 15 minutes (~96 rows/day). Without a bound this
# notebook grows forever; the sibling ghost_perf_cycles uses the same retention
# discipline (GHOST_PERF_RETENTION_DAYS). Keep the two consistent.
_LOG_RETENTION_DAYS = max(2, int(os.getenv("GHOST_DAILY_REPORT_RETENTION_DAYS", "120")))


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as exc:
        LOGGER.debug("daily_report section failed: %s", str(exc)[:100])
        return {"error": str(exc)[:120]} if default is None else default


def _today_ct() -> str:
    return _day_bounds_ct()[0]


def _tzinfo():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.getenv("GHOST_TZ", "America/Chicago"))
    except Exception:
        return _dt.timezone.utc


def _day_bounds_ct(day: Optional[str] = None) -> tuple[str, int, int]:
    """Return (YYYY-MM-DD, start_ts, end_ts) for the configured Ghost day.

    The original PR #157 code used a rolling 24h window. That answered "last 24h"
    but not "today's report." This helper gives true calendar-day boundaries in
    the operator timezone (America/Chicago by default).
    """
    tz = _tzinfo()
    now_dt = _dt.datetime.now(tz)
    if day:
        try:
            d = _dt.date.fromisoformat(day)
        except Exception:
            d = now_dt.date()
    else:
        d = now_dt.date()
    start = _dt.datetime(d.year, d.month, d.day, tzinfo=tz)
    end = start + _dt.timedelta(days=1)
    return d.isoformat(), int(start.timestamp()), int(end.timestamp())


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v or 0)
    except Exception:
        return default


def _as_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _round(v: Any, ndigits: int = 4) -> Optional[float]:
    f = _as_float(v)
    return round(f, ndigits) if f is not None else None


DOCTRINE_WORDS = ("Clarity", "Decision", "Direction", "Alignment", "Consistency", "Results")


def _build_report_doctrine(
    *,
    decisions: Dict[str, Any],
    wallet: Dict[str, Any],
    calibration: Dict[str, Any],
    breakers: Dict[str, Any],
    identity: Dict[str, Any],
) -> Dict[str, Any]:
    """Map the daily report onto Ghost's six-word doctrine.

    This is a read-only display/thinking layer. It summarizes the evidence
    already gathered by the report; it does not call the engine, change gates, or
    write state. Unknown values are explicitly ``insufficient``.
    """
    feed_states = breakers or {}
    open_breakers = sorted([name for name, state in feed_states.items() if state and state != "closed"])
    scan_cycles = _as_int(decisions.get("scan_cycles_today"))
    age = decisions.get("latest_cycle_age_seconds")
    age_i = _as_int(age) if age is not None else None
    paused = bool(decisions.get("latest_cycle_paused"))
    gate_open = bool(decisions.get("gate_open"))
    fired = _as_int(decisions.get("picks_fired_today"))
    wallet_ok = bool(wallet) and "error" not in wallet
    cal_status = calibration.get("status")
    cal_n = _as_int(calibration.get("resolved_n"))
    brier = calibration.get("brier")
    high_wr = calibration.get("high_conf_win_rate")
    health_score = _as_int(identity.get("health_score"))

    clarity_status = "pass" if scan_cycles > 0 and not open_breakers else ("hold" if scan_cycles > 0 else "insufficient")
    decision_status = "pass" if gate_open and fired > 0 and not paused else ("hold" if scan_cycles > 0 or paused else "insufficient")
    direction_status = "pass" if fired > 0 else ("hold" if decisions.get("closest_to_firing") or decisions.get("latest_symbol_evals") else "insufficient")
    alignment_status = "pass" if gate_open and not paused else ("hold" if paused or decisions.get("gate_reason") else "insufficient")
    consistency_status = "pass" if cal_n >= 30 and cal_status not in (None, "guessing") else ("hold" if cal_n > 0 else "insufficient")
    results_status = "pass" if wallet_ok and wallet.get("today_pnl", 0) and float(wallet.get("today_pnl") or 0) > 0 else ("hold" if wallet_ok else "insufficient")

    steps = [
        {
            "step": 1,
            "key": "clarity",
            "label": "Clarity",
            "status": clarity_status,
            "headline": (
                f"{scan_cycles} scan(s), latest {age_i // 60 if age_i is not None else '—'} min old"
                + (f"; open breakers: {', '.join(open_breakers)}" if open_breakers else "; feeds clear")
            ),
            "evidence": [
                {"name": "health_score", "value": health_score, "source": "health"},
                {"name": "scan_cycles_today", "value": scan_cycles, "source": "ghost_perf_cycles"},
                {"name": "latest_cycle_age_seconds", "value": age_i, "source": "ghost_perf_cycles"},
                {"name": "open_breakers", "value": open_breakers, "source": "circuit_breaker"},
            ],
        },
        {
            "step": 2,
            "key": "decision",
            "label": "Decision",
            "status": decision_status,
            "headline": (
                f"{'Gate open' if gate_open else 'Gate holding'}"
                + (f"; engine paused: {decisions.get('latest_cycle_pause_reason')}" if paused else f"; reason: {decisions.get('gate_reason')}")
            ),
            "evidence": [
                {"name": "gate_open", "value": gate_open, "source": "ghost_perf_cycles"},
                {"name": "picks_fired_today", "value": fired, "source": "ghost_perf_cycles"},
                {"name": "latest_cycle_paused", "value": paused, "source": "ghost_perf_cycles"},
                {"name": "gate_reason", "value": decisions.get("gate_reason"), "source": "ghost_perf_cycles"},
            ],
        },
        {
            "step": 3,
            "key": "direction",
            "label": "Direction",
            "status": direction_status,
            "headline": (
                "Live pick fired" if fired else
                f"Closest candidate: {(decisions.get('closest_to_firing') or {}).get('symbol') or 'none'}"
            ),
            "evidence": [
                {"name": "picks_fired_today", "value": fired, "source": "ghost_perf_cycles"},
                {"name": "closest_to_firing", "value": decisions.get("closest_to_firing"), "source": "ghost_perf_cycles"},
                {"name": "latest_symbol_evals_count", "value": len(decisions.get("latest_symbol_evals") or []), "source": "ghost_perf_symbol_evals"},
            ],
        },
        {
            "step": 4,
            "key": "alignment",
            "label": "Alignment",
            "status": alignment_status,
            "headline": f"Regime {decisions.get('regime') or 'unknown'}; phase {decisions.get('phase') or 'unknown'}",
            "evidence": [
                {"name": "regime", "value": decisions.get("regime"), "source": "ghost_perf_cycles"},
                {"name": "phase", "value": decisions.get("phase"), "source": "objective_mode"},
                {"name": "top_skip_reasons", "value": decisions.get("top_skip_reasons"), "source": "ghost_perf_cycles"},
            ],
        },
        {
            "step": 5,
            "key": "consistency",
            "label": "Consistency",
            "status": consistency_status,
            "headline": f"Watcher: {cal_status or 'insufficient'}; {cal_n} resolved",
            "evidence": [
                {"name": "resolved_n", "value": cal_n, "source": "watcher_summary"},
                {"name": "calibration_status", "value": cal_status, "source": "watcher_summary"},
                {"name": "high_conf_win_rate", "value": high_wr, "source": "watcher_summary"},
                {"name": "brier", "value": brier, "source": "watcher_summary"},
            ],
        },
        {
            "step": 6,
            "key": "results",
            "label": "Results",
            "status": results_status,
            "headline": (
                f"Wallet ${wallet.get('total_value')} · today P&L {wallet.get('today_pnl')}"
                if wallet_ok else "Wallet result unavailable"
            ),
            "evidence": [
                {"name": "wallet_total_value", "value": wallet.get("total_value"), "source": "ghost_paper_daily/trades"},
                {"name": "wallet_today_pnl", "value": wallet.get("today_pnl"), "source": "ghost_paper_daily"},
                {"name": "closed_today_wins", "value": wallet.get("closed_today_wins"), "source": "ghost_paper_trades"},
                {"name": "closed_today_losses", "value": wallet.get("closed_today_losses"), "source": "ghost_paper_trades"},
                {"name": "goal_pct", "value": wallet.get("goal_pct"), "source": "paper_wallet_config"},
            ],
        },
    ]
    counts = {s: sum(1 for step in steps if step["status"] == s) for s in ("pass", "hold", "insufficient")}
    return {
        "ok": True,
        "version": "1.0",
        "words": list(DOCTRINE_WORDS),
        "display_only": True,
        "headline": f"{counts['pass']} pass / {counts['hold']} hold / {counts['insufficient']} insufficient",
        "steps": steps,
        "summary": counts,
    }



def _coerce_json(v: Any) -> Any:
    if v is None or isinstance(v, (dict, list)):
        return v
    try:
        return _json.loads(v)
    except Exception:
        return v


def _fetch_cycles_readonly(day_start: int, day_end: int, *, limit: int = 200) -> List[Dict[str, Any]]:
    """Read cycle summaries without triggering performance-log DDL."""
    with _db.db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, cycle_ts, duration_ms, scanned, candidates, saved,
                   dedup_blocked, would_fire, binding_skip, paused,
                   pause_reason, suppressed, suppress_reason, skip_counts,
                   near_miss, regime, objective_mode, saved_prediction_ids
            FROM ghost_perf_cycles
            WHERE cycle_ts >= %s AND cycle_ts < %s
            ORDER BY cycle_ts DESC, id DESC
            LIMIT %s
            """,
            (day_start, day_end, max(1, min(500, int(limit)))),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0], "cycle_ts": r[1], "duration_ms": r[2],
            "scanned": r[3], "candidates": r[4], "saved": r[5],
            "dedup_blocked": r[6], "would_fire": bool(r[7]),
            "binding_skip": r[8], "paused": bool(r[9]),
            "pause_reason": r[10], "suppressed": r[11],
            "suppress_reason": r[12], "skip_counts": _coerce_json(r[13]) or {},
            "near_miss": _coerce_json(r[14]), "regime": _coerce_json(r[15]) or {},
            "objective_mode": _coerce_json(r[16]) or {},
            "saved_prediction_ids": _coerce_json(r[17]) or [],
        }
        for r in rows
    ]


def _fetch_symbol_evals_readonly(cycle_id: int, *, limit: int = 12) -> List[Dict[str, Any]]:
    """Read per-symbol details for one cycle without side effects."""
    with _db.db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT symbol, skip_code, fired, saved, direction, up_prob,
                   confidence, min_win_proba, regime_label, eval_ts
            FROM ghost_perf_symbol_evals
            WHERE cycle_id=%s
            ORDER BY up_prob DESC NULLS LAST, symbol ASC
            LIMIT %s
            """,
            (int(cycle_id), max(1, min(100, int(limit)))),
        )
        rows = cur.fetchall()
    return [
        {
            "symbol": r[0], "skip_code": r[1], "fired": bool(r[2]),
            "saved": bool(r[3]), "direction": r[4], "up_prob": r[5],
            "confidence": r[6], "min_win_proba": r[7],
            "regime_label": r[8], "eval_ts": r[9],
        }
        for r in rows
    ]


def _fetch_events_readonly(day_start: int, day_end: int, *, limit: int = 80) -> List[Dict[str, Any]]:
    """Read lifecycle events without triggering DDL."""
    with _db.db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, event_ts, event_type, prediction_id, symbol, cycle_id, payload
            FROM ghost_perf_events
            WHERE event_ts >= %s AND event_ts < %s
            ORDER BY event_ts DESC, id DESC
            LIMIT %s
            """,
            (day_start, day_end, max(1, min(500, int(limit)))),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0], "event_ts": r[1], "event_type": r[2],
            "prediction_id": r[3], "symbol": r[4], "cycle_id": r[5],
            "payload": _coerce_json(r[6]) or {},
        }
        for r in rows
    ]


def ensure_daily_report_tables(cur) -> None:
    """Create the append-only daily report notebook table.

    This table is observability only. It stores report payloads so the operator
    can ask for "today's logs" later even if the app restarted.
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_daily_report_logs (
            id SERIAL PRIMARY KEY,
            report_date TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            pr_version INT,
            git_sha TEXT,
            health_score INT,
            gate_open BOOLEAN,
            gate_reason TEXT,
            picks_saved_today INT,
            wallet_total_value FLOAT,
            wallet_today_pnl FLOAT,
            calibration_status TEXT,
            payload_json JSONB NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_daily_report_logs_date_created
        ON ghost_daily_report_logs (report_date, created_at DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_daily_report_logs_created
        ON ghost_daily_report_logs (created_at DESC)
        """
    )


def _prune_daily_report_logs(cur) -> int:
    """Drop notebook rows older than the retention window.

    Runs inside the same transaction as the snapshot insert so the append-only
    log can't grow without bound (96 rows/day at the 15-min cadence). Best-effort:
    a prune failure must never block a snapshot, so callers wrap it in a guard.
    Returns the number of rows deleted (0 when the driver doesn't report it).
    """
    cutoff = int(time.time()) - _LOG_RETENTION_DAYS * 86400
    cur.execute("DELETE FROM ghost_daily_report_logs WHERE created_at < %s", (cutoff,))
    return int(getattr(cur, "rowcount", 0) or 0)


def build_daily_report(day: Optional[str] = None) -> Dict[str, Any]:
    now = int(time.time())
    today, day_start, day_end = _day_bounds_ct(day)

    # ── 1. build identity + health ────────────────────────────────────────
    def _identity():
        from wolf_app import _deploy_meta, _health_public
        meta = _deploy_meta()
        h = _health_public()
        return {"pr_version": meta.get("pr_version") or meta.get("_pr_version"),
                "git_sha": meta.get("git_sha_short"),
                "health_score": h.get("score"), "health_status": h.get("status")}
    identity = _safe(_identity, {})

    # ── 2. what Ghost DID today: scans, gate, fires ───────────────────────
    def _decisions():
        # DB-only performance-log read. Do NOT call /api/wolf/gate-status here:
        # that endpoint recomputes a live prediction and has historically taken
        # long enough to 502 the report. The performance log is the authoritative
        # notebook of what Ghost actually scanned and why it held/fired.
        cycles = _fetch_cycles_readonly(day_start, day_end, limit=200)
        latest = cycles[0] if cycles else {}
        latest_evals: List[Dict[str, Any]] = []
        if latest.get("id"):
            latest_evals = _fetch_symbol_evals_readonly(int(latest["id"]), limit=12)

        picks_saved = sum(_as_int(c.get("saved")) for c in cycles)
        candidates = sum(_as_int(c.get("candidates")) for c in cycles)
        would_fire_cycles = sum(1 for c in cycles if c.get("would_fire"))
        scanned = latest.get("scanned") if latest else None
        # aggregate skip reasons across the day
        skips: Dict[str, int] = {}
        for c in cycles:
            for k, v in (c.get("skip_counts") or {}).items():
                skips[k] = skips.get(k, 0) + _as_int(v)
        top_skips = dict(sorted(skips.items(), key=lambda kv: -kv[1])[:6])
        nm = latest.get("near_miss") if latest else None
        try:
            from core.prediction import backfill_near_miss_for_display
            nm = backfill_near_miss_for_display(nm)
        except Exception:
            note_suppressed()
        symbol_evals = []
        for ev in latest_evals[:12]:
            symbol_evals.append({
                "symbol": ev.get("symbol"),
                "skip_code": ev.get("skip_code"),
                "fired": bool(ev.get("fired")),
                "saved": bool(ev.get("saved")),
                "direction": ev.get("direction"),
                "up_prob": _round(ev.get("up_prob"), 4),
                "confidence": _round(ev.get("confidence"), 4),
                "min_win_proba": _round(ev.get("min_win_proba"), 4),
                "regime_label": ev.get("regime_label"),
            })
        events = _fetch_events_readonly(day_start, day_end, limit=80)[:30]
        regime_payload = latest.get("regime") or {}
        return {
            "source": "ghost_perf_cycles_db_only",
            "gate_open": bool(latest.get("would_fire") or _as_int(latest.get("saved")) > 0),
            "gate_reason": latest.get("binding_skip") or ("open" if latest else "no_cycle_today"),
            "latest_cycle_id": latest.get("id"),
            "latest_cycle_ts": latest.get("cycle_ts"),
            "latest_cycle_age_seconds": max(0, now - _as_int(latest.get("cycle_ts"))) if latest else None,
            "latest_cycle_duration_ms": latest.get("duration_ms"),
            "latest_cycle_paused": bool(latest.get("paused")) if latest else False,
            "latest_cycle_pause_reason": latest.get("pause_reason"),
            "latest_cycle_suppressed": latest.get("suppressed"),
            "regime": regime_payload.get("label") or (nm or {}).get("regime_label"),
            "phase": ((latest.get("objective_mode") or {}).get("phase")
                      or (nm or {}).get("objective_mode")),
            "scan_cycles_today": len(cycles),
            "symbols_scanned": scanned,
            "picks_fired_today": picks_saved,
            "candidates_today": candidates,
            "would_fire_cycles_today": would_fire_cycles,
            "top_skip_reasons": top_skips,
            "closest_to_firing": nm,
            "latest_symbol_evals": symbol_evals,
            "recent_events": events,
        }
    decisions = _safe(_decisions, {})

    # ── 3. wallet day: opened, closed (with why), P&L, goal ───────────────
    def _wallet():
        # PR #158: DB-only wallet read. Do NOT call wallet_summary() here because
        # it refreshes live prices for every open symbol and can make the daily
        # report hang behind market-data providers. This report is observability;
        # it can use cost/realized/daily snapshot fields without live quotes.
        import json as _json
        with _db.db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(SUM(pnl),0), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), COUNT(*) "
                        "FROM ghost_paper_trades WHERE status='closed'")
            realized, total_wins, total_closed = cur.fetchone()
            cur.execute("SELECT COUNT(*), COALESCE(SUM(qty*entry_price),0) FROM ghost_paper_trades WHERE status='open'")
            open_count, invested = cur.fetchone()
            cur.execute("SELECT trade_date, equity, pnl FROM ghost_paper_daily ORDER BY trade_date DESC LIMIT 1")
            drow = cur.fetchone()
            today_pnl = float(drow[2]) if drow else 0.0
            equity = float(drow[1]) if drow else None
            cur.execute("SELECT val FROM ghost_state WHERE key='paper_wallet_config'")
            cfg_row = cur.fetchone()
            cfg = {}
            if cfg_row and cfg_row[0]:
                try:
                    cfg = _json.loads(cfg_row[0])
                except Exception:
                    cfg = {}
            start = float(cfg.get("starting_balance") or 10000.0)
            goal = float(cfg.get("monthly_goal") or 20000.0)
            if equity is None:
                equity = round(start + float(realized or 0), 2)
            cur.execute("""SELECT symbol, book, entry_price, entry_ts
                           FROM ghost_paper_trades
                           WHERE status='open' AND entry_ts >= %s AND entry_ts < %s
                           ORDER BY entry_ts DESC LIMIT 50""", (day_start, day_end))
            opened_today = [
                {"symbol": r[0], "book": r[1], "entry": r[2], "entry_ts": r[3]}
                for r in cur.fetchall()
            ]
            cur.execute("""SELECT symbol, exit_reason, pnl, pnl_pct, exit_ts
                           FROM ghost_paper_trades
                           WHERE status='closed' AND exit_ts >= %s AND exit_ts < %s
                           ORDER BY exit_ts DESC LIMIT 50""", (day_start, day_end))
            closed_today = [
                {"symbol": r[0], "reason": r[1], "pnl": r[2], "pnl_pct": r[3], "exit_ts": r[4]}
                for r in cur.fetchall()
            ]
        wins = [h for h in closed_today if (h.get("pnl") or 0) > 0]
        losses = [h for h in closed_today if (h.get("pnl") or 0) < 0]
        return {
            "total_value": round(float(equity), 2),
            "today_pnl": today_pnl,
            "open_positions": int(open_count or 0),
            "invested_cost": round(float(invested or 0), 2),
            "opened_today": opened_today,
            "closed_today": closed_today,
            "closed_today_wins": len(wins), "closed_today_losses": len(losses),
            "total_closed": int(total_closed or 0),
            "total_closed_wins": int(total_wins or 0),
            "goal": goal,
            "goal_pct": round(float(equity) / goal * 100, 1) if goal else None,
            "note": "DB-only wallet snapshot; avoids live price refresh so /api/report/daily stays fast.",
        }
    wallet = _safe(_wallet, {})

    # ── 4. is it working or guessing (Watcher calibration) ────────────────
    def _calibration():
        from core.watcher import watcher_summary
        ws = watcher_summary(days=30)
        cal = ws.get("shadow_calibration") or {}
        return {
            "verdict": (cal.get("verdict") or {}).get("headline"),
            "status": (cal.get("verdict") or {}).get("status"),
            "resolved_n": cal.get("resolved_n"),
            "high_conf_win_rate": (cal.get("high_confidence") or {}).get("win_rate"),
            "brier": cal.get("brier"),
            "bins": [{"band": b.get("label"), "n": b.get("n"), "win_rate": b.get("win_rate")}
                     for b in (cal.get("bins") or [])],
            "blind_spots": (ws.get("blind_spots") or {}).get("top_skip_codes", [])[:4],
        }
    calibration = _safe(_calibration, {})

    # ── 5. breakers ───────────────────────────────────────────────────────
    def _breakers():
        from core.circuit_breaker import all_breaker_status
        return {k: v.get("state") for k, v in (all_breaker_status() or {}).items()}
    breakers = _safe(_breakers, {})

    doctrine = _build_report_doctrine(
        decisions=decisions,
        wallet=wallet,
        calibration=calibration,
        breakers=breakers,
        identity=identity,
    )
    report = {
        "ok": True, "date": today, "generated_ts": now,
        "identity": identity, "decisions": decisions,
        "wallet": wallet, "calibration": calibration, "breakers": breakers,
        "doctrine": doctrine,
    }
    report["narrative"] = _narrate(report)
    return report



def snapshot_daily_report(day: Optional[str] = None) -> Dict[str, Any]:
    """Append the current daily report to Ghost's notebook table.

    This is the only mutating path in this module, and it writes only
    ``ghost_daily_report_logs``. It is safe observability: no prediction, gate,
    wallet, Watcher, or model state is changed.
    """
    report = build_daily_report(day=day)
    identity = report.get("identity") or {}
    decisions = report.get("decisions") or {}
    wallet = report.get("wallet") or {}
    calibration = report.get("calibration") or {}
    created_at = _as_int(report.get("generated_ts"), int(time.time()))
    with _db.db_conn() as conn:
        cur = conn.cursor()
        ensure_daily_report_tables(cur)
        cur.execute(
            """
            INSERT INTO ghost_daily_report_logs (
                report_date, created_at, pr_version, git_sha, health_score,
                gate_open, gate_reason, picks_saved_today, wallet_total_value,
                wallet_today_pnl, calibration_status, payload_json
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            RETURNING id
            """,
            (
                report.get("date"),
                created_at,
                identity.get("pr_version"),
                identity.get("git_sha"),
                identity.get("health_score"),
                bool(decisions.get("gate_open")),
                decisions.get("gate_reason"),
                _as_int(decisions.get("picks_fired_today")),
                _as_float(wallet.get("total_value")),
                _as_float(wallet.get("today_pnl")),
                calibration.get("status"),
                _json.dumps(report, default=str),
            ),
        )
        row = cur.fetchone()
        # Enforce retention in the same transaction so the notebook stays bounded.
        pruned = 0
        try:
            pruned = _prune_daily_report_logs(cur)
        except Exception:
            note_suppressed()
    return {
        "ok": True,
        "log_id": int(row[0]) if row else None,
        "report_date": report.get("date"),
        "created_at": created_at,
        "pruned_rows": pruned,
        "retention_days": _LOG_RETENTION_DAYS,
        "read_only_decisions": True,
        "writes_only": "ghost_daily_report_logs",
        "report": report,
    }


def latest_daily_report_logs(
    *,
    limit: int = 24,
    day: Optional[str] = None,
    include_payload: bool = False,
    by_day: bool = False,
) -> Dict[str, Any]:
    """Read persisted daily report notebook rows.

    GET callers use this to answer "what did Ghost log today?" without causing a
    new write. If the table is not present yet (fresh deploy before first
    scheduler tick), return an empty log rather than failing the dashboard.

    ``by_day=True`` collapses the ~96 snapshots/day down to the LATEST snapshot
    for each calendar day, so ``limit`` then means "how many days back" — the
    day-by-day audit trail the operator asked for ("today's day report logs").
    """
    lim = max(1, min(200, int(limit)))
    params: List[Any] = []
    where = ""
    if day:
        where = " WHERE report_date=%s"
        params.append(day)
    if by_day and not day:
        # DISTINCT ON keeps the newest snapshot per report_date; the outer sort
        # then returns the most recent days first.
        select_sql = f"""
            SELECT * FROM (
                SELECT DISTINCT ON (report_date)
                       id, report_date, created_at, pr_version, git_sha,
                       health_score, gate_open, gate_reason, picks_saved_today,
                       wallet_total_value, wallet_today_pnl, calibration_status,
                       payload_json
                FROM ghost_daily_report_logs
                ORDER BY report_date DESC, created_at DESC, id DESC
            ) latest_per_day
            ORDER BY report_date DESC
            LIMIT %s
        """
        query_params: tuple = (lim,)
    else:
        select_sql = f"""
            SELECT id, report_date, created_at, pr_version, git_sha,
                   health_score, gate_open, gate_reason, picks_saved_today,
                   wallet_total_value, wallet_today_pnl, calibration_status,
                   payload_json
            FROM ghost_daily_report_logs{where}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
        """
        query_params = tuple(params) + (lim,)
    try:
        with _db.db_conn() as conn:
            cur = conn.cursor()
            cur.execute(select_sql, query_params)
            rows = cur.fetchall()
    except Exception as exc:
        # Missing table before first snapshot is non-fatal; other DB read errors
        # are surfaced as an empty log with the truncated reason.
        LOGGER.debug("daily report log read failed: %s", str(exc)[:100])
        return {"ok": True, "read_only": True, "count": 0, "rows": [], "note": str(exc)[:120]}

    out_rows: List[Dict[str, Any]] = []
    for r in rows:
        payload = r[12]
        if isinstance(payload, str):
            try:
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        elif payload is None:
            payload = {}
        row = {
            "id": r[0],
            "date": r[1],
            "created_at": r[2],
            "pr_version": r[3],
            "git_sha": r[4],
            "health_score": r[5],
            "gate_open": bool(r[6]),
            "gate_reason": r[7],
            "picks_saved_today": r[8],
            "wallet_total_value": r[9],
            "wallet_today_pnl": r[10],
            "calibration_status": r[11],
            "narrative": (payload or {}).get("narrative") or [],
        }
        if include_payload:
            row["payload"] = payload
        out_rows.append(row)
    return {
        "ok": True,
        "read_only": True,
        "count": len(out_rows),
        "limit": lim,
        "date": day,
        "by_day": bool(by_day and not day),
        "rows": out_rows,
    }


def _narrate(r: Dict[str, Any]) -> List[str]:
    """Plain-English lines a human (or Claude) can read out loud."""
    d = r.get("decisions", {}) or {}
    w = r.get("wallet", {}) or {}
    c = r.get("calibration", {}) or {}
    idn = r.get("identity", {}) or {}
    br = r.get("breakers", {}) or {}
    doctrine = r.get("doctrine", {}) or {}
    lines: List[str] = []
    lines.append(f"Ghost daily report — {r.get('date')} (PR {idn.get('pr_version')}, "
                 f"health {idn.get('health_score')}/{idn.get('health_status')}).")
    if doctrine.get("steps"):
        lines.append("Ghost Doctrine: " + " → ".join(doctrine.get("words") or DOCTRINE_WORDS)
                     + f" ({doctrine.get('headline')}).")
        for step in doctrine.get("steps", [])[:6]:
            lines.append(f"  · {step.get('label')}: {step.get('status')} — {step.get('headline')}")
    # what it did
    fired = d.get("picks_fired_today")
    if fired == 0:
        close = d.get("closest_to_firing") or {}
        close_bits = ""
        if close.get("symbol"):
            close_bits = f" Closest miss: {close.get('symbol')} at up_prob {close.get('up_prob')} blocked by {close.get('skip')}."
        lines.append(f"Predictions: fired ZERO live picks today across "
                     f"{d.get('scan_cycles_today')} scans of {d.get('symbols_scanned')} symbols "
                     f"— gate closed ({d.get('gate_reason')}), regime {d.get('regime')}."
                     f"{close_bits} Silence = designed, not broken.")
    else:
        lines.append(f"Predictions: FIRED {fired} live pick(s) today — notable, gate opened.")
    # scan freshness — is Ghost actually awake right now?
    age = d.get("latest_cycle_age_seconds")
    if age is not None:
        mins = int(age // 60)
        fresh = "scanning live" if age <= 1200 else "STALE — no recent scan"
        lines.append(f"Freshness: last scan {mins} min ago ({fresh}).")
    if d.get("latest_cycle_paused"):
        lines.append(f"Engine: PAUSED ({d.get('latest_cycle_pause_reason') or 'reason unrecorded'}).")
    ts = d.get("top_skip_reasons") or {}
    if ts:
        lines.append("Why it held back (top skips): " + ", ".join(f"{k}={v}" for k, v in ts.items()) + ".")
    # wallet
    if "error" not in w:
        ct = w.get("closed_today") or []
        lines.append(f"Wallet: ${w.get('total_value')} ({w.get('goal_pct')}% of ${w.get('goal')} goal), "
                     f"today P&L {w.get('today_pnl')}. Closed {len(ct)} trades today "
                     f"({w.get('closed_today_wins')}W/{w.get('closed_today_losses')}L), "
                     f"{len(w.get('opened_today') or [])} opened.")
        for t in ct[:8]:
            lines.append(f"  · SELL {t['symbol']} via {t.get('reason')}: {t.get('pnl_pct')}% (${t.get('pnl')}).")
    # working or guessing
    if c.get("verdict"):
        lines.append(f"Working-or-guessing: {c.get('verdict')} "
                     f"(Brier {c.get('brier')}, {c.get('resolved_n')} resolved). "
                     f"NOT guessing — but calibration is the watch-item.")
    # data feeds — a tripped breaker is why a section can look thin
    if br and "error" not in br:
        tripped = [name for name, state in br.items() if state and state != "closed"]
        if tripped:
            lines.append("Data feeds: DEGRADED — open breaker(s): " + ", ".join(sorted(tripped)) + ".")
        else:
            lines.append("Data feeds: all healthy (no open breakers).")
    return lines
