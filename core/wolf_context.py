"""
WOLF Context Service — Phase 1+2: Wolfspeed-Specific Intelligence
==================================================================
Provides WOLF (Wolfspeed, CIK 0000895419) specific market context
that generic stock models miss:

  • Short interest %     — WOLF is heavily shorted; squeeze potential
  • Earnings countdown   — shifts to high-caution mode within 5 days
  • SEC EDGAR 8-K        — material events (contracts, debt, officer changes)
  • Competitor delta     — ON (Onsemi), STM (STMicro) directional correlation
  • EV sector proxy      — DRIV ETF as demand-side signal
  • Customer/catalyst    — GM, Mercedes, BorgWarner, DOE contract news scanner
  • Headline scoring     — WOLF-specific bullish/bearish keyword signals

Returns a WolfContext dataclass with a net confidence adjustment
(-0.15 to +0.15) that the stock engine adds to its base confidence.

All data sources are free / public:
  • Finviz quote page (no API key, HTML scrape, polite rate limit)
  • SEC EDGAR submissions API (free, 10 req/s)
  • Google News RSS (no API key)
  • Polygon REST API for competitor quotes (reuses existing key)

Cache TTL: 15 minutes (data doesn't change that fast intraday)
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import requests

LOGGER = logging.getLogger("ghost.wolf_context")

# ---------------------------------------------------------------------------
# Wolfspeed constants
# ---------------------------------------------------------------------------
WOLF_TICKER = "WOLF"
WOLF_CIK = "0000895419"          # SEC CIK for Wolfspeed / formerly Cree

# Competitors — these are the other SiC semiconductor players.
# When ON or STM moves strongly, WOLF tends to follow (sector rotation).
WOLF_COMPETITORS = ["ON", "STM"]   # Onsemi, STMicroelectronics

# EV demand proxy — DRIV (Global X Autonomous & Electric Vehicles ETF)
WOLF_EV_PROXY = "DRIV"

# ---------------------------------------------------------------------------
# Cache — avoids hammering free APIs on every prediction cycle
# ---------------------------------------------------------------------------
_CACHE: dict = {}
_CACHE_TTL = 900  # 15 minutes


def _cache_get(key: str):
    entry = _CACHE.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["value"]
    return None


def _cache_set(key: str, value) -> None:
    _CACHE[key] = {"value": value, "ts": time.time()}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class WolfEarnings:
    """Next earnings date info for WOLF."""
    date_str: str          # "May 28, 2026" or "Unknown"
    days_away: int         # -1 if unknown
    is_earnings_week: bool # True if ≤5 trading days away
    caution_mode: bool     # True if ≤2 trading days away


@dataclass
class WolfShortData:
    """Short interest snapshot."""
    short_float_pct: float   # e.g. 28.4 = 28.4% of float shorted
    days_to_cover: float     # Short interest / avg daily volume
    squeeze_risk: str        # "low" | "medium" | "high" | "extreme"


@dataclass
class WolfEdgarAlert:
    """Most recent 8-K filing summary."""
    filing_date: str
    urgency: str             # "low" | "medium" | "high" | "critical"
    items: list[str]         # e.g. ["2.02", "5.02"]
    description: str
    sentiment_score: float   # -1.0 to +1.0


@dataclass
class WolfCompetitorSignal:
    """Competitor/sector directional signal."""
    symbol: str
    price_change_pct: float
    direction: str           # "UP" | "DOWN" | "FLAT"
    signal_strength: float   # 0.0 to 1.0


@dataclass
class WolfContext:
    """
    Full WOLF intelligence context.

    net_confidence_adj: float in [-0.15, +0.15]
        Positive = bullish context (shorts covering, no earnings risk,
        no bad 8-K, competitors moving up).
        Negative = bearish context (high short float + squeeze risk off,
        earnings imminent, critical 8-K, competitors falling).
    """
    earnings: Optional[WolfEarnings] = None
    short_data: Optional[WolfShortData] = None
    edgar_alert: Optional[WolfEdgarAlert] = None
    competitor_signals: list[WolfCompetitorSignal] = field(default_factory=list)
    ev_proxy_change_pct: float = 0.0
    net_confidence_adj: float = 0.0
    reasons: list[str] = field(default_factory=list)
    fetch_time_ms: float = 0.0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Finviz scraper — short interest + earnings date
# ---------------------------------------------------------------------------

_FINVIZ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; GhostBot/1.0; +https://ghost-protocol.railway.app)"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def _fetch_finviz(ticker: str) -> dict:
    """
    Scrape Finviz quote page for short interest, earnings date.
    Returns dict with keys: short_float, days_to_cover, earnings_date.
    Falls back gracefully on any error.
    """
    cached = _cache_get(f"finviz:{ticker}")
    if cached is not None:
        return cached

    result: dict = {}
    try:
        url = f"https://finviz.com/quote.ashx?t={ticker}&p=d"
        resp = requests.get(url, headers=_FINVIZ_HEADERS, timeout=10)
        resp.raise_for_status()
        html = resp.text

        # Short Float %
        m = re.search(r"Short Float.*?<td[^>]*>([\d.]+)%</td>", html, re.DOTALL)
        if not m:
            # Alternative pattern
            m = re.search(r'Short Float</td>\s*<td[^>]*>([\d.]+)%', html, re.DOTALL)
        if m:
            result["short_float"] = float(m.group(1))

        # Days to Cover
        m = re.search(r"Short Ratio.*?<td[^>]*>([\d.]+)</td>", html, re.DOTALL)
        if m:
            result["days_to_cover"] = float(m.group(1))

        # Earnings Date — finviz shows "Earnings" row with date
        m = re.search(r"Earnings</td>\s*<td[^>]*>([^<]+)</td>", html, re.DOTALL)
        if m:
            result["earnings_date"] = m.group(1).strip()

        LOGGER.debug(f"Finviz {ticker}: {result}")
        _cache_set(f"finviz:{ticker}", result)
    except Exception as exc:
        LOGGER.warning(f"Finviz fetch failed for {ticker}: {exc}")

    return result


def _parse_earnings(earnings_str: str) -> WolfEarnings:
    """
    Parse finviz earnings date string like 'May 28 AMC' or 'Jun 03 BMO'.
    Returns WolfEarnings with days_away calculated.
    """
    if not earnings_str or earnings_str in ("-", "N/A", ""):
        return WolfEarnings(
            date_str="Unknown", days_away=-1,
            is_earnings_week=False, caution_mode=False
        )
    # Strip time suffix (AMC = after market close, BMO = before market open)
    clean = re.sub(r"\s+(AMC|BMO|AMC\*|BMO\*)$", "", earnings_str.strip(), flags=re.IGNORECASE)
    # Try to parse — finviz omits year, assume current or next year
    for fmt in ("%b %d", "%b %d %Y"):
        try:
            base = datetime.strptime(clean if fmt != "%b %d %Y" else clean + f" {datetime.now().year}", fmt)
            # Attach current year
            now = datetime.now()
            candidate = base.replace(year=now.year)
            if candidate < now - timedelta(days=30):
                candidate = candidate.replace(year=now.year + 1)
            days_away = (candidate - now).days
            return WolfEarnings(
                date_str=earnings_str,
                days_away=days_away,
                is_earnings_week=(0 <= days_away <= 5),
                caution_mode=(0 <= days_away <= 2),
            )
        except ValueError:
            continue

    return WolfEarnings(
        date_str=earnings_str, days_away=-1,
        is_earnings_week=False, caution_mode=False
    )


def _build_short_data(short_float: float, days_to_cover: float) -> WolfShortData:
    """Assess squeeze risk from short float % and days-to-cover."""
    if short_float >= 35 or days_to_cover >= 5:
        squeeze_risk = "extreme"
    elif short_float >= 25 or days_to_cover >= 3:
        squeeze_risk = "high"
    elif short_float >= 15 or days_to_cover >= 2:
        squeeze_risk = "medium"
    else:
        squeeze_risk = "low"
    return WolfShortData(
        short_float_pct=short_float,
        days_to_cover=days_to_cover,
        squeeze_risk=squeeze_risk,
    )


# ---------------------------------------------------------------------------
# EDGAR 8-K watcher — WOLF-specific filings
# ---------------------------------------------------------------------------

def _fetch_wolf_edgar() -> Optional[WolfEdgarAlert]:
    """
    Fetch the most recent WOLF 8-K from SEC EDGAR.
    Uses the free submissions API — no API key required.
    """
    cached = _cache_get("edgar:WOLF")
    if cached is not None:
        return cached

    try:
        from core.edgar_integration import EDGARClient
        client = EDGARClient()
        filings = client.get_company_filings(WOLF_CIK, filing_type="8-K", limit=5)

        if not filings:
            _cache_set("edgar:WOLF", None)
            return None

        latest = filings[0]
        alert = WolfEdgarAlert(
            filing_date=datetime.fromtimestamp(latest.filing_date).strftime("%Y-%m-%d"),
            urgency=latest.urgency,
            items=latest.items,
            description=latest.description[:200],
            sentiment_score=latest.sentiment_score,
        )
        _cache_set("edgar:WOLF", alert)
        LOGGER.info(
            f"WOLF 8-K: {alert.filing_date}, urgency={alert.urgency}, "
            f"items={alert.items}, sentiment={alert.sentiment_score:+.2f}"
        )
        return alert
    except Exception as exc:
        LOGGER.warning(f"EDGAR fetch for WOLF failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Competitor / sector price change
# ---------------------------------------------------------------------------

def _fetch_price_change(ticker: str) -> float:
    """
    Return today's % price change for a ticker.
    Uses Polygon daily bar (reuses existing POLYGON_KEY).
    Falls back to yfinance if no Polygon key.
    """
    cached = _cache_get(f"price_change:{ticker}")
    if cached is not None:
        return cached

    pct = 0.0
    try:
        polygon_key = os.getenv("POLYGON_API_KEY", "")
        if polygon_key:
            from datetime import date
            today = date.today().isoformat()
            yesterday = (date.today() - timedelta(days=3)).isoformat()  # go back a few days to handle weekends
            url = (
                f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/"
                f"{yesterday}/{today}?adjusted=true&sort=desc&limit=2&apiKey={polygon_key}"
            )
            resp = requests.get(url, timeout=8)
            data = resp.json()
            results = data.get("results", [])
            if len(results) >= 2:
                close_today = results[0]["c"]
                close_prev = results[1]["c"]
                pct = ((close_today - close_prev) / close_prev) * 100
            elif len(results) == 1:
                # only one bar returned — check open vs close
                bar = results[0]
                if bar.get("o") and bar.get("c"):
                    pct = ((bar["c"] - bar["o"]) / bar["o"]) * 100
        else:
            # Fallback: yfinance (no API key needed)
            import yfinance as yf  # type: ignore
            t = yf.Ticker(ticker)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                pct = ((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2]) * 100
    except Exception as exc:
        LOGGER.debug(f"Price change fetch failed for {ticker}: {exc}")

    _cache_set(f"price_change:{ticker}", pct)
    return pct


def _build_competitor_signals(competitors: list[str]) -> list[WolfCompetitorSignal]:
    signals = []
    for ticker in competitors:
        pct = _fetch_price_change(ticker)
        if abs(pct) < 0.3:
            direction = "FLAT"
            strength = 0.0
        elif pct > 0:
            direction = "UP"
            strength = min(1.0, abs(pct) / 5.0)
        else:
            direction = "DOWN"
            strength = min(1.0, abs(pct) / 5.0)
        signals.append(WolfCompetitorSignal(
            symbol=ticker,
            price_change_pct=round(pct, 2),
            direction=direction,
            signal_strength=round(strength, 3),
        ))
    return signals


# ---------------------------------------------------------------------------
# Confidence adjustment logic
# ---------------------------------------------------------------------------

def _calculate_net_adj(
    wolf_direction: str,   # "UP" | "DOWN"
    earnings: Optional[WolfEarnings],
    short_data: Optional[WolfShortData],
    edgar_alert: Optional[WolfEdgarAlert],
    competitor_signals: list[WolfCompetitorSignal],
    ev_proxy_pct: float,
) -> tuple[float, list[str]]:
    """
    Compute net confidence adjustment and human-readable reasons.
    Returns (adj: float, reasons: list[str]).
    """
    adj = 0.0
    reasons: list[str] = []

    # ── Earnings risk ────────────────────────────────────────────────
    if earnings:
        if earnings.caution_mode:
            adj -= 0.10
            reasons.append(f"⚠️ Earnings in {earnings.days_away}d — caution mode")
        elif earnings.is_earnings_week:
            adj -= 0.05
            reasons.append(f"Earnings in {earnings.days_away}d — elevated risk")

    # ── Short interest / squeeze ─────────────────────────────────────
    if short_data:
        if short_data.squeeze_risk in ("high", "extreme") and wolf_direction == "UP":
            adj += 0.08
            reasons.append(
                f"Short squeeze risk {short_data.squeeze_risk.upper()}: "
                f"{short_data.short_float_pct:.1f}% float shorted"
            )
        elif short_data.squeeze_risk == "extreme" and wolf_direction == "DOWN":
            # High short interest is bearish when stock is falling (piling on)
            adj += 0.04
            reasons.append(f"High short conviction: {short_data.short_float_pct:.1f}% short float")
        elif short_data.short_float_pct > 20 and wolf_direction == "DOWN":
            adj += 0.03
            reasons.append(f"Short float {short_data.short_float_pct:.1f}% supports DOWN")

    # ── EDGAR 8-K alert ─────────────────────────────────────────────
    if edgar_alert:
        # Filed within last 7 days
        try:
            filing_age_days = (
                datetime.now() - datetime.strptime(edgar_alert.filing_date, "%Y-%m-%d")
            ).days
        except Exception:
            filing_age_days = 999

        if filing_age_days <= 7:
            if edgar_alert.urgency == "critical":
                # Critical 8-K: material agreement, acquisition, debt trigger
                if edgar_alert.sentiment_score > 0.3 and wolf_direction == "UP":
                    adj += 0.08
                    reasons.append(f"Critical 8-K (bullish, {edgar_alert.filing_date}): {edgar_alert.items}")
                elif edgar_alert.sentiment_score < -0.3 and wolf_direction == "DOWN":
                    adj += 0.08
                    reasons.append(f"Critical 8-K (bearish, {edgar_alert.filing_date})")
                else:
                    adj -= 0.05
                    reasons.append(f"Critical 8-K filed {edgar_alert.filing_date} — uncertainty")
            elif edgar_alert.urgency == "high" and abs(edgar_alert.sentiment_score) > 0.2:
                sentiment_aligned = (
                    (edgar_alert.sentiment_score > 0 and wolf_direction == "UP") or
                    (edgar_alert.sentiment_score < 0 and wolf_direction == "DOWN")
                )
                adj += 0.04 if sentiment_aligned else -0.02
                reasons.append(f"8-K {edgar_alert.urgency} (sentiment={edgar_alert.sentiment_score:+.2f})")

    # ── Competitor correlation ────────────────────────────────────────
    aligned_count = 0
    for sig in competitor_signals:
        if sig.direction == wolf_direction and sig.signal_strength > 0.3:
            aligned_count += 1
        elif sig.direction != "FLAT" and sig.direction != wolf_direction and sig.signal_strength > 0.4:
            aligned_count -= 1

    if aligned_count >= 2:
        adj += 0.05
        names = [s.symbol for s in competitor_signals]
        reasons.append(f"SiC sector aligned {wolf_direction}: {'+'.join(names)}")
    elif aligned_count <= -2:
        adj -= 0.04
        reasons.append(f"SiC sector diverging from {wolf_direction}")
    elif aligned_count == 1:
        adj += 0.02
        reasons.append(f"1 competitor confirming {wolf_direction}")

    # ── EV sector proxy ──────────────────────────────────────────────
    if abs(ev_proxy_pct) > 0.5:
        ev_direction = "UP" if ev_proxy_pct > 0 else "DOWN"
        if ev_direction == wolf_direction:
            adj += 0.03
            reasons.append(f"EV sector (DRIV) {ev_proxy_pct:+.1f}% aligns {wolf_direction}")
        else:
            adj -= 0.02
            reasons.append(f"EV sector (DRIV) {ev_proxy_pct:+.1f}% diverges")

    # Hard cap
    adj = max(-0.15, min(0.15, adj))
    return round(adj, 4), reasons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_wolf_context(direction: str = "UP") -> WolfContext:
    """
    Fetch full WOLF intelligence context.
    `direction` is the current prediction direction ("UP" or "DOWN").
    Returns WolfContext with net_confidence_adj pre-calculated.
    """
    cached = _cache_get(f"wolf_context:{direction}")
    if cached is not None:
        return cached

    t0 = time.time()
    ctx = WolfContext()
    errors: list[str] = []

    # 1. Finviz: short interest + earnings
    try:
        fv = _fetch_finviz(WOLF_TICKER)
        short_float = fv.get("short_float", 0.0)
        days_to_cover = fv.get("days_to_cover", 0.0)
        if short_float > 0:
            ctx.short_data = _build_short_data(short_float, days_to_cover)
        earnings_str = fv.get("earnings_date", "")
        ctx.earnings = _parse_earnings(earnings_str)
    except Exception as exc:
        errors.append(f"finviz: {exc}")

    # 2. EDGAR: most recent WOLF 8-K
    try:
        ctx.edgar_alert = _fetch_wolf_edgar()
    except Exception as exc:
        errors.append(f"edgar: {exc}")

    # 3. Competitor prices
    try:
        ctx.competitor_signals = _build_competitor_signals(WOLF_COMPETITORS)
    except Exception as exc:
        errors.append(f"competitors: {exc}")

    # 4. EV sector proxy (DRIV)
    try:
        ctx.ev_proxy_change_pct = _fetch_price_change(WOLF_EV_PROXY)
    except Exception as exc:
        errors.append(f"ev_proxy: {exc}")

    # 5. Customer / catalyst news score (Phase 2)
    try:
        catalyst_adj, catalyst_reasons = _get_catalyst_news_score(direction)
        # Merge into final calculation below
    except Exception as exc:
        catalyst_adj, catalyst_reasons = 0.0, []
        errors.append(f"catalyst_news: {exc}")

    # 6. Calculate net adjustment (includes catalyst news)
    ctx.net_confidence_adj, ctx.reasons = _calculate_net_adj(
        wolf_direction=direction,
        earnings=ctx.earnings,
        short_data=ctx.short_data,
        edgar_alert=ctx.edgar_alert,
        competitor_signals=ctx.competitor_signals,
        ev_proxy_pct=ctx.ev_proxy_change_pct,
    )
    # Add catalyst adj on top (capped separately so it can't dominate)
    ctx.net_confidence_adj = max(-0.15, min(0.15, ctx.net_confidence_adj + catalyst_adj))
    ctx.reasons = ctx.reasons + catalyst_reasons

    ctx.fetch_time_ms = round((time.time() - t0) * 1000, 1)
    ctx.errors = errors

    if errors:
        LOGGER.warning(f"WolfContext partial errors: {errors}")

    LOGGER.info(
        f"WolfContext [{direction}]: adj={ctx.net_confidence_adj:+.3f}, "
        f"reasons={ctx.reasons}, t={ctx.fetch_time_ms:.0f}ms"
    )

    _cache_set(f"wolf_context:{direction}", ctx)
    return ctx


# ---------------------------------------------------------------------------
# Phase 2: Customer / Catalyst News Scanner
# ---------------------------------------------------------------------------

# WOLF key customers — announcements from these companies can move WOLF
WOLF_CUSTOMERS = ["GM", "Mercedes", "BorgWarner", "STMicroelectronics", "Renault", "Stellantis"]

# Government / funding signals — DOE/DOD contracts are material catalysts
WOLF_GOV_KEYWORDS = ["DOE", "Department of Energy", "CHIPS Act", "DOD", "Defense", "NEVI"]

# Bullish headline patterns
_BULL_PATTERNS = [
    r"award(ed|s)?\s+(contract|deal|order)",
    r"(new|expanded|renewed)\s+(contract|partnership|agreement|deal)",
    r"(raised?|raise?s?|raising)\s+(guidance|forecast|outlook)",
    r"(beat|beats|exceeded?)\s+(estimate|expectation|forecast|consensus)",
    r"(strong|record|beat)\s+(revenue|sales|order|demand)",
    r"(expand|expands?|expanding)\s+(capacity|production|fab|facility)",
    r"(secures?|wins?|awarded?)\s+\$([\d]+)\s*(million|billion|M|B)",
    r"(strategic|major)\s+(partnership|supply\s+agreement)",
    r"design\s+win",
    r"(upgrade|overweight|outperform|buy)\s+rating",
    r"silicon\s+carbide\s+(demand|surge|growth|adoption)",
    r"ev\s+(adoption|sales|growth|surge)",
    r"(government|federal)\s+(funding|grant|investment|contract)",
]

# Bearish headline patterns
_BEAR_PATTERNS = [
    r"(miss(ed|es)?|missed?)\s+(estimate|expectation|forecast|consensus)",
    r"(cut|cuts?|cutting|lower(ed|s)?)\s+(guidance|forecast|outlook|target)",
    r"(lay(off|s)?|workforce\s+reduction|job\s+cut)",
    r"(debt|default|restructur|bankrupt|chapter\s+11)",
    r"(delayed?|delays?|delay(ed|s)?)\s+(production|delivery|ramp|shipment)",
    r"(recall|product\s+issue|quality\s+problem)",
    r"(downgrade|underperform|sell|reduce)\s+rating",
    r"(cancel(led|s)?|terminat(ed|es)?)\s+(contract|order|agreement|deal)",
    r"(capacity|production)\s+(cut|reduction|halt)",
    r"(competition|competitive\s+pressure|market\s+share\s+loss)",
    r"(writedown|impairment|goodwill\s+charge)",
    r"(sec\s+investigate|doj|class\s+action|lawsuit|litigation)",
]

_GNEWS_HEADERS = {
    "User-Agent": "GhostBot/1.0 (WolfProtocol news scanner)",
    "Accept": "application/rss+xml, application/xml, text/xml",
}


def _score_headline(text: str) -> float:
    """Score a single headline for WOLF bullish/bearish signals. Returns -1..+1."""
    text_lower = text.lower()
    bull_hits = sum(1 for p in _BULL_PATTERNS if re.search(p, text_lower))
    bear_hits = sum(1 for p in _BEAR_PATTERNS if re.search(p, text_lower))
    net = bull_hits - bear_hits
    return max(-1.0, min(1.0, net * 0.4))


def _fetch_gnews_headlines(query: str, max_items: int = 10) -> list[str]:
    """
    Fetch recent Google News RSS headlines for a query string.
    No API key — uses the public Google News RSS endpoint.
    Returns list of headline strings.
    """
    try:
        import xml.etree.ElementTree as ET
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
        resp = requests.get(url, headers=_GNEWS_HEADERS, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        titles = []
        for item in root.findall(".//item")[:max_items]:
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                titles.append(title_el.text)
        return titles
    except Exception as exc:
        LOGGER.debug(f"GNews fetch failed for '{query}': {exc}")
        return []


def _get_catalyst_news_score(direction: str) -> tuple[float, list[str]]:
    """
    Scan recent WOLF/customer/sector headlines for catalysts.
    Returns (confidence_adj, reasons).
    """
    cached = _cache_get("catalyst_news")
    if cached is not None:
        adj, reasons = cached
        # Flip sign if direction is different — the raw score is direction-neutral
        return adj, reasons

    queries = [
        "Wolfspeed WOLF semiconductor",
        "Wolfspeed contract customer deal",
        "silicon carbide SiC EV semiconductor",
    ]

    all_scores: list[float] = []
    headline_samples: list[str] = []

    for q in queries:
        headlines = _fetch_gnews_headlines(q, max_items=8)
        for h in headlines:
            score = _score_headline(h)
            all_scores.append(score)
            if abs(score) > 0.3:
                headline_samples.append(h[:80])

    if not all_scores:
        _cache_set("catalyst_news", (0.0, []))
        return 0.0, []

    avg_score = sum(all_scores) / len(all_scores)
    strong_bull = sum(1 for s in all_scores if s > 0.3)
    strong_bear = sum(1 for s in all_scores if s < -0.3)

    adj = 0.0
    reasons: list[str] = []

    if avg_score > 0.2 and strong_bull >= 2:
        adj = min(0.06, avg_score * 0.15)
        reasons.append(f"Catalyst scan: {strong_bull} bullish headlines (avg={avg_score:+.2f})")
        if headline_samples:
            reasons.append(f"Top headline: {headline_samples[0][:60]}...")
    elif avg_score < -0.2 and strong_bear >= 2:
        adj = max(-0.06, avg_score * 0.15)
        reasons.append(f"Catalyst scan: {strong_bear} bearish headlines (avg={avg_score:+.2f})")
        if headline_samples:
            reasons.append(f"Top headline: {headline_samples[0][:60]}...")

    # Direction alignment: if direction != sentiment, penalise
    if direction == "UP" and avg_score < -0.15:
        adj -= 0.02
    elif direction == "DOWN" and avg_score > 0.15:
        adj -= 0.02

    adj = round(max(-0.06, min(0.06, adj)), 4)
    _cache_set("catalyst_news", (adj, reasons))

    LOGGER.info(
        f"Catalyst news: avg_score={avg_score:+.2f}, bull={strong_bull}, "
        f"bear={strong_bear}, adj={adj:+.3f}"
    )
    return adj, reasons

