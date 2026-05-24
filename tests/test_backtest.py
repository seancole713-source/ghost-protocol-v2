"""roadmap #4a — backtesting metrics. Separate file (no overlap with open PRs)."""
import math


def _t(pnl, outcome=None, conf=None, pa=None, ra=None):
    return {"pnl_pct": pnl, "outcome": outcome, "confidence": conf,
            "predicted_at": pa, "resolved_at": ra}


def test_backtest_empty():
    from core.backtest import backtest
    out = backtest([])
    assert out["ok"] is True
    assert out["trades"] == 0
    assert out["sharpe_per_trade"] is None
    assert out["avg_hold_hours"] is None
    assert out["max_drawdown_pct"] == 0.0


def test_backtest_core_metrics():
    from core.backtest import backtest
    trades = [
        _t(10.0, "WIN", 0.82, 1000, 1000 + 6 * 3600),    # 6h hold
        _t(-5.0, "LOSS", 0.81, 2000, 2000 + 12 * 3600),  # 12h hold
        _t(20.0, "WIN", 0.91, 3000, 3000 + 3 * 3600),    # 3h hold
    ]
    out = backtest(trades, bankroll=1000.0, stake_fraction=1.0)
    assert out["trades"] == 3
    # win rate over decided = 2/3
    assert out["win_rate_pct"] == round(2 / 3 * 100, 1)
    # avg hold = (6+12+3)/3 = 7.0h
    assert out["avg_hold_hours"] == 7.0
    # profit factor = (10+20)/5 = 6.0 (from realized_pnl)
    assert out["profit_factor"] == 6.0
    assert out["best_trade_pct"] == 20.0 and out["worst_trade_pct"] == -5.0
    # sharpe = mean/pstdev of [10,-5,20]
    import statistics
    mu = statistics.mean([10.0, -5.0, 20.0])
    sd = statistics.pstdev([10.0, -5.0, 20.0])
    assert abs(out["sharpe_per_trade"] - round(mu / sd, 4)) < 1e-9


def test_backtest_confidence_buckets():
    from core.backtest import backtest
    trades = [
        _t(5.0, "WIN", 0.83), _t(-3.0, "LOSS", 0.84),     # 80-90 bucket: 1W/1L
        _t(8.0, "WIN", 0.92), _t(9.0, "WIN", 0.95),       # 90+ bucket: 2W
        _t(-2.0, "LOSS", 0.74),                            # 70-80 bucket: 0W/1L
    ]
    out = backtest(trades)
    by = {b["bucket"]: b for b in out["by_confidence_bucket"]}
    assert by["80-90"]["count"] == 2 and by["80-90"]["win_rate_pct"] == 50.0
    assert by["90+"]["count"] == 2 and by["90+"]["win_rate_pct"] == 100.0
    assert by["70-80"]["count"] == 1 and by["70-80"]["win_rate_pct"] == 0.0
    assert by["<70"]["count"] == 0 and by["<70"]["win_rate_pct"] is None


def test_backtest_max_drawdown():
    from core.backtest import backtest
    # +50% then -40%: peak 1500, trough 900 => 40% drawdown
    out = backtest([_t(50.0, "WIN"), _t(-40.0, "LOSS")], bankroll=1000.0, stake_fraction=1.0)
    assert abs(out["max_drawdown_pct"] - 40.0) < 1e-6
