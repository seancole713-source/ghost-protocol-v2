"""Collects real evidence into the signal names `core.catalyst_checklist` reads.

Every box in the checklist is only as honest as the number behind it. This
module's only job is mapping Ghost's existing data sources (SEC filings, news
events, squeeze fuel metrics, price bars) onto those signal names -- and
leaving a signal out entirely, rather than guessing, whenever the underlying
source has nothing.

Every fetch here is best-effort and independently guarded: one source being
down (earnings, EDGAR, a price feed) must not take any other source down with
it, and must not manufacture a fake value to fill the gap. A missing signal
becomes an UNKNOWN box in the checklist, which is the correct outcome --
never a silent pass.

This module does no scoring itself. It is pure collection, kept separate from
`core.catalyst_checklist` so the checklist's pass/fail rules can be tested
against fixed evidence dicts without touching the network or the database.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

LOGGER = logging.getLogger("ghost.checklist_evidence")


def _safe(label: str, fn, *args, **kwargs) -> Any:
    """Run one collector; log and return None on failure, never raise."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - deliberately broad; isolate one source's failure
        LOGGER.warning("checklist_evidence: %s failed: %s", label, str(exc)[:160])
        return None


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # filters NaN


def _collect_earnings(symbol: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    from core.earnings_surprise import get_earnings_surprise

    data = _safe("earnings_surprise", get_earnings_surprise, symbol) or {}
    if data.get("available"):
        eps_actual = _num(data.get("eps_actual"))
        eps_expected = _num(data.get("eps_expected") or data.get("eps_estimate"))
        if eps_actual is not None and eps_expected not in (None, 0):
            out["earnings_surprise_pct"] = round(
                (eps_actual - eps_expected) / abs(eps_expected) * 100.0, 2,
            )
    return out


def _collect_fundamentals(symbol: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    from core.sec_fundamentals import get_fundamentals

    data = _safe("sec_fundamentals", get_fundamentals, symbol) or {}
    if not data.get("available"):
        return out

    revenue = _num(data.get("revenue"))
    revenue_prior = _num(data.get("revenue_year_ago"))
    if revenue is not None and revenue_prior not in (None, 0):
        out["revenue_growth_pct"] = round((revenue - revenue_prior) / abs(revenue_prior) * 100.0, 2)

    eps = _num(data.get("actual_eps") or data.get("eps_actual"))
    eps_prior = _num(data.get("eps_year_ago"))
    if eps is not None and eps_prior not in (None, 0):
        out["net_income_growth_pct"] = round((eps - eps_prior) / abs(eps_prior) * 100.0, 2)
        # No direct margin series here -- EPS growth outrunning revenue growth
        # is the closest point-in-time proxy this source can honestly give.
        rev_growth = out.get("revenue_growth_pct")
        if rev_growth is not None:
            out["margin_change_pct"] = round(out["net_income_growth_pct"] - rev_growth, 2)
    return out


def _collect_news(symbol: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    from core.news_events import recent_events_for_symbol

    events = _safe("news_events", recent_events_for_symbol, symbol) or []
    if not events:
        return out
    scores = [_num(e.get("sentiment")) for e in events if isinstance(e, dict)]
    scores = [s for s in scores if s is not None]
    if scores:
        out["news_sentiment"] = round(sum(scores) / len(scores), 3)
    guidance_events = [
        e for e in events
        if isinstance(e, dict) and "guidance" in str(e.get("event_type", "")).lower()
    ]
    if guidance_events:
        avg = sum(_num(e.get("sentiment")) or 0.0 for e in guidance_events) / len(guidance_events)
        out["guidance_direction"] = 1 if avg > 0 else (-1 if avg < 0 else 0)
    return out


def _collect_leadership_change(symbol: str) -> Dict[str, Any]:
    """A leadership-change signal only when SEC EDGAR shows one actually happened.

    Item 5.02 on an 8-K is the SEC's own leadership-change/officer-departure
    code. Absence of that item means no known event, not neutral news --
    so the signal is left out of the dict entirely rather than set to 0.0,
    which keeps the checklist box UNKNOWN instead of misreading silence as
    a wash.
    """
    out: Dict[str, Any] = {}
    from core.edgar_integration import _sentiment_from_items, fetch_recent_8k

    data = _safe("edgar_8k", fetch_recent_8k, symbol, days=30) or {}
    if not data.get("available"):
        return out
    for filing in data.get("filings") or []:
        items = [it.get("item") for it in filing.get("items", []) if isinstance(it, dict)]
        if "5.02" in items:
            out["leadership_change_sentiment"] = _sentiment_from_items(items)
            break  # most recent leadership filing only; do not average stale ones in
    return out


def _collect_squeeze_fuel(symbol: str, market_ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    ctx = market_ctx or {}
    for src_key, dst_key in (
        ("short_float_pct", "short_float_pct"),
        ("days_to_cover", "days_to_cover"),
        ("borrow_fee_pct", "borrow_fee_pct"),
    ):
        val = _num(ctx.get(src_key))
        if val is not None:
            out[dst_key] = val
    return out


def _collect_price_action(symbol: str, market_ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    ctx = market_ctx or {}

    price = _num(ctx.get("price"))
    prior_close = _num(ctx.get("prior_close"))
    if price is not None and prior_close not in (None, 0):
        out["premarket_gap_pct"] = round((price - prior_close) / prior_close * 100.0, 2)
        out["move_from_base_pct"] = round(abs(price - prior_close) / prior_close * 100.0, 2)

    session_vol = _num(ctx.get("session_volume"))
    avg_vol = _num(ctx.get("avg_daily_volume"))
    if session_vol is not None and avg_vol not in (None, 0):
        out["relative_volume"] = round(session_vol / avg_vol, 2)
    if price is not None and avg_vol is not None:
        out["avg_dollar_volume"] = round(price * avg_vol, 2)

    peak_move = _num(ctx.get("peak_move_pct"))
    if peak_move is not None:
        # Overwrites the prior_close-based estimate with the session's actual peak.
        out["move_from_base_pct"] = round(abs(peak_move), 2)

    trend = _num(ctx.get("trend_slope_pct"))
    if trend is not None:
        out["trend_slope_pct"] = trend
    return out


def _collect_context(market_ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    ctx = market_ctx or {}
    for key in ("sector_move_pct", "earnings_days_away"):
        val = _num(ctx.get(key))
        if val is not None:
            out[key] = val

    # market_move_pct prefers a caller-supplied value (e.g. a fresher batched
    # scan figure); otherwise a direct short-window SPY read. Both paths use
    # _num()'s None-on-failure so a bad fetch stays UNKNOWN, never a fake 0.0.
    market_move = _num(ctx.get("market_move_pct"))
    if market_move is None:
        from core.macro_regime import _fetch_yfinance_return

        spy_ret = _safe("spy_5d_return", _fetch_yfinance_return, "SPY", 5)
        market_move = round(spy_ret * 100.0, 2) if spy_ret is not None else None
    if market_move is not None:
        out["market_move_pct"] = market_move
    return out


# Which source a signal came from -- surfaced on the card as "Evidence" so a
# box is never just a number, but a number with a place it can be checked.
SOURCE_BY_SIGNAL: Dict[str, str] = {
    "earnings_surprise_pct": "Earnings report",
    "leadership_change_sentiment": "SEC 8-K filing (item 5.02)",
    "revenue_growth_pct": "SEC filing (10-Q/10-K)",
    "net_income_growth_pct": "SEC filing (10-Q/10-K)",
    "margin_change_pct": "SEC filing (10-Q/10-K)",
    "news_sentiment": "Recent news coverage",
    "guidance_direction": "Company guidance in recent news",
    "short_float_pct": "Exchange short-interest data",
    "days_to_cover": "Exchange short-interest data",
    "borrow_fee_pct": "Broker borrow data",
    "relative_volume": "Live market data",
    "premarket_gap_pct": "Live market data",
    "move_from_base_pct": "Live market data",
    "trend_slope_pct": "Live market data",
    "sector_move_pct": "Live market data",
    "market_move_pct": "S&P 500 (SPY), 5-day",
    "earnings_days_away": "Earnings calendar",
    "avg_dollar_volume": "Live market data",
}


def sources_for(evidence: Dict[str, Any]) -> Dict[str, str]:
    """The source label for every signal actually present in ``evidence``."""
    return {k: SOURCE_BY_SIGNAL.get(k, "Ghost data feed") for k in evidence}


def collect_evidence(
    symbol: str,
    *,
    market_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble every signal `catalyst_checklist` knows how to read for one symbol.

    ``market_ctx`` is an optional pre-fetched snapshot (e.g. from a batched
    scan) covering price/volume/fuel fields -- pass it when scanning many
    symbols at once so this stays a pure merge instead of triggering its own
    network calls per symbol. Any field this function cannot resolve is left
    out of the returned dict; `catalyst_checklist` treats an absent field as
    UNKNOWN, never as a pass.
    """
    sym = (symbol or "").strip().upper()
    evidence: Dict[str, Any] = {}
    evidence.update(_collect_earnings(sym))
    evidence.update(_collect_fundamentals(sym))
    evidence.update(_collect_news(sym))
    evidence.update(_collect_leadership_change(sym))
    evidence.update(_collect_squeeze_fuel(sym, market_ctx))
    evidence.update(_collect_price_action(sym, market_ctx))
    evidence.update(_collect_context(market_ctx))
    return evidence
