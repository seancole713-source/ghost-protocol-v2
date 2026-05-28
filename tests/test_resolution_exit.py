"""Limit-fill exit pricing for reconcile/watchdog resolution."""


def test_win_up_caps_at_target():
    from core.pnl import resolution_exit

    exit_price, pnl = resolution_exit("WIN", "UP", 60.8, 62.32, 59.0, 63.02)
    assert exit_price == 62.32
    assert pnl == 2.5


def test_loss_up_caps_at_stop():
    from core.pnl import resolution_exit

    exit_price, pnl = resolution_exit("LOSS", "UP", 63.36, 64.94, 62.3304, 61.68)
    assert exit_price == 62.3304
    assert pnl == round((62.3304 - 63.36) / 63.36 * 100, 3)


def test_win_down_caps_at_target():
    from core.pnl import resolution_exit

    exit_price, pnl = resolution_exit("WIN", "DOWN", 100.0, 95.0, 105.0, 92.0)
    assert exit_price == 95.0
    assert pnl == 5.0


def test_loss_down_caps_at_stop():
    from core.pnl import resolution_exit

    exit_price, pnl = resolution_exit("LOSS", "DOWN", 100.0, 95.0, 105.0, 108.0)
    assert exit_price == 105.0
    assert pnl == -5.0


def test_expired_uses_market_price():
    from core.pnl import resolution_exit

    exit_price, pnl = resolution_exit("EXPIRED", "UP", 50.0, 55.0, 48.0, 51.5)
    assert exit_price == 51.5
    assert pnl == 3.0
