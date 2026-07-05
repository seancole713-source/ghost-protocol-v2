"""Ghost Ask — Claude Q&A grounded in live Ghost Protocol state.

Uses ANTHROPIC_API_KEY (same as core/news.py). Does not place trades or
override pick gates; explains SILENCE, portfolio, and WOLF context only.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests
from core.db import ensure_ghost_state

LOGGER = logging.getLogger("ghost.ask")

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ASK_MODEL = os.getenv("GHOST_ASK_MODEL", "claude-haiku-4-5-20251001")
ASK_MAX_TOKENS = max(256, min(2048, int(os.getenv("GHOST_ASK_MAX_TOKENS", "900"))))
ASK_DAILY_LIMIT = max(1, int(os.getenv("GHOST_ASK_DAILY_LIMIT", "40")))


def _system_prompt() -> str:
    return (
        "You are Ghost Protocol Ask — an assistant for the Ghost trading signal product.\n"
        "Rules:\n"
        "1. The JSON context is ground truth for Ghost engine state (cooldown, gates, picks, portfolio).\n"
        "2. Official trade signals ONLY come from context.picks and open picks with passed gates. "
        "Never invent SUPER BUY / BUY NOW / pick prices.\n"
        "3. If trade_action is NO TRADE or SILENCE, say clearly not to treat Ghost Score bias as a buy.\n"
        "4. Portfolio symbols (LULU, AMC, etc.) can have picks only when they appear in context.picks "
        "after model + gate checks. Never imply a pick exists if context does not show one.\n"
        "5. Engine pause (context.engine_pause) is separate from model gates. "
        "no_v3_model on untrained watchlist symbols is a coverage gap — NOT 'WOLF model missing'. "
        "When context.live_gate or near_miss shows up_prob, the v3 model IS loaded; prob_low means "
        "probability below the BUY floor (working as designed).\n"
        "6. You may explain general market/earnings concepts when asked, but label them as general "
        "education, not Ghost signals.\n"
        "7. Not financial advice. Encourage 1% risk, stops, and following Ghost rules.\n"
        "8. Be concise (under 250 words unless user asks for detail). Use plain language.\n"
        "9. When context.market_session is Pre-Market and premarket_scan_enabled is true, "
        "Ghost scans the watchlist before the 9:30 ET open using extended-hours quotes "
        "(gap vs prior close). Pre-market fires require a higher confidence floor; "
        "open-buffer rules still apply after the bell.\n"
        "10. When open_pick_review_enabled is true, Ghost re-scans open picks every cycle. "
        "If the model no longer supports the trade (regime gate, prob below floor, etc.), "
        "the pick is withdrawn (outcome WITHDRAWN) — not a WIN/LOSS. A new pick may follow "
        "in the same or a later cycle. Explain withdrawals using context.latest_scan or "
        "recent resolves; do not treat a withdrawn pick as still actionable.\n"
    )


def build_ask_context(include_portfolio: bool = False) -> Dict[str, Any]:
    """Snapshot live Ghost state for Claude (sync, no HTTP loopback).

    include_portfolio defaults to False because the public /api/wolf/ask path
    consumes this context — personal holdings may only be included for
    auth-gated callers (/api/wolf/ask/context, MCP ghost_context). This flag
    is the PII control; never rely on exception handling to omit the block.
    """
    ctx: Dict[str, Any] = {"ts": int(time.time())}
    try:
        from core.market_hours import is_us_premarket, market_session_label
        from core.prediction import _premarket_scan_enabled
        from core.pick_review import open_pick_review_enabled
        ctx["market_session"] = market_session_label()
        ctx["premarket_scan_enabled"] = _premarket_scan_enabled()
        ctx["is_premarket"] = is_us_premarket()
        ctx["open_pick_review_enabled"] = open_pick_review_enabled()
    except Exception as e:
        ctx["market_session_error"] = str(e)[:120]
    try:
        from core.prediction import engine_pause_state
        ctx["engine_pause"] = engine_pause_state()
    except Exception as e:
        ctx["engine_pause_error"] = str(e)[:120]

    try:
        from core.risk_discipline import combined_trading_block, risk_settings
        ctx["risk_discipline"] = combined_trading_block()
        ctx["risk_settings"] = risk_settings()
    except Exception as e:
        ctx["risk_discipline_error"] = str(e)[:120]

    try:
        from api.wolf_endpoints import ghost_score_payload_sync
        gs = ghost_score_payload_sync(use_cache=False)
        ctx["ghost_score"] = {
            k: gs.get(k)
            for k in (
                "score", "signal", "bias_label", "trade_action", "trade_note",
                "signal_note", "confidence_floor", "open_pick_id",
            )
        }
    except Exception as e:
        ctx["ghost_score_error"] = str(e)[:120]

    try:
        from core.db import db_conn
        from core.prediction_filters import V32_ERA_MIN_ID as _V32_MIN
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT outcome FROM predictions WHERE symbol='WOLF' AND id >= %s "
                "AND outcome IN ('WIN','LOSS') ORDER BY resolved_at DESC NULLS LAST, id DESC",
                (_V32_MIN,),
            )
            outs = [r[0] for r in cur.fetchall()]
            wins, losses = outs.count("WIN"), outs.count("LOSS")
            tot = wins + losses
            ctx["wolf_track_record"] = {
                "wins": wins, "losses": losses,
                "win_rate_pct": round(wins / tot * 100, 1) if tot else 0,
                "last5": ["W" if o == "WIN" else "L" for o in outs[:5]],
            }
            cur.execute(
                "SELECT id, direction, confidence, entry_price, target_price, stop_price, "
                "predicted_at FROM predictions "
                "WHERE symbol='WOLF' AND outcome IS NULL AND expires_at > %s "
                "ORDER BY id DESC LIMIT 1",
                (int(time.time()),),
            )
            row = cur.fetchone()
            if row:
                ctx["open_wolf_pick"] = {
                    "id": row[0], "direction": row[1], "confidence": float(row[2]),
                    "entry_price": float(row[3]), "target_price": float(row[4]),
                    "stop_price": float(row[5]), "predicted_at": row[6],
                }
            cur.execute(
                "SELECT id, direction, confidence, outcome, pnl_pct, resolved_at FROM predictions "
                "WHERE symbol='WOLF' AND id >= %s AND outcome IN ('WIN','LOSS') "
                "ORDER BY resolved_at DESC NULLS LAST LIMIT 5",
                (_V32_MIN,),
            )
            ctx["recent_wolf_resolves"] = [
                {"id": r[0], "direction": r[1], "confidence": float(r[2]),
                 "outcome": r[3], "pnl_pct": float(r[4] or 0), "resolved_at": r[5]}
                for r in cur.fetchall()
            ]
    except Exception as e:
        ctx["wolf_picks_error"] = str(e)[:120]

    try:
        import json as _j
        from core.db import db_conn as _dbc
        with _dbc() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='gate_outcome_history'")
            row = cur.fetchone()
        hist = []
        if row and row[0]:
            try:
                hist = _j.loads(row[0])
            except Exception:
                hist = []
        latest = hist[-1] if isinstance(hist, list) and hist else {}
        ctx["latest_scan"] = {
            "binding_skip": latest.get("binding_skip") or latest.get("top_skip"),
            "skip_counts": latest.get("skip_counts"),
            "paused": latest.get("paused"),
            "pause_reason": latest.get("pause_reason"),
            "near_miss": latest.get("near_miss"),
        }
    except Exception as e:
        ctx["latest_scan_error"] = str(e)[:120]

    try:
        from core.signal_engine import predict_live_ex
        from core import prediction as _pred
        _scores: Dict[str, Any] = {}
        _sig, _reason = predict_live_ex("WOLF", "stock", scores=_scores)
        cfg = _pred._objective_effective_config()
        floor = float(_pred.CONFIDENCE_FLOOR)
        stats = _pred._objective_symbol_stats("WOLF", "UP")
        phase = "established" if int(stats.get("combined_total", 0)) >= int(cfg["min_samples"]) else "bootstrap"
        boot_conf = float(cfg.get("bootstrap_min_conf", floor))
        binding_conf = max(floor, boot_conf) if phase == "bootstrap" else floor
        up_prob = _scores.get("up_prob")
        mm = _scores.get("model_meta") or {}
        acc, min_p = mm.get("accuracy"), mm.get("min_win_proba")
        needed = None
        gap = None
        if acc is not None and min_p is not None:
            needed = max(min_p + (binding_conf - float(acc)) / 4.0, float(min_p))
            if up_prob is not None:
                gap = round(float(up_prob) - needed, 4)
        ctx["live_gate"] = {
            "model_emitted": _sig is not None,
            "reason": _reason,
            "up_prob": up_prob,
            "up_prob_needed_to_fire": round(needed, 4) if needed is not None else None,
            "up_prob_gap": gap,
            "regime": (_scores.get("regime") or {}).get("label"),
        }
    except Exception as e:
        ctx["live_gate_error"] = str(e)[:120]

    try:
        from core.portfolio_routes import build_portfolio_payload

        if include_portfolio:
            pf = build_portfolio_payload()
            if isinstance(pf, dict):
                ctx["portfolio"] = {
                    "positions": [
                        {
                            "symbol": p.get("symbol"),
                            "quantity": p.get("quantity"),
                            "buy_price": p.get("buy_price"),
                            "gain_loss_pct": p.get("gain_loss_pct"),
                            "gain_loss": p.get("gain_loss"),
                            "current_value": p.get("current_value"),
                        }
                        for p in (pf.get("positions") or [])[:20]
                    ],
                    "total_gain_loss": pf.get("total_gain_loss"),
                }
        else:
            ctx["portfolio"] = {"note": "portfolio context excluded (public endpoint)"}
    except Exception as e:
        ctx["portfolio_error"] = str(e)[:120]

    try:
        from core.news import get_symbol_sentiment
        ctx["wolf_news_sentiment"] = get_symbol_sentiment("WOLF")
    except Exception:
        ctx["wolf_news_sentiment"] = 0.0

    try:
        from config.symbols import OFFICIAL_WATCHLIST, watchlist_symbol_pairs
        from core.signal_engine import get_model_status

        scan_syms = [sym for sym, _atype in watchlist_symbol_pairs(include_portfolio=False)]
        model_st = get_model_status() or {}
        near = ((ctx.get("latest_scan") or {}).get("near_miss") or {})
        ctx["watchlist"] = {
            "official_count": len(OFFICIAL_WATCHLIST),
            "scan_count": len(scan_syms),
            "scan_symbols_sample": scan_syms[:8],
            "models_stored": len(model_st.get("stored_symbols") or {}),
            "models_serveable": len(model_st.get("symbols") or {}),
            "latest_near_miss_symbol": near.get("symbol"),
            "note": (
                "Scan loop uses STOCK_SYMBOLS (code watchlist). Portfolio is separate. "
                "Picks in predictions only appear after gates pass per symbol."
            ),
        }
    except Exception as e:
        ctx["watchlist_error"] = str(e)[:120]

    ctx["product_note"] = (
        "Official picks come from live model+gate results; portfolio tracks P&L and exit alerts. "
        "Use ghost_symbol_universe (MCP) or /api/admin/symbol-universe for the full layer map."
    )
    return ctx


def _check_daily_limit() -> Optional[str]:
    """Return error message if daily ask limit exceeded (CT date in ghost_state)."""
    try:
        import datetime
        import pytz
        from core.db import db_conn

        tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
        day = datetime.datetime.now(tz).strftime("%Y-%m-%d")
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='ghost_ask_day'")
            row = cur.fetchone()
            stored_day = row[0] if row else None
            cur.execute("SELECT val FROM ghost_state WHERE key='ghost_ask_count'")
            row2 = cur.fetchone()
            count = int(row2[0]) if row2 and row2[0] else 0
            if stored_day != day:
                count = 0
            if count >= ASK_DAILY_LIMIT:
                return f"Daily ask limit ({ASK_DAILY_LIMIT}) reached — try again tomorrow CT."
            count += 1
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('ghost_ask_day',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (day,))
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('ghost_ask_count',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (str(count),))
        return None
    except Exception as e:
        LOGGER.warning("ask limit check failed: %s", str(e)[:80])
        return None


def ask_ghost(
    question: str,
    *,
    history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Answer a user question using live Ghost context + Claude."""
    q = (question or "").strip()
    if not q:
        return {"ok": False, "error": "question required"}
    if len(q) > 2000:
        return {"ok": False, "error": "question too long (max 2000 chars)"}
    if not ANTHROPIC_KEY:
        return {"ok": False, "error": "ANTHROPIC_API_KEY not configured"}

    lim = _check_daily_limit()
    if lim:
        return {"ok": False, "error": lim}

    # Public endpoint: never include personal portfolio holdings.
    context = build_ask_context(include_portfolio=False)
    user_content = (
        "Ghost live context (JSON):\n"
        + json.dumps(context, default=str)[:12000]
        + "\n\nUser question:\n"
        + q
    )

    messages: List[Dict[str, str]] = []
    for h in (history or [])[-6:]:
        role = h.get("role")
        content = (h.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:2000]})
    messages.append({"role": "user", "content": user_content})

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ASK_MODEL,
                "max_tokens": ASK_MAX_TOKENS,
                "system": _system_prompt(),
                "messages": messages,
            },
            timeout=45,
        )
        if resp.status_code != 200:
            LOGGER.warning("Ghost Ask Claude %s: %s", resp.status_code, resp.text[:120])
            return {"ok": False, "error": f"Claude API error ({resp.status_code})"}
        data = resp.json()
        answer = ""
        for block in data.get("content") or []:
            if block.get("type") == "text":
                answer += block.get("text") or ""
        answer = answer.strip()
        if not answer:
            return {"ok": False, "error": "empty response from Claude"}
        return {
            "ok": True,
            "answer": answer,
            "model": ASK_MODEL,
            "context_summary": {
                "trade_action": (context.get("ghost_score") or {}).get("trade_action"),
                "engine_paused": (context.get("engine_pause") or {}).get("paused"),
                "portfolio_count": len((context.get("portfolio") or {}).get("positions") or []),
            },
        }
    except Exception as e:
        LOGGER.warning("Ghost Ask failed: %s", str(e)[:120])
        return {"ok": False, "error": str(e)[:200]}
