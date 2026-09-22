"""edge as a standalone service."""
from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "tests")

from edge import service as SV  # noqa: E402
from edge.ledger import MemoryStore  # noqa: E402
from test_edge_pipeline import FakeAlpaca  # noqa: E402

ET = ZoneInfo("America/New_York")


def at(hh, mm):
    return datetime(2026, 9, 23, hh, mm, tzinfo=ET).timestamp()


def test_refuses_to_start_without_the_explicit_switch(monkeypatch):
    monkeypatch.delenv("EDGE_STANDALONE", raising=False)
    assert SV.main() == 2


def test_a_tick_runs_the_same_pipeline(monkeypatch):
    for v in ("EDGE_PAPER_ENABLED", "EDGE_TELEGRAM_ENABLED", "EDGE_PROBE_ENABLED", "EDGE_BACKTEST_ENABLED"):
        monkeypatch.setenv(v, "0")
    store = MemoryStore()
    svc = SV.Service(store, get=FakeAlpaca(), http=False, notifier=False, clock=lambda: at(9, 10))
    out = svc.tick()
    assert out["card"]["status"] == "issued"
    assert store.get("edge_cards", "2026-09-23")["forecasts"] == ["SHOP"]


def test_one_bad_tick_never_stops_the_loop(monkeypatch):
    for v in ("EDGE_PAPER_ENABLED", "EDGE_TELEGRAM_ENABLED", "EDGE_PROBE_ENABLED", "EDGE_BACKTEST_ENABLED"):
        monkeypatch.setenv(v, "0")
    svc = SV.Service(MemoryStore(), get=FakeAlpaca(), http=False, notifier=False, clock=lambda: at(12, 0))
    ticks = []

    def boom():
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("provider down")
        svc.running = False
    svc.tick = boom
    svc.forever(interval_s=1)
    assert len(ticks) == 2
