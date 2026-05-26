"""Backtesting metrics over resolved picks (roadmap #4a).

Pure functions — the caller supplies the trade list, so this is unit-testable
without a DB. Reuses core.pnl.realized_pnl for the equity-curve-derived stats
(profit factor, max drawdown, expectancy) and adds Sharpe, win-rate-by-
confidence-bucket, and average hold time.
"""
import statistics
from typing import Any, Dict, List, Sequence

from core.pnl import realized_pnl

# Confidence buckets for win-rate attribution (mirrors /api/stats/confidence-buckets).
_CONF_BUCKETS = [("<70", 0.0, 0.70), ("70-80", 0.70, 0.80),
                 ("80-90", 0.80, 0.90), ("90+", 0.90, 1.01)]


def _sharpe_per_trade(returns: List[float]):
    """Per-trade Sharpe (risk-free 0): mean / population-stdev of trade returns.
    Not annualized — a unit-free quality ratio of the per-trade return series."""
    if len(returns) < 2:
        return None
    mu = statistics.mean(returns)
    sd = statistics.pstdev(returns)
    return round(mu / sd, 4) if sd > 0 else None


def backtest(trades: Sequence[Dict[str, Any]], bankroll: float = None,
             stake_fraction: float = None) -> Dict[str, Any]:
    """Backtest summary over resolved trades (oldest first). Each trade: pnl_pct
    (required) + optional outcome / confidence / predicted_at / resolved_at."""
    resolved = [t for t in trades if t.get("pnl_pct") is not None]
    n = len(resolved)
    pnl = realized_pnl(resolved, bankroll=bankroll, stake_fraction=stake_fraction)
    returns = [float(t["pnl_pct"]) for t in resolved]

    buckets = []
    for label, lo, hi in _CONF_BUCKETS:
        b = [t for t in resolved if t.get("confidence") is not None and lo <= float(t["confidence"]) < hi]
        bw = sum(1 for t in b if t.get("outcome") == "WIN")
        bl = sum(1 for t in b if t.get("outcome") == "LOSS")
        decided = bw + bl
        buckets.append({
            "bucket": label, "min": lo, "max": hi, "count": len(b),
            "wins": bw, "losses": bl,
            "win_rate_pct": round(bw / decided * 100, 1) if decided else None,
            "avg_pnl_pct": round(sum(float(t["pnl_pct"]) for t in b) / len(b), 3) if b else None,
        })

    holds = []
    for t in resolved:
        pa, ra = t.get("predicted_at"), t.get("resolved_at")
        if pa and ra and ra > pa:
            holds.append((ra - pa) / 3600.0)
    avg_hold_h = round(sum(holds) / len(holds), 2) if holds else None

    wr = pnl.get("win_rate")
    return {
        "ok": True,
        "trades": n,
        "sharpe_per_trade": _sharpe_per_trade(returns),
        "max_drawdown_pct": pnl["max_drawdown_pct"],
        "win_rate_pct": round(wr * 100, 1) if wr is not None else None,
        "expectancy_pct": pnl["expectancy_pct"],
        "profit_factor": pnl["profit_factor"],
        "total_return_pct": pnl["total_return_pct"],
        "realized_pnl_usd": pnl["realized_pnl_usd"],
        "best_trade_pct": pnl["best_trade_pct"],
        "worst_trade_pct": pnl["worst_trade_pct"],
        "avg_hold_hours": avg_hold_h,
        "by_confidence_bucket": buckets,
    }
