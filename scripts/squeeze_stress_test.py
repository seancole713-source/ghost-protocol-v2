#!/usr/bin/env python3
"""Stress-test squeeze monitor: fetch all watchlist symbols, report coverage + candidates."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.symbols import get_edge_set
from core.market_hours import is_us_rth, market_session_label
from core.squeeze_monitor import (
    compute_rvol,
    evaluate_squeeze_signal,
    format_squeeze_alert,
    rth_elapsed_fraction,
    _short_context,
    _sync_fetch_metrics,
)


def main() -> int:
    symbols = sorted(get_edge_set())
    elapsed = rth_elapsed_fraction()
    print(f"session={market_session_label()} rth={is_us_rth()} symbols={len(symbols)}")
    print(f"elapsed_frac={elapsed:.3f} alpaca={'yes' if os.getenv('ALPACA_KEY_ID') else 'no'}")
    print("-" * 72)

    ok = fail = candidates = 0
    t0 = time.time()
    rows = []

    for sym in symbols:
        t1 = time.time()
        metrics = _sync_fetch_metrics(sym)
        ms = (time.time() - t1) * 1000
        if not metrics:
            fail += 1
            rows.append((sym, "FAIL", ms, None, None, None))
            continue
        ok += 1
        short_ctx = _short_context(sym)
        rvol = compute_rvol(metrics["session_volume"], metrics["avg_daily_volume"], elapsed)
        kind = evaluate_squeeze_signal(
            metrics["peak_move_pct"],
            metrics["current_move_pct"],
            rvol,
            short_risk=short_ctx.get("squeeze_risk"),
        )
        if kind:
            candidates += 1
            alert = format_squeeze_alert(sym, kind, metrics, rvol, short_ctx)
            rows.append((sym, kind, ms, metrics["peak_move_pct"], rvol, alert.split("\n")[0]))
        else:
            rows.append((sym, "—", ms, metrics["peak_move_pct"], rvol, None))

    total_s = time.time() - t0
    print(f"fetch_ok={ok} fetch_fail={fail} candidates={candidates} total_time={total_s:.1f}s")
    print(f"avg_ms={(total_s/len(symbols)*1000):.0f}  est_cycle_sec={total_s:.0f} (interval=60)")
    print("-" * 72)
    for sym, status, ms, peak, rvol, extra in sorted(rows, key=lambda r: (r[1] == "—", - (r[3] or 0))):
        line = f"{sym:6} {status:16} peak={peak if peak is not None else 0:+.1f}% rvol={(rvol or 0):.2f}x {ms:.0f}ms"
        if extra:
            line += f" | {extra}"
        print(line)
    return 1 if fail > len(symbols) // 2 else 0


if __name__ == "__main__":
    raise SystemExit(main())
