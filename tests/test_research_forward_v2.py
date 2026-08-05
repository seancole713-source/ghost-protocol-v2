"""Focused tests for the fixed-50 research proof query path."""

import base64
import hashlib
import time

import pytest

from core.research_forward import (
    _proof_date_key,
    _secondary_metric_gates,
    evaluate_forward_proof,
    get_active_registrations,
    register_forward_experiment,
    update_forward_proof_status,
)
from core.research_artifacts import compute_artifact_sha


_ARTIFACT_RAW = b"fixed-proof-model"
_ARTIFACT_PAYLOAD = base64.b64encode(_ARTIFACT_RAW).decode("ascii")
_ARTIFACT_MODEL_SHA = hashlib.sha256(_ARTIFACT_RAW).hexdigest()
_ARTIFACT_TRAINED_AT = 1_700_000_000
_ARTIFACT_CALIBRATION = {"threshold": 0.70}
_ARTIFACT_GATE = {"feature_inversions": []}
_ARTIFACT_SHA = compute_artifact_sha(
    model_sha256=_ARTIFACT_MODEL_SHA,
    contract_id="contract",
    direction="UP",
    policy_lineage_id="WOLF/UP",
    policy_lineage_version=1,
    feature_order=("rsi",),
    feature_schema="features-v1",
    label_schema="labels-v1",
    validation_schema="validation-v1",
    hold_bars=3,
    calibration_proof=_ARTIFACT_CALIBRATION,
    gate_proof=_ARTIFACT_GATE,
    symbol_scope=("WOLF",),
    trained_at=_ARTIFACT_TRAINED_AT,
)


def _artifact_row(*, tampered=False):
    calibration = {"threshold": 0.71} if tampered else _ARTIFACT_CALIBRATION
    return (
        _ARTIFACT_SHA, _ARTIFACT_MODEL_SHA, "contract", "WOLF/UP", 1,
        '["WOLF"]', '["UP"]', "features-v1", "labels-v1",
        "validation-v1", 3, "", calibration, _ARTIFACT_GATE, '["rsi"]',
        _ARTIFACT_PAYLOAD, _ARTIFACT_TRAINED_AT, _ARTIFACT_TRAINED_AT,
        "ACTIVE", 0, "",
    )


class _Cursor:
    def __init__(self):
        self.sql = ""
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return []


class _ProofCursor:
    def __init__(
        self, *, update_rowcount=1, persisted_status="COLLECTING",
        closed_at=None, prediction_rows=None,
        tampered_artifact=False,
    ):
        self.executed = []
        self._result = None
        self.rowcount = update_rowcount
        self.registered_at = int(time.time()) - 1000
        self.persisted_status = persisted_status
        self.closed_at = closed_at
        self.prediction_rows = prediction_rows
        self.tampered_artifact = tampered_artifact

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "FROM ghost_research_registrations" in normalized:
            self._result = (
                "contract", _ARTIFACT_SHA, "UP", 0.70, ["WOLF"], 50, 120,
                20, 0.20, self.registered_at, self.persisted_status, self.closed_at,
                _registration_metadata(),
            )
        elif "FROM ghost_research_artifacts" in normalized:
            self._result = _artifact_row(tampered=self.tampered_artifact)
        elif "FROM ghost_research_predictions" in normalized:
            rows = self.prediction_rows or [
                (1, "WOLF", self.registered_at + 100, 0.84,
                 {"entry_price": 100.0}, "UP", None, None, "2026-08-04"),
                (2, "WOLF", self.registered_at + 200, 0.84,
                 {"entry_price": 100.0}, "UP", "WIN", 103.0, "2026-08-05"),
            ]
            evidence_window_end = int(params[4])
            self._result = [row for row in rows if int(row[2]) <= evidence_window_end]
        elif "FROM ghost_research_evaluations" in normalized:
            self._result = (2, 2)

    def fetchone(self):
        if isinstance(self._result, list):
            return self._result[0] if self._result else None
        return self._result

    def fetchall(self):
        return self._result if isinstance(self._result, list) else []


def test_registration_lookup_without_status_lists_all_rows():
    cur = _Cursor()
    assert get_active_registrations(status=None, cur=cur) == []
    assert "WHERE status" not in cur.sql
    assert cur.params == ()


def test_registration_lookup_with_status_filters_rows():
    cur = _Cursor()
    assert get_active_registrations(status="COLLECTING", cur=cur) == []
    assert "WHERE status = %s" in cur.sql
    assert cur.params == ("COLLECTING",)


def test_registration_rejects_non_hex_artifact_identity():
    with pytest.raises(ValueError, match="hexadecimal"):
        register_forward_experiment(
            contract_id="contract",
            artifact_sha="g" * 64,
            direction="UP",
            threshold=0.70,
            symbol_universe=["WOLF"],
            round_trip_slippage_bps=10.0,
            round_trip_commission_bps=0.0,
            cur=_Cursor(),
        )


def test_proof_date_prefers_frozen_session_over_next_utc_date():
    next_utc_day = 1_765_326_600  # 2025-12-10 00:30 UTC / 2025-12-09 18:30 CT
    assert _proof_date_key(next_utc_day, "2025-12-09") == "2025-12-09"
    assert _proof_date_key(next_utc_day, None) == "2025-12-10"


def test_evaluation_is_read_only_and_blocks_on_earlier_unresolved_row():
    cur = _ProofCursor()
    proof = evaluate_forward_proof("fwd_test", cur=cur)
    assert proof["n"] == 0
    assert proof["blocked_prediction_id"] == 1
    assert not any(sql.startswith("UPDATE ") for sql, _ in cur.executed)


def test_evaluation_fails_closed_when_artifact_package_mutates():
    proof = evaluate_forward_proof(
        "fwd_test", cur=_ProofCursor(tampered_artifact=True),
    )

    assert proof["ok"] is False
    assert proof["error"] == "artifact_integrity_failed:artifact_sha_package_mismatch"


def test_explicit_status_update_persists_once():
    cur = _ProofCursor()
    proof = update_forward_proof_status("fwd_test", cur=cur)
    assert proof["status"] == "COLLECTING"
    updates = [sql for sql, _ in cur.executed if sql.startswith("UPDATE ")]
    assert len(updates) == 1


def test_terminal_status_update_returns_persisted_closure(monkeypatch):
    cur = _ProofCursor()
    terminal = {
        "ok": True,
        "status": "PROVEN",
        "persisted_status": "COLLECTING",
        "closed_at_ts": None,
    }
    monkeypatch.setattr(
        "core.research_forward._evaluate_impl",
        lambda cursor, registration_id: dict(terminal),
    )
    monkeypatch.setattr("core.research_forward.time.time", lambda: 1_800_000_000)

    proof = update_forward_proof_status("fwd_test", cur=cur)

    assert proof["persisted_status"] == "PROVEN"
    assert proof["closed_at_ts"] == 1_800_000_000
    update = next(item for item in cur.executed if item[0].startswith("UPDATE "))
    assert update[1] == ("PROVEN", True, 1_800_000_000, "fwd_test")


def test_explicit_status_update_does_not_swallow_missing_row():
    cur = _ProofCursor(update_rowcount=0)
    with pytest.raises(RuntimeError, match="Failed to update registration"):
        update_forward_proof_status("fwd_test", cur=cur)


@pytest.mark.parametrize("terminal_status", ["FUTILE", "INCOMPLETE"])
def test_terminal_proof_excludes_predictions_after_closed_at(terminal_status):
    registered_at = int(time.time()) - 1000
    closed_at = registered_at + 150
    rows = [
        (1, "WOLF", registered_at + 100, 0.84,
         {"entry_price": 100.0}, "UP", "LOSS", 99.0, "2026-08-04"),
        (2, "WOLF", registered_at + 200, 0.84,
         {"entry_price": 100.0}, "UP", "WIN", 103.0, "2026-08-05"),
    ]
    cur = _ProofCursor(
        persisted_status=terminal_status,
        closed_at=closed_at,
        prediction_rows=rows,
    )
    cur.registered_at = registered_at

    proof = evaluate_forward_proof("fwd_test", cur=cur)

    assert proof["status"] == terminal_status
    assert proof["closed_at_ts"] == closed_at
    assert proof["n"] == 1
    assert proof["wins"] == 0
    prediction_query = next(
        params for sql, params in cur.executed
        if "FROM ghost_research_predictions" in sql
    )
    coverage_query = next(
        params for sql, params in cur.executed
        if "FROM ghost_research_evaluations" in sql
    )
    assert prediction_query[4] == closed_at
    assert coverage_query[4] == closed_at


def _registration_metadata():
    return {
        "coverage_schema": "eligible_evaluation/v1",
        "cost_model": {
            "schema": "round_trip_bps/v1",
            "slippage_bps": 10.0,
            "commission_bps": 0.0,
            "total_bps": 10.0,
        },
    }


def _passing_rows():
    outcomes = (["WIN"] * 5 + ["LOSS"]) * 8 + ["WIN", "WIN"]
    return [
        {
            "outcome": outcome,
            "calibrated_prob": 0.84,
            "context": {"entry_price": 100.0},
            "direction": "UP",
            "observed_value": 103.0 if outcome == "WIN" else 99.0,
        }
        for outcome in outcomes
    ]


def _gates(rows=None, **overrides):
    params = {
        "data_invalid": 0,
        "eligible_evaluations": 100,
        "fired_evaluations": 50,
        "registration_metadata": _registration_metadata(),
    }
    params.update(overrides)
    return _secondary_metric_gates(rows or _passing_rows(), **params)


def test_secondary_gates_pass_complete_frozen_evidence():
    gates = _gates()
    assert all(gate["passed"] for gate in gates.values()), gates
    assert gates["invalid_rate"]["value"] == 0.0
    assert gates["coverage"]["value"] == 0.5
    assert gates["brier_score"]["value"] < 0.25
    assert gates["calibration_gap"]["value"] == 0.0
    assert gates["net_expectancy"]["value"] > 0.0
    assert gates["profit_factor"]["value"] > 1.0
    assert gates["max_drawdown"]["value"] <= 0.10
    assert gates["block_bootstrap"]["value"] >= 0.70


def test_secondary_gates_reject_excess_invalid_rate():
    gates = _gates(data_invalid=6)
    assert gates["invalid_rate"]["passed"] is False
    assert gates["invalid_rate"]["value"] > 0.10


def test_secondary_gates_reject_missing_or_low_coverage():
    missing = _gates(eligible_evaluations=0, fired_evaluations=0)
    assert missing["coverage"]["passed"] is False
    assert missing["coverage"]["reason"] == "no_eligible_evaluations"

    low = _gates(eligible_evaluations=10_000, fired_evaluations=50)
    assert low["coverage"]["passed"] is False
    assert low["coverage"]["value"] == 0.005


def test_secondary_gates_reject_missing_registered_evidence_schemas():
    gates = _gates(registration_metadata={})
    assert gates["coverage"]["passed"] is False
    assert gates["coverage"]["reason"] == "coverage_schema_not_registered"
    assert gates["net_expectancy"]["passed"] is False
    assert gates["net_expectancy"]["reason"] == "cost_model_not_registered"


def test_secondary_gates_reject_bad_brier_and_calibration():
    bad_brier = _passing_rows()
    for row in bad_brier:
        row["calibrated_prob"] = 0.30
    gates = _gates(bad_brier)
    assert gates["brier_score"]["passed"] is False

    bad_calibration = _passing_rows()
    for row in bad_calibration:
        row["calibrated_prob"] = 0.70
    gates = _gates(bad_calibration)
    assert gates["brier_score"]["passed"] is True
    assert gates["calibration_gap"]["passed"] is False
    assert gates["calibration_gap"]["value"] > 0.10


def test_secondary_gates_reject_missing_probability_or_exit():
    missing_probability = _passing_rows()
    missing_probability[0]["calibrated_prob"] = None
    gates = _gates(missing_probability)
    assert gates["brier_score"]["passed"] is False
    assert gates["calibration_gap"]["passed"] is False

    missing_exit = _passing_rows()
    missing_exit[0]["observed_value"] = None
    gates = _gates(missing_exit)
    assert gates["net_expectancy"]["passed"] is False
    assert gates["profit_factor"]["passed"] is False
    assert gates["max_drawdown"]["passed"] is False


def test_secondary_gates_reject_negative_economics():
    rows = _passing_rows()
    for row in rows:
        row["observed_value"] = 100.05
    gates = _gates(rows)
    assert gates["net_expectancy"]["passed"] is False
    assert gates["profit_factor"]["passed"] is False


def test_secondary_gates_reject_excess_drawdown_despite_positive_expectancy():
    rows = _passing_rows()
    rows.sort(key=lambda row: row["outcome"] == "WIN")
    for row in rows:
        if row["outcome"] == "LOSS":
            row["observed_value"] = 92.0
    gates = _gates(rows)
    assert gates["net_expectancy"]["passed"] is True
    assert gates["profit_factor"]["passed"] is True
    assert gates["max_drawdown"]["passed"] is False


def test_secondary_gates_reject_temporally_clustered_wins():
    rows = _passing_rows()
    rows.sort(key=lambda row: row["outcome"] != "WIN")
    gates = _gates(rows)
    assert gates["block_bootstrap"]["passed"] is False
    assert gates["block_bootstrap"]["value"] < 0.70


def test_registration_requires_declared_slippage_and_commission():
    common = {
        "contract_id": "contract",
        "artifact_sha": "a" * 64,
        "direction": "UP",
        "threshold": 0.70,
        "symbol_universe": ["WOLF"],
    }
    with pytest.raises(ValueError, match="slippage and commission"):
        register_forward_experiment(**common)
    with pytest.raises(ValueError, match="slippage and commission"):
        register_forward_experiment(
            **common,
            round_trip_slippage_bps=10.0,
        )