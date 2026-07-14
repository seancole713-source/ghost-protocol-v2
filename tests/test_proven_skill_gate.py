"""PR #155: real-fire proven-skill blocker."""
import core.proven_skill_gate as g


def test_review_blocks_too_few_rows(monkeypatch):
    monkeypatch.delenv("V3_PROVEN_SKILL_MIN_RESOLVED", raising=False)
    out = g.review("GME", resolved=9, wins=9, avg_pnl_pct=1.0)
    assert out["ok"] is False
    assert out["fail_reason"].startswith("resolved<")


def test_review_blocks_low_tp_rate(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_RESOLVED", "10")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_TP_RATE", "0.55")
    out = g.review("GME", resolved=30, wins=10, avg_pnl_pct=0.2)
    assert out["ok"] is False
    assert out["tp_rate"] == 0.3333
    assert out["fail_reason"].startswith("tp_rate<")


def test_review_blocks_negative_avg_pnl(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_RESOLVED", "10")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_TP_RATE", "0.55")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_AVG_PNL_PCT", "0.0")
    out = g.review("XYZ", resolved=20, wins=12, avg_pnl_pct=-0.1)
    assert out["ok"] is False
    assert out["fail_reason"].startswith("avg_pnl_pct<")


def test_review_allows_proven_symbol(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_RESOLVED", "10")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_TP_RATE", "0.55")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_AVG_PNL_PCT", "0.0")
    # ITRI public shadow stats under contract-70 semantics: if n=20 includes
    # resolved WIN/LOSS/EXPIRED and 15 are wins, TP rate is 75%.
    out = g.review("ITRI", resolved=20, wins=15, avg_pnl_pct=0.807)
    assert out["ok"] is True
    assert out["tp_rate"] >= 0.55


def test_symbol_review_disabled(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_GATE", "0")
    out = g.symbol_review("ANY")
    assert out["ok"] is True and out["disabled"] is True


def test_calibration_review_not_applicable_below_threshold(monkeypatch):
    monkeypatch.delenv("V3_OVERCONFIDENCE_PROB_THRESHOLD", raising=False)
    out = g.calibration_review(prob=0.62, samples=25, wins=10)
    assert out["ok"] is True and out["not_applicable"] is True


def test_calibration_review_blocks_inverted_high_bucket(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    monkeypatch.setenv("V3_OVERCONFIDENCE_PROB_THRESHOLD", "0.70")
    monkeypatch.setenv("V3_OVERCONFIDENCE_MIN_SAMPLES", "20")
    monkeypatch.setenv("V3_OVERCONFIDENCE_MIN_WIN_RATE", "0.55")  # cannot loosen contract-70
    out = g.calibration_review(prob=0.82, samples=25, wins=10)  # 40% actual
    assert out["ok"] is False
    assert out["min_win_rate"] == 0.70
    assert out["fail_reason"].startswith("high_prob_bucket_wr<0.70")


def test_calibration_review_allows_good_high_bucket(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    monkeypatch.setenv("V3_OVERCONFIDENCE_PROB_THRESHOLD", "0.70")
    monkeypatch.setenv("V3_OVERCONFIDENCE_MIN_SAMPLES", "20")
    monkeypatch.setenv("V3_OVERCONFIDENCE_MIN_WIN_RATE", "0.55")  # cannot loosen contract-70
    out = g.calibration_review(prob=0.82, samples=30, wins=22)
    assert out["ok"] is True


def test_overconfidence_win_test_can_tighten_but_not_weaken_contract_70(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    monkeypatch.setenv("V3_OVERCONFIDENCE_MIN_WIN_RATE", "0.55")
    assert g.overconfidence_min_win_rate() == 0.70
    monkeypatch.setenv("V3_OVERCONFIDENCE_MIN_WIN_RATE", "0.80")
    assert g.overconfidence_min_win_rate() == 0.80


def test_global_calibration_review_disabled(monkeypatch):
    monkeypatch.setenv("V3_OVERCONFIDENCE_GATE", "0")
    out = g.global_calibration_review(0.95)
    assert out["ok"] is True and out["disabled"] is True


def test_symbol_review_counts_expired_as_resolved_non_win(monkeypatch):
    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
        def fetchone(self):
            return (12, 6, 0.4)

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import core.db as db
    monkeypatch.setenv("V3_PROVEN_SKILL_GATE", "1")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_RESOLVED", "10")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_TP_RATE", "0.55")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_AVG_PNL_PCT", "0.0")
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())

    out = g.symbol_review("BILL")
    assert out["ok"] is False
    assert out["resolved"] == 12
    assert out["tp_rate"] == 0.5
    assert out["fail_reason"].startswith("tp_rate<")
    assert "'EXPIRED'" in captured["sql"]
    assert captured["params"] == ("BILL",)


def test_global_calibration_review_counts_expired_as_resolved_non_win(monkeypatch):
    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
        def fetchone(self):
            return (25, 10)  # includes EXPIRED in denominator

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import core.db as db
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    monkeypatch.setenv("V3_OVERCONFIDENCE_GATE", "1")
    monkeypatch.setenv("V3_OVERCONFIDENCE_PROB_THRESHOLD", "0.70")
    monkeypatch.setenv("V3_OVERCONFIDENCE_MIN_SAMPLES", "20")
    monkeypatch.setattr(db, "db_conn", lambda: _Conn())

    out = g.global_calibration_review(0.82)
    assert out["ok"] is False
    assert out["samples"] == 25
    assert out["wins"] == 10
    assert out["win_rate"] == 0.4
    assert "'EXPIRED'" in captured["sql"]
    assert captured["params"] == (0.7,)
