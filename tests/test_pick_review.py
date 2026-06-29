"""Open pick review — withdraw when model changes mind."""
from unittest.mock import MagicMock, patch


from core import pick_review as pr


def _open_row(pid=1, sym="WOLF", conf=0.85, entry=10.0, predicted_at=0):
    return (pid, sym, "UP", conf, entry, 11.0, 9.5, predicted_at)


def test_withdraw_on_prob_low_skip():
    cur = MagicMock()
    cur.fetchall.return_value = [_open_row(predicted_at=0)]
    cur.rowcount = 1
    cur.fetchone.return_value = (None,)

    evals = [{
        "symbol": "WOLF",
        "fired": False,
        "skip_code": "v3_prob_low",
        "up_prob": 0.42,
        "min_win_proba": 0.55,
    }]
    with patch("core.prices.get_price", return_value=10.2):
        with patch("core.pnl.resolution_exit", return_value=(10.2, 2.0)):
            with patch("core.performance_log.record_pick_resolution"):
                out = pr.review_open_picks(cur, evals, [], now_ts=9999999999)
    assert len(out) == 1
    assert out[0]["reason"] == "v3_prob_low"
    assert cur.execute.call_args_list[1][0][1][0] == "WITHDRAWN"


def test_no_withdraw_when_still_fires_same_levels():
    cur = MagicMock()
    cur.fetchall.return_value = [_open_row(predicted_at=0)]
    evals = [{
        "symbol": "WOLF",
        "fired": True,
        "skip_code": None,
    }]
    all_picks = [{
        "symbol": "WOLF",
        "confidence": 0.86,
        "entry_price": 10.05,
        "target_price": 11.0,
        "stop_price": 9.5,
    }]
    out = pr.review_open_picks(cur, evals, all_picks, now_ts=9999999999)
    assert out == []


def test_supersede_on_large_entry_move():
    cur = MagicMock()
    cur.fetchall.return_value = [_open_row(entry=10.0, predicted_at=0)]
    cur.rowcount = 1
    cur.fetchone.return_value = ('{"schema":1}',)

    evals = [{"symbol": "WOLF", "fired": True}]
    all_picks = [{
        "symbol": "WOLF",
        "confidence": 0.85,
        "entry_price": 10.25,
        "target_price": 11.2,
        "stop_price": 9.6,
    }]
    with patch("core.prices.get_price", return_value=10.25):
        with patch("core.pnl.resolution_exit", return_value=(10.25, 2.5)):
            with patch("core.performance_log.record_pick_resolution"):
                out = pr.review_open_picks(cur, evals, all_picks, now_ts=9999999999)
    assert len(out) == 1
    assert out[0]["reason"] == "levels_updated"


def test_min_age_blocks_immediate_withdraw(monkeypatch):
    monkeypatch.setenv("GHOST_WITHDRAW_MIN_AGE_MIN", "60")
    cur = MagicMock()
    cur.fetchall.return_value = [_open_row(predicted_at=9999999900)]
    evals = [{"symbol": "WOLF", "fired": False, "skip_code": "v3_prob_low"}]
    out = pr.review_open_picks(cur, evals, [], now_ts=9999999999)
    assert out == []


def test_review_disabled(monkeypatch):
    monkeypatch.setenv("GHOST_OPEN_PICK_REVIEW", "0")
    cur = MagicMock()
    out = pr.review_open_picks(cur, [], [], now_ts=9999999999)
    assert out == []
    cur.execute.assert_not_called()
