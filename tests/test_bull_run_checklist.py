"""Regression tests for the evidence-gated YMM bull-case checklist."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from core import bull_run_checklist as bc
from core import bull_run_ledger as bl


AFTER_RELEASE = bc._EVENT_EVIDENCE_NOT_BEFORE_TS + 3_600
SOURCE = "https://ir.fulltruckalliance.com/2026-08-19-Full-Truck-Alliance-Q2-Results"


def _full_values(**overrides):
    values = {
        "revenue_actual_usd_m": 485.0,
        "eps_adjusted_ads_usd": 0.23,
        "transaction_growth_pct": 42.0,
        "order_growth_pct": 22.0,
        "shipper_growth_pct": 16.0,
        "profitability_improved": True,
        "guidance_outcome": "raised_accelerating",
        "premarket_gap_pct": 8.0,
        "relative_volume": 5.0,
        "price_change_pct": 8.0,
        "live_price": 11.10,
    }
    values.update(overrides)
    return values


def _record(value, *, unit=None):
    return bc._evidence(
        value,
        source=SOURCE,
        as_of_ts=AFTER_RELEASE,
        unit=unit,
        provenance="test",
    )


def _payload(**values):
    growth_keys = {
        "transaction_growth_pct", "order_growth_pct", "shipper_growth_pct",
        "profitability_improved",
    }
    consensus_keys = {"revenue_actual_usd_m", "eps_adjusted_ads_usd", "guidance_outcome"}
    evidence = {}
    for key, value in values.items():
        record = {
            "value": value,
            "source": SOURCE,
            "as_of_ts": AFTER_RELEASE,
            "source_timestamp": AFTER_RELEASE,
            "observation_timestamp": AFTER_RELEASE,
            "unit": "USD millions" if key == "revenue_actual_usd_m" else "reported unit",
            "currency": "USD" if key in {"revenue_actual_usd_m", "eps_adjusted_ads_usd"} else "N/A",
            "basis": "adjusted" if key == "eps_adjusted_ads_usd" else "reported",
            "calculation_methodology": "operator-transcribed claim; pending independent reconciliation",
        }
        if key in consensus_keys:
            record["expected_value"] = 0.0
        if key in growth_keys:
            record["comparable_prior_period_value"] = 0.0
        evidence[key] = record
    return {
        "scenario_id": bc.SCENARIO["scenario_id"],
        "period": bc.SCENARIO["period"],
        "evidence": evidence,
    }


def _client():
    from api.routes_ghost_system import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_missing_data_is_unavailable_not_bearish():
    out = bc.build_ymm_12_checklist({})
    assert out["confirmed"] == 0
    assert out["unknown"] == 12
    assert out["data_status"] == "DATA_UNAVAILABLE"
    assert out["decision"] == "data_unavailable"


def test_full_confirmation_is_strong_and_has_five_independent_groups():
    out = bc.build_ymm_12_checklist(_full_values())
    assert out["confirmed"] == 12
    assert out["decision"] == "strong"
    assert out["independent_confirmations"] == 5
    assert out["proven_probability"] is False
    assert out["scenario"]["target_horizon_trading_days"] == 5
    assert out["scenario"]["required_return_pct"] == 36.36


def test_strong_requires_guidance_even_when_eight_boxes_pass():
    values = _full_values()
    values.pop("guidance_outcome")
    out = bc.build_ymm_12_checklist(values)
    assert out["confirmed"] >= 8
    assert out["decision"] != "strong"
    assert "guidance" in out["critical_pending"]


def test_critical_rejection_vetoes_many_green_checks():
    out = bc.build_ymm_12_checklist(_full_values(guidance_outcome="cut"))
    assert out["confirmed"] >= 8
    assert out["decision"] == "weak"
    assert out["critical_rejected"] == ["guidance"]


def test_nested_breakouts_receive_only_one_independent_credit():
    out = bc.build_ymm_12_checklist({
        "live_price": 11.10,
        "relative_volume": 5.0,
        "price_change_pct": 8.0,
    })
    path = next(group for group in out["groups"] if group["key"] == "price_path")
    assert sum(c["passed"] for c in out["checks"] if c["group"] == "price_path") == 3
    assert path["status"] == "confirmed"
    assert path["confirmation_credit"] == 1
    assert out["independent_confirmations"] == 2


def test_breakout_requires_advancing_volume_confirmation():
    out = bc.build_ymm_12_checklist({
        "live_price": 11.10,
        "relative_volume": 5.0,
        "price_change_pct": -2.0,
    })
    volume = next(c for c in out["checks"] if c["key"] == "relative_volume")
    breakouts = [c for c in out["checks"] if c["group"] == "price_path"]
    assert volume["state"] == "red"
    assert all(c["state"] == "pending_confirmation" for c in breakouts)


def test_confirmed_rvol_requires_confirmed_price_direction():
    records = bc._normalize_direct_values({
        "live_price": 11.10,
        "relative_volume": 5.0,
        "price_change_pct": 8.0,
    })
    records["price_change_pct"]["status"] = "UNVERIFIED"
    records["price_change_pct"]["confidence_status"] = "UNVERIFIED"
    out = bc.build_ymm_12_checklist(evidence=records)
    volume = next(c for c in out["checks"] if c["key"] == "relative_volume")
    assert volume["state"] == "unverified"
    assert volume["passed"] is False
    assert "price-change evidence is confirmed" in volume["note"]


def test_threshold_gaps_and_strict_growth_are_honest():
    out = bc.build_ymm_12_checklist({
        "revenue_actual_usd_m": 460.0,
        "eps_adjusted_ads_usd": 0.19,
        "transaction_growth_pct": 30.0,
    })
    states = {c["key"]: c["state"] for c in out["checks"]}
    assert states["revenue_beat"] == "neutral"
    assert states["eps_beat"] == "neutral"
    assert states["transaction_growth"] == "neutral"


def test_twenty_percent_gap_is_chase_risk_not_extreme_pass():
    out = bc.build_ymm_12_checklist({"premarket_gap_pct": 20.0})
    gap = next(c for c in out["checks"] if c["key"] == "premarket_gap")
    assert gap["state"] == "chase_risk"
    assert gap["passed"] is False
    assert out["decision"] == "weak"


def test_operator_payload_rejects_raw_revenue_units():
    payload = _payload(revenue_actual_usd_m=412_900_000.0)
    with pytest.raises(bc.ChecklistInputError, match="declared unit"):
        bc.validate_operator_payload("YMM", payload, now_ts=AFTER_RELEASE + 60)


def test_operator_payload_rejects_wrong_period_and_stale_evidence():
    wrong_period = _payload(revenue_actual_usd_m=475.0)
    wrong_period["period"] = "2026-Q1"
    with pytest.raises(bc.ChecklistInputError, match="period must"):
        bc.validate_operator_payload("YMM", wrong_period, now_ts=AFTER_RELEASE + 60)

    predates_release = _payload(revenue_actual_usd_m=475.0)
    predates_release["evidence"]["revenue_actual_usd_m"]["as_of_ts"] = (
        bc._EVENT_EVIDENCE_NOT_BEFORE_TS - 1
    )
    with pytest.raises(bc.ChecklistInputError, match="predates"):
        bc.validate_operator_payload("YMM", predates_release, now_ts=AFTER_RELEASE + 60)

    stale = _payload(revenue_actual_usd_m=475.0)
    with pytest.raises(bc.ChecklistInputError, match="stale"):
        bc.validate_operator_payload(
            "YMM",
            stale,
            now_ts=AFTER_RELEASE + bc._OPERATOR_EVIDENCE_MAX_AGE_S + 1,
        )

    future = _payload(revenue_actual_usd_m=475.0)
    future["evidence"]["revenue_actual_usd_m"]["as_of_ts"] = AFTER_RELEASE + 1_000
    with pytest.raises(bc.ChecklistInputError, match="future"):
        bc.validate_operator_payload("YMM", future, now_ts=AFTER_RELEASE)


def test_operator_payload_requires_source_and_timestamp():
    payload = _payload(eps_adjusted_ads_usd=0.22)
    payload["evidence"]["eps_adjusted_ads_usd"].pop("source")
    with pytest.raises(bc.ChecklistInputError, match="evidence URL"):
        bc.validate_operator_payload("YMM", payload, now_ts=AFTER_RELEASE + 60)

    missing_ts = _payload(eps_adjusted_ads_usd=0.22)
    missing_ts["evidence"]["eps_adjusted_ads_usd"].pop("as_of_ts")
    with pytest.raises(bc.ChecklistInputError, match="Unix seconds"):
        bc.validate_operator_payload("YMM", missing_ts, now_ts=AFTER_RELEASE + 60)

    untrusted = _payload(eps_adjusted_ads_usd=0.22)
    untrusted["evidence"]["eps_adjusted_ads_usd"]["source"] = "https://example.com/result"
    with pytest.raises(bc.ChecklistInputError, match="official FTA IR or SEC"):
        bc.validate_operator_payload("YMM", untrusted, now_ts=AFTER_RELEASE + 60)


def test_operator_payload_marks_urls_unverified():
    payload = _payload(revenue_actual_usd_m=475.0)
    normalized = bc.validate_operator_payload("YMM", payload, now_ts=AFTER_RELEASE + 60)
    assert normalized["revenue_actual_usd_m"]["provenance"] == bc.OPERATOR_PROVENANCE


def test_auto_fetch_never_uses_latest_quarter_earnings(monkeypatch):
    monkeypatch.setattr(bc, "_now", lambda: AFTER_RELEASE + 60)
    monkeypatch.setattr(
        "core.earnings_surprise.get_earnings_surprise",
        lambda _symbol: pytest.fail("unsafe latest-quarter earnings feed was called"),
    )
    monkeypatch.setattr(
        "core.prices.get_extended_session",
        lambda _symbol: {
            "session": "premarket",
            "gap_pct": 4.0,
            "price_as_of_ts": AFTER_RELEASE,
        },
    )
    monkeypatch.setattr(
        "core.prices.get_intraday_session",
        lambda _symbol: {
            "price": 9.20,
            "change_pct": 4.0,
            "price_as_of_ts": AFTER_RELEASE,
            "feed": "test",
        },
    )
    monkeypatch.setattr(
        "core.squeeze_monitor.get_squeeze_picks",
        lambda: {"picks": [{"symbol": "YMM", "rvol": 2.5, "as_of_ts": AFTER_RELEASE}]},
    )
    evidence, sources = bc.fetch_auto_evidence("YMM")
    assert "revenue_actual_usd_m" not in evidence
    assert "eps_adjusted_ads_usd" not in evidence
    assert sources["earnings"]["reason"] == "event_safe_post_release_evidence_required"
    assert evidence["premarket_gap_pct"]["status"] == "UNVERIFIED"
    assert evidence["price_change_pct"]["status"] == "UNVERIFIED"
    assert evidence["relative_volume"]["status"] == "UNVERIFIED"
    assert evidence["live_price"]["status"] == "CONFIRMED"


def test_stale_rvol_snapshot_is_not_used(monkeypatch):
    monkeypatch.setattr(bc, "_now", lambda: AFTER_RELEASE + bc._RVOL_MAX_AGE_S + 10)
    monkeypatch.setattr("core.prices.get_extended_session", lambda _symbol: {})
    monkeypatch.setattr("core.prices.get_intraday_session", lambda _symbol: {})
    monkeypatch.setattr(
        "core.squeeze_monitor.get_squeeze_picks",
        lambda: {
            "last_scan_ts": AFTER_RELEASE,
            "picks": [{"symbol": "YMM", "rvol": 5.0}],
        },
    )
    evidence, sources = bc.fetch_auto_evidence("YMM")
    assert "relative_volume" not in evidence
    assert sources["relative_volume"]["reason"] == "stale_radar_snapshot"


def test_source_failures_return_data_unavailable_not_weak(monkeypatch):
    def fail(_symbol=None):
        raise RuntimeError("provider down")

    monkeypatch.setattr("core.prices.get_extended_session", fail)
    monkeypatch.setattr("core.squeeze_monitor.get_squeeze_picks", fail)
    out = bc.auto_fill_ymm_12("YMM")
    assert out["data_status"] == "DATA_UNAVAILABLE"
    assert out["decision"] == "data_unavailable"
    assert out["source_status"]["prices"]["reason"] == "RuntimeError"


def test_non_ymm_symbol_is_rejected():
    with pytest.raises(bc.UnsupportedScenarioError):
        bc.fetch_auto_evidence("AAPL")
    response = _client().get("/api/bull-run/checklist/AAPL")
    assert response.status_code == 404
    assert response.json()["ok"] is False


def test_get_route_returns_honest_unavailable_state(monkeypatch):
    monkeypatch.setattr(
        bl,
        "fetch_auto_evidence",
        lambda _symbol: ({}, {"prices": {"available": False, "reason": "test"}}),
    )
    monkeypatch.setattr(bl, "load_preserved_evidence", lambda: {})
    response = _client().get("/api/bull-run/checklist/YMM")
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "data_unavailable"
    assert body["symbol"] == "YMM"


def test_post_route_keeps_operator_claims_unverified_and_non_scoring(monkeypatch):
    monkeypatch.setattr(bc, "_now", lambda: AFTER_RELEASE + 60)
    auto = {
        "premarket_gap_pct": _record(8.0, unit="percent"),
        "relative_volume": _record(5.0, unit="multiple"),
        "price_change_pct": _record(8.0, unit="percent"),
        "live_price": _record(11.10, unit="USD"),
    }
    monkeypatch.setattr(bl, "fetch_auto_evidence", lambda _symbol: (auto, {}))
    monkeypatch.setattr(bl, "load_preserved_evidence", lambda: {})
    payload = _payload(
        revenue_actual_usd_m=485.0,
        eps_adjusted_ads_usd=0.23,
        transaction_growth_pct=42.0,
        order_growth_pct=22.0,
        shipper_growth_pct=16.0,
        profitability_improved=True,
        guidance_outcome="raised_accelerating",
    )
    response = _client().post("/api/bull-run/checklist/YMM/evaluate", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] != "strong"
    assert body["confirmed"] == 0
    assert body["trade_action"] == "NO_TRADE"
    assert all(
        item["evidence_summary"]["status"] != "CONFIRMED"
        for item in body["checks"]
        if item["key"] in {
            "revenue_beat", "eps_beat", "transaction_growth", "order_growth",
            "shipper_growth", "profitability", "guidance",
        }
    )


def test_post_route_rejects_ambiguous_operator_input(monkeypatch):
    monkeypatch.setattr(bc, "_now", lambda: AFTER_RELEASE + 60)
    response = _client().post(
        "/api/bull-run/checklist/YMM/evaluate",
        json=_payload(revenue_actual_usd_m=412_900_000.0),
    )
    assert response.status_code == 400
    assert "declared unit" in response.json()["error"]


def test_routes_are_registered():
    from api.routes_ghost_system import router

    methods_by_path = {route.path: route.methods for route in router.routes}
    assert methods_by_path["/api/bull-run/checklist/{symbol}"] == {"GET"}
    assert methods_by_path["/api/bull-run/checklist/{symbol}/evaluate"] == {"POST"}
