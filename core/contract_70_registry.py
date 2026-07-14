"""core/contract_70_registry.py — forward-only proof harness for the 70+ contract.

The 70+ win test is only meaningful if it is proven WITHOUT look-ahead bias. It
is trivial (and dishonest) to pick the symbols that already won and call the
pooled result 70+. This module enforces the correct protocol:

  1. SELECT a candidate universe from PAST evidence (symbols whose own 70+
     confidence bucket individually clears a Wilson-proven bar).
  2. FREEZE that universe with a registration timestamp (register_universe).
  3. EVALUATE only outcomes that resolve AFTER the registration timestamp
     (evaluate_forward) — selection uses the past, scoring uses only the future.

Nothing here fires trades, loosens a gate, or writes model/broker state. It only
reads resolved shadow outcomes and persists a small pre-registration record in
ghost_state so the forward proof cannot be back-dated. Success is reported by
the pooled forward Wilson lower bound clearing the target — never by raw
in-sample selection.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence

from core.watcher import contract_win_test_status, wilson_interval

_REGISTRY_KEY = "contract_70_forward_registry"


def select_candidate_universe(
    symbol_breakdown: Sequence[Dict[str, Any]],
    *,
    min_n: int = 8,
    min_wilson_low: float = 0.70,
) -> List[str]:
    """Pick symbols whose OWN 70+ bucket is individually Wilson-proven.

    ``symbol_breakdown`` is the per-symbol 70+ stats shape produced by
    :func:`core.watcher.contract_70_symbol_breakdown` (fields: symbol, n, wins,
    wilson_low). A symbol qualifies only when it has enough resolved 70+ samples
    AND its Wilson lower bound already clears the bar — i.e. it is not a lucky
    small sample. Pure/testable; selection is on PAST data by design.
    """
    picked: List[str] = []
    for row in symbol_breakdown:
        try:
            n = int(row.get("n") or 0)
            wl = row.get("wilson_low")
            wl = float(wl) if wl is not None else None
        except Exception:
            continue
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        if n >= int(min_n) and wl is not None and wl >= float(min_wilson_low):
            picked.append(sym)
    return sorted(set(picked))


def evaluate_forward(
    rows: Sequence[Dict[str, Any]],
    *,
    registered_symbols: Sequence[str],
    registered_at_ts: int,
    prob_floor: float = 0.70,
    target: float = 0.70,
) -> Dict[str, Any]:
    """Pooled forward-only 70+ status over the registered universe.

    Only rows that (a) belong to a registered symbol, (b) carry up_prob >=
    ``prob_floor``, (c) resolved AFTER ``registered_at_ts`` (eval_ts strictly
    greater), and (d) have a WIN/LOSS outcome are counted. Everything else is
    ignored so the score cannot include the selection window. Returns the same
    contract-status shape as the live 70+ readout, plus provenance.
    """
    reg = {str(s).upper() for s in (registered_symbols or [])}
    cutoff = int(registered_at_ts or 0)
    n = 0
    wins = 0
    used_symbols: Dict[str, Dict[str, int]] = {}
    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        if sym not in reg:
            continue
        try:
            p = float(r.get("up_prob"))
        except Exception:
            continue
        if p < float(prob_floor):
            continue
        try:
            ets = int(r.get("eval_ts") or 0)
        except Exception:
            ets = 0
        if ets <= cutoff:
            continue  # forward-only: skip anything from the selection window
        outcome = str(r.get("outcome") or "").upper()
        if outcome not in ("WIN", "LOSS", "EXPIRED"):
            continue
        n += 1
        g = used_symbols.setdefault(sym, {"n": 0, "wins": 0})
        g["n"] += 1
        if outcome == "WIN":
            wins += 1
            g["wins"] += 1
    status = contract_win_test_status(wins=wins, n=n, target=target)
    status["basis"] = "forward_only_registered_universe"
    status["registered_symbols"] = sorted(reg)
    status["registered_at_ts"] = cutoff
    status["prob_floor"] = float(prob_floor)
    status["symbols_used"] = [
        {"symbol": s, "n": g["n"], "wins": g["wins"]}
        for s, g in sorted(used_symbols.items())
    ]
    return status


def evaluate_forward_slices(
    rows: Sequence[Dict[str, Any]],
    *,
    registered_slices: Sequence[Dict[str, Any]],
    registered_at_ts: int,
    target: float = 0.70,
) -> Dict[str, Any]:
    """Pooled forward-only 70+ status over frozen slice definitions.

    Unlike the legacy symbol-universe evaluator, this counts rows that match a
    frozen slice spec (for example ``symbol=BILL`` AND ``regime=Trend-down``)
    and resolved strictly after registration. It never widens missing fields:
    if a future row lacks the slice dimension, it is ignored rather than counted.
    """
    from core.contract_70_slices import row_matches_slice

    cutoff = int(registered_at_ts or 0)
    specs = [s for s in (registered_slices or []) if isinstance(s, dict)]
    n = 0
    wins = 0
    used: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        try:
            ets = int(r.get("eval_ts") or 0)
        except Exception:
            ets = 0
        if ets <= cutoff:
            continue
        outcome = str(r.get("outcome") or "").upper()
        if outcome not in ("WIN", "LOSS", "EXPIRED"):
            continue
        matched_spec = None
        for spec in specs:
            if row_matches_slice(r, spec):
                matched_spec = spec
                break
        if matched_spec is None:
            continue
        n += 1
        key = json.dumps({"dims": matched_spec.get("dims") or [], "key": matched_spec.get("key") or {}}, sort_keys=True)
        g = used.setdefault(key, {"slice": {"dims": matched_spec.get("dims") or [], "key": matched_spec.get("key") or {}}, "n": 0, "wins": 0})
        g["n"] += 1
        if outcome == "WIN":
            wins += 1
            g["wins"] += 1
    status = contract_win_test_status(wins=wins, n=n, target=target)
    status["basis"] = "forward_only_registered_slices"
    status["registered_slices"] = [{"dims": s.get("dims") or [], "key": s.get("key") or {}} for s in specs]
    status["registered_at_ts"] = cutoff
    status["slices_used"] = [v for _, v in sorted(used.items())]
    return status


def register_universe(
    symbols: Sequence[str],
    *,
    min_n: int,
    min_wilson_low: float,
    now_ts: Optional[int] = None,
    cur=None,
) -> Dict[str, Any]:
    """Persist (or refresh) the frozen candidate universe in ghost_state.

    Idempotent-ish: re-registering overwrites the record and resets the forward
    window to the new timestamp. Kept deliberately explicit so a human/cron
    decides WHEN to freeze; this module never auto-registers on read.
    """
    ts = int(now_ts if now_ts is not None else time.time())
    payload = {
        "registered_at_ts": ts,
        "symbols": sorted({str(s).upper() for s in (symbols or [])}),
        "min_n": int(min_n),
        "min_wilson_low": float(min_wilson_low),
        "prob_floor": 0.70,
        "target": 0.70,
    }
    from core.db import db_conn, ensure_ghost_state

    def _write(c):
        ensure_ghost_state(c)
        c.execute(
            "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
            (_REGISTRY_KEY, json.dumps(payload)),
        )

    if cur is not None:
        _write(cur)
    else:
        with db_conn() as conn:
            _write(conn.cursor())
            conn.commit()
    return payload


def register_slices(
    slices: Sequence[Dict[str, Any]],
    *,
    min_n: int,
    min_wilson_low: float,
    now_ts: Optional[int] = None,
    cur=None,
) -> Dict[str, Any]:
    """Persist frozen slice definitions in ghost_state for forward proof.

    This is the slice-aware counterpart to ``register_universe``. It preserves
    the anti-look-ahead contract by recording the exact slice dimensions and the
    registration timestamp; future evaluation counts only rows that match those
    frozen dimensions and resolve after this timestamp. It writes only
    ``ghost_state`` and never changes model, gate, wallet, or broker state.
    """
    ts = int(now_ts if now_ts is not None else time.time())
    clean: List[Dict[str, Any]] = []
    seen = set()
    for item in slices or []:
        if not isinstance(item, dict):
            continue
        dims = [str(d) for d in (item.get("dims") or []) if str(d)]
        key_in = item.get("key") or {}
        if not dims or not isinstance(key_in, dict):
            continue
        key = {str(k): v for k, v in key_in.items() if str(k) in dims}
        # Do not widen incomplete slice specs. A frozen slice must provide a
        # value for every dimension it asks future rows to match.
        if set(key.keys()) != set(dims):
            continue
        spec = {"dims": dims, "key": key}
        sig = json.dumps(spec, sort_keys=True)
        if sig in seen:
            continue
        seen.add(sig)
        clean.append(spec)
    payload = {
        "registered_at_ts": ts,
        "mode": "slices",
        "slices": clean,
        # Convenience only: legacy UIs can still show the symbol subset, but
        # forward scoring uses the exact frozen slice specs above.
        "symbols": sorted({str((s.get("key") or {}).get("symbol")).upper()
                           for s in clean if (s.get("key") or {}).get("symbol")}),
        "min_n": int(min_n),
        "min_wilson_low": float(min_wilson_low),
        "target": 0.70,
    }
    from core.db import db_conn, ensure_ghost_state

    def _write(c):
        ensure_ghost_state(c)
        c.execute(
            "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
            (_REGISTRY_KEY, json.dumps(payload)),
        )

    if cur is not None:
        _write(cur)
    else:
        with db_conn() as conn:
            _write(conn.cursor())
            conn.commit()
    return payload


def load_registry(cur=None) -> Optional[Dict[str, Any]]:
    """Read the frozen universe record, or None if never registered."""
    from core.db import db_conn, ensure_ghost_state

    def _read(c) -> Optional[Dict[str, Any]]:
        ensure_ghost_state(c)
        c.execute("SELECT val FROM ghost_state WHERE key=%s", (_REGISTRY_KEY,))
        row = c.fetchone()
        if not (row and row[0]):
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    if cur is not None:
        return _read(cur)
    with db_conn() as conn:
        return _read(conn.cursor())
