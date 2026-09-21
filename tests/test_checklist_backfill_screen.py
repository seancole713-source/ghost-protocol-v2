"""The retrospective checklist screen.

Prospective calibration only started filling on 2026-09-04 (PR #180 unblocked
snapshot writes), so "does a higher checklist score win more often" was
otherwise weeks from an answer. This replays the checklist against shadow rows
that already resolved, using point-in-time evidence, to get a first read now.

It is a SCREEN, not proof — its job is to kill a dead idea cheaply. The tests
below pin the two properties that make it worth trusting for that job:

  * a genuinely discriminating checklist produces a large spread, a flat one
    produces ~0, so the screen can actually distinguish the two
  * it writes NOTHING to the checklist ledger, so a retrospective replay can
    never contaminate the prospective record it exists to pre-empt
"""
from __future__ import annotations

import core.checklist_backfill_screen as screen


def _rows(n, direction="UP", win_every=2):
    return [
        {"symbol": f"SYM{i%7}", "direction": direction,
         "eval_ts": 1_700_000_000 + i * 3600,
         "outcome": "WIN" if i % win_every == 0 else "LOSS"}
        for i in range(n)
    ]


def _patch(monkeypatch, rows, score_fn):
    monkeypatch.setattr(screen, "_resolved_rows", lambda limit: rows[:limit])

    import core.checklist_evidence as ev
    import core.catalyst_checklist as cc

    monkeypatch.setattr(ev, "collect_evidence", lambda sym, **kw: {"sym": sym})
    monkeypatch.setattr(
        cc, "evaluate_checklist",
        lambda sym, direction, evidence: {"score_pct": score_fn(sym, direction)},
    )


# ------------------------------------------------------- can it detect signal --

def test_screen_detects_a_discriminating_checklist(monkeypatch):
    """High scores on winners, low on losers -> large spread."""
    rows = _rows(60)
    outcome_by_ts = {r["eval_ts"]: r["outcome"] for r in rows}

    calls = {"i": 0}

    def score_fn(sym, direction):
        # Walk the rows in order, scoring winners high and losers low.
        row = rows[calls["i"]]
        calls["i"] += 1
        return 85.0 if outcome_by_ts[row["eval_ts"]] == "WIN" else 15.0

    _patch(monkeypatch, rows, score_fn)
    out = screen.run_screen(limit=60)
    up = out["directions"]["UP"]

    assert up["total_samples"] == 60
    assert up["spread_pp"] is not None and up["spread_pp"] > 50, up


def test_screen_reads_flat_when_the_checklist_carries_no_information(monkeypatch):
    """The result that should stop the project: score uncorrelated with outcome.

    Scores must be drawn INDEPENDENTLY of the outcome. An earlier version of
    this test alternated scores on call index while outcomes alternated on row
    index, which is perfect anti-correlation, not independence — and the screen
    correctly reported it as -100pp. Real signal, just inverted.
    """
    import random

    rows = _rows(240)
    rng = random.Random(20260904)

    def score_fn(sym, direction):
        return 85.0 if rng.random() < 0.5 else 15.0

    _patch(monkeypatch, rows, score_fn)
    out = screen.run_screen(limit=240)
    up = out["directions"]["UP"]

    assert up["total_samples"] == 240
    assert abs(up["spread_pp"]) < 20, f"uncorrelated scores read as signal: {up}"


# -------------------------------------------------------------- containment --

def test_screen_writes_nothing_to_the_checklist_ledger(monkeypatch):
    """A retrospective replay must never enter the prospective cohorts."""
    import core.checklist_ledger as ledger

    def explode(*a, **kw):
        raise AssertionError("screen must not write to the checklist ledger")

    monkeypatch.setattr(ledger, "store_snapshot", explode, raising=False)
    monkeypatch.setattr(ledger, "record_snapshot", explode, raising=False)

    _patch(monkeypatch, _rows(20), lambda s, d: 50.0)
    screen.run_screen(limit=20)


def test_a_bad_row_does_not_stop_the_screen(monkeypatch):
    rows = _rows(20)
    calls = {"i": 0}

    def score_fn(sym, direction):
        calls["i"] += 1
        if calls["i"] == 3:
            raise RuntimeError("evidence unavailable")
        return 50.0

    _patch(monkeypatch, rows, score_fn)
    out = screen.run_screen(limit=20)

    assert out["row_errors"] == 1
    assert out["directions"]["UP"]["total_samples"] == 19


def test_result_labels_itself_as_screening_evidence(monkeypatch):
    """The payload must carry its own caveat: a reader should never mistake a
    replay for prospective proof."""
    _patch(monkeypatch, _rows(10), lambda s, d: 50.0)
    out = screen.run_screen(limit=10)

    assert out["evidence"] == "retrospective_replay"
    assert "confirmed prospectively" in out["caveat"]
    assert out["screen_version"]


def test_both_directions_reported_separately(monkeypatch):
    rows = _rows(10, direction="UP") + _rows(10, direction="DOWN")
    _patch(monkeypatch, rows, lambda s, d: 50.0)

    out = screen.run_screen(limit=20)

    assert set(out["directions"]) == {"UP", "DOWN"}
    assert out["directions"]["UP"]["total_samples"] == 10
    assert out["directions"]["DOWN"]["total_samples"] == 10


# ------------------------------------------------------------------ wiring --

def test_context_never_computes_the_screen_on_the_read_path(monkeypatch):
    """Computing inline would warm an EDGAR fetch per symbol inside a request."""
    import core.ghost_ask as ga

    monkeypatch.setattr(
        screen, "run_screen",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not compute inline")),
    )
    monkeypatch.setattr(screen, "cached_screen", lambda: {"screen_version": "x"})

    ctx = ga.build_ask_context()

    assert ctx.get("checklist_backfill_screen") == {"screen_version": "x"}


def test_screen_job_registers_with_an_explicit_initial_delay():
    """register() defers by a full interval unless told otherwise -- the exact
    reason weekly_retrain never fired (PR #178). A daily job without this would
    wait a day past every deploy and, on a service that redeploys often, never
    run at all."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "wolf_app.py").read_text(encoding="utf-8")

    assert '"checklist_backfill_screen"' in src
    idx = src.index('"checklist_backfill_screen", _checklist_screen_job')
    tail = src[idx:idx + 220]
    assert "initial_delay_s=" in tail, "screen job would defer a full interval"
