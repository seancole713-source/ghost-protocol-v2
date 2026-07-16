"""tests/test_contract_70_verdict.py — pre-registered contract-70 verdict layer.

The layer changes what Ghost CLAIMS, never what it FIRES: read-only tripwire,
pre-registered criteria present, honest statuses under every evidence state.
"""
from __future__ import annotations

import os

import core.contract_70_verdict as v


# ── Pre-registration is complete and static ──────────────────────────

class TestPreregistration:
    def test_static_fields_present(self):
        p = v.preregistration()
        assert p["verdict_version"] == v.VERDICT_VERSION
        assert p["preregistered_at"] == "2026-07-16"
        assert p["status_at_registration"] == "UNPROVEN_AT_CURRENT_DATA"
        assert len(p["evidence_at_registration"]) >= 5
        assert p["falsified_if"]["min_n"] == 100
        assert p["falsified_if"]["win_rate_below"] == 0.65
        assert p["falsified_if"]["wilson_high_below"] == 0.70
        assert p["revived_if"]["forward_min_n"] == 25
        assert p["revived_if"]["wilson_low_at_least"] == 0.70
        assert "never the firing behavior" in p["non_negotiable"]

    def test_preregistration_needs_no_db(self, monkeypatch):
        """Static record must never touch the database."""
        import core.db as db
        def boom(*a, **k):
            raise AssertionError("preregistration touched the DB")
        monkeypatch.setattr(db, "db_conn", boom)
        assert v.preregistration()["verdict_version"] == "1.0"


# ── Verdict statuses under controlled evidence ───────────────────────

def _rows(wins: int, losses: int, prob: float = 0.75):
    rows = []
    for i in range(wins):
        rows.append({"symbol": f"W{i}", "eval_ts": 1, "up_prob": prob, "outcome": "WIN"})
    for i in range(losses):
        rows.append({"symbol": f"L{i}", "eval_ts": 1, "up_prob": prob, "outcome": "LOSS"})
    return rows


class TestVerdict:
    def _patch(self, monkeypatch, rows, forward=None):
        monkeypatch.setattr(v, "_load_resolved_rows", lambda d, l: rows)
        monkeypatch.setattr(
            v, "_forward_proof_status",
            lambda r: forward or {"registered": False, "revival_met": False})

    def test_insufficient_evidence_when_db_unreadable(self, monkeypatch):
        monkeypatch.setattr(v, "_load_resolved_rows", lambda d, l: None)
        out = v.contract_70_verdict()
        assert out["ok"] is True
        assert out["status"] == "INSUFFICIENT_EVIDENCE"
        assert "live" not in out

    def test_insufficient_n(self, monkeypatch):
        self._patch(monkeypatch, _rows(3, 2))
        out = v.contract_70_verdict()
        assert out["status"] == "INSUFFICIENT_N"
        assert out["live"]["n"] == 5

    def test_unproven_at_current_data(self, monkeypatch):
        # Mirrors the live shape: n=55, ~58% observed — not proven, not yet
        # falsifiable under the pre-registered n>=100 bar.
        self._patch(monkeypatch, _rows(32, 23))
        out = v.contract_70_verdict()
        assert out["status"] == "UNPROVEN_AT_CURRENT_DATA"

    def test_falsified_at_current_data(self, monkeypatch):
        # n=120 at 50% — CI excludes 0.70 decisively.
        self._patch(monkeypatch, _rows(60, 60))
        out = v.contract_70_verdict()
        assert out["status"] == "FALSIFIED_AT_CURRENT_DATA"

    def test_proven_via_wilson(self, monkeypatch):
        # 190/200 = 95% observed; Wilson low far above 0.70.
        self._patch(monkeypatch, _rows(190, 10))
        out = v.contract_70_verdict()
        assert out["status"] == "PROVEN"

    def test_proven_via_forward_revival(self, monkeypatch):
        self._patch(monkeypatch, _rows(32, 23),
                    forward={"registered": True, "revival_met": True})
        out = v.contract_70_verdict()
        assert out["status"] == "PROVEN"

    def test_expired_counts_as_non_win(self, monkeypatch):
        # 2026-07-14 correction: EXPIRED is in the denominator.
        rows = _rows(30, 0) + [
            {"symbol": f"E{i}", "eval_ts": 1, "up_prob": 0.75, "outcome": "EXPIRED"}
            for i in range(30)
        ]
        self._patch(monkeypatch, rows)
        out = v.contract_70_verdict()
        assert out["live"]["n"] == 60
        assert out["live"]["wins"] == 30

    def test_below_bucket_rows_ignored(self, monkeypatch):
        rows = _rows(5, 5) + _rows(50, 0, prob=0.60)
        self._patch(monkeypatch, rows)
        out = v.contract_70_verdict()
        assert out["live"]["n"] == 10

    def test_gates_untouched_disclaimer(self, monkeypatch):
        self._patch(monkeypatch, _rows(32, 23))
        out = v.contract_70_verdict()
        assert "firing behavior are unaffected" in out["disclaimer"]


# ── Read-only tripwire ───────────────────────────────────────────────

class TestReadOnly:
    def test_module_source_has_no_writes(self):
        path = os.path.join(os.path.dirname(__file__), "..", "core",
                            "contract_70_verdict.py")
        with open(path) as f:
            src = f.read()
        for banned in ("INSERT", "UPDATE", "DELETE", "CREATE TABLE",
                       "log_prediction", "register_slices", "register_universe"):
            assert banned not in src, f"verdict layer must not call {banned}"


# ── Contract block + route ───────────────────────────────────────────

class TestContractBlock:
    def test_ghost_contract_carries_70_block(self):
        from core.ghost_contract import ghost_contract
        c = ghost_contract()
        blk = c["contract_70"]
        assert blk["status"] == "UNPROVEN_AT_CURRENT_DATA"
        assert blk["preregistered_at"] == "2026-07-16"
        assert blk["verdict_endpoint"] == "/api/ghost/contract/70-verdict"
        # The 80% falsification block is untouched.
        assert c["falsification"]["status"] == "abandoned"


class TestRoute:
    def test_verdict_route_registered(self):
        from api.routes_ghost_system import router
        paths = [r.path for r in router.routes]
        assert "/api/ghost/contract/70-verdict" in paths

    def test_verdict_route_returns_ok(self, monkeypatch):
        monkeypatch.setattr(v, "_load_resolved_rows", lambda d, l: None)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.routes_ghost_system import router
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        r = client.get("/api/ghost/contract/70-verdict")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["status"] == "INSUFFICIENT_EVIDENCE"
        assert body["preregistration"]["preregistered_at"] == "2026-07-16"
