from core.stats_direction import compute_stats_by_direction


class StubCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


def test_compute_stats_by_direction_maps_up_to_buy_and_rest_to_sell():
    cur = StubCursor(
        [
            ("UP", 12, 7, 1.23),
            ("DOWN", 20, 3, -0.75),
        ]
    )

    out = compute_stats_by_direction(cur)

    assert out["ok"] is True
    assert out["by_direction"]["BUY"]["total"] == 12
    assert out["by_direction"]["BUY"]["wins"] == 7
    assert out["by_direction"]["BUY"]["losses"] == 5
    assert out["by_direction"]["BUY"]["win_rate_pct"] == 58.3
    assert out["by_direction"]["BUY"]["avg_pnl"] == 1.23

    assert out["by_direction"]["SELL"]["total"] == 20
    assert out["by_direction"]["SELL"]["wins"] == 3
    assert out["by_direction"]["SELL"]["losses"] == 17
    assert out["by_direction"]["SELL"]["win_rate_pct"] == 15.0
    assert out["by_direction"]["SELL"]["avg_pnl"] == -0.75
