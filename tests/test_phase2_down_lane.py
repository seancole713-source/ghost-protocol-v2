"""Phase 2 DOWN-lane fixes — regression tests.

Covers the five defects found in the Phase 2 review:
  1. Winner-takes-all: a stronger DOWN score must never suppress a fireable UP
     pick (UP is the production lane).
  2. DOWN is shadow-only by default (V3_DOWN_SIGNALS_ENABLED=0) — journaled in
     scores, never fired.
  3. core.prediction's SELL block honors the same flag (belt-and-suspenders).
  4. Directional model keys (meta_WOLF_up) parse back to bare symbols in
     admin purge / pick-expiry paths.
  5. Peer pools are direction-separated (covered in test_wolf_app_core).
"""
import time

import numpy as np
import pytest

import core.signal_engine as _se


def _uptrend_rows(n=220):
    rows = []
    for i in range(n):
        px = 100.0 + i * 0.4
        rows.append({"ts": "2026-05-20T%02d:00:00Z" % (i % 24),
                     "open": px - 0.2, "high": px + 0.5, "low": px - 0.5,
                     "close": px, "volume": 1000 + i * 5})
    return rows


class _Model:
    def __init__(self, p_win):
        self._p = p_win

    def predict_proba(self, X):
        return np.array([[1.0 - self._p, self._p]])


_META = {"edge": 0.3, "accuracy": 0.66, "wf_acc_mean": 0.64,
         "wf_edge_mean": 0.2, "wf_fold_count": 4, "trained_at": time.time(),
         # Phase 3: a model may only fire above its proven-precision threshold.
         "precision_gate": {"ok": True, "threshold": 0.55, "target": 0.70}}


def _patch_gates(monkeypatch):
    for k, v in {"V3_MIN_WIN_PROBA": "0.55", "V3_MIN_EDGE": "0.0",
                 "V3_MIN_HOLDOUT_ACC": "0.0", "V3_MIN_WF_ACC_MEAN": "0.0"}.items():
        monkeypatch.setenv(k, v)


def _patch_models(monkeypatch, up_p=None, down_p=None):
    def _lm(sym, direction="UP"):
        if direction == "UP" and up_p is not None:
            return _Model(up_p), _se.FEATURE_COLS, dict(_META)
        if direction == "DOWN" and down_p is not None:
            return _Model(down_p), _se.FEATURE_COLS, dict(_META)
        return None, None, None
    monkeypatch.setattr(_se, "load_model", _lm)
    monkeypatch.setattr(_se, "_fetch_ohlcv",
                        lambda s, a, period="5d", interval="1h": _uptrend_rows())


def test_stronger_down_never_suppresses_fireable_up(monkeypatch):
    """Winner-takes-all regression: DOWN 0.95 vs UP 0.70 — UP must still fire."""
    _patch_gates(monkeypatch)
    monkeypatch.delenv("V3_DOWN_SIGNALS_ENABLED", raising=False)
    _patch_models(monkeypatch, up_p=0.70, down_p=0.95)

    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is not None and sig[0] == "UP"
    assert reason is None
    # Journal still records the raw stronger signal for shadow analysis
    assert scores["winning_direction"] == "DOWN"
    assert scores["down_prob"] == 0.95
    assert scores["up_prob"] == 0.70


def test_down_is_shadow_only_when_flag_off(monkeypatch):
    """UP below threshold + strong DOWN + flag off -> nothing fires, DOWN journaled."""
    _patch_gates(monkeypatch)
    monkeypatch.delenv("V3_DOWN_SIGNALS_ENABLED", raising=False)
    _patch_models(monkeypatch, up_p=0.40, down_p=0.95)

    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is None
    assert reason == "prob_low"           # UP lane's reason, not a DOWN fire
    assert scores["down_prob"] == 0.95    # still journaled for shadow analysis
    assert scores["down_signals_enabled"] is False


def test_down_fires_when_flag_on_and_up_does_not(monkeypatch):
    _patch_gates(monkeypatch)
    monkeypatch.setenv("V3_DOWN_SIGNALS_ENABLED", "1")
    _patch_models(monkeypatch, up_p=0.40, down_p=0.95)

    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is not None and sig[0] == "DOWN"
    assert reason is None


def test_up_wins_over_down_even_with_flag_on(monkeypatch):
    """With both lanes fireable, UP fires first (production lane priority)."""
    _patch_gates(monkeypatch)
    monkeypatch.setenv("V3_DOWN_SIGNALS_ENABLED", "1")
    _patch_models(monkeypatch, up_p=0.70, down_p=0.95)

    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is not None and sig[0] == "UP"


def test_down_only_model_reports_shadow_reason(monkeypatch):
    _patch_gates(monkeypatch)
    monkeypatch.delenv("V3_DOWN_SIGNALS_ENABLED", raising=False)
    _patch_models(monkeypatch, up_p=None, down_p=0.95)

    sig, reason = _se.predict_live_ex("WOLF", "stock")
    assert sig is None
    assert reason == "down_shadow_only"


def test_down_signals_flag_parsing(monkeypatch):
    """The flag both signal_engine and core.prediction's SELL block read."""
    monkeypatch.delenv("V3_DOWN_SIGNALS_ENABLED", raising=False)
    assert _se._v3_down_signals_enabled() is False
    monkeypatch.setenv("V3_DOWN_SIGNALS_ENABLED", "1")
    assert _se._v3_down_signals_enabled() is True
    monkeypatch.setenv("V3_DOWN_SIGNALS_ENABLED", "off")
    assert _se._v3_down_signals_enabled() is False


# ── directional key parsing (purge / expiry safety) ─────────────────────────

def test_strip_model_direction_suffix():
    import wolf_app
    assert wolf_app._strip_model_direction_suffix("WOLF_up") == "WOLF"
    assert wolf_app._strip_model_direction_suffix("WOLF_down") == "WOLF"
    assert wolf_app._strip_model_direction_suffix("WOLF") == "WOLF"
    assert wolf_app._strip_model_direction_suffix("NVDA_up") == "NVDA"


def test_expire_open_picks_recognizes_directional_meta_keys(monkeypatch):
    """Open WOLF picks must NOT mass-expire when only meta_WOLF_up/down exist."""
    import wolf_app

    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append((sql, params))
            self._sql = sql

        def fetchall(self):
            if "LIKE 'meta_%'" in self._sql:
                return [("meta_WOLF_up",), ("meta_WOLF_down",)]
            if "FROM predictions" in self._sql:
                return [(1, "WOLF")]
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _Ctx())
    expired = wolf_app._expire_open_picks_without_v3_model()
    assert expired == 0
    assert not any("UPDATE predictions" in sql for sql, _ in executed)


def test_purge_keeps_wolf_directional_models(monkeypatch):
    """_purge_v3_stale_or_weak must not treat WOLF_up as off-watchlist."""
    import json as _j
    import wolf_app

    deleted = []
    stale_meta = _j.dumps({
        "label_type": "tp_sl_daily",
        "label_schema": "wrong-schema",   # not serveable -> purge candidate
        "feature_schema": "stale",
        "trained_at": int(time.time()),
    })

    class _Cur:
        def execute(self, sql, params=None):
            self._sql = sql
            if sql.startswith("DELETE"):
                deleted.append(params)

        def fetchall(self):
            if "LIKE 'meta_%'" in self._sql:
                return [("meta_WOLF_up", stale_meta), ("meta_ZOMBIE_up", stale_meta)]
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _Ctx())
    monkeypatch.setattr("config.symbols.watchlist_symbols",
                        lambda include_portfolio=True: ["WOLF"])
    purged = wolf_app._purge_v3_stale_or_weak()
    # ZOMBIE is off-watchlist -> purged by raw key stem. WOLF_up normalizes to
    # WOLF (on watchlist, tp_sl_daily label) -> kept.
    assert purged == 1
    assert deleted == [("model_ZOMBIE_up", "meta_ZOMBIE_up")]


def test_delete_model_non_wolf_only_keeps_wolf_directional(monkeypatch):
    """The admin purge button must never delete WOLF's _up/_down models."""
    import json as _j
    import wolf_app

    deleted = []
    meta = _j.dumps({"accuracy": 0.7})

    class _Cur:
        def execute(self, sql, params=None):
            self._sql = sql
            if sql.startswith("DELETE"):
                deleted.append(params)

        def fetchall(self):
            return [("meta_WOLF_up", meta), ("meta_WOLF_down", meta),
                    ("meta_NVDA_up", meta), ("meta_WOLF", meta)]

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "_cron_ok", lambda s, strict=True: True)
    monkeypatch.setattr("core.db.db_conn", lambda: _Ctx())

    import asyncio
    out = asyncio.run(wolf_app.delete_model(x_cron_secret="x", non_wolf_only=True))
    assert out["ok"] is True
    assert deleted == [("model_NVDA_up", "meta_NVDA_up")]
    kept = " ".join(out["kept"])
    assert "WOLF_up" in kept and "WOLF_down" in kept
