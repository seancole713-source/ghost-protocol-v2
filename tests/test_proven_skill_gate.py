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


def test_review_counts_expiries_as_non_wins(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_RESOLVED", "10")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_TP_RATE", "0.55")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_AVG_PNL_PCT", "0.0")
    # ITRI public shadow stats: 15 wins, 5 losses, 8 expiries. Expiries are
    # resolved non-wins inside the promised horizon, so honest TP rate is 15/28.
    out = g.review("ITRI", resolved=28, wins=15, avg_pnl_pct=0.807)
    assert out["ok"] is False
    assert out["tp_rate"] == 0.5357


def test_symbol_review_disabled(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_GATE", "0")
    out = g.symbol_review(
        "ANY", direction="UP", model_sha256="sha", feature_schema="feature",
        label_schema="label", validation_schema="validation", hold_bars=5,
    )
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
    out = g.global_calibration_review(
        0.95, direction="UP", model_sha256="sha", feature_schema="feature",
        label_schema="label", validation_schema="validation", hold_bars=5,
    )
    assert out["ok"] is True and out["disabled"] is True


def test_symbol_review_requires_exact_identity(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_GATE", "1")
    out = g.symbol_review(
        "WOLF", direction="UP", model_sha256="", feature_schema="feature",
        label_schema="label", validation_schema="validation", hold_bars=5,
    )
    assert out["ok"] is False
    assert out["fail_reason"] == "skill_identity_missing"


def test_symbol_review_filters_exact_generation(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_GATE", "1")
    seen = {}

    class _Cur:
        def execute(self, sql, params):
            seen["sql"] = sql
            seen["params"] = params

        def fetchone(self):
            return (20, 15, 0.5)

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("core.db.db_conn", lambda: _Conn())
    out = g.symbol_review(
        "wolf", direction="down", model_sha256="sha-down", feature_schema="feature-v4",
        label_schema="label-v2", validation_schema="validation-v3", hold_bars=5,
    )
    assert out["ok"] is True
    assert "direction=%s" in seen["sql"]
    assert "model_sha256=%s" in seen["sql"]
    assert "feature_schema=%s" in seen["sql"]
    assert seen["params"] == (
        "WOLF", "DOWN", "sha-down", "feature-v4", "label-v2", "validation-v3", 5,
    )


def test_global_calibration_uses_neutral_model_probability(monkeypatch):
    monkeypatch.setenv("V3_OVERCONFIDENCE_GATE", "1")
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    monkeypatch.setenv("V3_OVERCONFIDENCE_MIN_SAMPLES", "1")
    monkeypatch.setenv("V3_OVERCONFIDENCE_MIN_WIN_RATE", "0.5")
    seen = {}

    class _Cur:
        def execute(self, sql, params):
            seen["sql"] = sql
            seen["params"] = params

        def fetchone(self):
            return (2, 2)

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("core.db.db_conn", lambda: _Conn())
    out = g.global_calibration_review(
        0.9, direction="DOWN", model_sha256="sha-down", feature_schema="feature",
        label_schema="label", validation_schema="validation", hold_bars=5,
    )
    assert out["ok"] is True
    assert "prob_live_recalibrated >= %s" in seen["sql"]
    assert "up_prob >= %s" not in seen["sql"]
    assert "feature_schema=%s" in seen["sql"]
    assert seen["params"][1:] == (
        "DOWN", "sha-down", "feature", "label", "validation", 5,
    )
