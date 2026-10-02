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
import re
import time
from typing import Any, Dict, List, Optional, Sequence

from core.watcher import (
    contract_win_test_status,
    independent_symbol_session_rows,
    session_block_bootstrap_interval,
)

_REGISTRY_KEY = "contract_70_forward_registry"
# Append-only audit of every registration attempt (registered, idempotent
# retry, refused change, superseded record). Rows are only ever INSERTed.
_HISTORY_TABLE = "ghost_contract_70_registry_history"
_DEFAULT_VERSION_ID = "v1"
_LEGACY_VERSION_ID = "legacy_unversioned"
_VERSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class Contract70RegistrationRefused(Exception):
    """A registration would change the frozen experiment without a new version.

    ``details`` is JSON-safe and names the existing version and its
    registration timestamp so the caller can report why it was refused.
    """

    def __init__(self, reason: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


def _server_now() -> int:
    """Registration timestamps are created here only, never taken from callers."""
    return int(time.time())


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
    forward_rows: List[Dict[str, Any]] = []
    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        if sym not in reg:
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
        forward_rows.append(r)
    independent, pseudo_replicates = independent_symbol_session_rows(forward_rows)
    eligible: List[Dict[str, Any]] = []
    for r in independent:
        try:
            p = float(r.get("up_prob"))
        except Exception:
            continue
        if p >= float(prob_floor):
            eligible.append(r)
    n = 0
    wins = 0
    used_symbols: Dict[str, Dict[str, int]] = {}
    for r in eligible:
        sym = str(r.get("symbol") or "").upper()
        outcome = str(r.get("outcome") or "").upper()
        n += 1
        g = used_symbols.setdefault(sym, {"n": 0, "wins": 0})
        g["n"] += 1
        if outcome == "WIN":
            wins += 1
            g["wins"] += 1
    status = contract_win_test_status(wins=wins, n=n, target=target)
    block_rows = [dict(row, win=str(row.get("outcome") or "").upper() == "WIN")
                  for row in eligible]
    block = session_block_bootstrap_interval(block_rows)
    status["session_block_bootstrap"] = block
    status["proof_pass"] = bool(
        status["wilson_pass"] and block and float(block["low"]) >= float(target)
    )
    status["basis"] = "forward_only_registered_universe"
    status["registered_symbols"] = sorted(reg)
    status["registered_at_ts"] = cutoff
    status["prob_floor"] = float(prob_floor)
    status["raw_forward_rows"] = len(forward_rows)
    status["independent_forward_rows"] = len(independent)
    status["eligible_rows"] = len(eligible)
    status["pseudo_replicates_excluded"] = pseudo_replicates
    status["sampling_unit"] = "earliest_prediction_per_symbol_market_session"
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
    forward_rows: List[Dict[str, Any]] = []
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
        forward_rows.append(r)
    independent, pseudo_replicates = independent_symbol_session_rows(forward_rows)
    eligible: List[Dict[str, Any]] = []
    for r in independent:
        matched_spec = None
        for spec in specs:
            if row_matches_slice(r, spec):
                matched_spec = spec
                break
        if matched_spec is None:
            continue
        item = dict(r)
        item["_matched_slice_spec"] = matched_spec
        eligible.append(item)
    n = 0
    wins = 0
    used: Dict[str, Dict[str, Any]] = {}
    for r in eligible:
        outcome = str(r.get("outcome") or "").upper()
        matched_spec = r["_matched_slice_spec"]
        n += 1
        key = json.dumps({"dims": matched_spec.get("dims") or [], "key": matched_spec.get("key") or {}}, sort_keys=True)
        g = used.setdefault(key, {"slice": {"dims": matched_spec.get("dims") or [], "key": matched_spec.get("key") or {}}, "n": 0, "wins": 0})
        g["n"] += 1
        if outcome == "WIN":
            wins += 1
            g["wins"] += 1
    status = contract_win_test_status(wins=wins, n=n, target=target)
    block_rows = [dict(row, win=str(row.get("outcome") or "").upper() == "WIN")
                  for row in eligible]
    block = session_block_bootstrap_interval(block_rows)
    status["session_block_bootstrap"] = block
    status["proof_pass"] = bool(
        status["wilson_pass"] and block and float(block["low"]) >= float(target)
    )
    status["basis"] = "forward_only_registered_slices"
    status["registered_slices"] = [{"dims": s.get("dims") or [], "key": s.get("key") or {}} for s in specs]
    status["registered_at_ts"] = cutoff
    status["raw_forward_rows"] = len(forward_rows)
    status["independent_forward_rows"] = len(independent)
    status["eligible_rows"] = len(eligible)
    status["pseudo_replicates_excluded"] = pseudo_replicates
    status["sampling_unit"] = "earliest_prediction_per_symbol_market_session"
    status["slices_used"] = [v for _, v in sorted(used.items())]
    return status


def register_universe(
    symbols: Sequence[str],
    *,
    min_n: int,
    min_wilson_low: float,
    version_id: Optional[str] = None,
    cur=None,
) -> Dict[str, Any]:
    """Freeze the candidate universe in ghost_state (write-once per version).

    * An identical re-registration is idempotent: the EXISTING record (with
      its original ``registered_at_ts``) is returned and the forward window is
      never reset, no matter how the forward outcomes have gone since.
    * A changed universe or threshold is refused
      (:class:`Contract70RegistrationRefused`) unless an explicit, previously
      unused ``version_id`` is given; then the previous record is preserved as
      ``superseded`` in the append-only history table.
    * ``registered_at_ts`` is always created server-side.

    The returned dict is the active record plus a non-persisted
    ``registration_status`` (``registered`` | ``already_registered`` |
    ``registered_new_version``).
    """
    payload = {
        "symbols": sorted({str(s).upper() for s in (symbols or [])}),
        "min_n": int(min_n),
        "min_wilson_low": float(min_wilson_low),
        "prob_floor": 0.70,
        "target": 0.70,
    }
    return _register(payload, version_id=version_id, cur=cur)


def register_slices(
    slices: Sequence[Dict[str, Any]],
    *,
    min_n: int,
    min_wilson_low: float,
    version_id: Optional[str] = None,
    cur=None,
) -> Dict[str, Any]:
    """Persist frozen slice definitions in ghost_state for forward proof.

    Same write-once rules as :func:`register_universe`: identical retries
    return the existing record, a changed slice set or threshold needs an
    explicit new ``version_id`` (the old record is kept as superseded), and
    the timestamp is server-created.

    This is the slice-aware counterpart to ``register_universe``. It preserves
    the anti-look-ahead contract by recording the exact slice dimensions and the
    registration timestamp; future evaluation counts only rows that match those
    frozen dimensions and resolve after this timestamp. It writes only
    ``ghost_state`` and never changes model, gate, wallet, or broker state.
    """
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
        # Preserve the evidence that justified registration so a future audit can
        # prove WHY this exact slice was frozen, even after data/code changes.
        # Only copy scalar/statistical fields; never copy raw rows or secrets.
        evidence_fields = (
            "n", "wins", "win_rate", "wilson_low", "wilson_high", "raw_pass", "wilson_pass",
            "family_wilson_low", "family_size", "family_z", "family_confidence",
            "multiple_comparisons_correction",
        )
        evidence = {k: item.get(k) for k in evidence_fields if k in item}
        if evidence:
            spec["selection_evidence"] = evidence
        sig = json.dumps({"dims": spec["dims"], "key": spec["key"]}, sort_keys=True)
        if sig in seen:
            continue
        seen.add(sig)
        clean.append(spec)
    payload = {
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
    return _register(payload, version_id=version_id, cur=cur)


def _definition(record: Dict[str, Any]) -> Dict[str, Any]:
    """The frozen experiment: what is scored and against which thresholds.

    Selection evidence, timestamps and version metadata are provenance, not
    definition, so a retry with refreshed evidence is still "identical".
    """
    mode = "slices" if record.get("mode") == "slices" else "universe"
    target = record.get("target")
    out: Dict[str, Any] = {
        "mode": mode,
        "min_n": int(record.get("min_n") or 0),
        "min_wilson_low": round(float(record.get("min_wilson_low") or 0.0), 6),
        "target": round(float(0.70 if target is None else target), 6),
    }
    if mode == "slices":
        out["slices"] = sorted(
            json.dumps({"dims": list(s.get("dims") or []), "key": dict(s.get("key") or {})},
                       sort_keys=True)
            for s in (record.get("slices") or []) if isinstance(s, dict)
        )
    else:
        floor = record.get("prob_floor")
        out["symbols"] = sorted({str(x).upper() for x in (record.get("symbols") or [])})
        out["prob_floor"] = round(float(0.70 if floor is None else floor), 6)
    return out


def _version_of(record: Dict[str, Any]) -> str:
    return str(record.get("version_id") or _LEGACY_VERSION_ID)


def _ensure_history(c) -> None:
    c.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_HISTORY_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            event VARCHAR(40) NOT NULL,
            version_id VARCHAR(64),
            record TEXT NOT NULL,
            created_at_ts BIGINT NOT NULL
        )
        """
    )


def _log_attempt(c, event: str, version_id: Optional[str], record: Dict[str, Any], ts: int) -> None:
    c.execute(
        f"INSERT INTO {_HISTORY_TABLE} (event, version_id, record, created_at_ts) "
        "VALUES (%s,%s,%s,%s)",
        (event, version_id, json.dumps(record, sort_keys=True), int(ts)),
    )


def _read_current(c):
    c.execute("SELECT val FROM ghost_state WHERE key=%s FOR UPDATE", (_REGISTRY_KEY,))
    row = c.fetchone()
    if not (row and row[0]):
        return None, None
    try:
        return json.loads(row[0]), row[0]
    except Exception:
        return None, row[0]


def _register(payload: Dict[str, Any], *, version_id: Optional[str], cur=None) -> Dict[str, Any]:
    requested = None if version_id is None else str(version_id).strip()
    if requested is not None and not _VERSION_ID_RE.match(requested):
        raise ValueError("version_id must be 1-64 chars of [A-Za-z0-9._-]")
    from core.db import db_conn, ensure_ghost_state

    def _impl(c) -> Dict[str, Any]:
        ensure_ghost_state(c)
        _ensure_history(c)
        # Serialize registrations (also covers the no-row-yet case a row lock
        # cannot); the writes below are compare-and-swap regardless.
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_REGISTRY_KEY,))
        now = _server_now()
        attempt = dict(payload)
        for _ in range(3):
            current, raw = _read_current(c)
            if raw is not None and current is None:
                _log_attempt(c, "refused_unreadable_record", requested, attempt, now)
                return {"refused": "existing registry record is unreadable; refusing to overwrite",
                        "details": {}}
            if current is None:
                record = dict(attempt, registered_at_ts=now,
                              version_id=requested or _DEFAULT_VERSION_ID)
                c.execute(
                    "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
                    "ON CONFLICT(key) DO NOTHING",
                    (_REGISTRY_KEY, json.dumps(record)),
                )
                if c.rowcount == 1:
                    _log_attempt(c, "registered", record["version_id"], record, now)
                    return {"record": record, "status": "registered"}
                continue  # lost a race: re-read the winner and compare
            if _definition(current) == _definition(attempt):
                _log_attempt(c, "idempotent_retry", requested, attempt, now)
                return {"record": current, "status": "already_registered"}
            details = {
                "existing_version_id": _version_of(current),
                "existing_registered_at_ts": current.get("registered_at_ts"),
                "requested_version_id": requested,
            }
            if requested is None:
                _log_attempt(c, "refused_changed_definition", None, attempt, now)
                return {"refused": "registration differs from the frozen experiment; "
                                   "an explicit new version_id is required",
                        "details": details}
            c.execute(
                f"SELECT 1 FROM {_HISTORY_TABLE} WHERE version_id=%s "
                "AND event IN ('registered','superseded') LIMIT 1",
                (requested,),
            )
            if requested == _version_of(current) or c.fetchone():
                _log_attempt(c, "refused_reused_version", requested, attempt, now)
                return {"refused": "version_id was already used; pick a new one",
                        "details": details}
            record = dict(attempt, registered_at_ts=now, version_id=requested,
                          supersedes={"version_id": _version_of(current),
                                      "registered_at_ts": current.get("registered_at_ts")})
            c.execute(
                "UPDATE ghost_state SET val=%s WHERE key=%s AND val=%s",
                (json.dumps(record), _REGISTRY_KEY, raw),
            )
            if c.rowcount != 1:
                continue  # concurrently changed: re-read and decide again
            _log_attempt(c, "superseded", _version_of(current),
                         dict(current, superseded_by=requested, superseded_at_ts=now), now)
            _log_attempt(c, "registered", requested, record, now)
            return {"record": record, "status": "registered_new_version"}
        _log_attempt(c, "refused_concurrent_change", requested, attempt, now)
        return {"refused": "registry changed concurrently; retry", "details": {}}

    if cur is not None:
        result = _impl(cur)
    else:
        with db_conn() as conn:
            result = _impl(conn.cursor())
            conn.commit()
    if "refused" in result:
        # Raised after the attempt was logged (and, for an owned connection,
        # committed) so refused attempts stay in the audit history.
        raise Contract70RegistrationRefused(result["refused"], result.get("details"))
    out = dict(result["record"])
    out["registration_status"] = result["status"]
    return out


def registry_history(limit: int = 50, cur=None) -> List[Dict[str, Any]]:
    """Every registration attempt, newest first (append-only audit)."""
    from core.db import db_conn

    def _read(c) -> List[Dict[str, Any]]:
        _ensure_history(c)
        c.execute(
            f"SELECT id, event, version_id, record, created_at_ts FROM {_HISTORY_TABLE} "
            "ORDER BY id DESC LIMIT %s",
            (max(1, min(500, int(limit))),),
        )
        out = []
        for row in c.fetchall() or []:
            try:
                rec = json.loads(row[3])
            except Exception:
                rec = None
            out.append({"id": row[0], "event": row[1], "version_id": row[2],
                        "record": rec, "created_at_ts": row[4]})
        return out

    if cur is not None:
        return _read(cur)
    with db_conn() as conn:
        return _read(conn.cursor())


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
