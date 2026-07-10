"""PR #165: point-in-time SEC fundamentals as model features — no lookahead."""
import core.fundamental_features as ff


def _data():
    # Q1 FY25 filed May 2025; Q1 FY26 filed May 2026 (amended Jun 2026).
    return {
        "eps": [
            {"val": 1.00, "end": "2025-03-31", "fp": "Q1", "filed": "2025-05-01"},
            {"val": 1.50, "end": "2026-03-31", "fp": "Q1", "filed": "2026-05-01"},
            {"val": 1.60, "end": "2026-03-31", "fp": "Q1", "filed": "2026-06-15"},  # amendment
        ],
        "rev": [
            {"val": 100.0, "end": "2025-03-31", "fp": "Q1", "filed": "2025-05-01"},
            {"val": 120.0, "end": "2026-03-31", "fp": "Q1", "filed": "2026-05-01"},
        ],
    }


def test_no_lookahead_before_filing():
    # Bar on 2026-04-15: Q1-FY26 exists (quarter ended 3/31) but is NOT filed
    # yet — the feature must not see it.
    out = ff.pit_features_from_series(_data(), "2026-04-15")
    assert out["fund_eps_yoy"] == 0.0     # only one visible quarter -> neutral
    assert out["fund_rev_yoy"] == 0.0
    # Recency reflects the year-old filing.
    assert out["fund_days_since_filing"] > 300


def test_visible_after_filing():
    out = ff.pit_features_from_series(_data(), "2026-05-10")
    assert abs(out["fund_eps_yoy"] - 0.50) < 1e-6      # 1.00 -> 1.50 (+50%)
    assert abs(out["fund_rev_yoy"] - 0.20) < 1e-6      # 100 -> 120 (+20%)
    assert out["fund_days_since_filing"] == 9.0


def test_amendment_only_wins_after_its_own_filed_date():
    before = ff.pit_features_from_series(_data(), "2026-06-01")
    after = ff.pit_features_from_series(_data(), "2026-06-20")
    assert abs(before["fund_eps_yoy"] - 0.50) < 1e-6   # original 1.50
    assert abs(after["fund_eps_yoy"] - 0.60) < 1e-6    # amended 1.60


def test_neutral_on_missing_data():
    out = ff.pit_features_from_series(None, "2026-05-10")
    assert out == {"fund_eps_yoy": 0.0, "fund_rev_yoy": 0.0,
                   "fund_days_since_filing": 365.0}


def test_yoy_clamped():
    data = {"eps": [
        {"val": 0.01, "end": "2025-03-31", "fp": "Q1", "filed": "2025-05-01"},
        {"val": 5.00, "end": "2026-03-31", "fp": "Q1", "filed": "2026-05-01"},
    ], "rev": []}
    out = ff.pit_features_from_series(data, "2026-05-10")
    assert out["fund_eps_yoy"] == 2.0   # +49900% clamped to +200%


def test_feature_cols_gated_by_env(monkeypatch):
    import core.signal_engine as se
    monkeypatch.delenv("V3_FUNDAMENTAL_FEATURES", raising=False)
    cols_off = se._active_feature_cols()
    assert "fund_eps_yoy" not in cols_off
    monkeypatch.setenv("V3_FUNDAMENTAL_FEATURES", "on")
    cols_on = se._active_feature_cols()
    for c in ff.FUNDAMENTAL_FEATURE_NAMES:
        assert c in cols_on
    # Schema differs -> stored models retrain. Everything else unchanged.
    assert [c for c in cols_on if c not in ff.FUNDAMENTAL_FEATURE_NAMES] == cols_off


def test_get_features_never_raises(monkeypatch):
    monkeypatch.setattr(ff, "_series", lambda s: (_ for _ in ()).throw(RuntimeError("net down")))
    out = ff.get_fundamental_features_for_date("AAPL", "2026-05-10")
    assert out["fund_eps_yoy"] == 0.0
