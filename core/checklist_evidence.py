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

from datetime import datetime, timezone
import logging
import math
import time
from typing import Any, Dict, Iterable, Optional

from core.evidence_integrity import (
    CONFIRMED,
    VERIFIED_CONFLICT,
    is_confirmed,
    reconcile_signal,
)

LOGGER = logging.getLogger("ghost.checklist_evidence")

# Metadata keys are stored in the immutable ledger beside the scalar projection.
# Scoring code ignores underscore-prefixed keys.
RECORDS_KEY = "_records"
CONFLICTS_KEY = "_conflicts"
UNSUPPORTED_KEY = "_unsupported"


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
    return out if math.isfinite(out) else None


def _epoch(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _record(
    *,
    source: str,
    source_timestamp: Any,
    observation_timestamp: Any,
    reporting_period: str,
    unit: str,
    basis: str,
    actual_value: Any,
    expected_value: Any = "not_applicable",
    comparable_prior_period_value: Any = None,
    methodology: str,
    request_timestamp: int,
    provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "source": source,
        "source_timestamp": _epoch(source_timestamp),
        "observation_timestamp": _epoch(observation_timestamp),
        "request_timestamp": int(request_timestamp),
        "reporting_period": reporting_period,
        "currency": "N/A",
        "unit": unit,
        "basis": basis,
        "expected_value": expected_value,
        "actual_value": actual_value,
        "comparable_prior_period_value": comparable_prior_period_value,
        "calculation_methodology": methodology,
        "confidence_status": CONFIRMED,
        "status": CONFIRMED,
        "provenance": provenance or {},
    }


def _confirmed_projection(
    signal_key: str,
    records: Iterable[Dict[str, Any]],
    *,
    asof_ts: int,
    max_age_s: Optional[int] = None,
) -> tuple[Optional[float], Optional[Dict[str, Any]], list[Dict[str, Any]]]:
    """Return a scalar only from complete, issue-bounded reconciled records."""
    bounded = []
    for record in records:
        source_ts = _epoch(record.get("source_timestamp"))
        observation_ts = _epoch(record.get("observation_timestamp"))
        if source_ts is None or observation_ts is None:
            bounded.append({**record, "confidence_status": "UNVERIFIED", "status": "UNVERIFIED"})
            continue
        if source_ts > asof_ts or observation_ts > asof_ts:
            bounded.append({**record, "confidence_status": "UNVERIFIED", "status": "UNVERIFIED", "future_of_decision": True})
            continue
        if max_age_s is not None and asof_ts - source_ts > max_age_s:
            bounded.append({**record, "confidence_status": "UNVERIFIED", "status": "UNVERIFIED", "stale_at_decision": True})
            continue
        bounded.append(record)
    reconciled = reconcile_signal(signal_key, bounded)
    if reconciled is None or not is_confirmed(reconciled):
        return None, reconciled, bounded
    return _num(reconciled.get("actual_value", reconciled.get("value"))), reconciled, bounded


def _collect_earnings(symbol: str, *, asof_ts: int) -> Dict[str, Any]:
    """Returns ``{"earnings_surprise_pct": {"value": ..., "_ts": report_ts}}``
    or ``{}``.

    ``report_ts`` is the earnings report date yfinance attaches to the
    quarter -- the genuine point-in-time source_timestamp, not request time.
    get_earnings_surprise's own asof_ts gate already refuses to answer if
    that report postdates asof_ts, so a present value here is always safe.
    """
    from core.earnings_surprise import get_earnings_surprise

    data = _safe("earnings_surprise", get_earnings_surprise, symbol, asof_ts=asof_ts) or {}
    if not data.get("available"):
        return {}
    eps_actual = _num(data.get("eps_actual"))
    eps_expected = _num(data.get("eps_expected") or data.get("eps_estimate"))
    if eps_actual is None or eps_expected in (None, 0):
        return {}
    return {
        "earnings_surprise_pct": {
            "value": round((eps_actual - eps_expected) / abs(eps_expected) * 100.0, 2),
            "_ts": _epoch(data.get("report_ts")),
        },
    }


def _collect_fundamentals(symbol: str, *, asof_ts: int) -> Dict[str, Any]:
    """Each returned signal carries its own ``_ts`` -- the SEC filing date the
    underlying fact was drawn from. get_fundamentals's own asof_ts gate
    independently drops EPS or revenue if filed after asof_ts, so any value
    returned here was genuinely knowable by then."""
    out: Dict[str, Any] = {}
    from core.sec_fundamentals import get_fundamentals

    data = _safe("sec_fundamentals", get_fundamentals, symbol, asof_ts=asof_ts) or {}
    if not data.get("available"):
        return out

    revenue = _num(data.get("revenue"))
    revenue_prior = _num(data.get("revenue_year_ago"))
    revenue_ts = _epoch(data.get("revenue_filed_ts"))
    if revenue is not None and revenue_prior not in (None, 0):
        out["revenue_growth_pct"] = {
            "value": round((revenue - revenue_prior) / abs(revenue_prior) * 100.0, 2),
            "_ts": revenue_ts,
        }

    eps = _num(data.get("actual_eps") or data.get("eps_actual"))
    eps_prior = _num(data.get("eps_year_ago"))
    eps_ts = _epoch(data.get("eps_filed_ts"))
    if eps is not None and eps_prior not in (None, 0):
        net_income_growth = round((eps - eps_prior) / abs(eps_prior) * 100.0, 2)
        out["net_income_growth_pct"] = {"value": net_income_growth, "_ts": eps_ts}
        # No direct margin series here -- EPS growth outrunning revenue growth
        # is the closest point-in-time proxy this source can honestly give.
        # The derived fact is only knowable once *both* inputs are filed, so
        # its timestamp is the later of the two, not the earlier.
        rev_growth = out.get("revenue_growth_pct")
        if rev_growth is not None:
            margin_ts = max(t for t in (eps_ts, revenue_ts) if t is not None) if (eps_ts or revenue_ts) else None
            out["margin_change_pct"] = {
                "value": round(net_income_growth - rev_growth["value"], 2),
                "_ts": margin_ts,
            }
    return out


def _collect_news(symbol: str, *, asof_ts: int) -> Dict[str, Any]:
    """Each returned signal carries ``_ts`` = the newest contributing event's
    own asof_ts (already point-in-time bounded by recent_events_for_symbol).
    An aggregate is only as fresh as its most recent input."""
    out: Dict[str, Any] = {}
    from core.news_events import recent_events_for_symbol

    events = _safe(
        "news_events", recent_events_for_symbol, symbol, asof_ts=asof_ts,
    ) or []
    if not events:
        return out
    scored = [
        (e, _num(e.get("sentiment"))) for e in events if isinstance(e, dict)
    ]
    scored = [(e, s) for e, s in scored if s is not None]
    if scored:
        newest_ts = max((_epoch(e.get("asof_ts")) or 0) for e, _ in scored) or None
        out["news_sentiment"] = {
            "value": round(sum(s for _, s in scored) / len(scored), 3),
            "_ts": newest_ts,
        }
    guidance_scored = [
        (e, s) for e, s in scored
        if "guidance" in str(e.get("event_type", "")).lower()
    ]
    if guidance_scored:
        avg = sum(s for _, s in guidance_scored) / len(guidance_scored)
        newest_guidance_ts = max((_epoch(e.get("asof_ts")) or 0) for e, _ in guidance_scored) or None
        out["guidance_direction"] = {
            "value": 1 if avg > 0 else (-1 if avg < 0 else 0),
            "_ts": newest_guidance_ts,
        }
    return out


def _collect_leadership_change(symbol: str, *, asof_ts: int) -> Dict[str, Any]:
    """A leadership-change signal only when SEC EDGAR shows one actually happened.

    Item 5.02 on an 8-K is the SEC's own leadership-change/officer-departure
    code. Absence of that item means no known event, not neutral news --
    so the signal is left out of the dict entirely rather than set to 0.0,
    which keeps the checklist box UNKNOWN instead of misreading silence as
    a wash. fetch_recent_8k's own asof_ts filter already drops any filing
    dated after asof_ts.
    """
    from core.edgar_integration import _sentiment_from_items, fetch_recent_8k

    data = _safe("edgar_8k", fetch_recent_8k, symbol, days=30, asof_ts=asof_ts) or {}
    if not data.get("available"):
        return {}
    for filing in data.get("filings") or []:
        items = [it.get("item") for it in filing.get("items", []) if isinstance(it, dict)]
        if "5.02" in items:
            filing_ts = None
            try:
                from datetime import datetime as _dt, timezone as _tz
                filing_ts = int(
                    _dt.fromisoformat(str(filing.get("filing_date"))[:10])
                    .replace(tzinfo=_tz.utc).timestamp()
                )
            except Exception:
                pass
            return {
                "leadership_change_sentiment": {
                    "value": _sentiment_from_items(items), "_ts": filing_ts,
                },
            }
            # most recent leadership filing only; do not average stale ones in
    return {}


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


# How long each externally-timestamped signal stays fresh, keyed to its
# source's own natural cadence -- not the blanket 24h used for live market
# features. A quarterly SEC fact is still "current" for ~a quarter; a news
# event is not.
_EXTERNAL_SIGNAL_MAX_AGE_S: Dict[str, int] = {
    "earnings_surprise_pct": 100 * 86400,
    "revenue_growth_pct": 100 * 86400,
    "net_income_growth_pct": 100 * 86400,
    "margin_change_pct": 100 * 86400,
    "leadership_change_sentiment": 30 * 86400,  # matches fetch_recent_8k's days=30 window
    "news_sentiment": 7 * 86400,  # matches recent_events_for_symbol's default lookback
    "guidance_direction": 7 * 86400,
}

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
    """Return actual record sources, never inferred labels posing as provenance."""
    records = evidence.get(RECORDS_KEY) if isinstance(evidence, dict) else None
    if not isinstance(records, dict):
        return {}
    out: Dict[str, str] = {}
    for signal, rows in records.items():
        sources = sorted({str(r.get("source")) for r in (rows or []) if isinstance(r, dict) and r.get("source")})
        if sources:
            out[signal] = ", ".join(sources)
    return out


def _default_market_ctx(symbol: str) -> Dict[str, Any]:
    """Build a market snapshot for one symbol when the caller has none.

    Uses the same cached production sources the squeeze radar already relies
    on: batched Alpaca bars for price/volume and the short-interest cache for
    positioning fuel. Without this fallback, the live checklist endpoint
    (which calls collect_evidence with no ctx) could never fill a
    positioning/confirmation box and no veto could ever trip -- the
    already-ran guard would be permanently asleep.
    """
    sym = (symbol or "").strip().upper()
    ctx: Dict[str, Any] = {}
    from core.squeeze_monitor import _short_context, batched_market_metrics

    metrics = _safe("market_metrics", batched_market_metrics, [sym]) or {}
    row = metrics.get(sym)
    if isinstance(row, dict):
        ctx.update(row)
    short = _safe("short_context", _short_context, sym) or {}
    for key in ("short_float_pct", "days_to_cover"):
        if short.get(key) is not None:
            ctx[key] = short[key]
    return ctx


def collect_evidence(
    symbol: str,
    *,
    asof_ts: Optional[int] = None,
    market_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a point-in-time projection plus the immutable records behind it.

    Live market features (price, volume, positioning) are timestamped off the
    caller's own feature/price snapshot. Catalyst-class signals (earnings,
    fundamentals, news, leadership changes) come from independent sources
    that each carry their own genuine point-in-time timestamp -- the SEC
    filing date, the earnings report date, the news event's own asof_ts --
    never request time. A source that cannot prove when its value was
    knowable is dropped by its own asof_ts gate before it ever reaches here.
    """
    sym = (symbol or "").strip().upper()
    decision_ts = int(time.time()) if asof_ts is None else int(asof_ts)
    ctx = _default_market_ctx(sym) if market_ctx is None else dict(market_ctx)
    feature_source_ts = _epoch(ctx.get("feature_asof_ts"))
    price_source_ts = _epoch(ctx.get("price_as_of_ts"))

    raw: Dict[str, Any] = {}
    raw.update(_collect_squeeze_fuel(sym, ctx))
    price_action = _collect_price_action(sym, ctx)
    raw.update(price_action)
    feature_signals = set()
    for key in (
        "relative_volume",
        "trend_slope_pct",
        "sector_move_pct",
        "market_move_pct",
        "earnings_days_away",
    ):
        value = _num(ctx.get(key))
        if value is not None:
            raw[key] = value
            feature_signals.add(key)

    # Catalyst-class signals: each source is queried independently and best-
    # effort (one being down must not take the others down with it), and each
    # returns its own {"value", "_ts"} rather than a bare scalar -- there is
    # no shared snapshot timestamp to borrow the way market features have.
    timestamped: Dict[str, Dict[str, Any]] = {}
    for collected in (
        _safe("collect_earnings", _collect_earnings, sym, asof_ts=decision_ts) or {},
        _safe("collect_fundamentals", _collect_fundamentals, sym, asof_ts=decision_ts) or {},
        _safe("collect_news", _collect_news, sym, asof_ts=decision_ts) or {},
        _safe("collect_leadership_change", _collect_leadership_change, sym, asof_ts=decision_ts) or {},
    ):
        timestamped.update(collected)

    evidence: Dict[str, Any] = {
        "_asof_ts": decision_ts,
        RECORDS_KEY: {},
        CONFLICTS_KEY: [],
        UNSUPPORTED_KEY: [],
    }
    for signal, value in raw.items():
        source_ts = (
            price_source_ts
            if signal in price_action and signal not in feature_signals
            else feature_source_ts
        )
        record = _record(
            source="prediction_feature_snapshot" if market_ctx is not None else "live_market_snapshot",
            source_timestamp=source_ts,
            observation_timestamp=source_ts,
            reporting_period="issue_time",
            unit="ratio" if signal == "relative_volume" else ("USD" if signal == "avg_dollar_volume" else "percent"),
            basis="point_in_time",
            actual_value=value,
            methodology=f"Checklist projection for {signal}",
            request_timestamp=decision_ts,
            provenance={
                "symbol": sym,
                "source_timestamp": source_ts,
                "feature_asof_ts": feature_source_ts,
                "price_as_of_ts": price_source_ts,
            },
        )
        scalar, reconciled, bounded = _confirmed_projection(
            signal,
            [record],
            asof_ts=decision_ts,
            max_age_s=24 * 3600,
        )
        evidence[RECORDS_KEY][signal] = bounded
        if scalar is not None:
            evidence[signal] = scalar
        elif reconciled and reconciled.get("confidence_status") == VERIFIED_CONFLICT:
            evidence[CONFLICTS_KEY].append({
                "signal": signal,
                "records": reconciled.get("data_conflict") or [],
            })

    for signal, payload in timestamped.items():
        value = payload.get("value") if isinstance(payload, dict) else None
        source_ts = _epoch(payload.get("_ts")) if isinstance(payload, dict) else None
        record = _record(
            source=SOURCE_BY_SIGNAL.get(signal, "External source"),
            source_timestamp=source_ts,
            observation_timestamp=source_ts,
            reporting_period="issue_time",
            unit="score" if signal in ("news_sentiment", "guidance_direction", "leadership_change_sentiment") else "percent",
            basis="point_in_time",
            actual_value=value,
            methodology=f"Checklist projection for {signal}",
            request_timestamp=decision_ts,
            provenance={"symbol": sym, "source_timestamp": source_ts},
        )
        scalar, reconciled, bounded = _confirmed_projection(
            signal,
            [record],
            asof_ts=decision_ts,
            max_age_s=_EXTERNAL_SIGNAL_MAX_AGE_S.get(signal, 7 * 86400),
        )
        evidence[RECORDS_KEY][signal] = bounded
        if scalar is not None:
            evidence[signal] = scalar
        elif reconciled and reconciled.get("confidence_status") == VERIFIED_CONFLICT:
            evidence[CONFLICTS_KEY].append({
                "signal": signal,
                "records": reconciled.get("data_conflict") or [],
            })
    return evidence
