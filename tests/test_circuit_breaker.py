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


# ── CircuitBreaker class state-machine tests (PR #77 / F11) ─────────────

def test_cb_closed_allows_and_records():
    """Closed breaker allows calls and records success/failure."""
    from core.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_seconds=60)
    assert cb.state == "closed"
    assert cb.allow() is True
    cb.record_success()
    assert cb.state == "closed"
    assert cb.allow() is True


def test_cb_opens_after_threshold_failures():
    """After threshold consecutive failures, circuit opens."""
    from core.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_seconds=60)
    for _ in range(3):
        assert cb.allow() is True
        cb.record_failure()
    assert cb.state == "half_open"  # initial trip: probes available
    # Probes exhausted after half_open_max calls
    for _ in range(cb.half_open_max):
        cb.allow()
    assert cb.state == "open"
    assert cb.allow() is False  # truly blocked


def test_cb_blocks_during_cooldown():
    """Circuit stays open during cooldown, blocks all calls."""
    from core.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker("test", failure_threshold=2, cooldown_seconds=60)
    for _ in range(2):
        assert cb.allow() is True
        cb.record_failure()
    # Exhaust probes
    for _ in range(cb.half_open_max):
        cb.allow()
    assert cb.state == "open"
    assert cb.allow() is False


def test_cb_half_open_probe_success_closes():
    """A successful probe in half-open state closes the circuit."""
    from core.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker("test", failure_threshold=2, cooldown_seconds=0)
    for _ in range(2):
        assert cb.allow() is True
        cb.record_failure()
    # Cooldown=0, so circuit resets immediately
    assert cb.allow() is True  # probe
    cb.record_success()
    assert cb.state == "closed"


def test_cb_half_open_probe_failure_needs_threshold_to_retrip():
    """After cooldown, a single probe failure does NOT re-trip — it takes
    failure_threshold consecutive failures (same as initial trip)."""
    from core.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker("test", failure_threshold=2, cooldown_seconds=60)
    for _ in range(2):
        assert cb.allow() is True
        cb.record_failure()
    # Exhaust probes
    for _ in range(cb.half_open_max):
        cb.allow()
    assert cb.state == "open"
    # Manually expire the cooldown
    cb._circuit_open_until = 0.0
    cb._failure_count = 0
    cb._half_open_probes = 0
    # After cooldown, allow() grants a fresh probe
    assert cb.allow() is True
    cb.record_failure()
    # One failure is not enough — circuit stays closed
    assert cb.state == "closed"
    # Second consecutive failure re-trips
    assert cb.allow() is True
    cb.record_failure()
    assert cb.state == "half_open"


def test_cb_rate_limit_auto_open():
    """Rate-limit threshold auto-opens the circuit."""
    from core.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker("test", rate_limit_max_calls=3, rate_limit_window_s=60, cooldown_seconds=60)
    for _ in range(3):
        assert cb.allow() is True
    # 4th call should be blocked by rate-limit
    assert cb.allow() is False
    assert cb.state == "open"


def test_cb_rate_limit_exhausts_probes():
    """Rate-limit open exhausts half-open probes immediately (PR #72 fix)."""
    from core.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker("test", rate_limit_max_calls=2, rate_limit_window_s=60, cooldown_seconds=60)
    for _ in range(2):
        assert cb.allow() is True
    assert cb.allow() is False  # rate-limit blocked
    assert cb.state == "open"   # not half_open — probes exhausted


def test_cb_reset_clears_state():
    """Manual reset clears all state."""
    from core.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker("test", failure_threshold=2, cooldown_seconds=60)
    for _ in range(2):
        assert cb.allow() is True
        cb.record_failure()
    cb.reset()
    assert cb.state == "closed"
    assert cb.allow() is True
