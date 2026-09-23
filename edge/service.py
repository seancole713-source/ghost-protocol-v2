"""edge as its own service -- every job, no Ghost process required.

Today edge runs inside Ghost's scheduler (wolf_app registers four jobs). This
module is the same work as a standalone loop, so edge can move to its own
repository and Railway service without a rewrite:

    python -m edge.service            # needs DATABASE_URL + the data keys

It connects to Postgres itself (psycopg2), keeps the same edge_rows table, and
runs on the same clock:
  every 5 min   pipeline.run  (universe, miss review, research, card, intraday
                              radar, paper orders, grading, replay, messages)
  daily         readiness probe
  overnight     backtest + model training (once per BACKTEST_VERSION)

Do NOT run it alongside Ghost's own edge jobs against the same database: both
would act on the same day. EDGE_STANDALONE=1 is required, and Ghost's jobs
should be switched off (EDGE_SHADOW_ENABLED=0, EDGE_PROBE_ENABLED=0,
EDGE_BACKTEST_ENABLED=0) when this takes over.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from edge.contracts import ET

LOG = logging.getLogger("edge.service")


def pg_connect_factory(dsn: str) -> Callable[[], Any]:
    import psycopg2

    @contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return connect


class Service:
    def __init__(self, store, *, get=None, http=None, notifier=None, clock=time.time, sleep=time.sleep) -> None:
        import requests
        from edge.ledger import Ledger
        self.store, self.ledger = store, Ledger(store)
        self.get, self.http = get or requests.get, http if http is not None else requests
        self.notifier = notifier if notifier is not None else requests
        self.clock, self.sleep = clock, sleep
        self.last_probe: Optional[float] = None
        self.running = True

    def tick(self) -> Dict[str, Any]:
        from edge import pipeline
        now = int(self.clock())
        out = pipeline.run(self.get, self.ledger, now=now,
                           http=self.http if _on("EDGE_PAPER_ENABLED") else None,
                           notifier=self.notifier if _on("EDGE_TELEGRAM_ENABLED") else None)
        if pipeline.noteworthy(out):
            LOG.warning("EDGE_SHADOW %s", json.dumps(out, default=str)[:4000])
        if _on("EDGE_PROBE_ENABLED") and (self.last_probe is None or now - self.last_probe >= 86_400):
            self.probe(now)
        et = datetime.fromtimestamp(now, tz=ET)
        if _on("EDGE_BACKTEST_ENABLED") and not (5 <= et.hour < 20):
            self.backtest(et)
        return out

    def probe(self, now: int) -> None:
        from edge.probe import run as probe_run, summary_lines
        rep = probe_run(http=self.http)
        for ln in summary_lines(rep):
            LOG.warning("EDGE_PROBE_SUMMARY %s", ln)
        self.store.put("edge_probe", "latest", {k: v for k, v in rep.items() if k != "probes"})
        from edge import calendar as CAL
        from edge.contracts import ET
        from datetime import datetime
        LOG.warning("EDGE_CALENDAR %s", CAL.refresh(self.http, self.store,
                                                    today=datetime.fromtimestamp(now, tz=ET).date(), now=now))
        self.last_probe = now

    def backtest(self, et) -> None:
        from edge import backtest as BT
        from edge.pipeline import previous_trading_day
        if self.store.get("edge_backtest", BT.BACKTEST_VERSION):
            return
        days = max(5, min(250, int(os.getenv("EDGE_BACKTEST_DAYS", "60"))))
        out = BT.run(self.get, self.store, end_day=previous_trading_day(et.date()), days=days)
        LOG.warning("EDGE_BACKTEST %s", json.dumps(out, default=str)[:6000])

    def forever(self, interval_s: int = 300) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        while self.running:
            started = self.clock()
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - one bad tick never stops the service
                LOG.warning("edge tick failed: %s", str(exc)[:200])
            self.sleep(max(1.0, interval_s - (self.clock() - started)))


def _on(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    if not _on("EDGE_STANDALONE", "0"):
        LOG.error("refusing to start: set EDGE_STANDALONE=1, and switch off Ghost's edge jobs first")
        return 2
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        LOG.error("DATABASE_URL is required")
        return 2
    from edge.store_pg import PostgresStore
    store = PostgresStore(pg_connect_factory(dsn))
    store.ensure()
    Service(store).forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
