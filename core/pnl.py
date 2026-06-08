"""Realized-P&L tracking (audit §5).

Per-pick entry/exit/pnl_pct are already journaled on the predictions row. This
module turns that ledger into an aggregate: a sequential-compounding equity
curve plus the standard trade statistics (profit factor, drawdown, expectancy).

The model is deliberately simple and honest: trades are resolved in chronological
order and each one risks `stake_fraction` of current equity at its realized
pnl_pct. With the default stake_fraction=1.0 (WOLF holds one position at a time,
deduped) equity simply compounds by (1 + pnl_pct/100) per trade.
"""
import os
from typing import Any, Dict, List, Sequence, Tuple


def resolution_exit(
    outcome: str,
    direction: str,
    entry: float,
    target: float,
    stop: float,
    market_price: float,
) -> Tuple[float, float]:
    """Exit fill and pnl_pct for a resolved pick.

    WIN/LOSS use limit fills at target/stop (not the overshooting bar close).
    EXPIRED and WITHDRAWN use the live market price at resolution time.
    """
    if outcome == "WIN":
        exit_price = target
    elif outcome == "LOSS":
        exit_price = stop
    else:
        exit_price = market_price

    if direction == "UP":
        pnl_pct = (exit_price - entry) / entry * 100.0
    else:
        pnl_pct = (entry - exit_price) / entry * 100.0
    return exit_price, round(pnl_pct, 3)


def _bankroll() -> float:
    try:
        return max(1.0, float(os.getenv("GHOST_PNL_BANKROLL", "1000")))
    except Exception:
        return 1000.0


def _stake_fraction() -> float:
    try:
        return min(1.0, max(0.01, float(os.getenv("GHOST_PNL_STAKE_FRACTION", "1.0"))))
    except Exception:
        return 1.0


def realized_pnl(trades: Sequence[Dict[str, Any]],
                 bankroll: float = None,
                 stake_fraction: float = None) -> Dict[str, Any]:
    """Aggregate realized P&L from resolved trades (oldest first).

    Each trade is a dict with at least `pnl_pct`; optional `outcome`, `symbol`,
    `resolved_at`, `entry_price`, `exit_price`. Trades with a null pnl_pct are
    skipped. Returns summary stats + an equity curve suitable for charting.
    """
    bankroll = _bankroll() if bankroll is None else float(bankroll)
    frac = _stake_fraction() if stake_fraction is None else float(stake_fraction)

    resolved = [t for t in trades if t.get("pnl_pct") is not None]
    if not resolved:
        return {
            "ok": True, "count": 0, "bankroll": round(bankroll, 2),
            "stake_fraction": frac, "final_equity": round(bankroll, 2),
            "realized_pnl_usd": 0.0, "total_return_pct": 0.0,
            "wins": 0, "losses": 0, "expired": 0, "win_rate": None,
            "expectancy_pct": None, "avg_win_pct": None, "avg_loss_pct": None,
            "profit_factor": None, "max_drawdown_pct": 0.0,
            "best_trade_pct": None, "worst_trade_pct": None, "curve": [],
        }

    equity = bankroll
    peak = bankroll
    max_dd = 0.0
    curve: List[Dict[str, Any]] = []
    wins = losses = expired = 0
    gross_win = gross_loss = 0.0     # gross_loss accumulated as a positive magnitude
    win_pnls: List[float] = []
    loss_pnls: List[float] = []
    pnls: List[float] = []

    for t in resolved:
        pnl = float(t["pnl_pct"])
        pnls.append(pnl)
        equity *= (1.0 + frac * pnl / 100.0)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

        outcome = t.get("outcome")
        if outcome == "WIN":
            wins += 1
        elif outcome == "LOSS":
            losses += 1
        elif outcome == "EXPIRED":
            expired += 1
        if pnl >= 0:
            gross_win += pnl
            win_pnls.append(pnl)
        else:
            gross_loss += -pnl
            loss_pnls.append(pnl)

        curve.append({
            "ts": t.get("resolved_at"),
            "symbol": t.get("symbol"),
            "outcome": outcome,
            "pnl_pct": round(pnl, 3),
            "entry_price": t.get("entry_price"),
            "exit_price": t.get("exit_price"),
            "equity": round(equity, 2),
        })

    n = len(resolved)
    decided = wins + losses     # win rate over decided (WIN/LOSS) trades only
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else float("inf"))
    return {
        "ok": True,
        "count": n,
        "bankroll": round(bankroll, 2),
        "stake_fraction": frac,
        "final_equity": round(equity, 2),
        "realized_pnl_usd": round(equity - bankroll, 2),
        "total_return_pct": round((equity / bankroll - 1.0) * 100.0, 3),
        "wins": wins, "losses": losses, "expired": expired,
        "win_rate": round(wins / decided, 4) if decided else None,
        "expectancy_pct": round(sum(pnls) / n, 4),
        "avg_win_pct": round(sum(win_pnls) / len(win_pnls), 4) if win_pnls else None,
        "avg_loss_pct": round(sum(loss_pnls) / len(loss_pnls), 4) if loss_pnls else None,
        "profit_factor": (round(profit_factor, 4) if isinstance(profit_factor, float) and profit_factor != float("inf") else profit_factor),
        "max_drawdown_pct": round(max_dd, 3),
        "best_trade_pct": round(max(pnls), 3),
        "worst_trade_pct": round(min(pnls), 3),
        "curve": curve,
    }
