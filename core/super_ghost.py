"""Super Ghost prediction-intelligence engine.

This module is the first production slice of the user's "Super Ghost" vision:
not an auto-trading bot, but a prediction-grade intelligence layer that grades a
stock using the 25-point checklist Sean described.

Design goals:
- Every prediction driver is explicit and accountable.
- Unknown data is marked unknown; it never quietly becomes bullish/bearish.
- Live fetching is best-effort and breaker-gated through existing Ghost wrappers.
- The pure ``snapshot`` path is deterministic and testable without network.
- The output is suitable for UI/API display and future truth-ledger scoring.

Scores are directional in [-2, +2]: negative = bearish, positive = bullish,
0 = neutral/unknown. Conviction is separate from direction.
"""
from __future__ import annotations

import math
import json
import logging
import os
import statistics
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


Category = str

LOGGER = logging.getLogger("ghost.super_ghost")

# The "AI brain already built and waiting" is Ghost's existing Anthropic
# integration (core/ghost_ask.py, core/war_room.py). Super Ghost reuses the same
# key + endpoint so the news-reading analyst layer is real AI, not templated text.
# Default to the SAME model Ghost Ask uses in production (claude-haiku-4-5):
# it is the proven-good model string for this account. (The War Room Sonnet
# string returns HTTP 404 for this key, so do not default to it here.)
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SUPER_GHOST_AI_MODEL = os.getenv(
    "SUPER_GHOST_AI_MODEL",
    os.getenv("GHOST_ASK_MODEL", "claude-haiku-4-5-20251001"),
)
SUPER_GHOST_AI_MAX_TOKENS = max(512, min(4096, int(os.getenv("SUPER_GHOST_AI_MAX_TOKENS", "1400"))))


@dataclass(frozen=True)
class CheckSpec:
    id: int
    category: Category
    key: str
    title: str
    question: str
    weight: float = 1.0
    critical: bool = False


CHECKLIST: Tuple[CheckSpec, ...] = (
    CheckSpec(1, "company_fundamentals_news", "eps", "EPS beat/miss", "Did the company beat or miss EPS estimates?", 1.1, True),
    CheckSpec(2, "company_fundamentals_news", "revenue_growth", "Revenue growth", "Is top-line revenue growing year-over-year?", 1.2, True),
    CheckSpec(3, "company_fundamentals_news", "guidance", "2Q / 3Q guidance", "Is forward guidance improving or deteriorating?", 1.1, True),
    CheckSpec(4, "company_fundamentals_news", "news_catalysts", "Press releases / catalysts", "Any product, FDA, contract, launch, lawsuit, recall, or major news catalyst?", 1.1, True),
    CheckSpec(5, "company_fundamentals_news", "insider_trading", "Insider trading", "Are executives buying or selling stock?", 0.8),
    CheckSpec(6, "company_fundamentals_news", "institutional_ownership", "Institutional ownership", "Are institutions increasing or decreasing ownership?", 0.8),
    CheckSpec(7, "company_fundamentals_news", "analyst_ratings", "Analyst ratings", "Are analysts leaning buy/hold/sell and where are targets?", 0.9),
    CheckSpec(8, "price_action_performance", "perf_30d", "Last 30-day performance", "Is the stock trending up or down over the last month?", 1.0, True),
    CheckSpec(9, "price_action_performance", "range_52w", "52-week high/low", "Is price near the yearly high or low?", 0.8),
    CheckSpec(10, "price_action_performance", "avg_volume", "Average daily volume", "Does the stock have enough liquidity?", 0.7),
    CheckSpec(11, "price_action_performance", "rvol", "Relative volume", "Is today's volume significantly above average?", 0.9),
    CheckSpec(12, "price_action_performance", "relative_strength", "Relative strength", "Is it outperforming SPY / the market?", 1.0, True),
    CheckSpec(13, "price_action_performance", "moving_averages", "Moving averages", "How is price interacting with 20/50/200 EMA?", 1.1, True),
    CheckSpec(14, "price_action_performance", "support_resistance", "Support & resistance", "Where are the nearby floors and ceilings?", 0.9),
    CheckSpec(15, "market_context_indicators", "spx", "S&P 500 direction", "What is the broad-market direction?", 0.8, True),
    CheckSpec(16, "market_context_indicators", "nasdaq", "Nasdaq direction", "How are tech/growth stocks performing?", 0.8, True),
    CheckSpec(17, "market_context_indicators", "sector", "Sector performance", "Is the relevant sector leading or lagging?", 0.9, True),
    CheckSpec(18, "market_context_indicators", "vix", "VIX / fear gauge", "Is market fear high or low?", 0.8, True),
    CheckSpec(19, "market_context_indicators", "fed_cpi", "Fed / CPI context", "Are rates/inflation headlines supportive or restrictive?", 0.8),
    CheckSpec(20, "risk_management_planning", "risk_reward", "Risk-to-reward", "Is upside at least 2:1 versus stop distance?", 1.2, True),
    CheckSpec(21, "risk_management_planning", "stop_loss", "Stop-loss level", "Where is the invalidation / stop level?", 1.0, True),
    CheckSpec(22, "risk_management_planning", "target_price", "Target price", "Where is the initial take-profit target?", 1.0, True),
    CheckSpec(23, "risk_management_planning", "position_sizing", "Position sizing", "Does size respect 1-2% account risk?", 0.8),
    CheckSpec(24, "risk_management_planning", "market_correlation", "Market correlation / exposure", "Is the idea overexposed to one sector or correlated theme?", 0.7),
    CheckSpec(25, "risk_management_planning", "daily_loss_limit", "Daily loss limit", "Is the daily loss lock clear?", 1.1, True),
)

CHECKLIST_BY_ID = {c.id: c for c in CHECKLIST}
CHECKLIST_BY_KEY = {c.key: c for c in CHECKLIST}

_BULL_WORDS = (
    "beat", "beats", "raise", "raised", "raises", "upgrade", "upgraded", "outperform",
    "contract", "award", "approval", "approved", "launch", "partnership", "record",
    "strong", "growth", "profit", "surge", "rally", "breakout", "guidance raised",
)
_BEAR_WORDS = (
    "miss", "misses", "cut", "cuts", "lower", "lowered", "downgrade", "downgraded", "sell",
    "lawsuit", "investigation", "recall", "delay", "bankruptcy", "weak", "loss",
    "decline", "warning", "guidance cut", "ceo resign", "cfo resign", "resigns",
)
_GUIDANCE_BULL = ("raise guidance", "raised guidance", "raises guidance", "above guidance", "strong outlook", "upbeat outlook", "increase forecast")
_GUIDANCE_BEAR = ("cut guidance", "lower guidance", "lowered guidance", "weak outlook", "below guidance", "reduce forecast", "withdraw guidance")
_CATALYST_BULL = ("fda approval", "approval", "contract", "partnership", "launch", "new product", "award", "buyout", "acquisition", "patent")
_CATALYST_BEAR = ("lawsuit", "sec investigation", "investigation", "recall", "delay", "halt", "bankruptcy", "delisting", "downgrade")
_FED_BULL = ("rate cut", "cuts rates", "dovish", "cooling inflation", "cpi cools", "soft landing")
_FED_BEAR = ("rate hike", "higher for longer", "hawkish", "hot cpi", "inflation accelerates", "sticky inflation")

_SECTOR_ETF_BY_SECTOR = {
    "technology": "XLK",
    "communication services": "XLC",
    "consumer cyclical": "XLY",
    "consumer defensive": "XLP",
    "financial services": "XLF",
    "financial": "XLF",
    "healthcare": "XLV",
    "industrials": "XLI",
    "energy": "XLE",
    "basic materials": "XLB",
    "real estate": "XLRE",
    "utilities": "XLU",
}


def _now() -> int:
    return int(time.time())


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        out = float(v)
        if math.isfinite(out):
            return out
    except Exception:
        pass
    return None


def _i(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(float(v))
    except Exception:
        return None


def _pct(v: Optional[float]) -> Optional[float]:
    return round(float(v) * 100.0, 2) if v is not None else None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _safe_round(v: Any, nd: int = 3) -> Optional[float]:
    fv = _f(v)
    return round(fv, nd) if fv is not None else None


def _return_pct(start: Optional[float], end: Optional[float]) -> Optional[float]:
    s = _f(start)
    e = _f(end)
    if s is None or e is None or s <= 0:
        return None
    return (e - s) / s


def _history_points(snapshot: Dict[str, Any], key: str = "history") -> List[Dict[str, Any]]:
    rows = snapshot.get(key) or []
    if isinstance(rows, dict):
        rows = rows.get("points") or rows.get("rows") or []
    out = []
    for r in rows or []:
        if isinstance(r, dict):
            close = _f(r.get("close") or r.get("Close") or r.get("price"))
            vol = _i(r.get("volume") or r.get("Volume"))
            high = _f(r.get("high") or r.get("High"))
            low = _f(r.get("low") or r.get("Low"))
            ts = r.get("ts") or r.get("date") or r.get("Date")
            if close is not None:
                out.append({"ts": ts, "close": close, "volume": vol, "high": high, "low": low})
    return out


def _closes(rows: List[Dict[str, Any]]) -> List[float]:
    return [float(r["close"]) for r in rows if _f(r.get("close")) is not None]


def _volumes(rows: List[Dict[str, Any]]) -> List[int]:
    return [int(r["volume"]) for r in rows if _i(r.get("volume")) is not None]


def _ema(values: List[float], span: int) -> Optional[float]:
    if not values:
        return None
    if len(values) < max(2, min(span, 5)):
        return None
    alpha = 2.0 / (span + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * float(v) + (1 - alpha) * ema
    return ema


def _ret_from_rows(rows: List[Dict[str, Any]], lookback: int = 30) -> Optional[float]:
    vals = _closes(rows)
    if len(vals) < 2:
        return None
    if len(vals) > lookback:
        return _return_pct(vals[-lookback - 1], vals[-1])
    return _return_pct(vals[0], vals[-1])


def _headline_blob(articles: Iterable[Dict[str, Any]]) -> str:
    parts = []
    for a in articles or []:
        parts.append(str(a.get("title") or a.get("headline") or ""))
        parts.append(str(a.get("summary") or a.get("description") or ""))
    return " ".join(parts).lower()


def _keyword_score(text: str, bullish: Iterable[str], bearish: Iterable[str]) -> Tuple[float, List[str], List[str]]:
    t = (text or "").lower()
    bull_hits = [w for w in bullish if w in t]
    bear_hits = [w for w in bearish if w in t]
    if not bull_hits and not bear_hits:
        return 0.0, [], []
    raw = (len(bull_hits) - len(bear_hits)) / max(len(bull_hits) + len(bear_hits), 1)
    return round(raw, 3), bull_hits[:6], bear_hits[:6]


def _status(score: Optional[float], available: bool) -> str:
    if not available or score is None:
        return "unknown"
    if score >= 1.25:
        return "strong_bullish"
    if score > 0.25:
        return "bullish"
    if score <= -1.25:
        return "strong_bearish"
    if score < -0.25:
        return "bearish"
    return "neutral"


def _item(spec: CheckSpec, *, score: Optional[float], value: Any = None, evidence: str = "", source: str = "", available: Optional[bool] = None, confidence: Optional[float] = None, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    av = bool(available) if available is not None else score is not None
    sc = None if score is None else round(_clamp(float(score), -2.0, 2.0), 3)
    conf = confidence
    if conf is None:
        conf = 0.75 if av else 0.0
    return {
        "id": spec.id,
        "key": spec.key,
        "category": spec.category,
        "title": spec.title,
        "question": spec.question,
        "available": av,
        "status": _status(sc, av),
        "score": sc,
        "weight": spec.weight,
        "critical": spec.critical,
        "confidence": round(_clamp(float(conf), 0.0, 1.0), 3),
        "value": value,
        "evidence": evidence,
        "source": source or "computed",
        "data": data or {},
    }


def _unknown(spec: CheckSpec, reason: str, source: str = "unavailable") -> Dict[str, Any]:
    return _item(spec, score=None, evidence=reason, source=source, available=False, confidence=0.0)


def _add_item(items: Dict[str, Dict[str, Any]], key: str, **kwargs: Any) -> None:
    spec = CHECKLIST_BY_KEY[key]
    items[key] = _item(spec, **kwargs)


def _add_unknown(items: Dict[str, Dict[str, Any]], key: str, reason: str, source: str = "unavailable") -> None:
    items[key] = _unknown(CHECKLIST_BY_KEY[key], reason, source)


def _extract_news(symbol: str, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    articles = snapshot.get("news") or snapshot.get("articles") or []
    sym = (symbol or "").upper()
    out = []
    for a in articles or []:
        if not isinstance(a, dict):
            continue
        syms = [str(s).upper() for s in (a.get("symbols") or [])]
        if a.get("symbol"):
            syms.append(str(a.get("symbol")).upper())
        text = (str(a.get("title") or a.get("headline") or "") + " " + str(a.get("summary") or "")).upper()
        if sym and syms and sym not in syms:
            continue
        if sym and not syms and sym not in text and symbol.upper() not in text:
            # Keep market/fed macro news even if not symbol-tagged.
            cat = str(a.get("category") or "").lower()
            if cat not in ("market", "macro", "policy", "economic"):
                continue
        out.append(dict(a))
    return out[:50]


def _latest_earnings(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    e = snapshot.get("earnings") or snapshot.get("fundamentals") or {}
    if isinstance(e, dict) and ("quarters" in e):
        qs = [q for q in (e.get("quarters") or []) if isinstance(q, dict)]
        if qs:
            latest = dict(qs[-1])
            latest["quarters"] = qs
            return latest
    return e if isinstance(e, dict) else {}


def _evaluate_company(symbol: str, snapshot: Dict[str, Any], items: Dict[str, Dict[str, Any]]) -> None:
    earnings = _latest_earnings(snapshot)
    articles = _extract_news(symbol, snapshot)
    blob = _headline_blob(articles)

    # 1 EPS beat/miss
    act = _f(earnings.get("actual_eps") or earnings.get("eps_actual"))
    est = _f(earnings.get("estimate_eps") or earnings.get("eps_estimate"))
    if act is not None and est is not None:
        diff = act - est
        rel = diff / max(abs(est), 0.01)
        sc = 1.5 if diff > 0 and rel >= 0.05 else (0.7 if diff > 0 else (-1.5 if rel <= -0.05 else -0.7 if diff < 0 else 0.0))
        _add_item(items, "eps", score=sc, value={"actual_eps": act, "estimate_eps": est, "surprise": round(diff, 4)}, evidence=("EPS beat estimate" if diff > 0 else "EPS missed estimate" if diff < 0 else "EPS matched estimate"), source="earnings")
    else:
        _add_unknown(items, "eps", "EPS actual/estimate unavailable; do not count this as bullish.", "earnings")

    # 2 revenue growth YoY
    rev_growth = _f(earnings.get("revenue_yoy") or earnings.get("revenue_growth_yoy"))
    revenue = _f(earnings.get("revenue") or earnings.get("total_revenue"))
    prior = _f(earnings.get("revenue_year_ago") or earnings.get("prior_year_revenue"))
    if rev_growth is None and revenue is not None and prior and prior > 0:
        rev_growth = (revenue - prior) / prior
    if rev_growth is not None:
        sc = 1.6 if rev_growth >= 0.15 else (0.8 if rev_growth > 0.03 else (0.0 if rev_growth >= -0.03 else -1.2))
        _add_item(items, "revenue_growth", score=sc, value={"revenue_yoy_pct": _pct(rev_growth), "revenue": revenue}, evidence=f"Revenue YoY growth {rev_growth*100:+.1f}%", source="earnings")
    else:
        _add_unknown(items, "revenue_growth", "YoY revenue growth unavailable.", "earnings")

    # 3 guidance
    guidance = str(earnings.get("guidance") or snapshot.get("guidance") or "")
    guidance_blob = (guidance + " " + blob).lower()
    gscore, ghit, bhit = _keyword_score(guidance_blob, _GUIDANCE_BULL, _GUIDANCE_BEAR)
    if guidance or ghit or bhit:
        _add_item(items, "guidance", score=gscore * 1.5, value={"bullish_terms": ghit, "bearish_terms": bhit}, evidence=("Guidance/news terms: +" + ",".join(ghit) + " -" + ",".join(bhit)).strip(), source="guidance/news", confidence=0.65)
    else:
        _add_unknown(items, "guidance", "No explicit forward-guidance signal found in earnings/news snapshot.", "guidance/news")

    # 4 press/news catalysts
    cscore, bull_hits, bear_hits = _keyword_score(blob, _CATALYST_BULL + _BULL_WORDS, _CATALYST_BEAR + _BEAR_WORDS)
    sentiments = [_f(a.get("sentiment") or a.get("sentiment_score")) for a in articles]
    sentiments = [s for s in sentiments if s is not None]
    if sentiments:
        avg_sent = sum(sentiments) / len(sentiments)
        cscore = (cscore + avg_sent) / 2.0
    if articles:
        top_titles = [str(a.get("title") or a.get("headline") or "")[:120] for a in articles[:5]]
        _add_item(items, "news_catalysts", score=cscore * 1.7, value={"articles_scanned": len(articles), "bullish_terms": bull_hits, "bearish_terms": bear_hits, "avg_sentiment": _safe_round(sum(sentiments) / len(sentiments), 3) if sentiments else None}, evidence="; ".join([t for t in top_titles if t]) or "News scanned", source="news")
    else:
        _add_unknown(items, "news_catalysts", "No recent symbol/news articles available.", "news")

    # 5 insider trading
    insider = snapshot.get("insider_trading") or snapshot.get("insiders") or {}
    net = _f(insider.get("net_shares") or insider.get("net_shares_90d")) if isinstance(insider, dict) else None
    buys = _i(insider.get("buys") or insider.get("buy_count")) if isinstance(insider, dict) else None
    sells = _i(insider.get("sells") or insider.get("sell_count")) if isinstance(insider, dict) else None
    if net is not None or buys is not None or sells is not None:
        if net is not None:
            sc = 1.0 if net > 0 else (-1.0 if net < 0 else 0.0)
        else:
            sc = 0.8 if (buys or 0) > (sells or 0) else (-0.8 if (sells or 0) > (buys or 0) else 0.0)
        _add_item(items, "insider_trading", score=sc, value={"net_shares": net, "buys": buys, "sells": sells}, evidence="Insider buying supports; selling pressures" if sc else "Insider activity neutral", source="insider")
    else:
        _add_unknown(items, "insider_trading", "Insider transaction feed unavailable.", "insider")

    # 6 institutional ownership
    inst = snapshot.get("institutional_ownership") or snapshot.get("institutional") or {}
    change = _f(inst.get("recent_change_pct") or inst.get("change_pct")) if isinstance(inst, dict) else None
    pct = _f(inst.get("institutional_pct") or inst.get("held_pct")) if isinstance(inst, dict) else None
    if change is not None or pct is not None:
        sc = 1.0 if (change or 0) > 2 else (-1.0 if (change or 0) < -2 else (0.25 if pct and pct >= 50 else 0.0))
        _add_item(items, "institutional_ownership", score=sc, value={"institutional_pct": pct, "recent_change_pct": change}, evidence="Institutional ownership/change evaluated", source="institutional")
    else:
        _add_unknown(items, "institutional_ownership", "Institutional holder/change feed unavailable.", "institutional")

    # 7 analyst ratings
    analyst = snapshot.get("analysts") or snapshot.get("analyst") or {}
    if isinstance(analyst, dict) and analyst:
        recs = analyst.get("recommendations") or {}
        buy = _i(recs.get("strong_buy")) or 0
        buy += _i(recs.get("buy")) or 0
        hold = _i(recs.get("hold")) or 0
        sell = (_i(recs.get("sell")) or 0) + (_i(recs.get("underperform")) or 0)
        target = _f(analyst.get("price_target_avg") or analyst.get("target_mean"))
        current = _f(analyst.get("current_price") or snapshot.get("current_price"))
        upside = _return_pct(current, target) if current and target else None
        sc = 0.0
        if buy + hold + sell > 0:
            sc += (buy - sell) / max(buy + hold + sell, 1)
        if upside is not None:
            sc += _clamp(upside * 4.0, -1.0, 1.0)
        _add_item(items, "analyst_ratings", score=_clamp(sc, -1.5, 1.5), value={"buy": buy, "hold": hold, "sell": sell, "target_upside_pct": _pct(upside)}, evidence="Analyst mix and target upside evaluated", source="analyst")
    else:
        _add_unknown(items, "analyst_ratings", "Analyst rating/target feed unavailable.", "analyst")


def _evaluate_price_action(symbol: str, snapshot: Dict[str, Any], items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    rows = _history_points(snapshot)
    vals = _closes(rows)
    vols = _volumes(rows)
    current = _f(snapshot.get("current_price")) or (vals[-1] if vals else None)
    if current is None:
        current = _f(snapshot.get("price"))
    out: Dict[str, Any] = {"current_price": current, "history_points": len(vals)}

    # 8 30d performance
    perf30 = _f(snapshot.get("perf_30d"))
    if perf30 is None:
        perf30 = _ret_from_rows(rows, 30)
    if perf30 is not None:
        sc = 1.4 if perf30 >= 0.10 else (0.7 if perf30 > 0.02 else (-1.2 if perf30 <= -0.10 else -0.4 if perf30 < -0.02 else 0.0))
        _add_item(items, "perf_30d", score=sc, value={"return_30d_pct": _pct(perf30)}, evidence=f"30-day return {perf30*100:+.1f}%", source="price_history")
    else:
        _add_unknown(items, "perf_30d", "Not enough price history for 30-day performance.", "price_history")

    # 9 52-week high/low
    high52 = _f(snapshot.get("week52_high") or snapshot.get("52w_high"))
    low52 = _f(snapshot.get("week52_low") or snapshot.get("52w_low"))
    if (high52 is None or low52 is None) and vals:
        high52 = max(vals)
        low52 = min(vals)
    if current and high52 and low52 and high52 > low52:
        pos = (current - low52) / (high52 - low52)
        sc = 0.8 if pos >= 0.75 else (-0.8 if pos <= 0.25 else 0.0)
        _add_item(items, "range_52w", score=sc, value={"position_in_52w_range": round(pos, 3), "week52_low": low52, "week52_high": high52}, evidence=f"Price is {pos*100:.0f}% through the 52-week range", source="price_history")
    else:
        _add_unknown(items, "range_52w", "52-week range unavailable.", "price_history")

    # 10 avg daily volume
    avg_vol = _i(snapshot.get("avg_volume") or snapshot.get("average_volume"))
    if avg_vol is None and vols:
        avg_vol = int(statistics.mean(vols[-30:]))
    if avg_vol is not None:
        sc = 0.7 if avg_vol >= 500_000 else (-0.8 if avg_vol < 100_000 else 0.0)
        _add_item(items, "avg_volume", score=sc, value={"avg_volume": avg_vol}, evidence=("Liquid enough for typical retail sizing" if avg_vol >= 500_000 else "Thin liquidity can distort predictions"), source="volume")
    else:
        _add_unknown(items, "avg_volume", "Average volume unavailable.", "volume")

    # 11 RVOL
    cur_vol = _i(snapshot.get("volume") or snapshot.get("current_volume"))
    if cur_vol is None and vols:
        cur_vol = vols[-1]
    rvol = _f(snapshot.get("rvol") or snapshot.get("volume_ratio"))
    if rvol is None and cur_vol and avg_vol and avg_vol > 0:
        rvol = cur_vol / avg_vol
    if rvol is not None:
        # RVOL is direction-neutral: it amplifies whatever the price/news signal says. Positive if price trend is up, negative if down.
        trend_sign = 1 if (perf30 or 0) >= 0 else -1
        sc = trend_sign * (1.2 if rvol >= 2 else 0.6 if rvol >= 1.2 else 0.0)
        _add_item(items, "rvol", score=sc, value={"rvol": round(rvol, 2), "volume": cur_vol, "avg_volume": avg_vol}, evidence=f"Relative volume {rvol:.2f}x", source="volume")
    else:
        _add_unknown(items, "rvol", "Relative volume unavailable.", "volume")

    # 12 relative strength vs SPY
    spy_rows = _history_points(snapshot, "spy_history") or _history_points(snapshot, "market_history")
    spy30 = _ret_from_rows(spy_rows, 30)
    rs = None if perf30 is None or spy30 is None else perf30 - spy30
    if rs is not None:
        sc = 1.2 if rs >= 0.05 else (0.5 if rs > 0 else (-1.2 if rs <= -0.05 else -0.4))
        _add_item(items, "relative_strength", score=sc, value={"stock_30d_pct": _pct(perf30), "spy_30d_pct": _pct(spy30), "relative_strength_pct": _pct(rs)}, evidence=f"30d relative strength vs SPY {rs*100:+.1f}%", source="price_history")
    else:
        _add_unknown(items, "relative_strength", "SPY/stock relative-strength history unavailable.", "price_history")

    # 13 moving averages
    ema20 = _ema(vals, 20)
    ema50 = _ema(vals, 50)
    ema200 = _ema(vals, 200)
    out.update({"ema20": _safe_round(ema20, 4), "ema50": _safe_round(ema50, 4), "ema200": _safe_round(ema200, 4)})
    if current and ema20 and ema50:
        sc = 0.0
        if current > ema20:
            sc += 0.5
        else:
            sc -= 0.5
        if ema20 > ema50:
            sc += 0.4
        else:
            sc -= 0.4
        if ema200:
            sc += 0.7 if current > ema200 else -0.7
            if ema20 > ema50 > ema200:
                sc += 0.4
            elif ema20 < ema50 < ema200:
                sc -= 0.4
        _add_item(items, "moving_averages", score=sc, value={"current": current, "ema20": _safe_round(ema20, 4), "ema50": _safe_round(ema50, 4), "ema200": _safe_round(ema200, 4)}, evidence="EMA stack evaluated", source="price_history")
    else:
        _add_unknown(items, "moving_averages", "Not enough history for EMA20/50 evaluation.", "price_history")

    # 14 support / resistance
    lows = [r["low"] if r.get("low") is not None else r["close"] for r in rows[-60:] if r.get("close") is not None]
    highs = [r["high"] if r.get("high") is not None else r["close"] for r in rows[-60:] if r.get("close") is not None]
    support = max([x for x in lows if current and x < current], default=None) if lows else None
    resistance = min([x for x in highs if current and x > current], default=None) if highs else None
    if support is None and lows:
        support = min(lows)
    if resistance is None and highs:
        resistance = max(highs)
    out.update({"support": _safe_round(support, 4), "resistance": _safe_round(resistance, 4)})
    if current and support and resistance and current > 0:
        down = abs(current - support) / current
        up = abs(resistance - current) / current
        rr = up / max(down, 0.001)
        sc = 0.9 if rr >= 2 else (0.2 if rr >= 1 else -0.6)
        _add_item(items, "support_resistance", score=sc, value={"support": round(support, 4), "resistance": round(resistance, 4), "upside_to_resistance_pct": _pct(up), "downside_to_support_pct": _pct(down)}, evidence=f"Nearest support/resistance implies R:R ~{rr:.2f}:1", source="price_history")
    else:
        _add_unknown(items, "support_resistance", "Support/resistance cannot be computed without enough price data.", "price_history")
    return out


def _evaluate_market(snapshot: Dict[str, Any], items: Dict[str, Dict[str, Any]]) -> None:
    # 15 SPX / SPY direction
    spx_ret = _f(snapshot.get("spx_20d") or snapshot.get("spy_20d"))
    if spx_ret is None:
        spx_ret = _ret_from_rows(_history_points(snapshot, "spx_history") or _history_points(snapshot, "spy_history"), 20)
    if spx_ret is not None:
        sc = 0.9 if spx_ret > 0.03 else (-0.9 if spx_ret < -0.03 else 0.0)
        _add_item(items, "spx", score=sc, value={"spx_20d_pct": _pct(spx_ret)}, evidence=f"Broad market 20d {spx_ret*100:+.1f}%", source="market")
    else:
        _add_unknown(items, "spx", "SPX/SPY market direction unavailable.", "market")

    # 16 Nasdaq / QQQ direction
    ndx_ret = _f(snapshot.get("ixic_20d") or snapshot.get("nasdaq_20d") or snapshot.get("qqq_20d"))
    if ndx_ret is None:
        ndx_ret = _ret_from_rows(_history_points(snapshot, "ixic_history") or _history_points(snapshot, "qqq_history"), 20)
    if ndx_ret is not None:
        sc = 0.9 if ndx_ret > 0.03 else (-0.9 if ndx_ret < -0.03 else 0.0)
        _add_item(items, "nasdaq", score=sc, value={"nasdaq_20d_pct": _pct(ndx_ret)}, evidence=f"Nasdaq/growth 20d {ndx_ret*100:+.1f}%", source="market")
    else:
        _add_unknown(items, "nasdaq", "Nasdaq/QQQ direction unavailable.", "market")

    # 17 sector performance
    sector_ret = _f(snapshot.get("sector_20d") or snapshot.get("sector_return_20d"))
    if sector_ret is None:
        sector_ret = _ret_from_rows(_history_points(snapshot, "sector_history"), 20)
    if sector_ret is not None:
        sc = 1.0 if sector_ret > 0.04 else (-1.0 if sector_ret < -0.04 else 0.0)
        _add_item(items, "sector", score=sc, value={"sector": snapshot.get("sector"), "sector_etf": snapshot.get("sector_etf"), "sector_20d_pct": _pct(sector_ret)}, evidence=f"Sector 20d {sector_ret*100:+.1f}%", source="market")
    else:
        _add_unknown(items, "sector", "Sector performance unavailable.", "market")

    # 18 VIX
    vix = _f(snapshot.get("vix"))
    if vix is None:
        vix_rows = _history_points(snapshot, "vix_history")
        vals = _closes(vix_rows)
        vix = vals[-1] if vals else None
    if vix is not None:
        sc = 0.8 if vix < 15 else (0.2 if vix < 20 else (-0.8 if vix < 30 else -1.4))
        _add_item(items, "vix", score=sc, value={"vix": round(vix, 2)}, evidence=("Low fear" if vix < 15 else "Elevated fear" if vix >= 20 else "Normal volatility"), source="market")
    else:
        _add_unknown(items, "vix", "VIX unavailable.", "market")

    # 19 Fed/CPI
    articles = snapshot.get("macro_news") or snapshot.get("news") or []
    blob = _headline_blob(articles)
    fscore, bh, sh = _keyword_score(blob, _FED_BULL, _FED_BEAR)
    fed_rate = _f(snapshot.get("fed_rate") or snapshot.get("macro_fed_rate"))
    cpi = _f(snapshot.get("cpi_yoy"))
    if bh or sh or fed_rate is not None or cpi is not None:
        if fed_rate is not None and fed_rate > 5.0:
            fscore -= 0.25
        if cpi is not None and cpi > 3.5:
            fscore -= 0.25
        _add_item(items, "fed_cpi", score=_clamp(fscore * 1.2, -1.4, 1.4), value={"fed_rate": fed_rate, "cpi_yoy": cpi, "bullish_terms": bh, "bearish_terms": sh}, evidence="Fed/CPI macro context evaluated", source="macro")
    else:
        _add_unknown(items, "fed_cpi", "No Fed/CPI data or macro-news signal available.", "macro")


def _evaluate_risk(snapshot: Dict[str, Any], items: Dict[str, Dict[str, Any]], price_ctx: Dict[str, Any]) -> Dict[str, Any]:
    current = _f(price_ctx.get("current_price")) or _f(snapshot.get("current_price"))
    support = _f(snapshot.get("stop_loss") or price_ctx.get("support"))
    resistance = _f(snapshot.get("target_price") or price_ctx.get("resistance"))

    # Prefer stop below support and target at resistance. If only current exists, use conservative defaults.
    stop = _f(snapshot.get("stop_loss"))
    target = _f(snapshot.get("target_price"))
    if current:
        if stop is None:
            stop = support if support and support < current else current * 0.95
        if target is None:
            target = resistance if resistance and resistance > current else current * 1.10
    rr = None
    if current and stop and target and current > 0 and stop < current and target > current:
        risk = current - stop
        reward = target - current
        rr = reward / max(risk, 0.0001)

    # 20 risk-to-reward
    if rr is not None:
        sc = 1.4 if rr >= 3 else (0.8 if rr >= 2 else (-1.0 if rr < 1 else -0.2))
        _add_item(items, "risk_reward", score=sc, value={"risk_reward_ratio": round(rr, 2)}, evidence=f"Reward/risk {rr:.2f}:1", source="risk")
    else:
        _add_unknown(items, "risk_reward", "Cannot compute risk/reward without current, stop, and target.", "risk")

    # 21 stop-loss
    if current and stop and stop < current:
        stop_pct = (current - stop) / current
        sc = 0.7 if 0.015 <= stop_pct <= 0.12 else (-0.5 if stop_pct > 0.20 else 0.0)
        _add_item(items, "stop_loss", score=sc, value={"stop_loss": round(stop, 4), "stop_distance_pct": _pct(stop_pct)}, evidence="Stop below current price defines invalidation", source="risk")
    else:
        _add_unknown(items, "stop_loss", "No valid stop-loss below current price.", "risk")

    # 22 target price
    if current and target and target > current:
        upside = (target - current) / current
        sc = 0.8 if upside >= 0.05 else 0.2
        _add_item(items, "target_price", score=sc, value={"target_price": round(target, 4), "target_upside_pct": _pct(upside)}, evidence="Target above current price defined", source="risk")
    else:
        _add_unknown(items, "target_price", "No valid target price above current price.", "risk")

    # 23 position sizing
    risk_snapshot = snapshot.get("risk") or {}
    sizing = None
    if current and stop and stop < current:
        try:
            from core.risk_discipline import position_sizing_plan
            sizing = position_sizing_plan(current, stop, confidence=0.75)
        except Exception:
            sizing = None
    if isinstance(sizing, dict) and sizing.get("ok"):
        rp = _f(sizing.get("risk_pct_per_trade"))
        sc = 0.8 if rp is not None and rp <= 2.0 else -0.8
        _add_item(items, "position_sizing", score=sc, value=sizing, evidence="Size is based on fixed account-risk rules", source="risk")
    elif risk_snapshot:
        rp = _f(risk_snapshot.get("risk_pct_per_trade"))
        sc = 0.8 if rp is not None and rp <= 2.0 else (-0.8 if rp and rp > 2 else 0.0)
        _add_item(items, "position_sizing", score=sc, value=risk_snapshot, evidence="Risk settings evaluated", source="risk")
    else:
        _add_unknown(items, "position_sizing", "Position sizing unavailable.", "risk")

    # 24 market correlation / overexposure
    corr = _f(snapshot.get("market_correlation") or snapshot.get("sector_correlation"))
    exposure = _f((risk_snapshot or {}).get("sector_exposure_pct") or snapshot.get("sector_exposure_pct"))
    open_positions = _i((risk_snapshot or {}).get("open_positions") or snapshot.get("open_positions"))
    if corr is not None or exposure is not None or open_positions is not None:
        sc = 0.5
        if corr is not None and abs(corr) > 0.85:
            sc -= 0.5
        if exposure is not None and exposure > 35:
            sc -= 0.8
        if open_positions is not None and open_positions >= 5:
            sc -= 0.4
        _add_item(items, "market_correlation", score=sc, value={"correlation": corr, "sector_exposure_pct": exposure, "open_positions": open_positions}, evidence="Correlation/portfolio exposure evaluated", source="risk")
    else:
        _add_unknown(items, "market_correlation", "Portfolio/correlation exposure unavailable.", "risk")

    # 25 daily loss limit
    daily = snapshot.get("daily_loss_lock") or (risk_snapshot.get("daily_loss_lock") if isinstance(risk_snapshot, dict) else None)
    if not daily:
        try:
            from core.risk_discipline import daily_loss_lock_state
            daily = daily_loss_lock_state()
        except Exception:
            daily = None
    if isinstance(daily, dict):
        locked = bool(daily.get("locked") or daily.get("should_lock"))
        sc = -2.0 if locked else 1.0
        _add_item(items, "daily_loss_limit", score=sc, value=daily, evidence=("Daily loss lock engaged" if locked else "Daily loss lock clear"), source="risk")
    else:
        _add_unknown(items, "daily_loss_limit", "Daily loss lock state unavailable.", "risk")
    return {"entry": current, "stop_loss": stop, "target_price": target, "risk_reward_ratio": rr}


def _aggregate(symbol: str, items_by_key: Dict[str, Dict[str, Any]], risk_plan: Dict[str, Any]) -> Dict[str, Any]:
    items = [items_by_key[c.key] for c in CHECKLIST]
    available = [x for x in items if x.get("available") and x.get("score") is not None]
    total_weight = sum(float(x.get("weight") or 1.0) for x in available) or 1.0
    weighted = sum(float(x.get("score") or 0.0) * float(x.get("weight") or 1.0) for x in available)
    max_abs = sum(2.0 * float(x.get("weight") or 1.0) for x in available) or 1.0
    edge = _clamp(weighted / max_abs, -1.0, 1.0)
    data_quality = len(available) / len(CHECKLIST)
    critical = [x for x in items if x.get("critical")]
    critical_available = [x for x in critical if x.get("available") and x.get("score") is not None]
    critical_quality = len(critical_available) / max(len(critical), 1)
    blockers = [x for x in items if x.get("key") in ("daily_loss_limit", "risk_reward", "stop_loss") and x.get("score") is not None and float(x["score"]) <= -1.0]

    direction = "HOLD"
    if edge >= 0.18:
        direction = "UP"
    elif edge <= -0.18:
        direction = "DOWN"

    base_conviction = abs(edge) * 100.0 * (0.55 + 0.45 * data_quality)
    # "Adjust to the market": scale conviction by the detected regime so a long
    # in a risk-off/high-VIX tape (or a short in a melt-up) is trusted less.
    regime = detect_market_regime(items_by_key)
    regime_mult = _regime_conviction_multiplier(regime, direction)
    regime["conviction_multiplier"] = regime_mult
    regime["direction_assessed"] = direction
    conviction = round(_clamp(base_conviction * regime_mult, 0.0, 100.0), 1)
    confidence = round(_clamp(0.50 + (conviction / 100.0) * 0.45, 0.50, 0.95), 3)
    if data_quality < 0.60 or critical_quality < 0.55:
        confidence = min(confidence, 0.68)
    if blockers:
        confidence = min(confidence, 0.62)

    # Quality is not just raw directional edge. A prediction with modest edge
    # but complete, critical data coverage deserves a higher trust grade than a
    # stronger-looking edge built on missing news/fundamental/risk inputs.
    quality_score = round(_clamp(conviction * 0.70 + data_quality * 25.0 + critical_quality * 20.0 - len(blockers) * 12.0, 0, 100), 1)
    if quality_score >= 90:
        grade = "A+"
    elif quality_score >= 82:
        grade = "A"
    elif quality_score >= 74:
        grade = "B+"
    elif quality_score >= 66:
        grade = "B"
    elif quality_score >= 55:
        grade = "C"
    elif quality_score >= 42:
        grade = "D"
    else:
        grade = "F"

    if direction == "HOLD" or data_quality < 0.55 or critical_quality < 0.50:
        action = "NO EDGE — WATCH ONLY"
    elif blockers:
        action = "NO PREDICTION — RISK BLOCKED"
    elif quality_score >= 78 and confidence >= 0.70:
        action = f"HIGH-CONVICTION {direction} PREDICTION"
    elif quality_score >= 62:
        action = f"WATCHLIST {direction} BIAS"
    else:
        action = "LOW-CONFIDENCE — WAIT"

    by_category: Dict[str, Dict[str, Any]] = {}
    for cat in sorted({c.category for c in CHECKLIST}):
        cat_items = [x for x in items if x.get("category") == cat]
        cat_avail = [x for x in cat_items if x.get("available") and x.get("score") is not None]
        if cat_avail:
            cweighted = sum(float(x.get("score") or 0) * float(x.get("weight") or 1.0) for x in cat_avail)
            cmax = sum(2.0 * float(x.get("weight") or 1.0) for x in cat_avail) or 1.0
            cedge = _clamp(cweighted / cmax, -1.0, 1.0)
        else:
            cedge = 0.0
        by_category[cat] = {
            "available": len(cat_avail),
            "total": len(cat_items),
            "coverage": round(len(cat_avail) / max(len(cat_items), 1), 3),
            "edge": round(cedge, 3),
            "label": "bullish" if cedge > 0.15 else "bearish" if cedge < -0.15 else "neutral",
        }

    top_bullish = sorted([x for x in available if float(x.get("score") or 0) > 0], key=lambda x: float(x["score"]) * float(x.get("weight") or 1), reverse=True)[:5]
    top_bearish = sorted([x for x in available if float(x.get("score") or 0) < 0], key=lambda x: float(x["score"]) * float(x.get("weight") or 1))[:5]
    unknown_critical = [x for x in critical if not x.get("available")]

    brain = _ai_brain(symbol, direction, action, top_bullish, top_bearish, unknown_critical, risk_plan, data_quality, regime)

    return {
        "symbol": symbol,
        "ok": True,
        "engine": "super_ghost_checklist_v1",
        "ts": _now(),
        "disclaimer": "Prediction intelligence only; not financial advice and not auto-trading.",
        "prediction": {
            "direction": direction,
            "confidence": confidence,
            "conviction_score": conviction,
            "edge_score": round(edge * 100.0, 1),
            "quality_score": quality_score,
            "accuracy_grade": grade,
            "action": action,
            "data_quality": round(data_quality, 3),
            "critical_data_quality": round(critical_quality, 3),
            "blockers": [{"key": x.get("key"), "title": x.get("title"), "evidence": x.get("evidence")} for x in blockers],
        },
        "risk_plan": {k: _safe_round(v, 4) if isinstance(v, float) else v for k, v in risk_plan.items()},
        "coverage": {
            "available": len(available),
            "total": len(CHECKLIST),
            "missing_critical": [{"id": x["id"], "key": x["key"], "title": x["title"], "evidence": x.get("evidence")} for x in unknown_critical],
        },
        "categories": by_category,
        "top_drivers": {
            "bullish": _driver_summary(top_bullish),
            "bearish": _driver_summary(top_bearish),
        },
        "market_regime": regime,
        "checklist": items,
        "ai_brain": brain,
    }


def _driver_summary(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"id": x.get("id"), "key": x.get("key"), "title": x.get("title"), "status": x.get("status"), "score": x.get("score"), "evidence": x.get("evidence")} for x in items]


def detect_market_regime(items_by_key: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Classify the broad market regime so Ghost can *adjust to the market*.

    The user's explicit ask was a system that "knows how to adjust to the
    market." This reads the already-scored market-context items (SPX, Nasdaq,
    sector, VIX, Fed/CPI) and returns a regime label plus a conviction
    multiplier. Long setups are trusted less in risk-off / high-fear tape;
    short setups are trusted less in a strong risk-on melt-up. Unknown macro
    data keeps the multiplier neutral instead of guessing.
    """
    def _score(key: str) -> Optional[float]:
        it = items_by_key.get(key) or {}
        if not it.get("available") or it.get("score") is None:
            return None
        return float(it["score"])

    spx = _score("spx")
    ndx = _score("nasdaq")
    sector = _score("sector")
    vix_item = items_by_key.get("vix") or {}
    vix_val = _f((vix_item.get("value") or {}).get("vix")) if vix_item.get("available") else None
    fed = _score("fed_cpi")

    breadth = [v for v in (spx, ndx, sector) if v is not None]
    macro_available = len(breadth) + (1 if vix_val is not None else 0) + (1 if fed is not None else 0)
    if macro_available == 0:
        return {
            "label": "unknown",
            "risk_state": "unknown",
            "conviction_multiplier": 1.0,
            "vix": None,
            "breadth_score": None,
            "macro_inputs_available": 0,
            "note": "No market-context data available; conviction not adjusted for regime.",
        }

    breadth_score = round(sum(breadth) / len(breadth), 3) if breadth else 0.0
    high_vol = vix_val is not None and vix_val >= 25.0
    calm = vix_val is not None and vix_val < 15.0

    if breadth_score >= 0.5 and not high_vol:
        risk_state = "risk_on"
    elif breadth_score <= -0.5 or high_vol:
        risk_state = "risk_off"
    else:
        risk_state = "neutral"

    if high_vol and breadth_score <= -0.25:
        label = "risk_off_high_volatility"
    elif risk_state == "risk_on" and calm:
        label = "calm_risk_on"
    elif risk_state == "risk_on":
        label = "risk_on"
    elif risk_state == "risk_off":
        label = "risk_off"
    else:
        label = "mixed"

    return {
        "label": label,
        "risk_state": risk_state,
        "vix": round(vix_val, 2) if vix_val is not None else None,
        "breadth_score": breadth_score,
        "fed_cpi_score": fed,
        "macro_inputs_available": macro_available,
        "high_volatility": bool(high_vol),
        # Multiplier filled in per-direction by _regime_conviction_multiplier.
        "conviction_multiplier": 1.0,
        "note": f"Regime '{label}' from breadth {breadth_score:+.2f}"
        + (f", VIX {vix_val:.1f}" if vix_val is not None else "")
        + ".",
    }


def _regime_conviction_multiplier(regime: Dict[str, Any], direction: str) -> float:
    """How much to scale conviction given the regime and trade direction.

    This is the concrete 'adjust to the market' lever. Range ~[0.55, 1.15].
    """
    if not regime or regime.get("risk_state") in (None, "unknown"):
        return 1.0
    risk_state = regime.get("risk_state")
    high_vol = bool(regime.get("high_volatility"))
    mult = 1.0
    if direction == "UP":
        if risk_state == "risk_on":
            mult *= 1.12
        elif risk_state == "risk_off":
            mult *= 0.70
    elif direction == "DOWN":
        if risk_state == "risk_off":
            mult *= 1.10
        elif risk_state == "risk_on":
            mult *= 0.75
    # Fear tape compresses conviction in either direction (whippy, gap-prone).
    if high_vol:
        mult *= 0.85
    return round(_clamp(mult, 0.55, 1.15), 3)


def generate_ai_brief(symbol: str, report: Dict[str, Any], *, snapshot: Optional[Dict[str, Any]] = None, timeout: float = 45.0) -> Dict[str, Any]:
    """Use the real AI brain (Claude) to read the news + checklist and explain.

    This is the "built-in AI that reads the news" layer. It is OFF by default on
    the endpoint (opt-in via ?ai=1) so the fast deterministic report stays free
    and offline. It never fabricates: it is handed ONLY Ghost's own scored
    checklist, top drivers, regime, and recent headlines, and asked to explain
    and stress-test — not to invent numbers. Degrades gracefully with no key.
    """
    if not ANTHROPIC_KEY:
        return {"ok": False, "available": False, "reason": "ANTHROPIC_API_KEY not configured; deterministic ai_brain still provided."}

    pred = report.get("prediction") or {}
    headlines = []
    for a in ((snapshot or {}).get("news") or [])[:12]:
        if isinstance(a, dict):
            t = str(a.get("title") or a.get("headline") or "").strip()
            if t:
                headlines.append(t[:160])
    brain_input = {
        "symbol": symbol,
        "prediction": {
            "direction": pred.get("direction"),
            "confidence": pred.get("confidence"),
            "conviction_score": pred.get("conviction_score"),
            "quality_score": pred.get("quality_score"),
            "accuracy_grade": pred.get("accuracy_grade"),
            "action": pred.get("action"),
            "data_quality": pred.get("data_quality"),
        },
        "market_regime": report.get("market_regime"),
        "top_bullish": [{"title": d.get("title"), "evidence": d.get("evidence")} for d in (report.get("top_drivers") or {}).get("bullish", [])],
        "top_bearish": [{"title": d.get("title"), "evidence": d.get("evidence")} for d in (report.get("top_drivers") or {}).get("bearish", [])],
        "missing_critical": [m.get("title") for m in (report.get("coverage") or {}).get("missing_critical", [])],
        "recent_headlines": headlines,
        "risk_plan": report.get("risk_plan"),
    }
    system = (
        "You are Super Ghost's equity-research brain. You are given ONLY Ghost's own "
        "computed 25-point checklist result, the market regime, the strongest bullish and "
        "bearish drivers, what data is missing, and recent headlines. Your job: read the "
        "news and signals and explain, in plain English, why the stock might move and how "
        "the current market regime changes the trust level. RULES: (1) Do NOT invent prices, "
        "EPS, or figures not provided. (2) If critical data is missing, say so and lower trust. "
        "(3) This is prediction intelligence for a human, NOT financial advice and NOT an order. "
        "Return STRICT JSON with keys: thesis (string), news_read (string: what the headlines imply), "
        "regime_effect (string: how the market regime adjusts the call), bull_case (array of strings), "
        "bear_case (array of strings), what_would_change_my_mind (array of strings), "
        "trust (one of: high, medium, low), one_liner (string)."
    )
    user = "Ghost report (JSON):\n" + json.dumps(brain_input, default=str)[:9000] + "\n\nReturn only the JSON object."
    try:
        import requests
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": SUPER_GHOST_AI_MODEL, "max_tokens": SUPER_GHOST_AI_MAX_TOKENS, "system": system, "messages": [{"role": "user", "content": user}]},
            timeout=timeout,
        )
        if resp.status_code != 200:
            LOGGER.warning("Super Ghost AI brief %s: %s", resp.status_code, resp.text[:120])
            return {"ok": False, "available": False, "reason": f"AI model error ({resp.status_code})"}
        data = resp.json()
        text = ""
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text += block.get("text") or ""
        text = text.strip()
        parsed = _safe_parse_json(text)
        if parsed is None:
            return {"ok": True, "available": True, "model": SUPER_GHOST_AI_MODEL, "format": "text", "analysis": text[:4000]}
        parsed.update({"ok": True, "available": True, "model": SUPER_GHOST_AI_MODEL, "format": "json"})
        return parsed
    except Exception as e:
        LOGGER.warning("Super Ghost AI brief failed: %s", str(e)[:160])
        return {"ok": False, "available": False, "reason": str(e)[:160]}


def _safe_parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None
    return None


def _ai_brain(symbol: str, direction: str, action: str, bullish: List[Dict[str, Any]], bearish: List[Dict[str, Any]], unknown_critical: List[Dict[str, Any]], risk_plan: Dict[str, Any], data_quality: float, regime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    bull_text = "; ".join([f"{x['title']}: {x.get('evidence') or x.get('status')}" for x in bullish[:3]]) or "No strong bullish drivers yet."
    bear_text = "; ".join([f"{x['title']}: {x.get('evidence') or x.get('status')}" for x in bearish[:3]]) or "No strong bearish drivers yet."
    missing = ", ".join([x.get("title", "unknown") for x in unknown_critical[:5]])
    regime = regime or {}
    regime_label = regime.get("label", "unknown")
    regime_mult = regime.get("conviction_multiplier", 1.0)
    if direction == "UP":
        thesis = f"{symbol} has a bullish prediction bias, but trust depends on data coverage and risk/reward. Strongest supports: {bull_text}"
    elif direction == "DOWN":
        thesis = f"{symbol} has a bearish prediction bias. Main pressure points: {bear_text}"
    else:
        thesis = f"{symbol} does not have enough clean edge for a directional prediction yet."
    if regime_label != "unknown":
        if regime_mult > 1.0:
            regime_effect = f"Market regime '{regime_label}' SUPPORTS a {direction} bias — conviction scaled up x{regime_mult}."
        elif regime_mult < 1.0:
            regime_effect = f"Market regime '{regime_label}' works AGAINST a {direction} bias — conviction scaled down x{regime_mult}."
        else:
            regime_effect = f"Market regime '{regime_label}' is neutral for this direction."
    else:
        regime_effect = "Market regime unknown (macro feeds unavailable); conviction not regime-adjusted."
    return {
        "name": "Super Ghost Brain v1",
        "thesis": thesis,
        "counter_thesis": bear_text if direction == "UP" else bull_text if direction == "DOWN" else f"Bullish: {bull_text} | Bearish: {bear_text}",
        "regime_effect": regime_effect,
        "trust_instruction": "Trust strong predictions only when data quality, critical coverage, and risk/reward are all strong. Unknown data lowers the grade.",
        "what_to_verify_next": [x.get("title") for x in unknown_critical[:8]],
        "risk_sentence": (
            f"Entry/reference {risk_plan.get('entry')}, stop {risk_plan.get('stop_loss')}, target {risk_plan.get('target_price')}, "
            f"R:R {risk_plan.get('risk_reward_ratio')}"
        ),
        "data_quality_note": f"{data_quality*100:.0f}% of the 25-point checklist is populated.",
        "action": action,
    }


def build_super_ghost(symbol: str = "WOLF", *, snapshot: Optional[Dict[str, Any]] = None, ai: bool = False) -> Dict[str, Any]:
    """Build a Super Ghost 25-point prediction-intelligence report.

    ``snapshot`` is optional and used by tests / future batch jobs. When omitted,
    live best-effort data is fetched through existing Ghost data modules.

    ``ai=True`` additionally calls the real built-in AI brain (Claude) to read
    the headlines + checklist and return an explained, regime-aware brief. It is
    opt-in so the deterministic report stays fast/free/offline; it degrades
    gracefully (``ai_brief.available == False``) when no API key is configured.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "symbol required"}
    snap = dict(snapshot or _fetch_live_snapshot(sym))
    items: Dict[str, Dict[str, Any]] = {}
    _evaluate_company(sym, snap, items)
    price_ctx = _evaluate_price_action(sym, snap, items)
    _evaluate_market(snap, items)
    risk_plan = _evaluate_risk(snap, items, price_ctx)
    # Guarantee every checklist row exists exactly once.
    for spec in CHECKLIST:
        items.setdefault(spec.key, _unknown(spec, "Checklist evaluator did not produce this item.", "engine"))
    report = _aggregate(sym, items, risk_plan)
    if ai:
        report["ai_brief"] = generate_ai_brief(sym, report, snapshot=snap)
    return report


def _df_to_points(df: Any) -> List[Dict[str, Any]]:
    if df is None or getattr(df, "empty", False):
        return []
    out: List[Dict[str, Any]] = []
    try:
        for ix, row in df.iterrows():
            out.append({
                "ts": int(ix.timestamp()) if hasattr(ix, "timestamp") else str(ix),
                "open": _f(row.get("Open")),
                "high": _f(row.get("High")),
                "low": _f(row.get("Low")),
                "close": _f(row.get("Close")),
                "volume": _i(row.get("Volume")),
            })
    except Exception:
        return []
    return [r for r in out if r.get("close") is not None]


def _sector_etf(info: Dict[str, Any]) -> str:
    industry = str(info.get("industry") or "").lower()
    sector = str(info.get("sector") or "").lower()
    if "semiconductor" in industry:
        return "SMH"
    return _SECTOR_ETF_BY_SECTOR.get(sector, "SPY")


def _latest_earnings_from_yf(symbol: str, tk: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        eh = getattr(tk, "earnings_history", None)
        if eh is not None and not getattr(eh, "empty", True):
            row = eh.iloc[-1]
            out["estimate_eps"] = _f(row.get("epsEstimate"))
            out["actual_eps"] = _f(row.get("epsActual"))
    except Exception:
        pass
    try:
        inc = getattr(tk, "quarterly_income_stmt", None) or getattr(tk, "quarterly_financials", None)
        if inc is not None and not getattr(inc, "empty", True):
            rev_row = None
            for label in ("Total Revenue", "TotalRevenue", "Revenue"):
                if label in inc.index:
                    rev_row = inc.loc[label]
                    break
            if rev_row is not None and len(rev_row) >= 5:
                latest = _f(rev_row.iloc[0])
                year_ago = _f(rev_row.iloc[4])
                out["revenue"] = latest
                out["revenue_year_ago"] = year_ago
                if latest is not None and year_ago and year_ago > 0:
                    out["revenue_yoy"] = (latest - year_ago) / year_ago
    except Exception:
        pass
    return out


def _fetch_live_snapshot(symbol: str) -> Dict[str, Any]:
    """Best-effort live data fetch. Never raises; unknown fields reduce coverage."""
    snap: Dict[str, Any] = {"symbol": symbol}
    try:
        from core.yfinance_client import yf_fast_info, yf_history, yf_info, yf_news, yf_ticker
        info = yf_info(symbol) or {}
        fi = yf_fast_info(symbol)
        tk = yf_ticker(symbol)
        snap["info"] = info
        snap["sector"] = info.get("sector")
        snap["sector_etf"] = _sector_etf(info)
        for attr, key in (("last_price", "current_price"), ("regular_market_price", "current_price"), ("last_volume", "volume"), ("market_cap", "market_cap"), ("year_high", "week52_high"), ("year_low", "week52_low")):
            try:
                val = getattr(fi, attr, None) if fi is not None else None
                if val is not None and snap.get(key) is None:
                    snap[key] = val
            except Exception:
                pass
        snap["current_price"] = snap.get("current_price") or info.get("currentPrice") or info.get("regularMarketPrice")
        snap["avg_volume"] = info.get("averageVolume") or info.get("averageDailyVolume10Day")
        snap["week52_high"] = snap.get("week52_high") or info.get("fiftyTwoWeekHigh")
        snap["week52_low"] = snap.get("week52_low") or info.get("fiftyTwoWeekLow")
        snap["history"] = _df_to_points(yf_history(symbol, "1y", "1d"))
        snap["spy_history"] = _df_to_points(yf_history("SPY", "3mo", "1d"))
        snap["qqq_history"] = _df_to_points(yf_history("QQQ", "3mo", "1d"))
        snap["spx_history"] = _df_to_points(yf_history("^GSPC", "3mo", "1d"))
        snap["ixic_history"] = _df_to_points(yf_history("^IXIC", "3mo", "1d"))
        snap["vix_history"] = _df_to_points(yf_history("^VIX", "1mo", "1d"))
        if snap.get("sector_etf"):
            snap["sector_history"] = _df_to_points(yf_history(str(snap["sector_etf"]), "3mo", "1d"))
        if tk is not None:
            snap["earnings"] = _latest_earnings_from_yf(symbol, tk)
        # analyst data
        recs = {"strong_buy": 0, "buy": 0, "hold": 0, "underperform": 0, "sell": 0}
        try:
            if tk is not None:
                rec = getattr(tk, "recommendations", None)
                if rec is not None and not getattr(rec, "empty", True):
                    row = rec.iloc[0]
                    recs = {
                        "strong_buy": _i(row.get("strongBuy")) or 0,
                        "buy": _i(row.get("buy")) or 0,
                        "hold": _i(row.get("hold")) or 0,
                        "underperform": _i(row.get("sell")) or 0,
                        "sell": _i(row.get("strongSell")) or 0,
                    }
        except Exception:
            pass
        snap["analysts"] = {
            "current_price": snap.get("current_price"),
            "price_target_avg": info.get("targetMeanPrice"),
            "price_target_low": info.get("targetLowPrice"),
            "price_target_high": info.get("targetHighPrice"),
            "recommendations": recs,
        }
        # yfinance news + Ghost stored news
        news: List[Dict[str, Any]] = []
        try:
            from core.news import get_recent_articles
            news.extend(get_recent_articles(30, symbol=symbol) or [])
        except Exception:
            pass
        for n in (yf_news(symbol) or [])[:20]:
            if isinstance(n, dict):
                news.append({
                    "title": n.get("title") or "",
                    "summary": n.get("summary") or "",
                    "source": n.get("publisher") or "Yahoo Finance",
                    "url": n.get("link") or "",
                    "published_at": n.get("providerPublishTime"),
                    "symbols": [symbol],
                })
        snap["news"] = news
        try:
            from core.news_sentiment import fetch_news_sentiment
            ns = fetch_news_sentiment(symbol, limit=10)
            if ns.get("sentiment_score") is not None:
                for n in snap["news"]:
                    n.setdefault("sentiment", ns.get("sentiment_score"))
        except Exception:
            pass
        # EDGAR material events for earnings/officer changes/delisting risk.
        try:
            from core.edgar_integration import fetch_recent_8k
            ed = fetch_recent_8k(symbol, days=90)
            snap["edgar"] = ed
            if ed.get("has_officer_change"):
                snap.setdefault("news", []).append({"title": "Recent 8-K officer/CEO/CFO change detected", "category": "company", "symbols": [symbol], "sentiment": -0.2})
            if ed.get("has_earnings"):
                snap.setdefault("news", []).append({"title": "Recent 8-K earnings results detected", "category": "earnings", "symbols": [symbol], "sentiment": 0.0})
            if ed.get("has_delisting_risk"):
                snap.setdefault("news", []).append({"title": "Recent 8-K delisting risk detected", "category": "company", "symbols": [symbol], "sentiment": -0.8})
        except Exception:
            pass
        try:
            from core.macro_regime import get_macro_features
            macro = get_macro_features()
            snap.update({
                "vix": (macro.get("macro_vix_level") * 40.0) if macro.get("macro_vix_level") is not None else None,
                "spy_20d": macro.get("macro_spy_20d_return"),
                "fed_rate": (macro.get("macro_fed_rate") * 10.0) if macro.get("macro_fed_rate") is not None else None,
            })
        except Exception:
            pass
        try:
            from core.risk_discipline import daily_loss_lock_state, risk_settings
            snap["risk"] = risk_settings()
            snap["daily_loss_lock"] = daily_loss_lock_state()
        except Exception:
            pass
    except Exception as e:
        snap["fetch_error"] = str(e)[:160]
    return snap


def checklist_manifest() -> List[Dict[str, Any]]:
    """Machine-readable checklist manifest for docs/UI/tests."""
    return [{"id": c.id, "category": c.category, "key": c.key, "title": c.title, "question": c.question, "weight": c.weight, "critical": c.critical} for c in CHECKLIST]
