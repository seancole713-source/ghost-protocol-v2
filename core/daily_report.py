"""core/daily_report.py — one consolidated "today's report" (PR #157).

Everything Ghost did today and why, in one place, so a human can ask "what's
today's report?" and get the full picture — not just the wallet. Composes the
already-built pieces (scan cycles, gate, wallet, Watcher calibration, breakers)
into structured sections PLUS a plain-English narrative that reads out loud.

Read-only aggregation. Never raises — each section degrades to an error note so
one dead dependency can't blank the whole report.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Any, Dict, List

LOGGER = logging.getLogger("ghost.daily_report")


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as exc:
        LOGGER.debug("daily_report section failed: %s", str(exc)[:100])
        return {"error": str(exc)[:120]} if default is None else default


def _today_ct() -> str:
    try:
        import pytz
        import os
        tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
        return _dt.datetime.now(tz).strftime("%Y-%m-%d")
    except Exception:
        return _dt.date.today().isoformat()


def build_daily_report() -> Dict[str, Any]:
    now = int(time.time())
    today = _today_ct()
    day_start = now - 24 * 3600

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
        from api.routes_wolf_ops import wolf_gate_status, wolf_perf_log_cycles
        gate = wolf_gate_status()
        lp = (gate or {}).get("live_prediction", {})
        cycles = (wolf_perf_log_cycles(limit=200) or {}).get("cycles", [])
        today_cycles = [c for c in cycles if (c.get("cycle_ts") or 0) >= day_start]
        fired = sum(int(c.get("candidates") or 0) for c in today_cycles)
        scanned = today_cycles[0].get("scanned") if today_cycles else None
        # aggregate skip reasons across the day
        skips: Dict[str, int] = {}
        for c in today_cycles:
            for k, v in (c.get("skip_counts") or {}).items():
                skips[k] = skips.get(k, 0) + int(v or 0)
        top_skips = dict(sorted(skips.items(), key=lambda kv: -kv[1])[:6])
        nm = today_cycles[0].get("near_miss") if today_cycles else None
        return {
            "gate_open": bool(lp.get("would_alert")),
            "gate_reason": lp.get("reason"),
            "live_up_prob": lp.get("up_prob"),
            "up_prob_needed": lp.get("up_prob_needed_to_fire"),
            "regime": (lp.get("regime") or {}).get("label"),
            "phase": (gate or {}).get("symbol_stats", {}).get("phase"),
            "scan_cycles_today": len(today_cycles),
            "symbols_scanned": scanned,
            "picks_fired_today": fired,
            "top_skip_reasons": top_skips,
            "closest_to_firing": nm,
        }
    decisions = _safe(_decisions, {})

    # ── 3. wallet day: opened, closed (with why), P&L, goal ───────────────
    def _wallet():
        # PR #158: DB-only wallet read. Do NOT call wallet_summary() here because
        # it refreshes live prices for every open symbol and can make the daily
        # report hang behind market-data providers. This report is observability;
        # it can use cost/realized/daily snapshot fields without live quotes.
        import json as _json
        from core.db import db_conn
        with db_conn() as conn:
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
                           WHERE status='open' AND entry_ts >= %s
                           ORDER BY entry_ts DESC LIMIT 50""", (day_start,))
            opened_today = [
                {"symbol": r[0], "book": r[1], "entry": r[2], "entry_ts": r[3]}
                for r in cur.fetchall()
            ]
            cur.execute("""SELECT symbol, exit_reason, pnl, pnl_pct, exit_ts
                           FROM ghost_paper_trades
                           WHERE status='closed' AND exit_ts >= %s
                           ORDER BY exit_ts DESC LIMIT 50""", (day_start,))
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
        from api.routes_ghost_system import system_breakers_endpoint
        b = system_breakers_endpoint()
        return {k: v.get("state") for k, v in (b.get("breakers") or {}).items()}
    breakers = _safe(_breakers, {})

    report = {
        "ok": True, "date": today, "generated_ts": now,
        "identity": identity, "decisions": decisions,
        "wallet": wallet, "calibration": calibration, "breakers": breakers,
    }
    report["narrative"] = _narrate(report)
    return report


def _narrate(r: Dict[str, Any]) -> List[str]:
    """Plain-English lines a human (or Claude) can read out loud."""
    d = r.get("decisions", {}) or {}
    w = r.get("wallet", {}) or {}
    c = r.get("calibration", {}) or {}
    idn = r.get("identity", {}) or {}
    lines: List[str] = []
    lines.append(f"Ghost daily report — {r.get('date')} (PR {idn.get('pr_version')}, "
                 f"health {idn.get('health_score')}/{idn.get('health_status')}).")
    # what it did
    fired = d.get("picks_fired_today")
    if fired == 0:
        lines.append(f"Predictions: fired ZERO live picks today across "
                     f"{d.get('scan_cycles_today')} scans of {d.get('symbols_scanned')} symbols "
                     f"— gate closed ({d.get('gate_reason')}), up_prob {d.get('live_up_prob')} "
                     f"vs {d.get('up_prob_needed')} needed, regime {d.get('regime')}. Silence = designed, not broken.")
    else:
        lines.append(f"Predictions: FIRED {fired} live pick(s) today — notable, gate opened.")
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
    return lines
