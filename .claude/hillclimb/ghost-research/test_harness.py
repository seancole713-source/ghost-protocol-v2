"""Harness checks for the research eval. No network, no spend (stub clients only)."""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_eval as R  # noqa: E402
import grading as G  # noqa: E402

CASES = {c["id"]: c for c in (json.loads(x) for x in (HERE / "cases.jsonl").read_text().splitlines())}


def _run(case_id, factory):
    return R.run_case(CASES[case_id], 0, factory, allow_missing_model=False)


def _author(case, published_offset_s, statement="ON announced a dated event."):
    cut = G.cutoff_epoch(case["cutoff_et"])
    iso = time.strftime("%Y-%m-%dT%H:%M:%S-04:00", time.gmtime(cut - published_offset_s - 4 * 3600))
    claim = {"kind": "m_and_a", "statement": statement, "value": None, "unit": None, "unknowns": [],
             "citations": [{"url": "https://www.sec.gov/a", "quote": "q", "published_at": iso},
                           {"url": "https://www.reuters.com/b", "quote": "q", "published_at": iso}]}
    return json.dumps({"claims": [claim], "unknowns": []})


REVIEW = json.dumps({"entity_ok": True, "contradictions": [], "dilution_found": False, "stale": False, "notes": ""})


def test_oracle_passes_and_null_does_not_look_like_a_pass():
    ok = _run("r01", R.stub_factory("oracle"))["row"]
    assert ok["grade"] == {"catalyst_correct": 1, "pit_ok": 1, "trap_ok": 1}
    empty = _run("r03", R.stub_factory("empty"))
    assert "row" not in empty and empty["error"]["failure_class"] == "harness_error"   # no answer != "no catalyst"


def test_a_citation_outside_the_24h_window_fails_the_point_in_time_check():
    case = CASES["r01"]
    stub = lambda c: R._Stub([R._reply(_author(case, 3 * 24 * 3600)), R._reply(REVIEW)])  # noqa: E731
    row = _run("r01", stub)["row"]
    assert row["grade"]["pit_ok"] == 0 and row["meta"]["out_of_window_cites"]


def test_citation_after_the_cutoff_is_hindsight_and_fails():
    case = CASES["r01"]
    stub = lambda c: R._Stub([R._reply(_author(case, -2 * 3600)), R._reply(REVIEW)])  # noqa: E731
    assert _run("r01", stub)["row"]["grade"]["pit_ok"] == 0


def test_using_the_namesake_company_fails_the_trap_check():
    case = CASES["r05"]
    bad = json.loads(_author(case, 3600, "aTyr Pharma announced a trial result."))
    bad["claims"][0]["kind"] = "fda_regulatory"
    stub = lambda c: R._Stub([R._reply(json.dumps(bad)), R._reply(REVIEW)])  # noqa: E731
    row = _run("r05", stub)["row"]
    assert row["grade"]["trap_ok"] == 0 and row["meta"]["outcome"] == "fp" and row["grade"]["catalyst_correct"] == 0


def test_api_error_and_served_model_mismatch_land_in_errors_not_zero_grades():
    def boom(**kw):
        raise RuntimeError("connection reset")
    out = _run("r02", lambda c: NS(beta=NS(messages=NS(create=boom))))
    assert "row" not in out                                   # the worker records a failed call as not_researched
    wrong = R._reply("{}")
    wrong.model = "some-other-model"
    out = _run("r02", lambda c: NS(beta=NS(messages=NS(create=lambda **kw: wrong))))
    assert out["error"]["failure_class"] == "served_model_mismatch"


def test_summary_recomputes_from_rows_and_reports_majority_baseline():
    rows = [{"status": "ok", "grade": {"catalyst_correct": int(o in ("tp", "tn")), "pit_ok": 1, "trap_ok": 1},
             "meta": {"outcome": o, "expected_catalyst": o in ("tp", "fn")}} for o in ("tp", "fn", "tn", "tn", "tn")]
    s = G.summarize(rows)
    assert s["recall"] == 0.5 and s["specificity"] == 1.0 and s["majority_baseline"] == 0.6
