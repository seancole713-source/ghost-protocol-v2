"""Dedup guards for run_prediction_cycle save path."""
import json
import time


def test_symbol_has_open_pick_true():
    from core.prediction import _symbol_has_open_pick

    class _Cur:
        def execute(self, sql, params):
            self.params = params

        def fetchone(self):
            return (1,)

    assert _symbol_has_open_pick(_Cur(), "WOLF") is True


def test_symbol_has_open_pick_false():
    from core.prediction import _symbol_has_open_pick

    class _Cur:
        def fetchone(self):
            return None

        def execute(self, sql, params):
            pass

    assert _symbol_has_open_pick(_Cur(), "WOLF") is False


def test_run_prediction_cycle_blocks_second_open_pick(monkeypatch):
    import core.prediction as pred

    inserts = []
    open_after_first = {"WOLF": False}

    class _Cur:
        def execute(self, sql, params=None):
            if "pg_advisory_xact_lock" in sql:
                return
            if sql.strip().startswith("SELECT 1 FROM predictions"):
                sym = params[0]
                if open_after_first.get(sym):
                    self._row = (1,)
                else:
                    self._row = None
            elif sql.strip().startswith("INSERT INTO predictions"):
                sym = params[0]
                inserts.append(sym)
                open_after_first[sym] = True
                self._row = (len(inserts),)
            elif "ghost_state" in sql:
                return

        def fetchone(self):
            return getattr(self, "_row", None)

    class _Conn:
        def cursor(self):
            return _Cur()

        def rollback(self):
            pass

    class _Db:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    pick = {
        "symbol": "WOLF",
        "direction": "UP",
        "confidence": 0.95,
        "entry_price": 67.2,
        "target_price": 68.88,
        "stop_price": 66.1,
        "predicted_at": int(time.time()),
        "expires_at": int(time.time()) + 360000,
        "asset_type": "stock",
        "features": {},
        "scores": {},
    }

    monkeypatch.setattr(pred, "enforce_kill_conditions", lambda: {"paused": False})
    monkeypatch.setattr(pred, "objective_autotune_mode", lambda: "normal")
    monkeypatch.setattr(pred, "_check_regime", lambda: {"reason": "", "confidence_floor_override": 0.75})
    monkeypatch.setattr(pred, "_is_market_hours", lambda: True)
    monkeypatch.setattr(pred, "_is_premarket", lambda: False)
    monkeypatch.setattr(pred, "_circuit_breaker_floor", lambda: (0.75, False, ""))
    monkeypatch.setattr(pred, "db_conn", lambda: _Db())
    monkeypatch.setattr(pred, "_predict_symbol_ex", lambda *a, **k: (pick, None))

    saved1, _ = pred.run_prediction_cycle(with_diag=True)
    saved2, diag2 = pred.run_prediction_cycle(with_diag=True)

    assert len(saved1) == 1
    assert len(saved2) == 0
    assert diag2["dedup_blocked"] == 1
    assert inserts == ["WOLF"]
