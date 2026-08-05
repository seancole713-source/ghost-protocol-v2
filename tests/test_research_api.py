"""Tests for api/research_endpoints.py — read-only research API."""
import pytest
from fastapi.testclient import TestClient
import wolf_app


@pytest.fixture
def client():
    return TestClient(wolf_app.APP)


# ── contract endpoints ─────────────────────────────────────────────────────

def test_list_contracts(client):
    r = client.get("/api/research/contracts")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert len(data["contracts"]) >= 5
    names = {c["name"] for c in data["contracts"]}
    assert "tp_sl_swing" in names
    assert "intraday_continuation" in names


def test_get_contract_by_name(client):
    r = client.get("/api/research/contracts/tp_sl_swing/v1")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["contract"]["name"] == "tp_sl_swing"
    assert data["contract"]["live_eligible"] is True


def test_get_contract_not_found(client):
    r = client.get("/api/research/contracts/nonexistent/v1")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False


# ── artifact endpoints ────────────────────────────────────────────────────

@pytest.mark.integration
def test_list_artifacts(client):
    r = client.get("/api/research/artifacts")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "artifacts" in data


@pytest.mark.integration
def test_get_artifact_not_found(client):
    r = client.get("/api/research/artifacts/" + "a" * 64)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False


# ── prediction endpoints ───────────────────────────────────────────────────

@pytest.mark.integration
def test_list_predictions(client):
    r = client.get("/api/research/predictions")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "predictions" in data


@pytest.mark.integration
def test_list_pending_predictions(client):
    r = client.get("/api/research/predictions?resolved=false")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True


# ── proof endpoint ─────────────────────────────────────────────────────────

@pytest.mark.integration
def test_get_proof_not_found(client):
    r = client.get("/api/research/proof/abc/" + "a" * 64)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["registration"] is None


def test_legacy_diagnostics_never_claim_protocol_proof(client, monkeypatch):
    import core.research_forward as forward
    import core.research_ledger as ledger
    import core.research_proof as legacy

    monkeypatch.setattr(forward, "get_active_registrations", lambda status=None: [])
    monkeypatch.setattr(
        legacy, "get_forward_registration",
        lambda contract_id, artifact_sha: None,
    )
    monkeypatch.setattr(
        ledger,
        "get_resolved_predictions",
        lambda **kwargs: [
            {
                "contract_id": "abc",
                "artifact_sha": "a" * 64,
                "outcome": "WIN",
                "calibrated_prob": 0.99,
            }
            for _ in range(50)
        ],
    )

    data = client.get("/api/research/proof/abc/" + "a" * 64).json()

    assert data["proof"]["legacy_threshold_met"] is True
    assert data["proof"]["proven"] is False
    assert data["proof"]["status"] == "UNVERIFIED_LEGACY"


# ── activation endpoints ───────────────────────────────────────────────────

@pytest.mark.integration
def test_activation_history(client):
    r = client.get("/api/research/activation/history")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "events" in data


@pytest.mark.integration
def test_evidence_lease(client):
    r = client.get("/api/research/activation/lease/" + "a" * 64)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "lease" in data


# ── health endpoint ────────────────────────────────────────────────────────

@pytest.mark.integration
def test_health(client):
    r = client.get("/api/research/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "tables" in data
    assert "stats" in data


# ── index endpoint ─────────────────────────────────────────────────────────

def test_index(client):
    r = client.get("/api/research/")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "endpoints" in data


# ── no mutation methods ────────────────────────────────────────────────────

def test_no_post_endpoints(client):
    """Research API must not expose POST/PUT/DELETE endpoints."""
    r = client.post("/api/research/contracts")
    assert r.status_code in (405, 404)

    r = client.put("/api/research/artifacts")
    assert r.status_code in (405, 404)

    r = client.delete("/api/research/predictions")
    assert r.status_code in (405, 404)


# ── source scan: no mutation in API module ─────────────────────────────────

def test_api_module_has_no_mutations():
    """The API module must not contain INSERT/UPDATE/DELETE/CREATE SQL."""
    import ast
    import api.research_endpoints as re
    source = open(re.__file__).read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value.upper()
            if any(kw in s for kw in ("INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE")):
                raise AssertionError(f"API module contains mutation SQL: {node.value[:80]}")
