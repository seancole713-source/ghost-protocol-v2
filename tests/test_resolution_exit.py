"""Limit-fill exit pricing for reconcile/watchdog resolution."""


def test_win_up_caps_at_target():
    from core.pnl import resolution_exit

    exit_price, pnl = resolution_exit("WIN", "UP", 60.8, 62.32, 59.0, 63.02)
    assert exit_price == 62.32
    assert pnl == 2.5


def test_loss_up_fills_at_stop_when_evidence_is_at_or_above_stop():
    from core.pnl import resolution_exit

    # Callers without an observed price pass entry (above a long stop).
    exit_price, pnl = resolution_exit("LOSS", "UP", 63.36, 64.94, 62.3304, 63.36)
    assert exit_price == 62.3304
    assert pnl == round((62.3304 - 63.36) / 63.36 * 100, 3)


def test_loss_up_gap_through_stop_books_real_loss():
    """Audit repro: a gap to 8.00 under a 9.50 long stop is -20%, not -5%."""
    from core.pnl import resolution_exit

    assert resolution_exit("LOSS", "UP", 10.0, 11.0, 9.5, 8.0) == (8.0, -20.0)
    exit_price, pnl = resolution_exit("LOSS", "UP", 63.36, 64.94, 62.3304, 61.68)
    assert exit_price == 61.68
    assert pnl == round((61.68 - 63.36) / 63.36 * 100, 3)


def test_win_down_caps_at_target():
    from core.pnl import resolution_exit

    exit_price, pnl = resolution_exit("WIN", "DOWN", 100.0, 95.0, 105.0, 92.0)
    assert exit_price == 95.0
    assert pnl == 5.0


def test_loss_down_fills_at_stop_when_evidence_is_at_or_below_stop():
    from core.pnl import resolution_exit

    exit_price, pnl = resolution_exit("LOSS", "DOWN", 100.0, 95.0, 105.0, 100.0)
    assert exit_price == 105.0
    assert pnl == -5.0


def test_loss_down_gap_through_stop_books_real_loss():
    from core.pnl import resolution_exit

    exit_price, pnl = resolution_exit("LOSS", "DOWN", 100.0, 95.0, 105.0, 108.0)
    assert exit_price == 108.0
    assert pnl == -8.0


def test_loss_never_better_than_stop_and_missing_evidence_uses_stop():
    from core.pnl import resolution_exit

    # Evidence on the favourable side of the stop never improves the fill.
    assert resolution_exit("LOSS", "UP", 10.0, 11.0, 9.5, 9.9)[0] == 9.5
    assert resolution_exit("LOSS", "DOWN", 10.0, 9.0, 10.5, 10.1)[0] == 10.5
    for bad in (None, 0.0, -1.0, float("nan"), "x"):
        assert resolution_exit("LOSS", "UP", 10.0, 11.0, 9.5, bad)[0] == 9.5


def test_win_gap_beyond_target_is_not_credited():
    from core.pnl import resolution_exit

    assert resolution_exit("WIN", "UP", 10.0, 11.0, 9.5, 14.0) == (11.0, 10.0)
    assert resolution_exit("WIN", "DOWN", 10.0, 9.0, 10.5, 6.0) == (9.0, 10.0)


def test_expired_uses_market_price():
    from core.pnl import resolution_exit

    exit_price, pnl = resolution_exit("EXPIRED", "UP", 50.0, 55.0, 48.0, 51.5)
    assert exit_price == 51.5
    assert pnl == 3.0
