"""A latched pause held up by evidence that no longer exists.

PR #172 stopped the kill switch TRIPPING on stale outcomes by bounding
admissible evidence to KILL_RECENCY_DAYS. It did not fix the mirror image one
level up: a latch set by a now-aged-out trip still held forever, because
enforce_kill_conditions returned early on `prev["latched"]` without ever asking
whether the evidence behind it still existed.

Observed live 2026-09-04: all four conditions read `insufficient` with 0
samples, `any_triggered: false`, and the engine still paused on
`brier->degrade_watching` — a pause held up by rows the switch itself would no
longer accept, in a state that could never change (a pause stops firing, no
firing means no new outcomes, so the window stays empty forever).

The clear is deliberately narrow. It fires ONLY when resolved_available is 0.
A latch over evidence that exists and merely is not tripping still needs a
human, because there is something real to review. The switch stays armed in
both cases, so genuine degradation re-trips on the next resolved picks.
"""
from __future__ import annotations

import core.prediction as pred


def _evaluation(*, resolved_available: int, triggered: bool = False):
    return {
        "ok": True,
        "resolved_available": resolved_available,
        "recency_days": 14,
        "conditions": [{
            "name": "brier", "action": "degrade_watching",
            "triggered": triggered, "status": "red" if triggered else "insufficient",
            "window": 15, "samples": resolved_available, "current": None,
            "threshold": 0.35, "comparator": ">",
        }],
    }


def _run(monkeypatch, *, prev, evaluation):
    monkeypatch.setattr(pred, "_kill_cfg", lambda: {
        "enabled": True, "winrate_floor": 0.45, "winrate_window": 30,
        "brier_ceiling": 0.35, "brier_window": 15, "consec_losses": 3,
        "expectancy_window": 15, "cooldown_minutes": 1440, "min_samples": 10,
        "recency_days": 14,
    })
    monkeypatch.setattr(pred, "engine_pause_state", lambda: prev)
    monkeypatch.setattr(pred, "evaluate_kill_conditions", lambda **kw: evaluation)

    cleared = {"called": False}
    monkeypatch.setattr(
        pred, "_clear_engine_pause",
        lambda: cleared.__setitem__("called", True),
    )

    class Cur:
        def execute(self, sql, params=None): pass
        def fetchone(self): return None
        def fetchall(self): return []

    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass
        def rollback(self): pass

    class Ctx:
        def __enter__(self): return Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(pred, "db_conn", lambda: Ctx())
    monkeypatch.setattr(pred, "ensure_ghost_state", lambda cur: None)

    return pred.enforce_kill_conditions(), cleared


_LATCHED = {
    "paused": True, "reason": "brier->degrade_watching",
    "since": 1788456424, "latched": True,
}


def test_latch_clears_when_no_admissible_evidence_remains(monkeypatch):
    """The live 2026-09-04 state: nothing tripping, zero rows in window."""
    out, cleared = _run(
        monkeypatch, prev=_LATCHED, evaluation=_evaluation(resolved_available=0),
    )

    assert cleared["called"] is True
    assert out["paused"] is False
    assert out["latch_expired"] is True


def test_latch_holds_while_evidence_still_exists(monkeypatch):
    """The narrowness that makes this safe: evidence present but not tripping
    still needs a human, because there is something real to review."""
    out, cleared = _run(
        monkeypatch, prev=_LATCHED, evaluation=_evaluation(resolved_available=12),
    )

    assert cleared["called"] is False
    assert out["paused"] is True
    assert out.get("latch_expired") is None


def test_a_tripping_condition_still_pauses_even_with_no_history(monkeypatch):
    """Auto-clear must never pre-empt a live trip."""
    out, _ = _run(
        monkeypatch, prev={"paused": False},
        evaluation=_evaluation(resolved_available=0, triggered=True),
    )

    assert out["paused"] is True
    assert "brier" in out["reason"]


def test_unlatched_pause_still_auto_resumes_as_before(monkeypatch):
    """Cooldown-only pauses were already self-clearing; unchanged."""
    out, cleared = _run(
        monkeypatch,
        prev={"paused": True, "reason": "consecutive_losses->cooldown",
              "since": 1, "latched": False},
        evaluation=_evaluation(resolved_available=0),
    )

    assert cleared["called"] is True
    assert out["paused"] is False
    assert out.get("latch_expired") is None


def test_the_early_return_that_held_forever_is_gone():
    """Guards the exact shape of the regression."""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "core" / "prediction.py").read_text(encoding="utf-8")

    # The old code returned unconditionally on the latch. It must now be
    # conditioned on evidence still being present.
    assert re.search(
        r'if prev\.get\("latched"\):\s*\n(?:\s*#.*\n)*\s*if int\(ev\.get\("resolved_available"\)',
        src,
    ), "the latch branch no longer checks whether evidence remains"
