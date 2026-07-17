"""70+ slice search - honest conditional-pocket discovery over resolved outcomes."""
import core.contract_70_slices as cs


def test_up_prob_bucket_matches_watcher_edges():
    assert cs.up_prob_bucket(0.30) == "<50"
    assert cs.up_prob_bucket(0.50) == "50-55"
    assert cs.up_prob_bucket(0.58) == "55-60"
    assert cs.up_prob_bucket(0.65) == "60-70"
    assert cs.up_prob_bucket(0.70) == "70+"
    assert cs.up_prob_bucket(1.0) == "70+"
    assert cs.up_prob_bucket(None) is None
    assert cs.up_prob_bucket("nope") is None


def test_summarize_slices_groups_and_uses_wilson_lower_bound():
    rows = [{"symbol": "GOOD", "outcome": "WIN"} for _ in range(18)]
    rows += [{"symbol": "GOOD", "outcome": "LOSS"} for _ in range(2)]
    rows += [{"symbol": "BAD", "outcome": "WIN"} for _ in range(6)]
    rows += [{"symbol": "BAD", "outcome": "LOSS"} for _ in range(14)]
    out = cs.summarize_slices(rows, dims=["symbol"], target=0.70)
    by = {tuple(s["key"].values())[0]: s for s in out}
    # 18/20 = 90% raw but Wilson low 0.699 < 0.70 => not proven (correctly strict)
    assert by["GOOD"]["win_rate"] == 0.9
    assert by["GOOD"]["raw_pass"] is True
    assert by["GOOD"]["wilson_pass"] is False
    assert by["BAD"]["raw_pass"] is False
    # Strongest-first ordering by wilson_low
    assert out[0]["key"]["symbol"] == "GOOD"


def test_summarize_slices_skips_rows_missing_dimension_value():
    rows = [
        {"symbol": "A", "regime_label": None, "outcome": "WIN"},
        {"symbol": "A", "regime_label": "Trend-up", "outcome": "WIN"},
        {"symbol": "A", "regime_label": "Trend-up", "outcome": "LOSS"},
    ]
    out = cs.summarize_slices(rows, dims=["regime_label"], target=0.70)
    # The None-regime row must not create a slice nor dilute Trend-up.
    assert len(out) == 1
    assert out[0]["key"]["regime_label"] == "Trend-up"
    assert out[0]["n"] == 2


def test_summarize_slices_ignores_unresolved_outcomes():
    rows = [
        {"symbol": "A", "outcome": "WIN"},
        {"symbol": "A", "outcome": None},
        {"symbol": "A", "outcome": "PENDING"},
    ]
    out = cs.summarize_slices(rows, dims=["symbol"], target=0.70)
    assert out[0]["n"] == 1 and out[0]["wins"] == 1


def test_find_qualified_slices_requires_sample_and_wilson_bar():
    # 45/50 -> Wilson low ~0.786 clears 0.70 and n>=8: qualifies.
    strong = [{"symbol": "GOOD", "up_prob": 0.72, "regime_label": "Trend-up",
               "outcome": "WIN" if i < 45 else "LOSS"} for i in range(50)]
    res = cs.find_qualified_slices(strong, min_n=8, min_wilson_low=0.70)
    assert res["status"] == "qualified_slice_found"
    assert res["qualified_count"] >= 1
    assert all(s["wilson_low"] >= 0.70 for s in res["qualified"])
    assert res["qualified"][0]["wilson_pass"] is True


def test_find_qualified_slices_rejects_lucky_small_sample():
    # 3/3 = 100% raw but cannot be Wilson-proven at n=3.
    tiny = [{"symbol": "LUCKY", "outcome": "WIN"} for _ in range(3)]
    res = cs.find_qualified_slices(tiny, min_n=8, min_wilson_low=0.70)
    assert res["status"] == "no_qualified_slice"
    assert res["qualified_count"] == 0
    # Still surfaces the best slice for transparency, flagged under-sample.
    best = [b for b in res["best_per_dimension"] if b["key"].get("symbol") == "LUCKY"]
    assert best and best[0].get("under_min_sample") is True


def test_find_qualified_slices_empty_is_honest():
    res = cs.find_qualified_slices([], min_n=8, min_wilson_low=0.70)
    assert res["status"] == "no_qualified_slice"
    assert res["resolved_n"] == 0
    assert res["qualified"] == []


def test_flat_non_discriminative_prob_yields_no_qualified_band():
    # Mirrors the live pathology: every prob band runs ~0.56 -> no band qualifies.
    rows = []
    for band_prob in (0.57, 0.65, 0.75):
        for i in range(40):
            rows.append({"symbol": "MIX", "up_prob": band_prob,
                         "outcome": "WIN" if i < 23 else "LOSS"})  # 23/40 = 57.5%
    res = cs.find_qualified_slices(rows, min_n=8, min_wilson_low=0.70)
    assert res["status"] == "no_qualified_slice"
    # The 70+ band exists but is honestly below the bar.
    bands = cs.summarize_slices(rows, dims=["up_prob_bucket"], target=0.70)
    top = [b for b in bands if b["key"]["up_prob_bucket"] == "70+"]
    assert top and top[0]["wilson_pass"] is False


def test_slices_endpoint_routes(monkeypatch):
    from fastapi.testclient import TestClient
    from wolf_app import APP

    monkeypatch.setattr(
        "core.contract_70_slices.contract_70_slice_search",
        lambda **kw: {"ok": True, "read_only": True, "status": "no_qualified_slice",
                      "seen": kw},
    )
    r = TestClient(APP).get("/api/watcher/contract-70/slices?days=30&min_n=9")
    assert r.status_code == 200
    body = r.json()
    assert body["read_only"] is True
    assert body["seen"]["days"] == 30
    assert body["seen"]["min_n"] == 9


def test_fired_dimension_labels_and_separates():
    # 'fired' is Ghost's real conviction: cleared every gate vs did not.
    assert cs._dim_value({"fired": True}, "fired") == "fired"
    assert cs._dim_value({"fired": False}, "fired") == "unfired"
    assert cs._dim_value({"fired": None}, "fired") is None

    rows = [{"symbol": "X", "fired": True, "outcome": "WIN" if i < 16 else "LOSS"} for i in range(20)]
    rows += [{"symbol": "X", "fired": False, "outcome": "WIN" if i < 4 else "LOSS"} for i in range(20)]
    out = cs.summarize_slices(rows, dims=["fired"], target=0.70)
    by = {tuple(s["key"].values())[0]: s for s in out}
    assert by["fired"]["wins"] == 16 and by["fired"]["n"] == 20
    assert by["unfired"]["wins"] == 4 and by["unfired"]["n"] == 20
    # Strongest-first ordering surfaces the fired population.
    assert out[0]["key"]["fired"] == "fired"


def test_fired_dimension_included_in_default_search():
    # A strong, large fired population must be discoverable as a qualified slice.
    # Sized to survive the Sidak family-wide correction across the full default
    # dimension set (borderline slices are intentionally rejected now).
    rows = [{"symbol": "X", "fired": True, "regime_label": "Trend-up",
             "up_prob": 0.72, "outcome": "WIN" if i < 95 else "LOSS"} for i in range(100)]
    rows += [{"symbol": "X", "fired": False, "regime_label": "Choppy",
              "up_prob": 0.72, "outcome": "LOSS"} for _ in range(30)]
    res = cs.find_qualified_slices(rows, min_n=8, min_wilson_low=0.70)
    assert res["status"] == "qualified_slice_found"
    keys = [q["key"] for q in res["qualified"]]
    assert any(k.get("fired") == "fired" for k in keys)


def test_fired_dimension_missing_value_skips_row():
    rows = [
        {"symbol": "A", "fired": None, "outcome": "WIN"},
        {"symbol": "A", "fired": True, "outcome": "WIN"},
        {"symbol": "A", "fired": True, "outcome": "LOSS"},
    ]
    out = cs.summarize_slices(rows, dims=["fired"], target=0.70)
    # The None-fired row must not create an slice nor dilute the fired slice.
    assert len(out) == 1
    assert out[0]["key"]["fired"] == "fired"
    assert out[0]["n"] == 2


def test_expired_counts_as_resolved_non_win_in_slices():
    rows = [
        {"symbol": "BILL", "outcome": "WIN"},
        {"symbol": "BILL", "outcome": "LOSS"},
        {"symbol": "BILL", "outcome": "EXPIRED"},
        {"symbol": "BILL", "outcome": None},
    ]
    out = cs.summarize_slices(rows, dims=["symbol"], target=0.70)
    assert out[0]["n"] == 3
    assert out[0]["wins"] == 1
    assert out[0]["win_rate"] == round(1 / 3, 4)


def test_regime_gate_flag_dimension_labels():
    for flag in ("adx_trending", "above_ema200", "ema_trend_bullish"):
        assert cs._dim_value({flag: 1}, flag) == "yes"
        assert cs._dim_value({flag: 0}, flag) == "no"
        assert cs._dim_value({flag: None}, flag) is None
        assert cs._dim_value({}, flag) is None


def test_regime_gate_flags_are_searched_by_default():
    # A strong pocket conditioned on Ghost's own gate flags must be discoverable.
    rows = [{"symbol": "X", "adx_trending": 1, "above_ema200": 1, "up_prob": 0.6,
             "outcome": "WIN" if i < 46 else "LOSS"} for i in range(52)]
    rows += [{"symbol": "X", "adx_trending": 0, "above_ema200": 0, "up_prob": 0.6,
              "outcome": "LOSS"} for _ in range(40)]
    res = cs.find_qualified_slices(rows, min_n=8, min_wilson_low=0.70)
    assert res["status"] == "qualified_slice_found"
    keys = [q["key"] for q in res["qualified"]]
    assert any(k.get("adx_trending") == "yes" for k in keys)
    # The losing (no-trend) pocket must never qualify.
    assert not any(k.get("adx_trending") == "no" for k in keys)


def test_regime_gate_flag_missing_value_skips_row():
    rows = [
        {"symbol": "A", "adx_trending": None, "outcome": "WIN"},
        {"symbol": "A", "adx_trending": 1, "outcome": "WIN"},
        {"symbol": "A", "adx_trending": 1, "outcome": "LOSS"},
    ]
    out = cs.summarize_slices(rows, dims=["adx_trending"], target=0.70)
    assert len(out) == 1
    assert out[0]["key"]["adx_trending"] == "yes"
    assert out[0]["n"] == 2


def test_regime_gate_flag_expired_counts_as_non_win():
    rows = [{"symbol": "Y", "adx_trending": 1, "outcome": o}
            for o in (["WIN"] * 3 + ["EXPIRED"] * 2)]
    out = cs.summarize_slices(rows, dims=["adx_trending"], target=0.70)[0]
    assert out["wins"] == 3 and out["n"] == 5


def test_sidak_family_z_grows_with_family_size():
    # At k=1 the family z equals the standard 95% one-sided z (1.96).
    assert abs(cs._sidak_family_z(1) - 1.959964) < 1e-3
    # More simultaneous tests -> stricter z (monotonic non-decreasing).
    z1 = cs._sidak_family_z(1)
    z20 = cs._sidak_family_z(20)
    z200 = cs._sidak_family_z(200)
    assert z1 < z20 < z200
    assert z20 > 2.9  # ~3.02


def test_norm_ppf_matches_known_quantiles():
    assert abs(cs._norm_ppf(0.975) - 1.959964) < 1e-4
    assert abs(cs._norm_ppf(0.5) - 0.0) < 1e-9
    assert abs(cs._norm_ppf(0.995) - 2.575829) < 1e-4


def test_family_correction_rejects_borderline_slice_that_passes_naive():
    # A borderline slice whose NAIVE 95% Wilson low clears 0.70 but whose
    # family-corrected bound does not must NOT qualify once many slices are
    # searched. Build one strong-ish slice plus filler to inflate the family.
    rows = [{"symbol": "BORD", "up_prob": 0.6, "outcome": "WIN" if i < 40 else "LOSS"} for i in range(48)]
    for s in range(40):
        for i in range(9):
            rows.append({"symbol": f"S{s}", "up_prob": 0.6, "outcome": "WIN" if i < 5 else "LOSS"})
    res = cs.find_qualified_slices(rows, dimension_sets=[("symbol",)], min_n=8, min_wilson_low=0.70)
    assert res["family_size"] >= 40
    assert res["multiple_comparisons_correction"] == "sidak"
    # Whatever the naive bound, the family-corrected search must reject it here.
    res2 = cs.find_qualified_slices(rows, dimension_sets=[("symbol",)], min_n=8, min_wilson_low=0.70)
    assert res2["qualified_count"] == 0


def test_family_correction_still_admits_a_genuinely_strong_slice():
    # 92/100 is strong enough to clear even a single-family Sidak bar.
    strong = [{"symbol": "STRONG", "up_prob": 0.6, "outcome": "WIN" if i < 92 else "LOSS"} for i in range(100)]
    res = cs.find_qualified_slices(strong, dimension_sets=[("symbol",)], min_n=8, min_wilson_low=0.70)
    assert res["qualified_count"] == 1
    q = res["qualified"][0]
    assert q["key"]["symbol"] == "STRONG"
    assert q["family_wilson_low"] >= 0.70
    # The family-corrected bound is never HIGHER than the naive bound.
    assert q["family_wilson_low"] <= q["wilson_low"]


def test_family_fields_present_in_payload():
    rows = [{"symbol": "X", "outcome": "WIN"} for _ in range(10)]
    res = cs.find_qualified_slices(rows, dimension_sets=[("symbol",)], min_n=8, min_wilson_low=0.70)
    assert "family_size" in res and "family_z" in res
    assert res["family_confidence"] == 0.95
