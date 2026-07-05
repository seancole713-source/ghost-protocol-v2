"""
core/war_room.py — Equity Research War Room: 6-agent chain powered by Claude.

PR #81: Runs the full Analyst → Valuation → Bull → Bear → Fact-Checker → Judge
pipeline for any symbol. Uses Ghost's existing Anthropic integration. Returns
structured JSON the cockpit can render.

Auth: requires GHOST_MCP_TOKEN or admin cookie (not public).
Rate limit: WAR_ROOM_DAILY_LIMIT per CT day (default 5).
"""
from __future__ import annotations
from core.quiet import note_suppressed

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests
from core.db import ensure_ghost_state

LOGGER = logging.getLogger("ghost.war_room")

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
WAR_ROOM_MODEL = os.getenv("WAR_ROOM_MODEL", "claude-sonnet-4-20250514")
WAR_ROOM_MAX_TOKENS = max(1024, min(8192, int(os.getenv("WAR_ROOM_MAX_TOKENS", "4096"))))
WAR_ROOM_DAILY_LIMIT = max(1, int(os.getenv("WAR_ROOM_DAILY_LIMIT", "5")))


_WAR_ROOM_SYSTEM_PROMPT = """You are running an equity-research War Room as a coordinated team of expert agents. This is a deep, multi-step investigation, not a hot take, and not financial advice — your job is to give the clearest possible picture so the investor can decide for themselves.

Run the team in this order and show each step. Be specific. Cite sources where you can. When you don't know a number, say so — don't guess.

═══════════════ 1. ANALYST — Business & Financials ═══════════════
Explain what the company actually makes money from. What are its revenue segments? Who are its customers? What's the competitive moat (if any)?

Then pull the latest numbers you can find:
- Revenue trend (last 4 quarters, year-over-year)
- Gross margin and operating margin trend
- Net income / EPS trend (profitable yet? when?)
- Total debt, cash on hand, net debt
- Free cash flow (burning cash or generating it?)
- ROE, ROCE, or any return metric available
- Any recent guidance changes, restructurings, or one-time items

Flag clearly which figures are from the most recent filing and which are estimates or stale. If the company has gone through a restructuring, re-listing, or M&A, explain what changed and whether the old financials are still comparable.

═══════════════ 2. VALUATION — Cheap or Expensive? ═══════════════
Pull current: P/E (trailing and forward), P/B, EV/EBITDA, Price to sales, Market cap and enterprise value.

Compare EACH metric against:
(a) The company's own 3-5 year history
(b) Its 3-5 closest competitors (name them, show their multiples)

Say plainly: is the market pricing in optimism or pessimism right now? If the company isn't profitable yet, explain how to think about the valuation. If the stock is down significantly from its highs, address the question directly: is this a value opportunity or a value trap?

═══════════════ 3. THE BULL — Strongest Honest Case to Buy ═══════════
Build the strongest case to be long at today's price. Steel-man it.
- What are the 2-3 growth drivers that could meaningfully expand the business?
- What catalysts are coming in the next 6-18 months?
- What does the market misunderstand or undervalue?
- What's the upside scenario in numbers?
- What's the bull-case price target and why?

Every claim must be specific enough that the Fact-Checker can verify it. No vague narratives — name the product, the customer, the contract, the factory, the TAM.

═══════════════ 4. THE BEAR — Strongest Honest Case to Avoid ═══════════
Build the strongest case to stay out or wait. Attack the Bull's weakest claims directly.
- What's the biggest risk that kills the bull case?
- Competition: who's taking share, who has better tech or pricing?
- Financial risk: dilution risk, debt covenants, cash runway?
- What does the market see that the Bull is missing?
- What's the bear-case price target and why?

Be ruthless. If the company has missed guidance, say so. If insiders are selling, say so.

═══════════════ 5. FACT-CHECKER — Do This Out Loud, Ruthlessly ═══════════
Go through every important number and claim both the Bull and Bear used. For each one, mark it:
  ✅ VERIFIED — confirmed from a recent filing, earnings call, or reliable source (cite it)
  ⚠️ UNVERIFIED — plausible but couldn't confirm; needs investor to check
  ❌ LIKELY WRONG — contradicts available data or is outdated

Correct or drop anything that's confident but unsupported. This step matters most.

═══════════════ 6. JUDGE — The Decision Dossier ═══════════════
After the fact-check, weigh what actually survived and deliver the dossier. Write for a smart non-expert — zero jargon, plain English.

THE READ: Bullish / Neutral / Cautious at today's price. How confident are you honestly (high / medium / low conviction)?

WHY: The 3-4 factors that actually decided it. Only include claims that survived the Fact-Checker.

VALUATION VERDICT: Cheap, Fair, or Expensive — in one line with the key multiple.

THE BIGGEST RISK: The single thing most likely to make this call wrong.

WHAT WOULD CHANGE THE VIEW: The specific event, number, or data point that would flip the call.

VERIFY BEFORE YOU ACT: The exact figures and claims the investor must confirm themselves.

POSITION THINKING (not advice): How someone might think about sizing and staging an entry to manage risk. What would make this a "wait" instead of a "now"? What's the thesis-break condition?

═══════════════ RULES ═══════════════
- Every number needs a source or a clear "I'm not sure" label.
- No jargon without explaining it.
- The Bull and Bear must actually clash — the Bear must name which Bull claim it's attacking.
- The Fact-Checker is the most important step.
- This is research, not advice. The investor makes the final call.
"""


def _check_daily_limit() -> Optional[str]:
    """Check WAR_ROOM_DAILY_LIMIT. Returns error string if limit hit, None if ok."""
    try:
        import datetime as _dt, pytz as _tz
        from core.db import db_conn
        tz = _tz.timezone("America/Chicago")
        today = _dt.datetime.now(tz).strftime("%Y-%m-%d")
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='war_room_daily_count'")
            row = cur.fetchone()
            if row:
                parts = (row[0] or "").split("|", 1)
                date_str = parts[0]
                count = int(parts[1]) if len(parts) > 1 else 0
                if date_str == today and count >= WAR_ROOM_DAILY_LIMIT:
                    return f"Daily limit reached ({WAR_ROOM_DAILY_LIMIT} requests). Resets at midnight CT."
                if date_str != today:
                    count = 0
            else:
                count = 0
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('war_room_daily_count',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (f"{today}|{count + 1}",),
            )
        return None
    except Exception as e:
        LOGGER.warning("war room limit check failed: %s", str(e)[:80])
        return None


def run_war_room(symbol: str) -> Dict[str, Any]:
    """Run the full 6-agent War Room analysis for a symbol."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "symbol required"}
    if not ANTHROPIC_KEY:
        return {"ok": False, "error": "ANTHROPIC_API_KEY not configured"}

    lim = _check_daily_limit()
    if lim:
        return {"ok": False, "error": lim}

    # Build context: live price + Ghost's own prediction state for this symbol
    context_parts = [f"Symbol: {sym}", f"Analysis Date: {time.strftime('%Y-%m-%d')}"]
    try:
        from core.prices import get_stock_price
        px = get_stock_price(sym)
        if px:
            context_parts.append(f"Current Price: ${px:.2f}")
    except Exception:
        note_suppressed()
    try:
        from core.prediction import predict_symbol
        pick = predict_symbol(sym, "stock", {})
        if pick:
            context_parts.append(f"Ghost Signal: {pick.get('direction')} @ {pick.get('confidence', 0):.1%} confidence")
        else:
            context_parts.append("Ghost Signal: No active signal (gates not met)")
    except Exception:
        note_suppressed()
    user_prompt = (
        "\n".join(context_parts)
        + f"\n\nRun the full 6-agent War Room analysis for {sym}. "
        + "Follow the system prompt exactly — Analyst → Valuation → Bull → Bear → Fact-Checker → Judge. "
        + "Be thorough. Every number needs a source or a clear 'I'm not sure' label."
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": WAR_ROOM_MODEL,
                "max_tokens": WAR_ROOM_MAX_TOKENS,
                "system": _WAR_ROOM_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=120,
        )
        if resp.status_code != 200:
            LOGGER.warning("War Room Claude %s: %s", resp.status_code, resp.text[:120])
            return {"ok": False, "error": f"Claude API error ({resp.status_code})"}

        data = resp.json()
        answer = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                answer += block.get("text", "")

        return {
            "ok": True,
            "symbol": sym,
            "analysis": answer,
            "model": WAR_ROOM_MODEL,
            "ts": int(time.time()),
        }
    except Exception as e:
        LOGGER.error("War Room failed: %s", str(e)[:200])
        return {"ok": False, "error": str(e)[:200]}
