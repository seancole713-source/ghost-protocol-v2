"""Guards the Super Ghost Master Plan artifacts so the roadmap stays complete + honest.

These tests make "the map is complete" a CI-enforced property: every MASTER DIRECTIVE
requirement must map to a real phase that has an acceptance gate, statuses must be valid,
and the honesty boundary (no guaranteed-profit claims) must be present.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "docs" / "super_ghost_master_plan.json"
DOC = ROOT / "docs" / "SUPER_GHOST_MASTER_BUILD.md"

VALID_STATUS = {"done", "partial", "planned"}


def _load():
    return json.loads(PLAN.read_text(encoding="utf-8"))


def test_plan_json_is_valid_and_present():
    assert PLAN.exists(), "master plan JSON missing"
    d = _load()
    for key in ("mission", "honesty_boundary", "requirements", "phases", "definition_of_done"):
        assert key in d, f"master plan missing '{key}'"


def test_master_doc_exists_and_links_manifest():
    assert DOC.exists(), "master build markdown missing"
    text = DOC.read_text(encoding="utf-8")
    assert "super_ghost_master_plan.json" in text, "doc must reference the machine-readable manifest"
    assert "test_master_plan.py" in text, "doc must reference its own enforcement test"


def test_every_requirement_maps_to_a_phase_with_a_gate():
    d = _load()
    phases = {p["id"]: p for p in d["phases"]}
    # Every phase must have a non-empty acceptance gate.
    for pid, p in phases.items():
        assert p.get("gate"), f"phase {pid} has no acceptance gate"
    # Every requirement maps to an existing phase + valid status, with unique IDs.
    seen = set()
    for r in d["requirements"]:
        rid = r["id"]
        assert rid not in seen, f"duplicate requirement id {rid}"
        seen.add(rid)
        assert r["status"] in VALID_STATUS, f"{rid} has invalid status {r['status']}"
        assert r["phase"] in phases, f"{rid} maps to unknown phase {r['phase']}"
        assert r.get("directive"), f"{rid} missing directive text"


def test_requirement_ids_are_well_formed():
    d = _load()
    pat = re.compile(r"^R-[A-Z]+-\d{2}$")
    for r in d["requirements"]:
        assert pat.match(r["id"]), f"malformed requirement id: {r['id']}"


def test_requirement_count_is_substantial():
    # The directive is large; a too-small map means we dropped requirements.
    d = _load()
    assert len(d["requirements"]) >= 80, "requirement set looks incomplete (<80)"


def test_directive_domains_are_all_covered():
    """Each major MASTER DIRECTIVE domain must have at least one requirement prefix."""
    d = _load()
    prefixes = {r["id"].split("-")[1] for r in d["requirements"]}
    required_domains = {
        "ARCH",   # architecture
        "MODEL",  # AI/ML/DL/ensemble/RL
        "FEAT",   # feature engineering
        "DATA",   # providers/db/cache/infra-data
        "LABEL",  # labeling
        "VAL",    # validation
        "LEARN",  # continuous learning
        "EXPL",   # explainability
        "RISK",   # risk management
        "UI",     # user interface
        "ENG",    # engineering standards
        "PHIL",   # development philosophy
    }
    missing = required_domains - prefixes
    assert not missing, f"master plan is missing directive domains: {sorted(missing)}"


def test_phases_are_ordered_and_gated():
    d = _load()
    ids = [p["id"] for p in d["phases"]]
    assert ids[0] == "P0", "phase 0 (foundation) must be first"
    # P0 must be the shipped foundation.
    p0 = next(p for p in d["phases"] if p["id"] == "P0")
    assert p0["status"] == "done"


def test_honesty_boundary_forbids_guaranteed_profit_claims():
    d = _load()
    hb = d["honesty_boundary"]
    blob = json.dumps(hb).lower()
    assert "no_guaranteed_profit" in hb
    assert "guarantee" in blob or "guaranteed" in blob
    # The doc itself must carry the not-financial-advice boundary.
    text = DOC.read_text(encoding="utf-8").lower()
    assert "not financial advice" in text
    assert "no guaranteed profit" in text or "guaranteed profit" in text


def test_shipped_foundation_requirements_are_marked_done():
    """Sanity: the truth-ledger + explanation foundation we verified live must read 'done'."""
    d = _load()
    by_id = {r["id"]: r for r in d["requirements"]}
    for rid in ("R-LEARN-01", "R-LEARN-02", "R-EXPL-01", "R-ENG-09"):
        assert by_id[rid]["status"] == "done", f"{rid} should be done (shipped + verified live)"
