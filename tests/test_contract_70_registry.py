"""Forward-only 70+ proof harness — anti-look-ahead protocol tests."""
import core.contract_70_registry as reg


def test_select_candidate_universe_requires_sample_and_wilson():
    breakdown = [
        {"symbol": "YMM", "n": 4, "wins": 4, "wilson_low": 0.5101},   # too few n
        {"symbol": "XPO", "n": 7, "wins": 5, "wilson_low": 0.3589},   # wilson too low
        {"symbol": "GOOD", "n": 20, "wins": 18, "wilson_low": 0.72},  # qualifies
        {"symbol": "GOOD2", "n": 12, "wins": 11, "wilson_low": 0.70}, # qualifies (== bar)
        {"symbol": "BAD", "n": 30, "wins": 12, "wilson_low": 0.24},   # fails
    ]
    picked = reg.select_candidate_universe(breakdown, min_n=8, min_wilson_low=0.70)
    assert picked == ["GOOD", "GOOD2"]


def test_evaluate_forward_ignores_selection_window_and_unregistered():
    registered = ["GOOD", "GOOD2"]
    cutoff = 1000
    rows = [
        # in selection window (<= cutoff): must be ignored even though wins
        {"symbol": "GOOD", "up_prob": 0.8, "eval_ts": 900, "outcome": "WIN"},
        {"symbol": "GOOD", "up_prob": 0.8, "eval_ts": 1000, "outcome": "WIN"},
        # forward wins for registered symbols
        {"symbol": "GOOD", "up_prob": 0.75, "eval_ts": 1100, "outcome": "WIN"},
        {"symbol": "GOOD2", "up_prob": 0.72, "eval_ts": 1200, "outcome": "WIN"},
        # forward but below prob floor -> ignored
        {"symbol": "GOOD", "up_prob": 0.60, "eval_ts": 1300, "outcome": "WIN"},
        # forward but unregistered symbol -> ignored
        {"symbol": "OTHER", "up_prob": 0.9, "eval_ts": 1400, "outcome": "WIN"},
        # forward loss for registered symbol
        {"symbol": "GOOD2", "up_prob": 0.71, "eval_ts": 1500, "outcome": "LOSS"},
        # unresolved -> ignored
        {"symbol": "GOOD", "up_prob": 0.8, "eval_ts": 1600, "outcome": None},
    ]
    out = reg.evaluate_forward(rows, registered_symbols=registered,
                               registered_at_ts=cutoff, prob_floor=0.70, target=0.70)
    assert out["n"] == 3        # 2 forward wins + 1 forward loss
    assert out["wins"] == 2
    assert out["basis"] == "forward_only_registered_universe"
    assert out["registered_at_ts"] == cutoff


def test_evaluate_forward_small_sample_not_wilson_proven():
    # A tiny forward sample, even 100% raw, must NOT be Wilson-proven 70+.
    rows = [
        {"symbol": "GOOD", "up_prob": 0.8, "eval_ts": 1100, "outcome": "WIN"},
        {"symbol": "GOOD", "up_prob": 0.8, "eval_ts": 1200, "outcome": "WIN"},
        {"symbol": "GOOD", "up_prob": 0.8, "eval_ts": 1300, "outcome": "WIN"},
    ]
    out = reg.evaluate_forward(rows, registered_symbols=["GOOD"],
                               registered_at_ts=1000)
    assert out["win_rate"] == 1.0
    assert out["raw_pass"] is True
    assert out["wilson_pass"] is False     # 3/3 cannot prove 70+
    assert out["status"] == "raw_70_unproven"


def test_evaluate_forward_large_clean_sample_can_prove():
    # 60 forward outcomes at ~88% clears the Wilson-proven bar honestly.
    rows = [{"symbol": "GOOD", "up_prob": 0.8, "eval_ts": 1000 + i,
             "outcome": "WIN" if i % 8 else "LOSS"} for i in range(1, 61)]
    out = reg.evaluate_forward(rows, registered_symbols=["GOOD"],
                               registered_at_ts=1000)
    assert out["n"] == 60
    assert out["wilson_pass"] is True
    assert out["status"] == "passed_wilson"


def test_register_and_load_roundtrip_uses_ghost_state_only(monkeypatch):
    writes = []

    class _Cur:
        def execute(self, sql, params=None):
            writes.append((sql, params))
        def fetchone(self):
            import json
            return (json.dumps({"registered_at_ts": 42, "symbols": ["GOOD"],
                                "min_n": 8, "min_wilson_low": 0.7,
                                "prob_floor": 0.7, "target": 0.7}),)

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import core.db as db
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(db, "ensure_ghost_state", lambda c=None: None)

    payload = reg.register_universe(["good", "GOOD"], min_n=8, min_wilson_low=0.7, now_ts=42)
    assert payload["registered_at_ts"] == 42
    assert payload["symbols"] == ["GOOD"]
    joined = "\n".join(sql for sql, _ in writes)
    assert "ghost_state" in joined
    assert "predictions" not in joined
    assert "ghost_paper_trades" not in joined

    loaded = reg.load_registry()
    assert loaded["registered_at_ts"] == 42
