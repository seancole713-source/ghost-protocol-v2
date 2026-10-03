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


def test_dilution_is_graded_only_when_the_case_has_a_dilution_label():
    case = dict(CASES["r02"], expected={"catalyst": True, "dilution": True})
    rec = {"claims": []}
    g = G.grade_case(case, rec=rec, verdict={"catalyst": True, "dilutive": None}, author_raw=None)
    assert g["grade"]["dilution_correct"] == 0 and g["meta"]["dilution_outcome"] == "unknown"   # unknown != no
    g = G.grade_case(case, rec=rec, verdict={"catalyst": True, "dilutive": True}, author_raw=None)
    assert g["grade"]["dilution_correct"] == 1
    trap = dict(case, expected={"catalyst": False, "dilution": False})       # a secondary sale is not dilution
    assert G.grade_case(trap, rec=rec, verdict={"catalyst": False, "dilutive": True}, author_raw=None)["meta"]["dilution_outcome"] == "fp"
    assert "dilution_correct" not in G.grade_case(CASES["r03"], rec=rec, verdict={"catalyst": False}, author_raw=None)["grade"]


def test_a_date_only_citation_is_judged_as_a_whole_day_not_midnight():
    cut = G.cutoff_epoch("2026-10-02 08:34")                 # ON pilot: SEC exhibit stamped "2026-10-01"
    assert G.in_window("2026-10-01", cut) is True            # the day overlaps the window
    assert G.in_window("2026-10-02", cut) is True
    assert G.in_window("2026-09-30", cut) is False           # wholly before the window
    assert G.in_window("2026-10-03", cut) is False           # after the cutoff: hindsight
    assert G.in_window("2026-10-01T17:22:00-04:00", cut) is True
    assert G.in_window("2026-10-02T09:00:00", cut) is False  # naive = Eastern, after the cutoff
    assert G.in_window(None, cut) is None


def test_the_openai_reviewer_path_is_used_recorded_and_its_fallback_flagged(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "stub")
    monkeypatch.setenv("EDGE_OPENAI_MODEL", "gpt-6-sol")
    case = CASES["r01"]
    author = R._Stub([R._reply(_author(case, 3600))])                # no Claude review reply needed
    out = R.run_case(case, 0, lambda c: author, allow_missing_model=False,
                     http_factory=lambda c: R._StubHttp(R.CLEAN_REVIEW))
    row = out["row"]
    assert row["meta"]["reviewer"] == "openai:gpt-6-sol" and row["meta"]["reviewer_fallback"] is False
    assert row["meta"]["reviewer_calls"][0]["served_model"] == "gpt-6-sol"
    assert any("[reviewer:openai" in t["content"] for t in out["trace"] if t["role"] == "user")
    # An unusable OpenAI reply makes the worker fall back to Claude, as in production: flagged, not hidden.
    both = R._Stub([R._reply(_author(case, 3600)), R._reply(REVIEW)])
    row = R.run_case(case, 0, lambda c: both, allow_missing_model=False,
                     http_factory=lambda c: R._StubHttp(None, status=500))["row"]
    assert row["meta"]["reviewer_fallback"] is True


def test_a_substituted_reviewer_model_fails_the_attempt(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "stub")
    monkeypatch.setenv("EDGE_OPENAI_MODEL", "gpt-6-sol")

    class Swapped(R._StubHttp):
        def post(self, url, **kw):
            r = super().post(url, **kw)
            r._p["model"] = "gpt-4.1"
            return r
    case = CASES["r01"]
    out = R.run_case(case, 0, lambda c: R._Stub([R._reply(_author(case, 3600))]), allow_missing_model=False,
                     http_factory=lambda c: Swapped(R.CLEAN_REVIEW))
    assert out["error"]["failure_class"] == "served_model_mismatch"


def test_an_exact_midnight_stamp_is_a_date_placeholder_not_a_time():
    cut = G.cutoff_epoch("2026-10-02 08:34")
    assert G.in_window("2026-10-01T00:00:00-04:00", cut) is True      # eval v1 r01: SEC exhibit
    assert G.in_window("2026-09-30T00:00:00-04:00", cut) is False
    assert G.in_window("2026-10-01T00:01:00-04:00", cut) is False     # a real time is still judged as one
