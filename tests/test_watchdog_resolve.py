"""Watchdog shares bar-path TP/SL resolution with reconcile."""
from core.tp_sl_resolve import resolve_open_prediction


def test_watchdog_bar_path_matches_reconcile():
    ts = 1717340400
    rows = [{"ts": "2026-06-03T00:00:00Z", "high": 12.0, "low": 10.5}]
    assert resolve_open_prediction(
        direction="UP",
        target=11.0,
        stop=9.5,
        predicted_at=ts,
        hold_bars=3,
        daily_bars=rows,
        snapshot_price=10.6,
    ) == "WIN"
