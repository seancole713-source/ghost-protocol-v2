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

LOGGER = logging.getLogger("ghost.ask")

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ASK_MODEL = os.getenv("GHOST_ASK_MODEL", "claude-haiku-4-5-20251001")
ASK_MAX_TOKENS = max(256, min(2048, int(os.getenv("GHOST_ASK_MAX_TOKENS", "900"))))
ASK_DAILY_LIMIT = max(1, int(os.getenv("GHOST_ASK_DAILY_LIMIT", "40")))


def _system_prompt() -> str:
    return (
        "You are Ghost Protocol Ask — an assistant for the Ghost WOLF trading signal product.\n"
        "Rules:\n"
        "1. The JSON context is ground truth for Ghost engine state (cooldown, gates, picks, portfolio).\n"
        "2. Official WOLF trade signals ONLY come from context.picks and open picks with passed gates. "
        "Never invent SUPER BUY / BUY NOW / pick prices.\n"
        "3. If trade_action is NO TRADE or SILENCE, say clearly not to treat Ghost Score bias as a buy.\n"
        "4. Portfolio symbols (LULU, AMC, etc.) are tracked for P&L and exit alerts — Ghost does NOT "
        "issue picks on those unless context shows an open Ghost pick for that symbol.\n"
        "5. You may explain general market/earnings concepts when asked, but label them as general "
        "education, not Ghost signals.\n"
        "6. Not financial advice. Encourage 1% risk, stops, and following Ghost rules.\n"
        "7. Be concise (under 250 words unless user asks for detail). Use plain language.\n"
    )


def build_ask_context() -> Dict[str, Any]:
    """Snapshot live Ghost state for Claude (sync, no HTTP loopback)."""
    ctx: Dict[str, Any] = {"ts": int(time.time())}
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
        gs = ghost_score_payload_sync(use_cache=True)
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
        _V32_MIN = 223438
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
        from core.portfolio_routes import get_portfolio
        pf = get_portfolio()
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
    except Exception as e:
        ctx["portfolio_error"] = str(e)[:120]

    try:
        from core.news import get_symbol_sentiment
        ctx["wolf_news_sentiment"] = get_symbol_sentiment("WOLF")
    except Exception:
        ctx["wolf_news_sentiment"] = 0.0

    ctx["product_note"] = "WOLF-only official picks; portfolio = tracking and exit alerts."
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
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
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

    context = build_ask_context()
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
