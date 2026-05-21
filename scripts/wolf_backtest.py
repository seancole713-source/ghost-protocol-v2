#!/usr/bin/env python3
"""
WOLF Walk-Forward Backtest — Phase 3
=====================================
Pulls Wolfspeed (WOLF) historical OHLCV data and runs a walk-forward
validation to establish a real win-rate baseline for the prediction model.

Why walk-forward (not simple backtest):
  A simple backtest on all historical data overfits. Walk-forward:
  - Trains on a rolling 90-day window
  - Tests on the NEXT 30 days (out-of-sample)
  - Repeats across all available history
  - Reports accuracy per window + aggregate

WOLF-specific features used:
  RSI(14), MACD, Bollinger Bands, ATR(14), volume ratio,
  momentum_5d, momentum_10d, body_ratio, upper_wick, lower_wick

Data sources (free):
  1. Polygon REST API (daily bars)   — uses POLYGON_API_KEY
  2. yfinance                        — fallback, no key needed

Usage:
  python scripts/wolf_backtest.py
  python scripts/wolf_backtest.py --days 365 --train 90 --test 30
  python scripts/wolf_backtest.py --output results/wolf_backtest_2026.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
LOGGER = logging.getLogger("wolf.backtest")

SYMBOL = "WOLF"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_wolf_history(days: int = 730) -> list[dict]:
    """
    Fetch WOLF daily OHLCV history.
    Tries Polygon first, falls back to yfinance.
    Returns list of dicts: {date, open, high, low, close, volume}
    Sorted oldest → newest.
    """
    polygon_key = os.getenv("POLYGON_API_KEY", "")
    bars = []

    if polygon_key:
        bars = _fetch_polygon(polygon_key, days)
        if bars:
            LOGGER.info(f"Polygon: fetched {len(bars)} WOLF bars")

    if not bars:
        LOGGER.info("Falling back to yfinance for WOLF history")
        bars = _fetch_yfinance(days)
        if bars:
            LOGGER.info(f"yfinance: fetched {len(bars)} WOLF bars")

    if not bars:
        LOGGER.error("No WOLF price data available — check POLYGON_API_KEY or yfinance install")
        sys.exit(1)

    return sorted(bars, key=lambda b: b["date"])


def _fetch_polygon(api_key: str, days: int) -> list[dict]:
    import requests
    end_dt = date.today()
    start_dt = end_dt - timedelta(days=days + 30)  # buffer for weekends
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{SYMBOL}/range/1/day/"
        f"{start_dt.isoformat()}/{end_dt.isoformat()}"
        f"?adjusted=true&sort=asc&limit=5000&apiKey={api_key}"
    )
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if data.get("status") not in ("OK", "DELAYED"):
            LOGGER.warning(f"Polygon status: {data.get('status')}")
            return []
        return [
            {
                "date": datetime.fromtimestamp(r["t"] / 1000).strftime("%Y-%m-%d"),
                "open": r["o"], "high": r["h"], "low": r["l"],
                "close": r["c"], "volume": r["v"],
            }
            for r in data.get("results", [])
        ]
    except Exception as exc:
        LOGGER.warning(f"Polygon fetch failed: {exc}")
        return []


def _fetch_yfinance(days: int) -> list[dict]:
    try:
        import yfinance as yf  # type: ignore
        t = yf.Ticker(SYMBOL)
        period = f"{min(days, 1825)}d"
        hist = t.history(period=period)
        if hist.empty:
            return []
        bars = []
        for dt_idx, row in hist.iterrows():
            bars.append({
                "date": str(dt_idx.date()),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            })
        return bars
    except Exception as exc:
        LOGGER.warning(f"yfinance fetch failed: {exc}")
        return []


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def compute_features(bars: list[dict]) -> list[dict]:
    """
    Add WOLF-specific technical features to each bar.
    Bars must be sorted oldest → newest.
    Only bars with sufficient lookback are returned (skips first 30).
    """
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    opens = [b["open"] for b in bars]
    results = []

    for i in range(30, len(bars)):
        c = closes[i]
        bar = bars[i].copy()

        # ── RSI(14) ──────────────────────────────────────────────────────
        gains, losses = [], []
        for j in range(i - 14, i):
            diff = closes[j + 1] - closes[j]
            (gains if diff > 0 else losses).append(abs(diff))
        avg_gain = sum(gains) / 14 if gains else 0
        avg_loss = sum(losses) / 14 if losses else 0.0001
        rs = avg_gain / avg_loss
        bar["rsi_14"] = round(100 - (100 / (1 + rs)), 2)

        # ── MACD (12,26,9) ───────────────────────────────────────────────
        ema12 = _ema(closes[max(0, i - 30):i + 1], 12)
        ema26 = _ema(closes[max(0, i - 30):i + 1], 26)
        macd_line = ema12 - ema26
        bar["macd_line"] = round(macd_line, 4)

        # ── Bollinger Bands (20,2) ───────────────────────────────────────
        sma20 = sum(closes[i - 20:i]) / 20
        std20 = (sum((x - sma20) ** 2 for x in closes[i - 20:i]) / 20) ** 0.5
        bb_upper = sma20 + 2 * std20
        bb_lower = sma20 - 2 * std20
        bar["bb_pct_b"] = round((c - bb_lower) / (bb_upper - bb_lower + 0.0001), 4)
        bar["bb_width"] = round((bb_upper - bb_lower) / sma20 if sma20 > 0 else 0, 4)

        # ── ATR(14) ──────────────────────────────────────────────────────
        trs = []
        for j in range(i - 14, i):
            tr = max(
                highs[j + 1] - lows[j + 1],
                abs(highs[j + 1] - closes[j]),
                abs(lows[j + 1] - closes[j]),
            )
            trs.append(tr)
        atr14 = sum(trs) / 14
        bar["atr_pct"] = round(atr14 / c * 100 if c > 0 else 0, 4)

        # ── Volume ratio ─────────────────────────────────────────────────
        avg_vol20 = sum(volumes[i - 20:i]) / 20
        bar["volume_ratio"] = round(volumes[i] / avg_vol20 if avg_vol20 > 0 else 1.0, 3)

        # ── Momentum ─────────────────────────────────────────────────────
        bar["momentum_5d"] = round((c / closes[i - 5] - 1) * 100 if closes[i - 5] > 0 else 0, 3)
        bar["momentum_10d"] = round((c / closes[i - 10] - 1) * 100 if closes[i - 10] > 0 else 0, 3)
        bar["momentum_20d"] = round((c / closes[i - 20] - 1) * 100 if closes[i - 20] > 0 else 0, 3)

        # ── Candlestick anatomy ──────────────────────────────────────────
        body = abs(c - opens[i])
        candle_range = highs[i] - lows[i] + 0.0001
        bar["body_ratio"] = round(body / candle_range, 3)
        bar["upper_wick"] = round((highs[i] - max(c, opens[i])) / candle_range, 3)
        bar["lower_wick"] = round((min(c, opens[i]) - lows[i]) / candle_range, 3)
        bar["is_green"] = int(c > opens[i])

        # ── 5-day return label (what we're predicting) ───────────────────
        if i + 5 < len(bars):
            future_close = closes[i + 5]
            bar["future_5d_pct"] = round((future_close / c - 1) * 100, 3)
            bar["label_up"] = int(future_close > c * 1.005)  # UP if +0.5% or more
        else:
            bar["future_5d_pct"] = None
            bar["label_up"] = None

        results.append(bar)

    return results


def _ema(prices: list[float], period: int) -> float:
    """Exponential Moving Average of the last `period` bars."""
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


# ---------------------------------------------------------------------------
# Simple rule-based predictor (mirrors stock_engine logic)
# ---------------------------------------------------------------------------

def predict_direction(feat: dict) -> str:
    """
    Mirror of the stock engine rules applied to WOLF features.
    Returns 'UP', 'DOWN', or 'HOLD'.
    """
    rsi = feat.get("rsi_14", 50)
    macd = feat.get("macd_line", 0)
    bb = feat.get("bb_pct_b", 0.5)
    mom5 = feat.get("momentum_5d", 0)
    vol_ratio = feat.get("volume_ratio", 1)

    bull_signals = 0
    bear_signals = 0

    # RSI
    if rsi < 35:
        bull_signals += 2  # Oversold
    elif rsi < 45:
        bull_signals += 1
    elif rsi > 65:
        bear_signals += 2  # Overbought
    elif rsi > 55:
        bear_signals += 1

    # MACD
    if macd > 0:
        bull_signals += 1
    elif macd < 0:
        bear_signals += 1

    # Bollinger %B
    if bb < 0.2:
        bull_signals += 2  # Price near lower band — mean reversion signal
    elif bb < 0.35:
        bull_signals += 1
    elif bb > 0.8:
        bear_signals += 2
    elif bb > 0.65:
        bear_signals += 1

    # Momentum
    if mom5 > 3:
        bull_signals += 1
    elif mom5 < -3:
        bear_signals += 1

    # Volume confirmation
    if vol_ratio > 1.5:
        # High volume amplifies direction
        if bull_signals > bear_signals:
            bull_signals += 1
        elif bear_signals > bull_signals:
            bear_signals += 1

    net = bull_signals - bear_signals
    if net >= 3:
        return "UP"
    elif net <= -3:
        return "DOWN"
    return "HOLD"


# ---------------------------------------------------------------------------
# Walk-forward engine
# ---------------------------------------------------------------------------

@dataclass
class WindowResult:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    test_bars: int
    up_predictions: int
    down_predictions: int
    hold_predictions: int
    correct: int
    wrong: int
    accuracy_pct: float
    up_accuracy_pct: float
    down_accuracy_pct: float


def run_walk_forward(
    features: list[dict],
    train_days: int = 90,
    test_days: int = 30,
) -> list[WindowResult]:
    """
    Run walk-forward validation across all available data.
    Returns list of WindowResult (one per test window).
    """
    # Filter to bars with labels
    labeled = [f for f in features if f.get("label_up") is not None]
    if len(labeled) < train_days + test_days:
        LOGGER.error(f"Need {train_days + test_days} labeled bars, only have {len(labeled)}")
        return []

    results = []
    i = train_days  # Start of first test window

    while i + test_days <= len(labeled):
        train_window = labeled[i - train_days:i]
        test_window = labeled[i:i + test_days]

        # In a real ML backtest we'd fit a model on train_window.
        # Here we use the rule-based predictor (no fitting needed).
        # Phase 3 upgrade: replace predict_direction() with fitted XGBoost.

        train_start = train_window[0]["date"]
        train_end = train_window[-1]["date"]
        test_start = test_window[0]["date"]
        test_end = test_window[-1]["date"]

        up_pred = down_pred = hold_pred = correct = wrong = 0
        up_correct = up_wrong = down_correct = down_wrong = 0

        for bar in test_window:
            pred = predict_direction(bar)
            actual_up = bar["label_up"]  # 1 = UP, 0 = DOWN/FLAT

            if pred == "UP":
                up_pred += 1
                if actual_up == 1:
                    correct += 1; up_correct += 1
                else:
                    wrong += 1; up_wrong += 1
            elif pred == "DOWN":
                down_pred += 1
                if actual_up == 0:
                    correct += 1; down_correct += 1
                else:
                    wrong += 1; down_wrong += 1
            else:
                hold_pred += 1  # HOLDs are excluded from accuracy

        total_traded = correct + wrong
        accuracy = (correct / total_traded * 100) if total_traded > 0 else 0
        up_acc = (up_correct / (up_correct + up_wrong) * 100) if (up_correct + up_wrong) > 0 else 0
        down_acc = (down_correct / (down_correct + down_wrong) * 100) if (down_correct + down_wrong) > 0 else 0

        results.append(WindowResult(
            train_start=train_start, train_end=train_end,
            test_start=test_start, test_end=test_end,
            test_bars=len(test_window),
            up_predictions=up_pred, down_predictions=down_pred, hold_predictions=hold_pred,
            correct=correct, wrong=wrong,
            accuracy_pct=round(accuracy, 1),
            up_accuracy_pct=round(up_acc, 1),
            down_accuracy_pct=round(down_acc, 1),
        ))

        i += test_days  # Advance to next window

    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(results: list[WindowResult], output_path: Optional[str] = None) -> None:
    if not results:
        print("No results to report.")
        return

    print(f"\n{'='*70}")
    print(f"  WOLF WALK-FORWARD BACKTEST — {len(results)} windows")
    print(f"{'='*70}")
    print(f"  {'TRAIN WINDOW':<25} {'TEST WINDOW':<25} {'ACC%':>5} {'UP%':>5} {'DN%':>5} {'Trades':>6}")
    print(f"  {'-'*25} {'-'*25} {'-'*5} {'-'*5} {'-'*5} {'-'*6}")
    for r in results:
        print(
            f"  {r.train_start} → {r.train_end}  "
            f"{r.test_start} → {r.test_end}  "
            f"{r.accuracy_pct:>5.1f} {r.up_accuracy_pct:>5.1f} {r.down_accuracy_pct:>5.1f} "
            f"{r.correct + r.wrong:>6}"
        )

    all_acc = [r.accuracy_pct for r in results if r.correct + r.wrong > 0]
    all_trades = sum(r.correct + r.wrong for r in results)
    all_correct = sum(r.correct for r in results)

    agg_acc = all_correct / all_trades * 100 if all_trades > 0 else 0

    print(f"\n  Aggregate accuracy : {agg_acc:.1f}%  ({all_correct}/{all_trades} trades)")
    print(f"  Avg window accuracy: {sum(all_acc)/len(all_acc):.1f}%")
    print(f"  Best window        : {max(all_acc):.1f}%")
    print(f"  Worst window       : {min(all_acc):.1f}%")
    print(f"  HOLD rate          : {sum(r.hold_predictions for r in results) / sum(r.test_bars for r in results) * 100:.1f}%")
    print(f"{'='*70}\n")

    if agg_acc >= 55:
        print("  ✅ PASS — aggregate accuracy ≥55%. Strategy is viable.")
    elif agg_acc >= 50:
        print("  ⚠️  MARGINAL — accuracy 50-55%. Needs WOLF-specific model training (Phase 3).")
    else:
        print("  ❌ FAIL — accuracy <50%. Rule-based model insufficient for WOLF. Retrain needed.")

    if output_path:
        data = {
            "symbol": SYMBOL,
            "run_date": date.today().isoformat(),
            "aggregate_accuracy_pct": round(agg_acc, 2),
            "total_trades": all_trades,
            "windows": [asdict(r) for r in results],
        }
        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"  Results saved → {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="WOLF Walk-Forward Backtest")
    parser.add_argument("--days", type=int, default=730, help="Total history days to fetch (default: 730)")
    parser.add_argument("--train", type=int, default=90, help="Training window size in days (default: 90)")
    parser.add_argument("--test", type=int, default=30, help="Test window size in days (default: 30)")
    parser.add_argument("--output", type=str, default="", help="Save JSON results to this path")
    args = parser.parse_args()

    LOGGER.info(f"Fetching {args.days} days of WOLF history...")
    bars = fetch_wolf_history(days=args.days)
    LOGGER.info(f"Computing features on {len(bars)} bars...")
    features = compute_features(bars)
    labeled = [f for f in features if f.get("label_up") is not None]
    LOGGER.info(f"Walk-forward: {len(labeled)} labeled bars, train={args.train}d, test={args.test}d")

    results = run_walk_forward(features, train_days=args.train, test_days=args.test)
    print_summary(results, output_path=args.output or None)


if __name__ == "__main__":
    main()
