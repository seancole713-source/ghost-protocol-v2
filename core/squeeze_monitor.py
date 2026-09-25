"""
core/squeeze_monitor.py — Watchlist squeeze radar (all 44 symbols)
==================================================================
Telegram alerts for intraday short-squeeze *moments* — the thing the v3 pick
engine is NOT built for (3-day TP/SL holds + regime gates).

Unlike wolf_monitor (WOLF-only, daily-bar volume), this module:
  • Scans the full STOCK_SYMBOLS watchlist every N seconds during RTH
  • Uses time-adjusted relative volume (RVOL) so morning spikes fire early
  • Uses session HIGH vs prior close (catches the move even if price fades)
  • Tags high short-float names when yfinance short data is available

Enable: SQUEEZE_MONITOR_ENABLED=1 (default on)
"""

from __future__ import annotations
from core.quiet import note_suppressed

import asyncio
import logging
import math
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
import threading
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.market_hours import (
    PREMARKET_MINUTES,
    PREMARKET_START_MIN,
    RTH_CLOSE_MIN,
    RTH_MINUTES,
    RTH_OPEN_MIN,
    SESSION_TZ,
    session_hm,
)

from core.squeeze_evidence import (
    CONTRACT, EVIDENCE_FIELDS, BarFetch, MarketSnapshot, evidence_status, observation_ts,
)

from core.yfinance_client import ungated_ticker as _ungated_yf_ticker  # noqa: E402
LOGGER = logging.getLogger("ghost.squeeze")

CHECK_INTERVAL_SEC = int(os.getenv("SQUEEZE_MONITOR_INTERVAL", "60"))
SQUEEZE_PRICE_PCT = float(os.getenv("SQUEEZE_PRICE_PCT", "5.0"))
SQUEEZE_VOL_MULT = float(os.getenv("SQUEEZE_VOL_MULT", "2.5"))
FORMING_PRICE_PCT = float(os.getenv("SQUEEZE_FORMING_PRICE_PCT", "3.0"))

# WATCH tier (detection, not approval): a lower bar that flags "something
# unusual is happening" without implying a trade. High recall lives here; the
# trade gate (SQUEEZE_PRICE_PCT / SQUEEZE_VOL_MULT + confidence) stays high
# precision. A WATCH is visible but never a trade signal.
WATCH_PRICE_PCT = float(os.getenv("SQUEEZE_WATCH_PRICE_PCT", "2.0"))
WATCH_VOL_MULT = float(os.getenv("SQUEEZE_WATCH_VOL_MULT", "1.5"))
# Repeated-signal escalation: N independent WATCH observations over this many
# days promote a symbol to an escalated (human-visible) WATCH.
WATCH_ESCALATE_COUNT = int(os.getenv("SQUEEZE_WATCH_ESCALATE_COUNT", "3"))
WATCH_ESCALATE_WINDOW_DAYS = int(os.getenv("SQUEEZE_WATCH_ESCALATE_WINDOW_DAYS", "5"))


def _squeeze_risk_tag(short_float_pct, days_to_cover) -> str:
    """low/medium/high/extreme from short %-of-float and days-to-cover
    (mirrors core.wolf_context._build_short_data thresholds).
    Canonical location — api/wolf_endpoints.py imports from here (PR #125 audit)."""
    sfp = short_float_pct or 0
    dtc = days_to_cover or 0
    if sfp >= 35 or dtc >= 5:
        return "extreme"
    if sfp >= 25 or dtc >= 3:
        return "high"
    if sfp >= 15 or dtc >= 2:
        return "medium"
    return "low"
FORMING_VOL_MULT = float(os.getenv("SQUEEZE_FORMING_VOL_MULT", "2.0"))
TP_PCT_ACTIVE = float(os.getenv("SQUEEZE_TP_PCT_ACTIVE", "4.0"))
TP_PCT_FORMING = float(os.getenv("SQUEEZE_TP_PCT_FORMING", "2.5"))

# Premarket volume baseline: premarket volume is a small fraction of RTH volume,
# so comparing 3:00 AM volume against the RTH daily average inflates RVOL to
# absurd values (e.g. 156×). A full premarket session is ~this fraction of a
# full RTH day; RVOL during premarket is measured against that premarket pace.
PREMARKET_VOL_FRACTION = float(os.getenv("SQUEEZE_PREMARKET_VOL_FRACTION", "0.05"))

_TIMEOUT = float(os.getenv("PRICE_PROVIDER_TIMEOUT_S", "8.0"))

COOLDOWN_SEC = int(os.getenv("SQUEEZE_ALERT_COOLDOWN", "7200"))
MIN_TELEGRAM_CONFIDENCE = int(os.getenv("SQUEEZE_TELEGRAM_MIN_CONFIDENCE", "75"))
REPRICE_ALERT_PCT = float(os.getenv("SQUEEZE_REPRICE_ALERT_PCT", "1.5"))
_last_alert: Dict[str, float] = {}
_short_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_short_warm_pending: set[asyncio.Task[Any]] = set()
_SHORT_CACHE_TTL = 86400
_SHORT_FAILURE_CACHE_TTL = max(
    300,
    int(os.getenv("SQUEEZE_SHORT_FAILURE_CACHE_TTL_S", "900")),
)
_SHORT_PREWARM_REFRESH_S = max(
    3600,
    int(os.getenv("SQUEEZE_SHORT_PREWARM_REFRESH_S", "14400")),
)
_SHORT_PREWARM_RETRY_S = max(
    300,
    min(3600, int(os.getenv("SQUEEZE_SHORT_PREWARM_RETRY_S", "900"))),
)

# --- Multi-symbol Alpaca bar batching (squeeze breaker-cascade fix) ---------
# The per-symbol scan made ~2 Alpaca bar calls per symbol (30d 1Day for avg
# volume + session 5Min for session volume/VWAP). Across the full watchlist
# that overran Alpaca's 150-call/60s cap, tripped the *primary* breaker, and
# cascaded all price/volume traffic onto yfinance (15/min), which then blew
# too. Alpaca's /v2/stocks/bars endpoint accepts a multi-symbol `symbols=`
# list, so one paginated request covers the whole universe. _batch_fetch_bars()
# prewarms this scan-local store before the per-symbol loop; _fetch_volumes()
# reads from it and falls back to the original per-symbol path on any miss, so
# behavior is preserved when batching is disabled or a symbol is absent.
SQUEEZE_BATCH_BARS = os.getenv("SQUEEZE_BATCH_BARS", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
_BATCH_SYMBOLS_PER_REQ = int(os.getenv("SQUEEZE_BATCH_SYMBOLS_PER_REQ", "50"))
_batch_bars: Dict[str, Dict[str, Any]] = {}
_BATCH_DEADLINE_S = 30.0
_BATCH_MAX_PAGES = 20
_batch_worker_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="squeeze-bars")
_batch_worker_future: Future | None = None
_batch_worker_lock = threading.Lock()
_batch_bars_lock = __import__("threading").Lock()
_last_scan_report: Dict[str, Any] = {
    "ok": False,
    "message": "No scan completed yet",
}
_scan_cache_path = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "squeeze_last_scan.json",
)
_alert_history: List[Dict[str, Any]] = []
_ALERT_HISTORY_MAX = 30
_alert_session_date: Optional[Any] = None

# WATCH-tier escalation state: {SYMBOL: [observation_ts, ...]} across sessions.
# One weak observation is noise; N independent observations over a few sessions
# escalate to a human-visible WATCH. In-memory only (resets on redeploy) — the
# persisted scan report carries the current watch list, and the escalation
# counter is best-effort visibility, not a trade gate.
_watch_observations: Dict[str, List[float]] = {}


def rth_elapsed_fraction(now: Optional[datetime] = None) -> float:
    """Fraction of regular session elapsed (0..1), minimum 1/390 for RVOL."""
    now_ct, hm = session_hm(now)
    if now_ct.weekday() >= 5:
        return 1.0
    if hm < RTH_OPEN_MIN:
        # Premarket: fraction of the premarket session elapsed (3:00–8:30 AM CT),
        # NOT a fraction of RTH. Using RTH_MINUTES here made the fraction ~0 at
        # 3:00 AM, so RVOL compared premarket volume against a near-zero expected
        # volume and exploded to 156× (forensic: premarket RVOL correction).
        return max(1.0 / PREMARKET_MINUTES, (hm - PREMARKET_START_MIN) / PREMARKET_MINUTES)
    if hm >= RTH_CLOSE_MIN:
        return 1.0
    elapsed = hm - RTH_OPEN_MIN
    return max(elapsed / RTH_MINUTES, 1.0 / RTH_MINUTES)


def compute_rvol(session_volume: float, avg_daily_volume: float, elapsed_frac: float, *, premarket: bool = False) -> float:
    """Time-adjusted relative volume: vol so far / expected vol by this point in session.

    During premarket, the expected volume is scaled by PREMARKET_VOL_FRACTION so
    premarket volume is measured against a premarket pace, not the RTH daily
    average (which would understate expected volume and inflate RVOL).
    """
    if avg_daily_volume <= 0 or session_volume <= 0:
        return 0.0
    baseline = avg_daily_volume * PREMARKET_VOL_FRACTION if premarket else avg_daily_volume
    expected = baseline * max(elapsed_frac, 1.0 / RTH_MINUTES)
    return session_volume / expected if expected > 0 else 0.0


def evaluate_squeeze_signal(
    peak_move_pct: float,
    current_move_pct: float,
    rvol: float,
    *,
    short_risk: Optional[str] = None,
) -> Optional[str]:
    """
    Return alert kind or None.
      squeeze_active — peak +5% and RVOL ≥ threshold (classic squeeze)
      squeeze_forming — +3% / 2× RVOL, or high-short names at slightly lower bar
    """
    move = max(peak_move_pct, current_move_pct)
    high_short = short_risk in ("high", "extreme")

    if move >= SQUEEZE_PRICE_PCT and rvol >= SQUEEZE_VOL_MULT:
        return "squeeze_active"
    forming_move = FORMING_PRICE_PCT if not high_short else max(FORMING_PRICE_PCT - 0.5, 2.5)
    forming_vol = FORMING_VOL_MULT if not high_short else max(FORMING_VOL_MULT - 0.3, 1.8)
    if move >= forming_move and rvol >= forming_vol:
        return "squeeze_forming"
    return None


def prefilter_candidate(peak_move_pct: float, current_move_pct: float, rvol: float) -> bool:
    """Cheap gate before short-interest fetch (avoids 44× yfinance per cycle)."""
    move = max(peak_move_pct, current_move_pct)
    if move < 2.0 or rvol < 1.5:
        return False
    return True


def evaluate_watch_signal(
    peak_move_pct: float,
    current_move_pct: float,
    rvol: float,
) -> bool:
    """High-recall anomaly detection, independent of the trade gate.

    Returns True when a symbol clears the WATCH bar (unusual move OR volume)
    but is NOT a trade candidate. This is the "detect everything unusual" tier:
    a WATCH is visible and escalates on repetition, but never fires a trade.
    """
    move = max(peak_move_pct, current_move_pct)
    return move >= WATCH_PRICE_PCT or rvol >= WATCH_VOL_MULT


def _record_watch_observation(
    symbol: str,
    peak_move_pct: float,
    current_move_pct: float,
    rvol: float,
) -> Dict[str, Any]:
    """Record a WATCH observation and return its (possibly escalated) row.

    Repeated independent observations over WATCH_ESCALATE_WINDOW_DAYS escalate
    the symbol to `escalated=True`, which the UI surfaces as a human-visible
    WATCH. This is detection visibility, never a trade signal.
    """
    sym = symbol.upper()
    now = time.time()
    cutoff = now - WATCH_ESCALATE_WINDOW_DAYS * 86400
    obs = [t for t in _watch_observations.get(sym, []) if t >= cutoff]
    obs.append(now)
    _watch_observations[sym] = obs
    escalated = len(obs) >= WATCH_ESCALATE_COUNT
    return {
        "symbol": sym,
        "peak_move_pct": round(peak_move_pct, 2),
        "current_move_pct": round(current_move_pct, 2),
        "rvol": round(rvol, 2),
        "observations": len(obs),
        "escalated": escalated,
        "first_observed_ts": int(obs[0]),
        "last_observed_ts": int(now),
    }


def get_squeeze_status() -> Dict[str, Any]:
    """Last completed watchlist scan snapshot (for /api/squeeze/status)."""
    _ensure_scan_cache_loaded()
    return dict(_last_scan_report)


def _ensure_scan_cache_loaded() -> None:
    """Load persisted last scan when in-memory state is empty (e.g. after deploy overnight)."""
    global _last_scan_report
    if _last_scan_report.get("status") == "complete" and _last_scan_report.get("ts"):
        return
    if not os.path.isfile(_scan_cache_path):
        return
    try:
        import json

        with open(_scan_cache_path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if isinstance(cached, dict) and cached.get("ts"):
            _last_scan_report = cached
    except Exception as exc:
        LOGGER.debug("[SqueezeMonitor] load scan cache: %s", exc)


def _persist_scan_report(report: Dict[str, Any]) -> None:
    if report.get("status") != "complete":
        return
    try:
        import json

        os.makedirs(os.path.dirname(_scan_cache_path), exist_ok=True)
        payload = {
            k: report.get(k)
            for k in (
                "ok", "ts", "session", "symbols", "fetch_ok", "fetch_fail",
                "fetch_failed_symbols",
                "no_intraday_print", "no_intraday_print_symbols",
                "fetch_skipped", "fetch_skipped_symbols",
                "invalid_baseline", "invalid_baseline_symbols", "invalid_quote", "invalid_quote_symbols",
                "stale_quote", "stale_quote_symbols", "data_status_by_symbol", "data_contract",
                "picks", "candidates", "watches", "leaders", "duration_ms", "status", "elapsed_frac",
            )
        }
        with open(_scan_cache_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception as exc:
        LOGGER.debug("[SqueezeMonitor] persist scan cache: %s", exc)


def squeeze_confidence(
    peak_move_pct: float,
    rvol: float,
    *,
    short_risk: Optional[str] = None,
    kind: str = "squeeze_forming",
) -> int:
    """0–95 squeeze confidence from move, RVOL, and short-float context.

    Capped at 95 — 100% confidence is never credible in any prediction system.
    Extreme short risk adds squeeze potential but also signals fragility, so the
    short-risk bonus is halved for "extreme" to avoid overconfidence on the
    riskiest names.

    PR #161 recalibration: RVOL was overweighted (up to 30 pts) relative to
    move (up to 40 pts). The daily log showed 90% confidence alerts losing
    just as often as 70% ones — RVOL is participation noise, not edge.
    Reduced RVOL ceiling 30→18 and increased move ceiling 40→50 so the
    score better reflects actual price action quality.
    """
    move = max(0.0, peak_move_pct)
    # Move is the primary signal — price already moved, squeeze is real
    move_pts = min(50.0, move * 5.0)
    # RVOL confirms participation but is NOT edge — reduced weight
    rvol_pts = min(18.0, max(0.0, (rvol - 1.0) * 6.0))
    short_pts = {"extreme": 8.0, "high": 10.0, "medium": 8.0, "low": 3.0}.get(
        short_risk or "", 0.0,
    )
    kind_pts = 8.0 if kind == "squeeze_active" else 0.0
    return int(round(min(95.0, max(0.0, move_pts + rvol_pts + short_pts + kind_pts))))


def squeeze_trade_levels(
    buy_price: float,
    session_high: float,
    kind: str,
) -> Tuple[float, float]:
    """Return (buy, sell) — sell targets session high when still above TP."""
    buy = round(buy_price, 2)
    tp_pct = TP_PCT_ACTIVE if kind == "squeeze_active" else TP_PCT_FORMING
    tp_sell = round(buy * (1.0 + tp_pct / 100.0), 2)
    high_sell = round(session_high, 2) if session_high > buy else tp_sell
    sell = max(tp_sell, high_sell) if high_sell > buy else tp_sell
    return buy, round(sell, 2)


def format_squeeze_alert(
    symbol: str,
    kind: str,
    metrics: Dict[str, Any],
    rvol: float,
    short_ctx: Dict[str, Any],
) -> str:
    """Telegram body for radar-only squeeze alerts.

    These are NOT Ghost v3 high-conviction trades. The label is intentionally
    explicit so low/medium-confidence radar cannot be mistaken for a real pick.
    """
    buy, sell = squeeze_trade_levels(metrics["price"], metrics["session_high"], kind)
    conf = squeeze_confidence(
        metrics["peak_move_pct"],
        rvol,
        short_risk=short_ctx.get("squeeze_risk"),
        kind=kind,
    )
    return (
        f"📡 SQUEEZE RADAR — {symbol.upper()}\n"
        f"Radar only — not a Ghost gated trade\n"
        f"Buy: ${buy:.2f}\n"
        f"Sell: ${sell:.2f}\n"
        f"Confidence: {conf}%"
    )


def _squeeze_alert_key(symbol: str, kind: str, buy: float, sell: float, conf: int) -> str:
    """Stable de-dupe key with coarse price buckets.

    A same-symbol radar alert should not repeat every scan, but it may re-alert
    when the setup materially reprices. Bucketing by REPRICE_ALERT_PCT gives that
    balance without spamming tiny penny moves.
    """
    pct = max(0.1, REPRICE_ALERT_PCT) / 100.0
    # Relative price buckets: nearby prices stay in the same bucket, but a
    # material move (e.g. >~1.5%) produces a new key and can re-alert.
    def _bucket(px: float) -> int:
        px = max(0.01, float(px))
        return int(math.floor(math.log(px) / math.log(1.0 + pct)))
    buy_bucket = _bucket(buy)
    sell_bucket = _bucket(sell)
    conf_bucket = int(conf // 10) * 10
    return f"squeeze:{symbol.upper()}:{kind}:b{buy_bucket}:s{sell_bucket}:c{conf_bucket}"


def candidate_to_pick(
    symbol: str,
    kind: str,
    metrics: Dict[str, Any],
    rvol: float,
    short_ctx: Dict[str, Any],
) -> Dict[str, Any]:
    """Telegram-aligned official-radar row; advisory/nonofficial input is rejected."""
    from config.symbols import V3_WHITELIST_STOCKS

    if (
        symbol.upper() not in V3_WHITELIST_STOCKS
        or metrics.get("advisory_only") is True
        or metrics.get("decision_eligible") is False
    ):
        raise ValueError("official squeeze candidate required")
    if evidence_status(metrics) != "ready":
        raise ValueError("fresh complete squeeze evidence required")
    from core.squeeze_scorecard import build_scorecard_row

    row = build_scorecard_row(symbol, metrics, rvol, short_ctx, kind=kind)
    conf = squeeze_confidence(
        metrics["peak_move_pct"],
        rvol,
        short_risk=short_ctx.get("squeeze_risk"),
        kind=kind,
    )
    row["confidence_pct"] = conf
    row["short_risk"] = short_ctx.get("squeeze_risk")
    row["message"] = format_squeeze_alert(symbol, kind, metrics, rvol, short_ctx)
    return row


def get_squeeze_picks() -> Dict[str, Any]:
    """Active squeeze picks from the latest scan + recent Telegram alerts."""
    from core.market_hours import is_us_extended_hours, next_radar_resume_label
    from core.squeeze_scorecard import scorecard_legend
    from core.squeeze_live_drift import build_live_drift_board, enrich_pick_rows, first_alert_buy_map, live_price_map, attach_live_drift

    _ensure_scan_cache_loaded()
    st = dict(_last_scan_report)
    picks = [dict(row) for row in (st.get("picks") or st.get("candidates") or [])]
    leaders = [dict(row) for row in (st.get("leaders") or [])]
    watches = [dict(row) for row in (st.get("watches") or [])]
    alerts = list(_alert_history)
    picks = enrich_pick_rows(picks, alerts, leaders)
    alert_map = first_alert_buy_map(alerts)
    live_map = live_price_map(picks, leaders)
    enriched_alerts: List[Dict[str, Any]] = []
    for a in alerts:
        item = dict(a)
        sym = (item.get("symbol") or "").upper()
        live = live_map.get(sym) or item.get("price") or item.get("buy")
        if live is not None:
            try:
                item["live_price"] = round(float(live), 4)
            except (TypeError, ValueError):
                note_suppressed()
        alert_buy = alert_map.get(sym)
        if alert_buy is not None and item.get("live_price") is not None:
            attach_live_drift(item, alert_buy=float(alert_buy), live_price=item["live_price"])
        enriched_alerts.append(item)
    enabled = os.getenv("SQUEEZE_MONITOR_ENABLED", "1") == "1"
    radar_active = enabled and is_us_extended_hours()
    last_ts = st.get("ts")
    scan_ts = observation_ts(last_ts)
    scan_age = time.time() - scan_ts if scan_ts is not None else None
    snapshot_stale = not radar_active or scan_age is None or scan_age < 0 or scan_age > max(180, 3 * CHECK_INTERVAL_SEC)
    for row in picks + leaders + watches:
        row["snapshot_stale"] = snapshot_stale
        ts = observation_ts(row.get("price_as_of_ts"))
        row["price_age_s"] = time.time() - ts if ts else None
    try:
        from core.broad_market_context import get_broad_market_context
        market_context = get_broad_market_context()
    except Exception:
        market_context = {
            "ok": False, "status": "unavailable", "error": "snapshot_unavailable",
            "observations": [], "display_only": True, "decision_eligible": False,
        }
    try:
        from core.external_context_ledger import recent_external_discoveries
        external_discovery = recent_external_discoveries(limit=50)
    except Exception:
        external_discovery = {
            "ok": False, "status": "unavailable", "items": [], "count": 0,
            "advisory_only": True, "decision_eligible": False,
        }
    try:
        from core.external_context_ledger import latest_external_radar_snapshot
        external_radar = latest_external_radar_snapshot()
    except Exception:
        external_radar = {
            "ok": False, "status": "unavailable", "items": [],
            "advisory_only": True, "decision_eligible": False,
        }
    return {
        "scan_ok": bool(st.get("ok") and st.get("status") == "complete" and not snapshot_stale),
        "data_contract": st.get("data_contract"),
        "data_degraded": snapshot_stale or any((st.get(key) or 0) > 0 for key in (
            "fetch_fail", "fetch_skipped", "invalid_baseline", "invalid_quote", "stale_quote",
        )),
        "data_status_by_symbol": st.get("data_status_by_symbol", {}),
        **{key: st.get(key) for key in ("invalid_baseline", "invalid_quote", "stale_quote")},
        **{key + "_symbols": st.get(key + "_symbols", []) for key in ("invalid_baseline", "invalid_quote", "stale_quote")},
        "picks": picks,
        "pick_count": len(picks),
        "watches": watches,
        "watch_count": len(watches),
        "external_discovery": external_discovery,
        "external_radar": external_radar,
        "broad_market_context": market_context,
        "alert_history": enriched_alerts,
        "live_drift": build_live_drift_board(alerts, picks, leaders),
        "last_scan_ts": last_ts,
        "last_scan_status": st.get("status"),
        "last_scan_session": st.get("session"),
        "fetch_ok": st.get("fetch_ok"),
        "fetch_fail": st.get("fetch_fail"),
        "fetch_failed_symbols": list(st.get("fetch_failed_symbols") or []),
        # Only complete successful empty intraday responses count as no-print.
        "no_intraday_print": st.get("no_intraday_print"),
        "no_intraday_print_symbols": list(st.get("no_intraday_print_symbols") or []),
        "fetch_skipped": st.get("fetch_skipped"),
        "fetch_skipped_symbols": list(st.get("fetch_skipped_symbols") or []),
        # PR #137 (audit): these counts belong to the snapshot at last_scan_ts,
        # NOT to right now — one degraded cycle (e.g. during a breaker trip)
        # persists here until the next scan overwrites it, so this surface can
        # legitimately disagree with newer scan log lines.
        "fetch_note": "Counts describe evidence at last_scan_ts. fetch_ok means complete, recent same-feed bars, not a live trade tick. No-print requires a successful empty response; IEX does not cover all trading.",
        "symbols": st.get("symbols"),
        "duration_ms": st.get("duration_ms"),
        "leaders": leaders,
        "scorecard": scorecard_legend(),
        "radar_active": radar_active,
        "radar_resume_ct": next_radar_resume_label(),
        "snapshot_stale": snapshot_stale,
        "scan_age_s": scan_age,
    }


def _retain_short_warm_task(task: asyncio.Task[Any]) -> None:
    """Keep a timed-out vendor thread observable until it exits."""
    _short_warm_pending.add(task)

    def consume(done: asyncio.Task[Any]) -> None:
        _short_warm_pending.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            LOGGER.debug("[SqueezeMonitor] late short-cache task: %s", exc)

    task.add_done_callback(consume)


async def prewarm_short_cache() -> bool:
    """Background: warm short-interest cache before RTH (Finviz/yfinance, one symbol at a time)."""
    from config.symbols import get_edge_set
    from core.market_hours import is_us_extended_hours

    # Stay below the dedicated 12 logical-call/minute short-data budget.
    delay = float(os.getenv("SQUEEZE_SHORT_PREWARM_DELAY_S", "5.5"))
    timeout_s = max(
        3.0,
        min(30.0, float(os.getenv("SQUEEZE_SHORT_PREWARM_TIMEOUT_S", "12"))),
    )
    pending_limit = max(
        1,
        min(32, int(os.getenv("SQUEEZE_SHORT_PREWARM_PENDING_MAX", "8"))),
    )
    symbols = sorted(get_edge_set())
    complete = True
    LOGGER.info("[SqueezeMonitor] Short-cache prewarm — %s symbols", len(symbols))
    for sym in symbols:
        if not is_us_extended_hours():
            return False
        if len(_short_warm_pending) >= pending_limit:
            LOGGER.warning(
                "[SqueezeMonitor] short-cache pending limit reached (%s); retrying later",
                pending_limit,
            )
            return False
        try:
            # Keep the blocking vendor client off the event loop. Await one at
            # a time, but do not let one broken ticker strand the entire daily
            # queue. A timed-out vendor thread may finish and populate the cache
            # later; the bounded default executor prevents event-loop blockage.
            task = asyncio.create_task(asyncio.to_thread(_short_context, sym))
            done, _ = await asyncio.wait(
                {task},
                timeout=timeout_s,
            )
            if task not in done:
                complete = False
                _retain_short_warm_task(task)
                LOGGER.warning(
                    "[SqueezeMonitor] short-cache timeout %s (%.0fs)",
                    sym,
                    timeout_s,
                )
            else:
                context = task.result()
                if not _short_context_useful(context):
                    complete = False
        except Exception as exc:
            complete = False
            LOGGER.debug("[SqueezeMonitor] prewarm %s: %s", sym, exc)
        await asyncio.sleep(delay)
    return complete


async def maintain_short_cache() -> None:
    """Keep the daily warmer alive across closed-market process starts.

    The previous one-shot task returned immediately when a deploy happened
    outside extended hours and was never recreated the next session. This loop
    waits for the market window and retries only every four hours by default;
    successful entries remain cached for a day, so retries primarily revisit
    transient provider failures.
    """
    from core.market_hours import is_us_extended_hours

    while True:
        try:
            market_open = is_us_extended_hours()
        except Exception as exc:
            LOGGER.warning(
                "[SqueezeMonitor] market-window check failed: %s",
                str(exc)[:120],
            )
            market_open = False
        if market_open:
            try:
                completed = await prewarm_short_cache()
            except Exception as exc:
                LOGGER.warning(
                    "[SqueezeMonitor] short-cache maintenance failed: %s",
                    str(exc)[:120],
                )
                completed = False
            await asyncio.sleep(
                _SHORT_PREWARM_REFRESH_S
                if completed is not False
                else _SHORT_PREWARM_RETRY_S
            )
        else:
            await asyncio.sleep(300)


async def start_squeeze_monitor() -> None:
    enabled = os.getenv("SQUEEZE_MONITOR_ENABLED", "1") == "1"
    if not enabled:
        LOGGER.info("[SqueezeMonitor] Disabled by SQUEEZE_MONITOR_ENABLED=0")
        return

    LOGGER.info(
        "[SqueezeMonitor] Starting — watchlist scan every %ss "
        "(active: +%.1f%% & %.1fx RVOL)",
        CHECK_INTERVAL_SEC,
        SQUEEZE_PRICE_PCT,
        SQUEEZE_VOL_MULT,
    )
    if os.getenv("SQUEEZE_SHORT_PREWARM", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        asyncio.create_task(maintain_short_cache())
    _ensure_scan_cache_loaded()
    while True:
        try:
            await _run_watchlist_scan()
        except Exception as exc:
            LOGGER.error("[SqueezeMonitor] scan failed: %s", exc, exc_info=False)
            global _last_scan_report
            _last_scan_report = {
                "ok": False,
                "ts": int(time.time()),
                "status": "error",
                "error": str(exc)[:200],
            }
        # P3 (audit): degraded mode — double scan interval when APIs are down
        _interval = CHECK_INTERVAL_SEC
        try:
            from core.degraded_mode import degraded_squeeze_interval_mult
            _mult = degraded_squeeze_interval_mult()
            if _mult > 1.0:
                _interval = int(CHECK_INTERVAL_SEC * _mult)
        except Exception:
            note_suppressed()
        await asyncio.sleep(_interval)


def _reset_alert_history_if_new_session() -> None:
    """Clear in-memory Telegram alert list at start of each CT calendar day."""
    global _alert_history, _alert_session_date
    from core.market_hours import session_hm

    today = session_hm()[0].date()
    if _alert_session_date != today:
        _alert_history = []
        _alert_session_date = today


async def _run_watchlist_scan() -> None:

    from core.market_hours import is_us_extended_hours, is_us_premarket, is_us_rth

    if not is_us_extended_hours():
        return

    _reset_alert_history_if_new_session()

    from config.symbols import get_edge_set

    symbols = sorted(get_edge_set())
    elapsed = rth_elapsed_fraction()
    _premarket = is_us_premarket()
    t0 = time.time()
    report: Dict[str, Any] = {
        "ok": True,
        "ts": int(time.time()),
        "session": "rth" if is_us_rth() else ("premarket" if is_us_premarket() else "extended"),
        "symbols": len(symbols),
        "fetch_ok": 0,
        # A complete empty response, a failed request and invalid evidence are
        # distinct outcomes. IEX absence never proves market-wide inactivity.
        "fetch_fail": 0,
        "fetch_failed_symbols": [],
        "no_intraday_print": 0,
        "no_intraday_print_symbols": [],
        "coverage_note": "No usable intraday print on the selected feed is not proof of no trading market-wide; IEX is limited coverage.",
        "fetch_skipped": 0,
        "fetch_skipped_symbols": [],
        "candidates": [],
        "watches": [],
        "alerts_sent": 0,
        "duration_ms": 0,
        "elapsed_frac": round(elapsed, 4),
        "status": "running",
    }
    global _last_scan_report, _batch_worker_future
    _last_scan_report = dict(report)

    fetch_timeout = float(os.getenv("SQUEEZE_FETCH_TIMEOUT_S", "18"))
    # Inter-symbol delay for sequential fetches
    fetch_delay = float(os.getenv("SQUEEZE_FETCH_DELAY_S", "0.3"))
    # Prewarm the whole watchlist's bars in a couple of multi-symbol requests so
    # the per-symbol loop reads from cache instead of hammering Alpaca (which
    # tripped the shared breaker and cascaded price traffic onto yfinance).
    # A successful batch prewarm contains every field the radar needs. The
    # helper owns and clears the shared batch store before any async fallback,
    # preventing Hunter and radar refreshes from corrupting each other's data.
    snapshot = await _async_market_snapshot(symbols)
    metrics_map = snapshot.metrics
    batch_statuses = snapshot.statuses
    report["data_contract"] = CONTRACT
    report["data_status_by_symbol"] = batch_statuses
    for status in ("invalid_baseline", "invalid_quote", "stale_quote"):
        report[status] = 0
        report[status + "_symbols"] = []
    fallback_symbols = [sym for sym in symbols if sym not in batch_statuses]
    fallback_attempted: set = set()
    for sym in fallback_symbols:
        with _batch_worker_lock:
            if _batch_worker_future is not None and not _batch_worker_future.done():
                break  # timed-out prior work remains owned; never queue behind it
            _batch_worker_future = _batch_worker_pool.submit(_single_market_snapshot, sym)
            future = _batch_worker_future
        fallback_attempted.add(sym)
        try:
            fallback = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)), timeout=fetch_timeout,
            )
            metrics_map.update(fallback.metrics)
            batch_statuses.update(fallback.statuses)
        except asyncio.TimeoutError:
            LOGGER.warning("[SqueezeMonitor] fetch timeout %s (%.0fs)", sym, fetch_timeout)
            metrics_map[sym] = None
            break
        except Exception as exc:
            LOGGER.debug("[SqueezeMonitor] fetch %s: %s", sym, exc)
            metrics_map[sym] = None
        if fetch_delay > 0:
            await asyncio.sleep(fetch_delay)
    short_ctx_map: Dict[str, Dict[str, Any]] = {}
    for symbol in symbols:
        metrics = metrics_map.get(symbol)
        status = batch_statuses.get(symbol, {}).get("status")
        if metrics:
            status = evidence_status(metrics)
        elif status is None:
            status = "fetch_fail" if symbol in fallback_attempted else "fetch_skipped"
        if status != "ready":
            counter = status if status in ("no_intraday_print", "invalid_baseline", "invalid_quote", "stale_quote", "fetch_skipped") else "fetch_fail"
            report[counter] += 1
            report["fetch_failed_symbols" if counter == "fetch_fail" else counter + "_symbols"].append(symbol)
            report["data_status_by_symbol"].setdefault(symbol, {})["status"] = status
            continue
        metrics["quote_status"] = "fresh_bar"
        metrics["price_age_s"] = round(time.time() - observation_ts(metrics["price_as_of_ts"]), 3)
        report["fetch_ok"] += 1
        rvol = compute_rvol(metrics["session_volume"], metrics["avg_daily_volume"], elapsed, premarket=_premarket)
        peak_pct = metrics["peak_move_pct"]
        current_pct = metrics["current_move_pct"]

        report.setdefault("leaders", []).append({
            **{key: metrics.get(key) for key in EVIDENCE_FIELDS},
            "symbol": symbol,
            "peak_move_pct": round(peak_pct, 2),
            "current_move_pct": round(current_pct, 2),
            "rvol": round(rvol, 2),
            "price": round(float(metrics["price"]), 2),
        })

        kind = evaluate_squeeze_signal(peak_pct, current_pct, rvol, short_risk=None)
        short_ctx: Dict[str, Any] = {}
        if not kind and prefilter_candidate(peak_pct, current_pct, rvol):
            short_ctx = _cached_short_context(symbol)
            short_ctx_map[symbol] = short_ctx
            kind = evaluate_squeeze_signal(
                peak_pct, current_pct, rvol, short_risk=short_ctx.get("squeeze_risk"),
            )
        elif kind:
            short_ctx = _cached_short_context(symbol)
            short_ctx_map[symbol] = short_ctx

        # WATCH tier: high-recall anomaly detection, independent of the trade
        # gate. A symbol that clears the WATCH bar but is NOT a trade candidate
        # stays visible as an escalating WATCH (never a trade signal).
        if not kind and evaluate_watch_signal(peak_pct, current_pct, rvol):
            watch = _record_watch_observation(symbol, peak_pct, current_pct, rvol)
            watch.update({key: metrics.get(key) for key in EVIDENCE_FIELDS})
            report["watches"].append(watch)
            try:
                from core.explosion_benchmark import record_observation
                record_observation(symbol, price=metrics.get("price"), kind="watch")
            except Exception:
                note_suppressed()

        if kind:
            pick = candidate_to_pick(symbol, kind, metrics, rvol, short_ctx)
            report["candidates"].append(pick)
            try:
                from core.squeeze_outcomes import record_squeeze_prediction

                record_squeeze_prediction(pick, source="candidate")
            except Exception:
                note_suppressed()
            try:
                from core.explosion_benchmark import record_observation
                record_observation(
                    symbol, price=metrics.get("price"), kind="candidate",
                    confidence_pct=pick.get("confidence_pct"),
                )
            except Exception:
                note_suppressed()
            if _maybe_alert(symbol, kind, metrics, rvol, short_ctx):
                report["alerts_sent"] += 1
                alerted_at = int(time.time())
                _alert_history.insert(0, {**pick, "alerted_at": alerted_at})
                del _alert_history[_ALERT_HISTORY_MAX:]
                try:
                    from core.squeeze_outcomes import record_squeeze_prediction

                    record_squeeze_prediction(
                        {**pick, "alerted_at": alerted_at},
                        source="telegram",
                        alerted_at=alerted_at,
                    )
                except Exception:
                    note_suppressed()
                try:
                    from core.explosion_benchmark import record_observation
                    record_observation(
                        symbol, price=metrics.get("price"), kind="alert",
                        confidence_pct=pick.get("confidence_pct"),
                        observed_at=alerted_at,
                    )
                except Exception:
                    note_suppressed()
    report["picks"] = list(report["candidates"])
    _enrich_watches_with_quorum(report.get("watches") or [])
    from core.squeeze_scorecard import build_scorecard_row

    leaders = report.get("leaders") or []
    leaders.sort(key=lambda x: (x.get("peak_move_pct") or 0, x.get("rvol") or 0), reverse=True)
    enriched: List[Dict[str, Any]] = []
    for leader in leaders[:8]:
        sym = leader["symbol"]
        metrics = metrics_map.get(sym)
        if not metrics:
            enriched.append(leader)
            continue
        short_ctx = short_ctx_map.get(sym) or _cached_short_context(sym)
        enriched.append(
            build_scorecard_row(
                sym,
                metrics,
                float(leader["rvol"]),
                short_ctx,
                kind="squeeze_forming",
            )
        )
    report["leaders"] = enriched

    report["duration_ms"] = int((time.time() - t0) * 1000)
    report["status"] = "complete"
    _last_scan_report = report
    _persist_scan_report(report)
    LOGGER.info(
        "[SqueezeMonitor] scan ok=%s no_print=%s fail=%s skipped=%s candidates=%s ms=%s",
        report["fetch_ok"],
        report["no_intraday_print"],
        report["fetch_fail"],
        report["fetch_skipped"],
        len(report["candidates"]),
        report["duration_ms"],
    )


def _watch_quorum_priority(watch: Dict[str, Any]) -> tuple[float, float, int, str]:
    """Rank advisory WATCH corroboration without changing WATCH list order."""
    move = max(
        float(watch.get("peak_move_pct") or 0.0),
        float(watch.get("current_move_pct") or 0.0),
    )
    rvol = float(watch.get("rvol") or 0.0)
    move_strength = move / max(WATCH_PRICE_PCT, 0.01)
    volume_strength = rvol / max(WATCH_VOL_MULT, 0.01)
    anomaly_strength = max(move_strength, volume_strength)
    return (
        0.0 if watch.get("escalated") is True else 1.0,
        -anomaly_strength,
        -int(watch.get("observations") or 0),
        str(watch.get("symbol") or ""),
    )


def _enrich_watches_with_quorum(watches: List[Dict[str, Any]]) -> None:
    """Attach bounded advisory corroboration after all trade decisions are final.

    The strongest anomalies receive the bounded verification budget instead of
    whichever symbols sort first alphabetically. Results are attached in place,
    preserving display order and never changing classification, confidence,
    alerts, or wallet inputs.
    """
    budget = max(0, int(os.getenv("SQUEEZE_QUORUM_WATCH_BUDGET", "3")))
    prioritized = sorted(watches, key=_watch_quorum_priority)
    selected = prioritized[:budget]
    for watch in watches:
        if not any(watch is selected_watch for selected_watch in selected):
            watch["quorum"] = {
                "verdict": "deferred",
                "advisory_only": True,
                "reason": "per_scan_budget",
            }
            continue
        try:
            from core.data_quorum import evaluate_quorum
            watch["quorum"] = evaluate_quorum(watch["symbol"], use_cache=True)
        except Exception as exc:
            watch["quorum"] = {
                "verdict": "unavailable",
                "advisory_only": True,
                "reason": str(exc)[:100],
            }


def _short_context_from_finviz(symbol: str) -> Dict[str, Any]:
    """Finviz scrape fallback when Yahoo short data is unavailable (429 / Alpaca-only mode)."""
    out: Dict[str, Any] = {
        "short_float_pct": None,
        "days_to_cover": None,
        "squeeze_risk": None,
    }
    try:
        from core.wolf_context import _fetch_finviz

        fv = _fetch_finviz(symbol.upper())
        sf = fv.get("short_float")
        dtc = fv.get("days_to_cover")
        if sf is not None:
            out["short_float_pct"] = round(float(sf), 2)
        if dtc is not None:
            out["days_to_cover"] = round(float(dtc), 2)
    except Exception as exc:
        LOGGER.debug("[SqueezeMonitor] finviz short %s: %s", symbol, exc)
    return out


def _short_context(symbol: str) -> Dict[str, Any]:
    sym = symbol.upper()
    cached = _short_cache.get(sym)
    if cached and (time.time() - cached[0]) < _short_cache_ttl(cached[1]):
        return cached[1]
    out: Dict[str, Any] = {
        "short_float_pct": None,
        "days_to_cover": None,
        "squeeze_risk": None,
        # Squeeze Hunter fuel fields (free yfinance data).
        "shares_short": None,
        "shares_short_prior_month": None,
        "short_interest_change_pct": None,
        "float_shares": None,
        "shares_outstanding": None,
        "institutional_ownership_pct": None,
    }
    if _yf_short_enabled():
        from core.circuit_breaker import _yfinance_short_cb
        if _yfinance_short_cb.allow():
            try:
                import yfinance as yf

                info = _ungated_yf_ticker(sym).info or {}
                sf = info.get("shortPercentOfFloat")
                dtc = info.get("shortRatio")
                if sf is not None:
                    value = float(sf) * 100
                    if 0 <= value <= 100:
                        out["short_float_pct"] = round(value, 2)
                if dtc is not None:
                    value = float(dtc)
                    if 0 <= value <= 60:
                        out["days_to_cover"] = round(value, 2)
                # Squeeze Hunter fuel fields (free).
                ss = info.get("sharesShort")
                ssp = info.get("sharesShortPriorMonth")
                if ss is not None:
                    out["shares_short"] = int(ss)
                if ssp is not None:
                    out["shares_short_prior_month"] = int(ssp)
                if ss is not None and ssp:
                    try:
                        out["short_interest_change_pct"] = round(
                            (float(ss) - float(ssp)) / float(ssp) * 100, 2
                        )
                    except (TypeError, ValueError, ZeroDivisionError):
                        out["short_interest_change_pct"] = None
                fs = info.get("floatShares")
                so = info.get("sharesOutstanding")
                if fs is not None:
                    out["float_shares"] = int(fs)
                if so is not None:
                    out["shares_outstanding"] = int(so)
                inst = info.get("heldPercentInstitutions")
                if inst is not None:
                    out["institutional_ownership_pct"] = round(float(inst) * 100, 2)
                _yfinance_short_cb.record_success()
            except Exception as exc:
                _yfinance_short_cb.record_failure()
                LOGGER.debug("[SqueezeMonitor] yfinance short %s: %s", sym, exc)
    if out["short_float_pct"] is None and out["days_to_cover"] is None:
        # Merge Finviz fallback into the existing dict — do NOT replace it, or
        # we discard the free yfinance fuel fields (float_shares, institutional
        # ownership, shares short, SI change) that were already populated.
        fv = _short_context_from_finviz(sym)
        for k, v in fv.items():
            if v is not None:
                out[k] = v
    out["squeeze_risk"] = (
        _squeeze_risk_tag(out["short_float_pct"], out["days_to_cover"])
        if out["short_float_pct"] is not None or out["days_to_cover"] is not None
        else None
    )
    _short_cache[sym] = (time.time(), out)
    return out


def _cached_short_context(symbol: str) -> Dict[str, Any]:
    """Return only already-warmed short data; never block the live scan."""
    cached = _short_cache.get(symbol.upper())
    if cached and (time.time() - cached[0]) < _short_cache_ttl(cached[1]):
        return dict(cached[1])
    return {
        "short_float_pct": None,
        "days_to_cover": None,
        "squeeze_risk": None,
    }


def _short_cache_ttl(context: Dict[str, Any]) -> int:
    """Cache useful evidence for a day, but retry empty provider failures."""
    return _SHORT_CACHE_TTL if _short_context_useful(context) else _SHORT_FAILURE_CACHE_TTL


def _short_context_useful(context: Dict[str, Any]) -> bool:
    """True when a short-data lookup produced at least one decision field."""
    return any(
        context.get(key) is not None
        for key in (
            "short_float_pct",
            "days_to_cover",
            "shares_short",
            "float_shares",
        )
    )


def _yf_short_enabled() -> bool:
    """Separate from price fallback — short-interest can use Yahoo when not rate-limited."""
    if os.getenv("SQUEEZE_YF_SHORT", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return True


def _yf_fallback_enabled() -> bool:
    """Avoid hammering Yahoo during 44-symbol parallel scans (429 kills SPCE/WOLF)."""
    if os.getenv("ALPACA_KEY_ID", "") and os.getenv("SQUEEZE_YF_FALLBACK", "0").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        return False
    return os.getenv("SQUEEZE_YF_FALLBACK", "1").strip().lower() in ("1", "true", "yes", "on")


def _alpaca_headers() -> Optional[dict]:
    key = os.getenv("ALPACA_KEY_ID", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return None
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _alpaca_prev_close(symbol: str) -> Optional[float]:
    """Prior session close from Alpaca 1Day bars (no Yahoo), matched by date.

    Delegates to core.prices: newest-first bars, and only the bar dated the
    expected previous session counts (an ascending limit=5 request returned
    the oldest bars in its 10-day window -- U17).
    """
    headers = _alpaca_headers()
    if not headers:
        return None
    try:
        from core.prices import _alpaca_daily_close, _expected_prev_session
        return _alpaca_daily_close(symbol.upper(), _expected_prev_session(), headers)
    except Exception as exc:
        LOGGER.debug("[SqueezeMonitor] alpaca prev_close %s: %s", symbol, exc)
    return None


def _single_market_snapshot(symbol: str) -> MarketSnapshot:
    """Fallback preserves the same status contract as the full-universe batch."""
    sym = (symbol or "").upper().strip()
    if _alpaca_headers():
        return batched_market_snapshot([sym], force=True)
    metrics = _yf_fetch_metrics(sym) if sym and _yf_fallback_enabled() else None
    status = evidence_status(metrics) if metrics else "fetch_fail"
    return MarketSnapshot(metrics={sym: metrics}, statuses={sym: {"status": status, "reason": "yfinance_fallback"}})


def _sync_fetch_metrics(symbol: str) -> Optional[Dict[str, Any]]:
    """Compatibility view of the single-symbol evidence-aware fallback."""
    return _single_market_snapshot(symbol).metrics.get((symbol or "").upper().strip())


def _vwap_from_bars(bars: List[Dict[str, Any]]) -> Optional[float]:
    num = den = 0.0
    for b in bars:
        v = float(b.get("v", 0) or 0)
        if v <= 0:
            continue
        h = float(b.get("h", 0) or 0)
        low = float(b.get("l", 0) or 0)
        c = float(b.get("c", 0) or 0)
        if h <= 0 and low <= 0 and c <= 0:
            continue
        tp = (h + low + c) / 3.0
        num += tp * v
        den += v
    return round(num / den, 4) if den > 0 else None


def _volumes_from_bars(
    daily_bars: List[Dict[str, Any]],
    intraday_bars: List[Dict[str, Any]],
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(avg_daily_volume, session_volume, session_vwap) from raw bars.

    Identical arithmetic to the per-symbol path in _fetch_volumes: 20-day
    average daily volume, summed session volume, and volume-weighted VWAP.
    """
    avg_vol = session_vol = vwap = None
    vols = [float(b.get("v", 0)) for b in (daily_bars or [])[-20:] if b.get("v")]
    if vols:
        avg_vol = sum(vols) / len(vols)
    if intraday_bars:
        session_vol = sum(float(b.get("v", 0)) for b in intraday_bars if b.get("v"))
        vwap = _vwap_from_bars(intraday_bars)
    return avg_vol, session_vol, vwap


def _metrics_from_batch_bars(symbol: str) -> Optional[Dict[str, Any]]:
    """Build the complete radar snapshot from prewarmed Alpaca bars."""
    return _metrics_from_bar_set(_batch_bars.get(symbol.upper()))


def _metrics_from_bar_set(cached: dict | None) -> Optional[Dict[str, Any]]:
    if not cached:
        return None
    daily = list(cached.get("daily") or [])
    intraday = list(cached.get("intraday") or [])
    if not daily or not intraday:
        return None
    from core.daily_bar_contract import bar_session_date, prior_daily_bars

    stamps = [observation_ts(bar.get("t")) for bar in intraday]
    if any(stamp is None for stamp in stamps) or len(set(stamps)) != len(stamps):
        return None
    intraday.sort(key=lambda bar: observation_ts(bar.get("t")))
    session_date = bar_session_date(intraday[-1].get("t"))
    if session_date is None:
        return None
    if any(bar_session_date(bar.get("t")) != session_date for bar in intraday):
        return None
    daily = prior_daily_bars(daily, session_date)
    if not daily:
        return None
    if len({bar_session_date(row.get("t")) for row in daily}) != len(daily):
        return None
    try:
        for bar in intraday:
            for field in ("c", "h", "l", "v"):
                value = float(bar[field])
                if not math.isfinite(value) or value < 0 or (field != "v" and value == 0):
                    return None
        price = float(intraday[-1].get("c") or 0.0)
        session_high = max(float(bar.get("h") or 0.0) for bar in intraday)
        prior_close = float(daily[-1].get("c") or 0.0)
        avg_vol, session_vol, vwap = _volumes_from_bars(daily, intraday)
        if min(price, session_high, prior_close) <= 0 or not avg_vol or avg_vol <= 0:
            return None
        if not session_vol or session_vol <= 0:
            # Never fabricate session volume (forensic MD-3/SQ-4).
            session_vol = 0.0
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return {
        "price": price,
        "prior_close": prior_close,
        "session_high": session_high,
        "session_volume": float(session_vol),
        "avg_daily_volume": float(avg_vol),
        "vwap": vwap,
        "market_data_contract": CONTRACT,
        "price_as_of_ts": observation_ts(intraday[-1].get("t")),
        "price_timestamp_basis": "5Min_bar_start",
        "price_feed": getattr(cached.get("intraday_result"), "feed", None),
        "daily_feed": getattr(cached.get("daily_result"), "feed", None),
        "intraday_feed": getattr(cached.get("intraday_result"), "feed", None),
        "bars_complete": all(getattr(cached.get(key), "complete", False) for key in ("daily_result", "intraday_result")),
        "volume_basis": "selected_feed_session_vs_prior_daily",
        "price_source": "alpaca_batch_bar",
        "reference_session_date": bar_session_date(daily[-1].get("t")).isoformat(),
        "peak_move_pct": (session_high - prior_close) / prior_close * 100,
        "current_move_pct": (price - prior_close) / prior_close * 100,
    }


def batched_market_snapshot(symbols: List[str], *, force: bool = False) -> MarketSnapshot:
    """Each requested symbol has a status; empty success is not request failure."""
    snapshot = MarketSnapshot()
    if not _batch_bars_lock.acquire(blocking=False):
        snapshot.statuses = {sym: {"status": "fetch_skipped", "reason": "batch_busy"} for sym in symbols}
        return snapshot
    try:
        _batch_fetch_bars(symbols, force=force)
        for symbol in symbols:
            cached = _batch_bars.get(symbol.upper())
            if cached is None:
                continue  # disabled/unconfigured: caller may use a bounded fallback
            daily = cached["daily_result"]
            intraday = cached["intraday_result"]
            detail = {"daily": daily.summary(), "intraday": intraday.summary()}
            metrics = None
            if not daily.complete or not intraday.complete:
                detail["reason"] = daily.reason if not daily.complete else intraday.reason
                status = "fetch_skipped" if (
                    daily.pages == intraday.pages == 0
                    and detail["reason"] in ("rate_limited_batch", "deadline_exceeded")
                ) else "fetch_fail"
            elif not cached["intraday"]:
                status = "no_intraday_print"
                detail["reason"] = "successful_empty_selected_feed"
            else:
                metrics = _metrics_from_batch_bars(symbol)
                status = evidence_status(metrics) if metrics else "invalid_baseline"
                detail["reason"] = status if status != "ready" else None
            detail["status"] = status
            detail["price_as_of_ts"] = (metrics or {}).get("price_as_of_ts")
            detail["reference_session_date"] = (metrics or {}).get("reference_session_date")
            ts = observation_ts(detail["price_as_of_ts"])
            detail["price_age_s"] = round(time.time() - ts, 3) if ts else None
            snapshot.statuses[symbol] = detail
            snapshot.metrics[symbol] = metrics if status == "ready" else None
        return snapshot
    finally:
        _batch_bars.clear()
        _batch_bars_lock.release()


def batched_market_metrics(symbols: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
    """Hunter compatibility view: only validated metrics, never failure-as-evidence."""
    return batched_market_snapshot(symbols).metrics


async def _async_market_snapshot(symbols: List[str]) -> MarketSnapshot:
    """Never block the event loop or abandon ownership of a timed-out thread."""
    global _batch_worker_future
    with _batch_worker_lock:
        if _batch_worker_future is not None and not _batch_worker_future.done():
            return MarketSnapshot(statuses={sym: {"status": "fetch_skipped", "reason": "batch_still_running"} for sym in symbols})
        _batch_worker_future = _batch_worker_pool.submit(batched_market_snapshot, symbols)
        future = _batch_worker_future
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), _BATCH_DEADLINE_S + 2)
    except asyncio.TimeoutError:
        return MarketSnapshot(statuses={sym: {"status": "fetch_fail", "reason": "batch_timeout"} for sym in symbols})


def _alpaca_multi_bars(
    symbols: List[str], *, timeframe: str, start: str, end: str,
    page_limit: int = 10000, deadline: float | None = None,
) -> BarFetch:
    """A complete paginated response is required even for an empty symbol.

    Alpaca pages are symbol-major, so an interrupted page stream cannot prove
    that a later symbol did not trade. Partial responses never escape as success.
    """
    import requests
    from core.prices import _alpaca_bar_feeds, _note_alpaca_feed_status

    headers = _alpaca_headers()
    if not headers or not symbols:
        return BarFetch(reason="not_configured")
    deadline = time.monotonic() + _BATCH_DEADLINE_S if deadline is None else deadline
    syms = ",".join(s.upper() for s in symbols)
    failure = BarFetch(reason="no_feed")
    for feed in _alpaca_bar_feeds():
        out: Dict[str, List[Dict[str, Any]]] = {}
        token = None
        seen: set[str] = set()
        for page in range(_BATCH_MAX_PAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return BarFetch(feed=feed, reason="deadline_exceeded", pages=page)
            url = (
                f"https://data.alpaca.markets/v2/stocks/bars?symbols={syms}"
                f"&timeframe={timeframe}&start={start}&end={end}"
                f"&limit={page_limit}&feed={feed}&sort=asc&adjustment=raw"
            )
            if token:
                url += f"&page_token={quote(token, safe='')}"
            try:
                response = requests.get(url, headers=headers, timeout=min(_TIMEOUT, remaining))
                if response.status_code != 200:
                    _note_alpaca_feed_status(feed, response.status_code)
                    failure = BarFetch(feed=feed, reason=f"http_{response.status_code}", pages=page + 1)
                    if response.status_code == 429:
                        return failure
                    break
                data = response.json()
                if time.monotonic() > deadline:
                    return BarFetch(feed=feed, reason="deadline_exceeded", pages=page + 1)
                if not isinstance(data, dict) or "bars" not in data or not isinstance(data["bars"], (dict, type(None))):
                    return BarFetch(feed=feed, reason="invalid_response", pages=page + 1)
                for sym, bars in (data["bars"] or {}).items():
                    if not isinstance(bars, list) or any(not isinstance(bar, dict) for bar in bars):
                        return BarFetch(feed=feed, reason="invalid_response", pages=page + 1)
                    out.setdefault(sym.upper(), []).extend(bars)
                token = data.get("next_page_token")
                if not token:
                    return BarFetch(out, complete=True, feed=feed, pages=page + 1)
                if not isinstance(token, str) or token in seen:
                    return BarFetch(feed=feed, reason="repeated_page_token", pages=page + 1)
                seen.add(token)
            except Exception as exc:
                LOGGER.debug("[SqueezeMonitor] multi-bars %s feed=%s: %s", timeframe, feed, type(exc).__name__)
                failure = BarFetch(feed=feed, reason="request_failed", pages=page + 1)
                break
        else:
            return BarFetch(feed=feed, reason="page_limit", pages=_BATCH_MAX_PAGES)
    return failure


def _batch_fetch_bars(symbols: List[str], *, force: bool = False) -> None:
    """Keep completion, feed and failure provenance for both timeframes."""
    _batch_bars.clear()
    if (not SQUEEZE_BATCH_BARS and not force) or not symbols or not _alpaca_headers():
        return
    now_utc = datetime.now(timezone.utc)
    now_ct = session_hm(now_utc)[0]
    day_start = now_ct.replace(hour=PREMARKET_START_MIN // 60, minute=PREMARKET_START_MIN % 60, second=0, microsecond=0).astimezone(timezone.utc)
    daily_start = (now_utc - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    intraday_start = day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline = time.monotonic() + _BATCH_DEADLINE_S
    stop_reason = None
    chunk_size = max(1, _BATCH_SYMBOLS_PER_REQ)
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        if stop_reason:
            daily = intraday = BarFetch(reason=stop_reason)
        else:
            daily = _alpaca_multi_bars(chunk, timeframe="1Day", start=daily_start, end=end_str, deadline=deadline)
            intraday = (
                BarFetch(reason="http_429") if daily.reason == "http_429" else
                _alpaca_multi_bars(chunk, timeframe="5Min", start=intraday_start, end=end_str, deadline=deadline)
            )
            if "http_429" in (daily.reason, intraday.reason):
                stop_reason = "rate_limited_batch"
            elif time.monotonic() >= deadline:
                stop_reason = "deadline_exceeded"
        for sym in chunk:
            u = sym.upper()
            _batch_bars[u] = {
                "daily": daily.bars.get(u, []), "intraday": intraday.bars.get(u, []),
                "daily_result": daily, "intraday_result": intraday,
            }


def _fetch_volumes(symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (avg_daily_volume, session_volume_so_far, session_vwap)."""
    sym = symbol.upper()
    # Fast path: bars prewarmed by _batch_fetch_bars for this scan. Falls
    # through to the per-symbol fetch below on miss or unusable batch data.
    _cached = _batch_bars.get(sym)
    if _cached is not None:
        b_avg, b_sess, b_vwap = _volumes_from_bars(
            _cached.get("daily", []), _cached.get("intraday", []),
        )
        if b_avg and b_avg > 0:
            if not b_sess or b_sess <= 0:
                b_sess = 0.0
            return b_avg, b_sess, b_vwap
    headers = _alpaca_headers()
    if headers:
        try:
            import requests

            now_utc = datetime.now(timezone.utc)
            try:
                from zoneinfo import ZoneInfo

                ct = ZoneInfo(SESSION_TZ)
            except Exception:
                ct = None
            if ct:
                day_start = datetime.now(ct).replace(
                    hour=PREMARKET_START_MIN // 60,
                    minute=PREMARKET_START_MIN % 60,
                    second=0,
                    microsecond=0,
                )
                day_start = day_start.astimezone(timezone.utc)
            else:
                day_start = now_utc.replace(hour=9, minute=0, second=0, microsecond=0)
            start_str = day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            end_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            avg_vol = session_vol = vwap = None
            from core.prices import _alpaca_bar_feeds, _note_alpaca_feed_status
            for feed in _alpaca_bar_feeds():
                url = (
                    f"https://data.alpaca.markets/v2/stocks/{sym}/bars"
                    f"?timeframe=1Day&start={(now_utc - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
                    f"&end={end_str}&limit=25&feed={feed}"
                )
                r = requests.get(url, headers=headers, timeout=_TIMEOUT)
                if r.status_code != 200:
                    _note_alpaca_feed_status(feed, r.status_code)
                    continue
                dbars = r.json().get("bars") or []
                vols = [float(b.get("v", 0)) for b in dbars[-20:] if b.get("v")]
                if vols:
                    avg_vol = sum(vols) / len(vols)
                    break
            for feed in _alpaca_bar_feeds():
                url = (
                    f"https://data.alpaca.markets/v2/stocks/{sym}/bars"
                    f"?timeframe=5Min&start={start_str}&end={end_str}&limit=10000&feed={feed}"
                )
                r = requests.get(url, headers=headers, timeout=_TIMEOUT)
                if r.status_code != 200:
                    _note_alpaca_feed_status(feed, r.status_code)
                    continue
                bars = r.json().get("bars") or []
                if bars:
                    session_vol = sum(float(b.get("v", 0)) for b in bars if b.get("v"))
                    vwap = _vwap_from_bars(bars)
                    break
            if avg_vol and session_vol:
                return avg_vol, session_vol, vwap
            if avg_vol:
                return avg_vol, 0.0, vwap
        except Exception as exc:
            LOGGER.debug("[SqueezeMonitor] alpaca vol %s: %s", sym, exc)

    if not _yf_fallback_enabled():
        return None, None, None

    from core.circuit_breaker import _yfinance_cb
    if not _yfinance_cb.allow():
        return None, None, None

    try:
        import yfinance as yf

        t = _ungated_yf_ticker(sym)
        hist = t.history(period="30d", interval="1d")
        intraday = t.history(period="1d", interval="5m")
        if hist is None or hist.empty:
            return None, None, None
        avg_vol = float(hist["Volume"].iloc[-20:].mean())
        vwap = None
        if intraday is not None and not intraday.empty:
            session_vol = float(intraday["Volume"].sum())
            tp = (intraday["High"] + intraday["Low"] + intraday["Close"]) / 3.0
            vols = intraday["Volume"].astype(float)
            den = float(vols.sum())
            vwap = round(float((tp * vols).sum() / den), 4) if den > 0 else None
        else:
            session_vol = float(hist["Volume"].iloc[-1])
        _yfinance_cb.record_success()
        return avg_vol, session_vol, vwap
    except Exception:
        _yfinance_cb.record_failure()
        return None, None, None


def _yf_fetch_metrics(symbol: str) -> Optional[Dict[str, Any]]:
    """No previous-close/open fallback when intraday evidence is unavailable."""
    from core.circuit_breaker import _yfinance_cb
    if not _yfinance_cb.allow():
        return None
    try:
        import yfinance as yf

        ticker = _ungated_yf_ticker(symbol)
        daily = ticker.history(period="1mo", interval="1d", auto_adjust=False)
        intraday = ticker.history(period="1d", interval="5m", prepost=True, auto_adjust=False)
        if daily is None or daily.empty or intraday is None or intraday.empty:
            return None

        def rows(frame):
            return [{"t": index.isoformat(), **{short: float(row[name]) for short, name in (
                ("c", "Close"), ("h", "High"), ("l", "Low"), ("v", "Volume"),
            )}} for index, row in frame.iterrows()]

        metrics = _metrics_from_bar_set({
            "daily": rows(daily), "intraday": rows(intraday),
            "daily_result": BarFetch(complete=True, feed="yfinance"),
            "intraday_result": BarFetch(complete=True, feed="yfinance"),
        })
        _yfinance_cb.record_success()
        if metrics:
            metrics["price_source"] = "yfinance_bar"
        return metrics if metrics and evidence_status(metrics) == "ready" else None
    except Exception:
        _yfinance_cb.record_failure()
        return None


def _symbol_loss_streak(symbol: str) -> int:
    """Return consecutive resolved losses for a symbol (most recent first).

    Reads ghost_squeeze_outcomes to find the current cold streak.
    Returns 0 if the most recent resolved outcome was not a loss.
    """
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT outcome FROM ghost_squeeze_outcomes
                WHERE symbol = %s AND outcome IS NOT NULL
                ORDER BY alerted_at DESC
                LIMIT 10
                """,
                (symbol.upper(),),
            )
            rows = cur.fetchall()
            streak = 0
            for (outcome,) in rows:
                if outcome == 'LOSS':
                    streak += 1
                else:
                    break
            return streak
    except Exception:
        return 0


_MAX_LOSS_STREAK = int(os.getenv("SQUEEZE_MAX_LOSS_STREAK", "3"))


def _check_expected_value(
    buy: float,
    sell: float,
    stop: float,
    confidence_pct: int,
) -> bool:
    """Simple EV gate: expected gain must exceed expected loss.

    win_prob from confidence_pct, gain = (sell - buy) / buy,
    loss = (buy - stop) / buy. Requires EV > 0 to pass.
    """
    if buy <= 0 or sell <= buy or stop >= buy:
        return False
    win_prob = confidence_pct / 100.0
    loss_prob = 1.0 - win_prob
    gain_pct = (sell - buy) / buy
    loss_pct = (buy - stop) / buy
    ev = win_prob * gain_pct - loss_prob * loss_pct
    return ev > 0.0


def _maybe_alert(
    symbol: str,
    kind: str,
    metrics: Dict[str, Any],
    rvol: float,
    short_ctx: Dict[str, Any],
) -> bool:
    from config.symbols import V3_WHITELIST_STOCKS

    if (
        symbol.upper() not in V3_WHITELIST_STOCKS
        or metrics.get("advisory_only") is True
        or metrics.get("decision_eligible") is False
    ):
        LOGGER.warning("[SqueezeMonitor] reject nonofficial/advisory alert %s", symbol)
        return False
    if evidence_status(metrics) != "ready":
        LOGGER.warning("[SqueezeMonitor] reject invalid/stale alert evidence %s", symbol)
        return False
    buy, sell = squeeze_trade_levels(metrics["price"], metrics["session_high"], kind)
    conf = squeeze_confidence(
        metrics["peak_move_pct"],
        rvol,
        short_risk=short_ctx.get("squeeze_risk"),
        kind=kind,
    )
    if conf < MIN_TELEGRAM_CONFIDENCE:
        LOGGER.info("[SqueezeMonitor] suppress low-confidence %s %s conf=%s", symbol, kind, conf)
        return False

    # PR #161: per-symbol loss streak breaker — suppress if on cold streak
    streak = _symbol_loss_streak(symbol)
    if streak >= _MAX_LOSS_STREAK:
        LOGGER.info(
            "[SqueezeMonitor] suppress cold-streak %s %s streak=%s conf=%s",
            symbol, kind, streak, conf,
        )
        return False

    # PR #161: EV gate — don't alert if expected value is negative
    from core.squeeze_scorecard import compute_stop
    stop = compute_stop(
        metrics["price"],
        vwap=metrics.get("vwap"),
        prior_close=metrics.get("prior_close") if metrics.get("prior_close", 0) > 0 else None,
    )
    if not _check_expected_value(buy, sell, stop, conf):
        LOGGER.info(
            "[SqueezeMonitor] suppress negative-EV %s %s buy=%.2f sell=%.2f stop=%.2f conf=%s",
            symbol, kind, buy, sell, stop, conf,
        )
        return False

    key = _squeeze_alert_key(symbol, kind, buy, sell, conf)
    now = time.time()
    last = _last_alert.get(key)
    if last is not None and now - last < COOLDOWN_SEC:
        return False
    _last_alert[key] = now
    msg = format_squeeze_alert(symbol, kind, metrics, rvol, short_ctx)
    return _send_telegram(key, msg)


def _send_telegram(key: str, message: str) -> bool:
    """Send a Telegram alert and return the confirmed delivery status.

    Returns True only when the sender reports success (or alerts are disabled).
    A failed send returns False so the caller does NOT record a 'telegram'
    source row — 'alert generated' must not be mistaken for 'alert delivered'
    (forensic: delivery telemetry).
    """
    try:
        from core.telegram import send_telegram_message_once

        ok = send_telegram_message_once(key, message, cooldown_s=COOLDOWN_SEC)
        LOGGER.info("[SqueezeMonitor] Alert [%s]: %s", key, "OK" if ok else "FAILED")
        return bool(ok)
    except Exception as exc:
        LOGGER.error("[SqueezeMonitor] Telegram failed [%s]: %s", key, exc)
        return False
