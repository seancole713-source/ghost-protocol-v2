"""roadmap #1c — env-tunable, recency-guarded T23 circuit breaker.

Separate file to avoid colliding with other open PRs' test appends.
"""
import time


def _breaker_db(rows, monkeypatch):
    """Wire core.prediction.db_conn to return `rows` (list of (outcome, resolved_at))."""
    import core.prediction as _pred

    class _Cur:
        def execute(self, sql, params=None): self._sql = sql
        def fetchall(self): return list(rows)

    class _Conn:
        def cursor(self): return _Cur()

    class _Ctx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(_pred, "db_conn", lambda: _Ctx())
    monkeypatch.setattr(_pred, "CONFIDENCE_FLOOR", 0.80)


def test_breaker_raises_floor_on_recent_loss_streak(monkeypatch):
    import core.prediction as _pred
    now = int(time.time())
    rows = [("LOSS", now - i * 3600) for i in range(5)]   # 5 recent losses
    _breaker_db(rows, monkeypatch)
    floor, active, detail = _pred._circuit_breaker_floor()
    assert active is True
    assert abs(floor - 0.90) < 1e-9          # 0.80 + 0.10, under 0.92 cap
    assert detail == "5_loss_streak"


def test_breaker_relaxes_on_stale_streak(monkeypatch):
    """Deadlock fix: a 5-loss streak whose newest loss is older than
    CB_RECENCY_DAYS no longer suppresses firing."""
    import core.prediction as _pred
    now = int(time.time())
    old = now - 30 * 86400                    # 30 days ago, > default 14
    rows = [("LOSS", old - i * 3600) for i in range(5)]
    _breaker_db(rows, monkeypatch)
    floor, active, detail = _pred._circuit_breaker_floor()
    assert active is False
    assert floor == 0.80                      # back to base — engine can fire
    assert detail == "stale_streak"


def test_breaker_clear_when_not_all_losses(monkeypatch):
    import core.prediction as _pred
    now = int(time.time())
    rows = [("LOSS", now), ("WIN", now - 100), ("LOSS", now - 200),
            ("LOSS", now - 300), ("LOSS", now - 400)]
    _breaker_db(rows, monkeypatch)
    floor, active, _ = _pred._circuit_breaker_floor()
    assert active is False and floor == 0.80


def test_breaker_needs_full_streak(monkeypatch):
    import core.prediction as _pred
    now = int(time.time())
    rows = [("LOSS", now - i * 3600) for i in range(3)]   # only 3 < default 5
    _breaker_db(rows, monkeypatch)
    floor, active, _ = _pred._circuit_breaker_floor()
    assert active is False and floor == 0.80


def test_breaker_env_tunable(monkeypatch):
    import core.prediction as _pred
    now = int(time.time())
    monkeypatch.setenv("CB_LOSS_STREAK", "3")
    monkeypatch.setenv("CB_FLOOR_DELTA", "0.05")
    monkeypatch.setenv("CB_FLOOR_CAP", "0.99")
    rows = [("LOSS", now - i * 3600) for i in range(3)]   # 3-streak now trips
    _breaker_db(rows, monkeypatch)
    floor, active, detail = _pred._circuit_breaker_floor()
    assert active is True
    assert abs(floor - 0.85) < 1e-9          # 0.80 + 0.05
    assert detail == "3_loss_streak"
