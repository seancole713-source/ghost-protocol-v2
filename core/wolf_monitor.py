"""
core/wolf_monitor.py — Phase 5: Autonomous WOLF Monitoring
===========================================================
Background asyncio task that runs inside the FastAPI process.
Sends Telegram alerts for:
  1. Volume spikes (>2× 20-day average)
  2. Intraday price moves (>3% from prior close)
  3. Short squeeze conditions (price surge + high volume)
  4. New SEC 8-K filings from EDGAR (checked every 30 min)
  5. Earnings countdown (7d, 3d, 1d, same-day warnings)

Design principles:
  - Single asyncio task: start_wolf_monitor() → long-running coroutine
  - All failures are caught and logged — never crashes the app
  - Uses existing core.telegram_hunter.send_telegram_message (no new deps)
  - Uses existing core.wolf_context.get_wolf_context (cached, free)
  - State is in-memory (last_alert_ts dict) — resets on redeploy (fine)
  - Checks run every CHECK_INTERVAL_SEC (default: 300s / 5 min)

Enable/disable:
  WOLF_MONITOR_ENABLED=1   env var (default: 1 when symbol is WOLF)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

LOGGER = logging.getLogger("wolf.monitor")

# ── Config ────────────────────────────────────────────────────────────────
SYMBOL = "WOLF"
CHECK_INTERVAL_SEC = int(os.getenv("WOLF_MONITOR_INTERVAL", "300"))   # 5 min default
EDGAR_INTERVAL_SEC = int(os.getenv("WOLF_EDGAR_INTERVAL", "1800"))    # 30 min
VOLUME_SPIKE_MULT = float(os.getenv("WOLF_VOLUME_SPIKE", "2.0"))      # 2× avg = spike
PRICE_MOVE_PCT = float(os.getenv("WOLF_PRICE_MOVE_PCT", "3.0"))       # 3% intraday alert
SQUEEZE_PRICE_PCT = float(os.getenv("WOLF_SQUEEZE_PRICE_PCT", "5.0")) # 5% + high vol
SQUEEZE_VOL_MULT = float(os.getenv("WOLF_SQUEEZE_VOL", "2.5"))        # 2.5× avg vol

# Cooldown: don't re-send same alert type within N seconds
COOLDOWN: dict[str, int] = {
    "volume_spike": 3600,       # 1 hour
    "price_move": 1800,         # 30 min
    "short_squeeze": 7200,      # 2 hours
    "edgar_8k": 3600,           # 1 hour per filing
    "earnings_7d": 86400,       # 1 day
    "earnings_3d": 86400,
    "earnings_1d": 43200,
    "earnings_day": 21600,
}

# In-memory last-sent timestamps (resets on restart)
_last_alert: dict[str, float] = {}
# Last known 8-K filing date to detect new ones
_last_edgar_date: Optional[str] = None
# Earnings countdown: track which distances already alerted
_earnings_alerted: set[str] = set()


# ── Entry point ───────────────────────────────────────────────────────────

async def start_wolf_monitor() -> None:
    """
    Long-running coroutine. Call via asyncio.create_task().
    Runs forever, sleeping between checks.
    """
    enabled = os.getenv("WOLF_MONITOR_ENABLED", "1") == "1"
    if not enabled:
        LOGGER.info("[WolfMonitor] Disabled by WOLF_MONITOR_ENABLED=0")
        return

    LOGGER.info(f"[WolfMonitor] 🐺 Starting — check every {CHECK_INTERVAL_SEC}s")
    edgar_last_check = 0.0

    while True:
        try:
            await _run_checks()
        except Exception as exc:
            LOGGER.error(f"[WolfMonitor] check failed: {exc}", exc_info=False)

        # EDGAR checks run on separate (slower) schedule
        now = time.time()
        if now - edgar_last_check >= EDGAR_INTERVAL_SEC:
            try:
                await _check_edgar()
                edgar_last_check = now
            except Exception as exc:
                LOGGER.error(f"[WolfMonitor] EDGAR check failed: {exc}", exc_info=False)

        await asyncio.sleep(CHECK_INTERVAL_SEC)


# ── Price + volume checks ─────────────────────────────────────────────────

async def _run_checks() -> None:
    """Fetch current WOLF price/volume and check for alerts."""
    price_data = await _fetch_wolf_intraday()
    if not price_data:
        return

    current_price = price_data.get("price")
    prior_close = price_data.get("prior_close")
    current_vol = price_data.get("volume")
    avg_vol = price_data.get("avg_vol_20d")

    if not current_price:
        return

    # ── Volume spike ─────────────────────────────────────────────────
    if current_vol and avg_vol and avg_vol > 0:
        vol_mult = current_vol / avg_vol
        if vol_mult >= VOLUME_SPIKE_MULT:
            _maybe_send(
                "volume_spike",
                f"📊 WOLF Volume Spike\n"
                f"Volume: {vol_mult:.1f}× average ({_fmt_vol(current_vol)} vs avg {_fmt_vol(avg_vol)})\n"
                f"Price: ${current_price:.2f}"
            )

    # ── Intraday price move ──────────────────────────────────────────
    if prior_close and prior_close > 0:
        move_pct = (current_price - prior_close) / prior_close * 100
        if abs(move_pct) >= PRICE_MOVE_PCT:
            direction = "🟢 UP" if move_pct > 0 else "🔴 DOWN"
            _maybe_send(
                "price_move",
                f"⚡ WOLF Price Alert\n"
                f"{direction} {abs(move_pct):.1f}% intraday\n"
                f"Price: ${current_price:.2f}  (prior close: ${prior_close:.2f})"
            )

        # ── Short squeeze signal ─────────────────────────────────────
        if (
            current_vol and avg_vol and avg_vol > 0
            and move_pct >= SQUEEZE_PRICE_PCT
            and (current_vol / avg_vol) >= SQUEEZE_VOL_MULT
        ):
            vol_mult = current_vol / avg_vol
            _maybe_send(
                "short_squeeze",
                f"🚨 WOLF Short Squeeze Signal\n"
                f"Price +{move_pct:.1f}% with {vol_mult:.1f}× volume\n"
                f"Price: ${current_price:.2f} — Check short float!"
            )

    # ── Earnings countdown ───────────────────────────────────────────
    await _check_earnings_countdown()


async def _check_earnings_countdown() -> None:
    """Alert at 7d, 3d, 1d and same-day before earnings."""
    try:
        from core.wolf_context import get_wolf_context
        ctx = get_wolf_context(direction="UP")
        if not ctx.earnings or not ctx.earnings.date_str:
            return

        d = ctx.earnings.days_away
        if d < 0:
            _earnings_alerted.discard("7d")
            _earnings_alerted.discard("3d")
            _earnings_alerted.discard("1d")
            _earnings_alerted.discard("day")
            return

        milestones = [("7d", 7, 8), ("3d", 3, 4), ("1d", 1, 2), ("day", 0, 1)]
        for key, lo, hi in milestones:
            if lo <= d < hi and key not in _earnings_alerted:
                _earnings_alerted.add(key)
                msg = (
                    f"📅 WOLF Earnings Countdown\n"
                    f"Earnings in {d} day(s): {ctx.earnings.date_str}\n"
                    f"{'⚠️ CAUTION MODE — reducing position size!' if d <= 1 else 'Monitor closely.'}"
                )
                _send(key, msg)
    except Exception as exc:
        LOGGER.debug(f"[WolfMonitor] earnings check error: {exc}")


# ── EDGAR 8-K check ───────────────────────────────────────────────────────

async def _check_edgar() -> None:
    """Check for new WOLF 8-K filings. Alert on critical/high urgency."""
    global _last_edgar_date
    try:
        from core.wolf_context import get_wolf_context
        ctx = get_wolf_context(direction="UP")
        if not ctx.edgar_alert:
            return

        filing_date = ctx.edgar_alert.filing_date
        if filing_date == _last_edgar_date:
            return  # Already alerted on this one

        urgency = ctx.edgar_alert.urgency or "low"
        if urgency not in ("critical", "high"):
            _last_edgar_date = filing_date
            return  # Low-urgency filings — skip

        _last_edgar_date = filing_date
        items_str = ", ".join(ctx.edgar_alert.items) if ctx.edgar_alert.items else "—"
        _maybe_send(
            f"edgar_8k_{filing_date}",  # unique key per filing
            f"🔔 WOLF SEC 8-K Filing ({urgency.upper()})\n"
            f"Date: {filing_date}\n"
            f"Items: {items_str}\n"
            f"{ctx.edgar_alert.description or ''}",
            cooldown_override=COOLDOWN["edgar_8k"],
        )
    except Exception as exc:
        LOGGER.debug(f"[WolfMonitor] EDGAR check error: {exc}")


# ── Price fetcher ─────────────────────────────────────────────────────────

async def _fetch_wolf_intraday() -> Optional[dict]:
    """
    Fetch WOLF current price + volume using existing wolf_helpers quorum.
    Returns dict with price, prior_close, volume, avg_vol_20d or None on error.
    """
    try:
        import yfinance as yf  # type: ignore
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _sync_yf_fetch)
        return data
    except Exception as exc:
        LOGGER.debug(f"[WolfMonitor] price fetch error: {exc}")
        return None


def _sync_yf_fetch() -> Optional[dict]:
    """Blocking yfinance call — run in executor."""
    from core.circuit_breaker import _yfinance_cb
    if not _yfinance_cb.allow():
        return None
    try:
        import yfinance as yf  # type: ignore
        t = yf.Ticker(SYMBOL)
        hist = t.history(period="30d")
        if hist.empty or len(hist) < 2:
            return None
        current = hist.iloc[-1]
        prior = hist.iloc[-2]
        avg_vol = float(hist["Volume"].iloc[-20:].mean()) if len(hist) >= 20 else float(hist["Volume"].mean())
        _yfinance_cb.record_success()
        return {
            "price": float(current["Close"]),
            "prior_close": float(prior["Close"]),
            "volume": float(current["Volume"]),
            "avg_vol_20d": avg_vol,
        }
    except Exception:
        _yfinance_cb.record_failure()
        return None


# ── Alert sender ──────────────────────────────────────────────────────────

def _maybe_send(key: str, message: str, cooldown_override: Optional[int] = None) -> None:
    """Send alert only if cooldown has passed."""
    cooldown = cooldown_override or COOLDOWN.get(key, 1800)
    now = time.time()
    if now - _last_alert.get(key, 0) < cooldown:
        return
    _send(key, message)


def _send(key: str, message: str) -> None:
    """Send Telegram alert and update last-sent timestamp."""
    _last_alert[key] = time.time()
    try:
        from core.telegram_hunter import send_telegram_message
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M UTC")
        full_msg = f"{message}\n\n⏰ {ts}"
        ok = send_telegram_message(full_msg)
        LOGGER.info(f"[WolfMonitor] Alert sent [{key}]: {'OK' if ok else 'FAILED'}")
    except Exception as exc:
        LOGGER.error(f"[WolfMonitor] Telegram send failed [{key}]: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────

def _fmt_vol(v: float) -> str:
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v/1_000:.0f}K"
    return str(int(v))
