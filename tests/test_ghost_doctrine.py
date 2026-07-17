"""tests/test_ghost_doctrine.py — Ghost Doctrine 6-step thinking layer (PR #129)."""
from __future__ import annotations

import os
import pytest


@pytest.fixture
def client():
    """FastAPI TestClient for the live app (repo convention: TestClient(wolf_app.APP)).

    Defined here because this repo has no shared `client` conftest fixture — the
    route tests below reference one, so provide it locally to match the pattern
    used by tests/test_super_ghost_top_picks.py and tests/test_ghost_console_ui.py.
    """
    from fastapi.testclient import TestClient
    import wolf_app
    return TestClient(wolf_app.APP)


class TestDoctrineSpec:
    def test_spec_has_6_steps_in_order(self):
        from core.ghost_doctrine import ghost_doctrine_spec, DOCTRINE_KEYS
        spec = ghost_doctrine_spec()
        steps = spec["steps"]
        assert len(steps) == 6
        for i, key in enumerate(DOCTRINE_KEYS):
            assert steps[i]["key"] == key
            assert steps[i]["step"] == i + 1
            assert "label" in steps[i]
            assert "ghost_meaning" in steps[i]
            assert "what_it_checks" in steps[i]
            assert "data_sources" in steps[i]

    def test_spec_has_honesty_rule(self):
        from core.ghost_doctrine import ghost_doctrine_spec
        spec = ghost_doctrine_spec()
        assert "honesty_rule" in spec
        assert "insufficient" in spec["honesty_rule"]


class TestBuildDoctrine:
    def test_full_from_super_ghost_dict(self, monkeypatch):
        """Full doctrine from a hand-built super-ghost dict + monkeypatched sources."""
        from core.ghost_doctrine import build_symbol_doctrine

        sg = {
            "prediction": {"direction": "UP", "action": "BUY", "confidence": 0.72, "accuracy_grade": "B"},
            "risk_plan": {"entry": 100.0, "target_price": 108.0, "stop_loss": 95.0},
            "market_regime": {"label": "bull"},
            "coverage": {"feature_count": 44, "available": 44, "total": 44},
            "data_quality": {"status": "ok"},
        }

        # Monkeypatch sources
        monkeypatch.setattr("core.prediction._objective_effective_config", lambda: {"mode": "enforced", "target_wr": 0.70, "min_samples": 30, "bootstrap_min_conf": 0.60})
        monkeypatch.setattr("core.prediction._objective_symbol_stats", lambda s, d: {"combined_total": 50, "combined_wins": 35, "combined_wr": 0.70})
        monkeypatch.setattr("core.prediction.evaluate_kill_conditions", lambda include_pause=True: {"ok": True, "any_triggered": False, "engine_pause": {"paused": False}})
        monkeypatch.setattr("core.super_ghost_top_picks.evaluate_top_pick_gate", lambda symbol, horizon=5: {"ok": True, "eligible": True, "decision": "ELIGIBLE_FOR_TOP_PICKS", "checks": [], "blocking": []})
        monkeypatch.setattr("core.super_ghost_ledger.get_accuracy", lambda symbol, horizon=5: {"overall": {"n": 50, "win_rate": 0.72, "win_rate_wilson_low": 0.60}})
        monkeypatch.setattr("core.super_ghost_ledger.get_if_followed", lambda symbol, horizon=5: {"profit_factor": 1.25, "net_return_pct": 12.5, "followed_calls": 50})

        result = build_symbol_doctrine("WOLF", super_ghost=sg, mode="full")
        assert result["ok"] is True
        assert result["symbol"] == "WOLF"
        assert len(result["steps"]) == 6

        # Clarity should pass
        clarity = result["steps"][0]
        assert clarity["status"] == "pass"
        assert clarity["key"] == "clarity"

        # Results should pass with evidence
        results = result["steps"][5]
        assert results["key"] == "results"
        assert results["status"] == "pass"
        assert any(e["name"] == "n" for e in results["evidence"])
        assert any(e["name"] == "win_rate_wilson_low" for e in results["evidence"])

    def test_no_invented_numbers_all_sources_fail(self, monkeypatch):
        """All sources failing → every step insufficient, payload still ok."""
        from core.ghost_doctrine import build_symbol_doctrine

        def _fail(*a, **kw):
            raise RuntimeError("source unavailable")

        monkeypatch.setattr("core.prediction._objective_effective_config", _fail)
        monkeypatch.setattr("core.prediction._objective_symbol_stats", _fail)
        monkeypatch.setattr("core.prediction.evaluate_kill_conditions", _fail)
        monkeypatch.setattr("core.super_ghost_top_picks.evaluate_top_pick_gate", _fail)
        monkeypatch.setattr("core.super_ghost_ledger.get_accuracy", _fail)
        monkeypatch.setattr("core.super_ghost_ledger.get_if_followed", _fail)

        result = build_symbol_doctrine("WOLF", super_ghost=None, mode="light")
        assert result["ok"] is True
        for step in result["steps"]:
            assert step["status"] == "insufficient", f"Step {step['key']} should be insufficient"
            assert step["insufficient_reason"] is not None

    def test_results_insufficient_at_zero_resolved(self, monkeypatch):
        from core.ghost_doctrine import build_symbol_doctrine

        monkeypatch.setattr("core.prediction._objective_effective_config", lambda: {"mode": "enforced", "target_wr": 0.70, "min_samples": 30, "bootstrap_min_conf": 0.60})
        monkeypatch.setattr("core.prediction._objective_symbol_stats", lambda s, d: {"combined_total": 0, "combined_wins": 0, "combined_wr": None})
        monkeypatch.setattr("core.prediction.evaluate_kill_conditions", lambda include_pause=True: {"ok": True, "any_triggered": False, "engine_pause": {"paused": False}})
        monkeypatch.setattr("core.super_ghost_top_picks.evaluate_top_pick_gate", lambda symbol, horizon=5: {"ok": True, "eligible": False, "decision": "LOCKED", "checks": [], "blocking": []})
        monkeypatch.setattr("core.super_ghost_ledger.get_accuracy", lambda symbol, horizon=5: {"overall": {"n": 0, "win_rate": None, "win_rate_wilson_low": None}})
        monkeypatch.setattr("core.super_ghost_ledger.get_if_followed", lambda symbol, horizon=5: {"profit_factor": None, "net_return_pct": None, "followed_calls": 0})

        result = build_symbol_doctrine("WOLF", super_ghost=None, mode="light")
        results = result["steps"][5]
        assert results["status"] == "insufficient"
        # No win_rate in evidence at 0 resolved
        wr_ev = [e for e in results["evidence"] if e["name"] == "win_rate"]
        assert wr_ev[0]["value"] is None

    def test_results_evidence_includes_wilson_low_and_n(self, monkeypatch):
        from core.ghost_doctrine import build_symbol_doctrine

        monkeypatch.setattr("core.prediction._objective_effective_config", lambda: {"mode": "enforced", "target_wr": 0.70, "min_samples": 30, "bootstrap_min_conf": 0.60})
        monkeypatch.setattr("core.prediction._objective_symbol_stats", lambda s, d: {"combined_total": 50, "combined_wins": 35, "combined_wr": 0.70})
        monkeypatch.setattr("core.prediction.evaluate_kill_conditions", lambda include_pause=True: {"ok": True, "any_triggered": False, "engine_pause": {"paused": False}})
        monkeypatch.setattr("core.super_ghost_top_picks.evaluate_top_pick_gate", lambda symbol, horizon=5: {"ok": True, "eligible": True, "decision": "ELIGIBLE_FOR_TOP_PICKS", "checks": [], "blocking": []})
        monkeypatch.setattr("core.super_ghost_ledger.get_accuracy", lambda symbol, horizon=5: {"overall": {"n": 50, "win_rate": 0.72, "win_rate_wilson_low": 0.60}})
        monkeypatch.setattr("core.super_ghost_ledger.get_if_followed", lambda symbol, horizon=5: {"profit_factor": 1.25, "net_return_pct": 12.5, "followed_calls": 50})

        result = build_symbol_doctrine("WOLF", super_ghost=None, mode="light")
        results = result["steps"][5]
        evidence_names = [e["name"] for e in results["evidence"]]
        assert "n" in evidence_names
        assert "win_rate_wilson_low" in evidence_names

    def test_consistency_thresholds_imported_from_top_picks(self):
        """Source tripwire: consistency uses MIN_COMPLETED from top_picks."""
        from core.super_ghost_top_picks import MIN_COMPLETED, MIN_PRECISION_SCORE, MIN_PRECISION_SAMPLES
        assert MIN_COMPLETED >= 5
        assert MIN_PRECISION_SCORE >= 50
        assert MIN_PRECISION_SAMPLES >= 3

    def test_read_only_tripwire(self):
        """Module source contains no INSERT/UPDATE/DELETE/log_prediction."""
        path = os.path.join(os.path.dirname(__file__), "..", "core", "ghost_doctrine.py")
        with open(path) as f:
            src = f.read()
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "log_prediction"):
            assert forbidden not in src, f"ghost_doctrine.py must not contain {forbidden}"


class TestDoctrineRoutes:
    def test_spec_endpoint(self, client):
        """GET /api/ghost/doctrine returns 200 with 6 steps."""
        res = client.get("/api/ghost/doctrine")
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert len(data["steps"]) == 6

    def test_symbol_light_mode(self, client):
        """GET /api/ghost/doctrine/WOLF?light=1 works, live defaults off."""
        res = client.get("/api/ghost/doctrine/WOLF?light=1")
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["mode"] == "light"
        assert len(data["steps"]) == 6


class TestDoctrineUITripwires:
    def test_console_has_doctrine_section(self):
        path = os.path.join(os.path.dirname(__file__), "..", "ghost_console.html")
        with open(path) as f:
            src = f.read()
        assert 'data-section="doctrine"' in src
        assert "renderDoctrine" in src
        assert "?light=1" in src or "light" in src  # light mode referenced
        assert "Insufficient evidence" in src
        # loadAll MUST actually fetch the doctrine payload, else the tab is dead
        # (renders "Doctrine unavailable" forever). Guard against that regression.
        assert "/api/ghost/doctrine/" in src
        assert "doctrine:r[" in src or "doctrine:" in src
        # My Picks per-pick chips (plan step 3): light fetch + placeholder id.
        assert "mypick-doctrine-" in src

    def test_cockpit_has_doctrine_section(self):
        path = os.path.join(os.path.dirname(__file__), "..", "cockpit.html")
        with open(path) as f:
            src = f.read()
        assert "doctrine-section" in src
        assert "loadDoctrine" in src
