"""One-shot stop-geometry sweep on the box.

V3_STOP_VOL_MULT=1.8 is the binding constraint on the whole engine. Confirmed
live 2026-09-05 from the running fleet retrain: every lane failed, 10 of the
first 11 with NEGATIVE walk-forward edge, and ARDT/UP scored acc=67.7% with
edge=0.0% -- a wide stop inflating the win rate into something that looks
excellent and predicts nothing. That fails the unrelaxable wf_edge_mean >= 0.0
check, so tier = "proven" if passes else "research" stamps every model
"research", and research tier is hard-blocked as the first statement of the lane
evaluator: the v3_research_tier skip on 92 of 107 symbols.

The two sweep scripts already answer which multiplier restores edge. They can
only run on the deployed container -- the OHLCV chain needs provider keys and
egress that exist nowhere else -- so this job runs them there and puts the
tables in the logs.

These tests pin the three properties that make running them on a live trading
engine safe.
"""
from __future__ import annotations

import threading

import pytest

import core.geometry_sweep_job as gsj


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("GEOMETRY_SWEEP_ENABLED", raising=False)
    monkeypatch.delenv("GEOMETRY_SWEEP_TIMEOUT_S", raising=False)
    monkeypatch.setattr(gsj, "sweep_marker", lambda: None)
    monkeypatch.setattr(gsj, "_store_marker", lambda payload: None)


# ------------------------------------------------- subprocess isolation --

def test_the_sweep_runs_in_a_child_process(monkeypatch):
    """THE critical property. The scripts set os.environ["V3_STOP_VOL_MULT"] as
    they walk candidate geometries. Imported into the app process that would
    silently re-label the LIVE engine mid-flight and leave it wherever the loop
    ended. A child process has its own environment block."""
    calls = {}

    class _Done:
        returncode = 0
        stdout = "mult 0.65 wf_edge +0.22\n"
        stderr = ""

    def fake_run(argv, **kw):
        calls["argv"] = argv
        calls["kw"] = kw
        return _Done()

    monkeypatch.setattr(gsj.subprocess, "run", fake_run)

    out = gsj.run_one_sweep("geometry_edge_sweep.py")

    assert out["ok"] is True
    assert calls["argv"][0] == gsj.sys.executable, "must be a separate interpreter"
    assert calls["argv"][1].endswith("geometry_edge_sweep.py")
    assert calls["kw"]["env"] is not gsj.os.environ, "child must not share the env block"


def test_the_parent_environment_is_never_mutated(monkeypatch):
    """If the sweep's stop multiplier leaked into this process, every label the
    live engine wrote afterwards would use the wrong geometry."""
    monkeypatch.setenv("V3_STOP_VOL_MULT", "1.8")

    class _Done:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    monkeypatch.setattr(gsj.subprocess, "run", lambda *a, **k: _Done())

    gsj.run_one_sweep("geometry_edge_sweep.py")

    assert gsj.os.environ["V3_STOP_VOL_MULT"] == "1.8"


# ------------------------------------------------------- lock discipline --

def test_it_defers_while_a_retrain_holds_the_lock(monkeypatch):
    """A fleet retrain and this sweep are both CPU-heavy; overlapping them on a
    live engine starves both."""
    held = threading.Lock()
    held.acquire()
    monkeypatch.setitem(__import__("sys").modules, "wolf_app",
                        type("M", (), {"_RETRAIN_JOB_LOCK": held})())
    monkeypatch.setattr(gsj, "run_one_sweep",
                        lambda *a, **k: pytest.fail("must not run under the lock"))

    out = gsj.run_geometry_sweep_once()

    assert out["status"] == "deferred"
    assert out["reason"] == "retrain_in_progress"


def test_the_lock_is_released_even_when_a_sweep_explodes(monkeypatch):
    """A leaked lock would block every future retrain, permanently."""
    lock = threading.Lock()
    monkeypatch.setitem(__import__("sys").modules, "wolf_app",
                        type("M", (), {"_RETRAIN_JOB_LOCK": lock})())

    def boom(*a, **k):
        raise RuntimeError("sweep died")

    monkeypatch.setattr(gsj, "run_one_sweep", boom)

    with pytest.raises(RuntimeError):
        gsj.run_geometry_sweep_once()

    assert not lock.locked(), "retrain lock leaked"


# ------------------------------------------------------------ run once --

def test_it_runs_once_ever(monkeypatch):
    """A failing sweep that retried every cycle would be a self-inflicted load
    generator on a live trading engine."""
    monkeypatch.setattr(gsj, "sweep_marker", lambda: {"finished_at": 1788600000})
    monkeypatch.setattr(gsj, "run_one_sweep",
                        lambda *a, **k: pytest.fail("must not re-run"))

    out = gsj.run_geometry_sweep_once()

    assert out["status"] == "already_run"


def test_the_marker_is_written_even_when_the_sweep_fails(monkeypatch):
    stored = {}
    monkeypatch.setattr(gsj, "_store_marker", lambda p: stored.update(p))
    monkeypatch.setattr(gsj, "run_one_sweep",
                        lambda script, **k: {"script": script, "ok": False,
                                             "reason": "timeout"})
    monkeypatch.setitem(__import__("sys").modules, "wolf_app",
                        type("M", (), {"_RETRAIN_JOB_LOCK": threading.Lock()})())

    out = gsj.run_geometry_sweep_once()

    assert out["ok"] is False
    assert stored.get("version") == gsj.SWEEP_VERSION
    assert stored.get("finished_at")


def test_a_timeout_is_reported_not_raised(monkeypatch):
    def timeout(*a, **k):
        raise gsj.subprocess.TimeoutExpired(cmd="sweep", timeout=1)

    monkeypatch.setattr(gsj.subprocess, "run", timeout)

    out = gsj.run_one_sweep("geometry_edge_sweep.py")

    assert out["ok"] is False
    assert out["reason"] == "timeout"


def test_a_missing_script_is_reported_not_raised(monkeypatch):
    out = gsj.run_one_sweep("no_such_sweep.py")

    assert out["ok"] is False
    assert out["reason"] == "script_missing"


# ------------------------------------------------------------- coverage --

def test_both_sweeps_are_run(monkeypatch):
    """Edge alone is not the answer: geometry_edge_sweep says which multiplier
    restores edge, provable_oppoint_sweep says whether a provable operating
    point exists there -- the question the 2026-07-08 run only asked at the 70%
    target, while production now runs contract 55."""
    ran = []
    monkeypatch.setattr(gsj, "run_one_sweep",
                        lambda script, **k: (ran.append(script) or
                                             {"script": script, "ok": True}))
    monkeypatch.setitem(__import__("sys").modules, "wolf_app",
                        type("M", (), {"_RETRAIN_JOB_LOCK": threading.Lock()})())

    gsj.run_geometry_sweep_once()

    assert ran == ["geometry_edge_sweep.py", "provable_oppoint_sweep.py"]


def test_both_sweep_scripts_actually_exist():
    """A named script that is not in the image would fail only in production."""
    root = gsj._repo_root()
    for script in gsj.SWEEPS:
        assert (root / "scripts" / script).exists(), script


def test_output_is_logged_at_warning_so_it_survives_the_log_level(caplog):
    """The whole point of the job is that the numbers reach a human."""
    import logging

    class _Done:
        returncode = 0
        stdout = "mult  meanWfEdge\n0.65  +0.22\n1.8   -0.06\n"
        stderr = ""

    import unittest.mock as mock
    with mock.patch.object(gsj.subprocess, "run", return_value=_Done()):
        with caplog.at_level(logging.WARNING, logger="ghost.geometry_sweep"):
            gsj.run_one_sweep("geometry_edge_sweep.py")

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "0.65" in logged and "+0.22" in logged


def test_job_registers_with_an_explicit_initial_delay():
    """register() defers by a full interval otherwise — the PR #178 trap."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "wolf_app.py").read_text(encoding="utf-8")
    idx = src.index('"geometry_sweep",')

    assert "initial_delay_s=" in src[idx:idx + 700]
