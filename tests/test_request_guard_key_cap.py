"""SEC-03: the failure-lockout map must stay at its key cap even when every
bucket is non-empty (stale buckets expire; hard cap evicts the rest)."""
from shared.request_guard import FailureLockout


def _lockout(monkeypatch, cap=64):
    monkeypatch.delenv("T_LIMIT", raising=False)
    monkeypatch.delenv("T_WINDOW", raising=False)
    lk = FailureLockout(limit_env="T_LIMIT", window_env="T_WINDOW",
                        default_limit=3, default_window_s=900)
    monkeypatch.setattr(lk, "_MAX_KEYS", cap)
    return lk


def test_stale_non_empty_buckets_expire_at_the_cap(monkeypatch):
    lk = _lockout(monkeypatch)
    t0 = 1_000_000.0
    for i in range(64):
        lk.record_failure(f"old-{i}", now=t0)
    # Long after the window, new keys arrive: the old one-failure buckets are
    # stale but non-empty and must not keep the map above the cap.
    for i in range(20):
        lk.record_failure(f"new-{i}", now=t0 + 10_000)
    assert len(lk._fails) <= 64
    assert not any(k.startswith("old-") for k in lk._fails)


def test_hard_cap_holds_with_all_buckets_in_window(monkeypatch):
    lk = _lockout(monkeypatch)
    t0 = 1_000_000.0
    for i in range(200):
        lk.record_failure(f"k-{i}", now=t0 + i * 0.001)
    assert len(lk._fails) <= 64
    assert "k-199" in lk._fails  # most recent survives


def test_hard_cap_prefers_evicting_unlocked_keys(monkeypatch):
    lk = _lockout(monkeypatch, cap=8)
    t0 = 1_000_000.0
    for _ in range(3):  # limit=3 -> locked
        lk.record_failure("attacker", now=t0)
    for i in range(50):
        lk.record_failure(f"noise-{i}", now=t0 + 1 + i * 0.001)
    assert len(lk._fails) <= 8
    assert lk.retry_after("attacker", now=t0 + 60) > 0
