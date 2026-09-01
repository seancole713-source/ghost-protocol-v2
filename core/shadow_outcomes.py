"""Shadow scoring — resolve every silenced model evaluation against real prices.

Ghost evaluates the full watchlist every scan cycle but historically only
learned from the rare pick that cleared the gates. This module turns the
silenced evaluations (ghost_perf_symbol_evals) into virtual picks: one per
symbol per CT trading day, resolved with the exact same TP/SL bar-path rules
as live picks (core.tp_sl_resolve + core.vol_targets). The result is a
per-symbol live hit-rate scoreboard that accrues ~44 resolved virtual trades
per trading day without risking a dollar.

Shadow rows live in their own table (ghost_shadow_outcomes) so perf-log
pruning never erases the scoreboard history.
"""
from __future__ import annotations
from core.quiet import note_suppressed

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from core.db import ensure_ghost_state

LOGGER = logging.getLogger("ghost.shadow")

# Probability floor used for scoreboard bucketing (matches the live v3 BUY
# floor on current models). Bucket edges only — resolution does not gate.
PROB_FLOOR = 0.55


def shadow_enabled() -> bool:
    return (os.getenv("GHOST_SHADOW_SCORING", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def ensure_shadow_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_shadow_outcomes (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            eval_ts BIGINT NOT NULL,
            up_prob FLOAT,
            confidence FLOAT,
            skip_code TEXT,
            fired BOOLEAN NOT NULL DEFAULT FALSE,
            entry_price FLOAT NOT NULL,
            target_price FLOAT NOT NULL,
            stop_price FLOAT NOT NULL,
            expires_at BIGINT NOT NULL,
            outcome TEXT,
            exit_price FLOAT,
            pnl_pct FLOAT,
            resolved_at BIGINT,
            created_at BIGINT NOT NULL,
            UNIQUE (symbol, trade_date)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_pending "
        "ON ghost_shadow_outcomes (symbol) WHERE outcome IS NULL"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_shadow_eval_ts "
        "ON ghost_shadow_outcomes (eval_ts DESC)"
    )
    # Additive migration: persist the market regime AT ISSUANCE on the outcome
    # row itself. The 70+ slice search conditions the win test on regime, and
    # ghost_perf_symbol_evals (the join source) is pruned after ~90 days
    # (GHOST_PERF_RETENTION_DAYS) while shadow outcomes are not — without a
    # durable column the conditioning signal would decay out from under a
    # forward proof. CREATE TABLE IF NOT EXISTS never adds columns to an
    # existing prod table, so this ALTER is required.
    cur.execute(
        "ALTER TABLE ghost_shadow_outcomes ADD COLUMN IF NOT EXISTS regime_label TEXT"
    )
    # Additive migration: persist Ghost's own regime-GATE flags at issuance so
    # the 70+ slice search can condition on the exact binary market conditions
    # Ghost gates on (adx_trending / above_ema200 / ema_trend_bullish). These
    # are the most principled discriminators: "in which market setups does
    # Ghost's TP/SL actually clear 70%?" Stored as SMALLINT 0/1 (NULL = unknown
    # for older rows). Durable for the same reason regime_label is — the
    # perf-eval join source is pruned after ~90 days while outcomes are not.
    for _col in ("adx_trending", "above_ema200", "ema_trend_bullish"):
        cur.execute(
            f"ALTER TABLE ghost_shadow_outcomes ADD COLUMN IF NOT EXISTS {_col} SMALLINT"
        )
    # Exact evidence identity. Legacy NULL rows remain observable/resolvable but
    # cannot prove the current direction/model generation.
    for _col, _type in (
        ("direction", "TEXT"), ("model_prob", "FLOAT"),
        ("prob_model_raw", "FLOAT"), ("prob_train_calibrated", "FLOAT"),
        ("prob_live_recalibrated", "FLOAT"), ("confidence_final", "FLOAT"),
        ("model_sha256", "TEXT"), ("feature_schema", "TEXT"),
        ("label_schema", "TEXT"), ("validation_schema", "TEXT"),
        ("hold_bars", "INT"),
    ):
        cur.execute(
            f"ALTER TABLE ghost_shadow_outcomes ADD COLUMN IF NOT EXISTS {_col} {_type}"
        )
    # The original symbol/day constraint allowed an obsolete same-day model to
    # block evidence for its replacement. Preserve one observation per exact
    # direction/model/schema/horizon generation instead.
    cur.execute(
        "ALTER TABLE ghost_shadow_outcomes "
        "DROP CONSTRAINT IF EXISTS ghost_shadow_outcomes_symbol_trade_date_key"
    )
    cur.execute(
        "DROP INDEX IF EXISTS idx_shadow_generation_daily"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_generation_daily_v2 "
        "ON ghost_shadow_outcomes "
        "(symbol, trade_date, direction, model_sha256, feature_schema, "
        " label_schema, validation_schema, hold_bars) "
        "WHERE direction IS NOT NULL AND model_sha256 IS NOT NULL "
        "AND feature_schema IS NOT NULL AND label_schema IS NOT NULL "
        "AND validation_schema IS NOT NULL AND hold_bars IS NOT NULL"
    )


def _ct_date(ts: int) -> str:
    try:
        import pytz

        tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
        return datetime.fromtimestamp(int(ts), tz).strftime("%Y-%m-%d")
    except Exception:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")


def _is_ct_weekday(ts: int) -> bool:
    try:
        return datetime.fromisoformat(_ct_date(int(ts))).weekday() < 5
    except (TypeError, ValueError, OverflowError, OSError):
        return False


def pick_daily_first(evals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Earliest post-close eval per exact symbol/model generation.

    Intraday repeats from one generation are pseudo-replicates, but replacing a
    model during the post-close window creates a new evidence population that
    remains visible. Premarket/intraday rows are excluded because the daily
    model was trained on completed close bars, not partial-session snapshots.
    """
    chosen: Dict[tuple, Dict[str, Any]] = {}
    for ev in evals:
        ts = ev.get("eval_ts")
        sym = str(ev.get("symbol") or "").upper()
        if not sym or not ts:
            continue
        if not _is_ct_weekday(int(ts)):
            continue
        try:
            from core.market_hours import in_daily_model_issuance_window
            import pytz

            zone = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
            issued_at = datetime.fromtimestamp(int(ts), zone)
        except Exception:
            continue
        if not in_daily_model_issuance_window(issued_at):
            continue
        identity = _shadow_identity(ev)
        generation = (
            identity.get("direction"), identity.get("model_sha256"),
            identity.get("feature_schema"), identity.get("label_schema"),
            identity.get("validation_schema"), identity.get("hold_bars"),
        ) if identity else (None, None, None, None, None, None)
        key = (sym, _ct_date(int(ts)), *generation)
        prev = chosen.get(key)
        if prev is None or int(ts) < int(prev["eval_ts"]):
            chosen[key] = ev
    # Deterministic order without comparing legacy None identity fields against
    # current string fields. Concurrent seeders therefore keep lock order while
    # mixed migration populations cannot crash the whole cycle.
    return sorted(
        chosen.values(),
        key=lambda ev: (
            str(ev.get("symbol") or "").upper(),
            _ct_date(int(ev.get("eval_ts") or 0)),
            str((_shadow_identity(ev) or {}).get("direction") or ""),
            str((_shadow_identity(ev) or {}).get("model_sha256") or ""),
            int(ev.get("eval_ts") or 0),
        ),
    )


def _eval_entry_price(ev: Dict[str, Any]) -> Optional[float]:
    """Entry for the virtual trade: real entry when fired, else scan price."""
    try:
        p = float(ev.get("entry_price") or 0)
        if p > 0:
            return p
    except Exception:
        note_suppressed()
    scores = ev.get("scores")
    if isinstance(scores, str):
        try:
            scores = json.loads(scores)
        except Exception:
            scores = None
    if isinstance(scores, dict):
        try:
            p = float(scores.get("price") or 0)
            if p > 0:
                return p
        except Exception:
            note_suppressed()
    return None


# Advisory-lock key for the shadow seeder — arbitrary constant, unique app-wide.
_SEED_ADVISORY_LOCK_KEY = 749_301_552


def _score_dict(ev: Dict[str, Any]) -> Dict[str, Any]:
    scores = ev.get("scores")
    if isinstance(scores, str):
        try:
            scores = json.loads(scores)
        except Exception:
            return {}
    return scores if isinstance(scores, dict) else {}


def _shadow_identity(ev: Dict[str, Any]) -> Dict[str, Any]:
    scores = _score_dict(ev)
    direction = str(ev.get("direction") or scores.get("winning_direction") or "").upper()
    if direction not in ("UP", "DOWN"):
        return {}
    identities = scores.get("model_identity_by_direction")
    identity = identities.get(direction) if isinstance(identities, dict) else None
    if not isinstance(identity, dict):
        return {}
    prob_key = "up_prob" if direction == "UP" else "down_prob"
    try:
        prob = float(scores.get(prob_key))
    except (TypeError, ValueError, OverflowError):
        return {}
    if not 0.0 <= prob <= 1.0:
        return {}
    model_sha256 = identity.get("model_sha256")
    feature_schema = identity.get("feature_schema")
    label_schema = identity.get("label_schema")
    validation_schema = identity.get("validation_schema")
    if not all(isinstance(value, str) and value for value in (
        model_sha256, feature_schema, label_schema, validation_schema,
    )):
        return {}
    hold_value = identity.get("hold_bars")
    if isinstance(hold_value, bool):
        return {}
    try:
        hold_numeric = float(hold_value)
    except (TypeError, ValueError, OverflowError):
        return {}
    if not hold_numeric.is_integer() or hold_numeric < 1:
        return {}
    hold_bars = int(hold_numeric)
    return {
        "direction": direction, "model_prob": prob,
        "model_sha256": model_sha256,
        "feature_schema": feature_schema,
        "label_schema": label_schema,
        "validation_schema": validation_schema,
        "hold_bars": hold_bars,
    }


def _regime_flag(ev: Dict[str, Any], key: str) -> Optional[int]:
    """Extract a binary regime-gate flag (0/1) from an eval's scores.regime.

    ``scores`` may arrive as a dict or a JSON string (psycopg variance). Returns
    None when the flag is absent so it is stored as NULL rather than a guessed 0.
    """
    scores = ev.get("scores")
    if isinstance(scores, str):
        try:
            scores = json.loads(scores)
        except Exception:
            return None
    if not isinstance(scores, dict):
        return None
    regime = scores.get("regime")
    if not isinstance(regime, dict) or key not in regime:
        return None
    try:
        return 1 if int(regime.get(key)) else 0
    except Exception:
        return None


def seed_shadow_rows(days_back: int = 3) -> int:
    """Insert pending shadow rows from recent symbol evals (idempotent)."""
    from core.db import db_conn

    cutoff = int(time.time()) - max(1, int(days_back)) * 86400
    with db_conn() as conn:
        cur = conn.cursor()
        # Single-seeder guard: the hourly job and the market-scan seed can run
        # concurrently; seeding is idempotent, so if another transaction holds
        # the lock just skip — the next run catches anything missed. This (plus
        # deterministic insert order in pick_daily_first) removes the
        # "deadlock detected" failures seen in production.
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_SEED_ADVISORY_LOCK_KEY,))
        row = cur.fetchone()
        if not (row and row[0]):
            LOGGER.debug("shadow seed: another seeder holds the lock, skipping")
            return 0
        ensure_shadow_table(cur)
        try:
            # Evidence for BOTH lanes: a DOWN-only evaluation has no up_prob
            # yet is valid forward proof. Identity/priceability are enforced
            # per-row below, so the read must not silently drop DOWN rows.
            cur.execute(
                "SELECT symbol, eval_ts, up_prob, confidence, skip_code, fired, "
                "entry_price, target_price, stop_price, scores, regime_label, direction, "
                "prob_model_raw, prob_train_calibrated, prob_live_recalibrated, confidence_final "
                "FROM ghost_perf_symbol_evals "
                "WHERE eval_ts >= %s",
                (cutoff,),
            )
            rows = cur.fetchall()
        except Exception as e:
            LOGGER.debug("shadow seed: eval read failed: %s", str(e)[:80])
            return 0

        evals = [
            {
                "symbol": r[0], "eval_ts": r[1], "up_prob": r[2], "confidence": r[3],
                "skip_code": r[4], "fired": bool(r[5]), "entry_price": r[6],
                "target_price": r[7], "stop_price": r[8], "scores": r[9],
                "regime_label": r[10], "direction": r[11],
                "prob_model_raw": r[12], "prob_train_calibrated": r[13],
                "prob_live_recalibrated": r[14], "confidence_final": r[15],
            }
            for r in rows
        ]
        from core.tp_sl_resolve import expires_at_nth_trading_close, label_hold_bars, tp_sl_prices_from_vol
        from core.vol_targets import base_vol_pct

        hold = label_hold_bars()
        now = int(time.time())
        inserted = 0
        new_rows: List[Dict[str, Any]] = []
        # Drop evals without a resolvable entry first (rows logged before the
        # scan price was captured) so they can't shadow out priced ones as the
        # "earliest of the day".
        priced = [ev for ev in evals if _eval_entry_price(ev) is not None]
        for ev in pick_daily_first(priced):
            entry = _eval_entry_price(ev)
            if entry is None:
                continue
            sym = str(ev["symbol"]).upper()
            identity = _shadow_identity(ev)
            direction = identity.get("direction")
            if direction not in ("UP", "DOWN"):
                continue
            if ev.get("fired") and ev.get("target_price") and ev.get("stop_price"):
                target, stop = float(ev["target_price"]), float(ev["stop_price"])
            else:
                target, stop = tp_sl_prices_from_vol(
                    entry, base_vol_pct(sym, "stock"), direction,
                )
            if target <= 0 or stop <= 0:
                continue
            eval_ts = int(ev["eval_ts"])
            row_expires_at = expires_at_nth_trading_close(eval_ts, hold)
            cur.execute(
                """
                INSERT INTO ghost_shadow_outcomes
                    (symbol, trade_date, eval_ts, up_prob, confidence, skip_code, fired,
                     entry_price, target_price, stop_price, expires_at, created_at,
                     regime_label, adx_trending, above_ema200, ema_trend_bullish,
                     direction, model_prob, prob_model_raw, prob_train_calibrated,
                     prob_live_recalibrated, confidence_final,
                     model_sha256, feature_schema,
                     label_schema, validation_schema, hold_bars)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    sym, _ct_date(eval_ts), eval_ts,
                    ev.get("up_prob"), ev.get("confidence"), ev.get("skip_code"),
                    bool(ev.get("fired")),
                    round(entry, 6), round(target, 6), round(stop, 6),
                    row_expires_at, now,
                    (str(ev.get("regime_label")) if ev.get("regime_label") is not None else None),
                    _regime_flag(ev, "adx_trending"),
                    _regime_flag(ev, "above_ema200"),
                    _regime_flag(ev, "ema_trend_bullish"),
                    direction,
                    ev.get("prob_train_calibrated") if ev.get("prob_train_calibrated") is not None else identity.get("model_prob"),
                    ev.get("prob_model_raw"),
                    ev.get("prob_train_calibrated") if ev.get("prob_train_calibrated") is not None else identity.get("model_prob"),
                    ev.get("prob_live_recalibrated"), ev.get("confidence_final"),
                    identity.get("model_sha256"),
                    identity.get("feature_schema"), identity.get("label_schema"),
                    identity.get("validation_schema"), identity.get("hold_bars"),
                ),
            )
            inserted += cur.rowcount or 0
            returned = cur.fetchone()
            if returned:
                new_rows.append({
                    "shadow_outcome_id": int(returned[0]),
                    "symbol": sym,
                    "direction": direction,
                    "eval_ts": eval_ts,
                    "entry": round(entry, 6),
                    "target": round(target, 6),
                    "stop": round(stop, 6),
                    "expires_at": row_expires_at,
                    "scores": ev.get("scores"),
                })
    if inserted:
        LOGGER.info("Shadow seed: %d new virtual picks", inserted)
    # Checklist snapshots run AFTER the seeding transaction closes: the
    # catalyst collectors touch the network, and network I/O must never sit
    # inside the advisory-locked seed transaction. Seeding never depends on
    # snapshot success.
    if new_rows:
        try:
            snapshot_shadow_checklists(new_rows)
        except Exception as exc:  # noqa: BLE001 - snapshots are best-effort
            LOGGER.warning("shadow checklist snapshots failed: %s", str(exc)[:160])
    return inserted


def _shadow_market_ctx(scores: Any) -> Dict[str, Any]:
    """Rebuild issue-time market context from the eval row's own frozen data.

    Only values the scan itself persisted are used -- nothing is re-fetched,
    so a snapshot written hours after the eval cannot see anything the scan
    did not. Whatever the row lacks stays absent and the corresponding
    checklist box reads UNKNOWN, which is the honest outcome.
    """
    if isinstance(scores, str):
        try:
            scores = json.loads(scores)
        except Exception:
            scores = None
    if not isinstance(scores, dict):
        return {}
    feats = scores.get("features")
    feats = feats if isinstance(feats, dict) else {}
    ctx: Dict[str, Any] = {}
    if scores.get("price") is not None:
        ctx["price"] = scores.get("price")
    asof = feats.get("feature_asof_ts")
    if asof is not None:
        ctx["feature_asof_ts"] = asof
        # The scan price is captured in the same feature snapshot.
        ctx["price_as_of_ts"] = asof
    if feats.get("volume_ratio") is not None:
        ctx["relative_volume"] = feats.get("volume_ratio")
    mom = feats.get("mom_4h")
    if isinstance(mom, (int, float)):
        ctx["trend_slope_pct"] = float(mom) * 100.0
    macro = feats.get("macro_spy_20d_return")
    if isinstance(macro, (int, float)):
        ctx["market_move_pct"] = float(macro) * 100.0
    return ctx


def snapshot_shadow_checklists(rows: List[Dict[str, Any]], *, budget: Optional[int] = None) -> int:
    """Freeze a checklist snapshot for each newly-seeded shadow pick.

    This is what gives the checklist its daily game-play: every virtual pick
    becomes a calibration sample when it resolves, instead of the ledger
    accruing samples only while the official engine is unpaused. Evidence is
    collected as of the row's own eval_ts (the catalyst collectors carry
    their own no-lookahead gates), and each snapshot links to its shadow row
    so the resolver can copy the outcome back later.
    """
    max_rows = budget if budget is not None else int(
        os.getenv("SHADOW_CHECKLIST_BUDGET_PER_RUN", "40")
    )
    from core.catalyst_checklist import evaluate_checklist
    from core.checklist_evidence import collect_evidence
    from core.checklist_ledger import store_snapshot

    written = 0
    skipped = 0
    for row in rows:
        if written >= max(0, max_rows):
            skipped += 1
            continue
        try:
            eval_ts = int(row["eval_ts"])
            ctx = _shadow_market_ctx(row.get("scores"))
            evidence = collect_evidence(
                row["symbol"], asof_ts=eval_ts, market_ctx=ctx,
            )
            report = evaluate_checklist(row["symbol"], row["direction"], evidence)
            store_snapshot(
                symbol=row["symbol"],
                direction=row["direction"],
                report=report,
                evidence=evidence,
                issued_at=eval_ts,
                entry_price=row.get("entry"),
                target_price=row.get("target"),
                stop_price=row.get("stop"),
                deadline_ts=row.get("expires_at"),
                lane="shadow",
                shadow_outcome_id=row["shadow_outcome_id"],
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            LOGGER.warning(
                "shadow checklist snapshot failed for %s: %s",
                row.get("symbol"), str(exc)[:160],
            )
    if written:
        LOGGER.info("Shadow checklist: %d snapshots frozen", written)
    if skipped:
        LOGGER.warning(
            "Shadow checklist: %d rows skipped by per-run budget (%d)",
            skipped, max_rows,
        )
    return written


def resolve_shadow_rows(max_symbols: int = 60) -> int:
    """Resolve pending shadow rows with the same bar-path rules as live picks."""
    from core.db import db_conn
    from core.pnl import resolution_exit
    from core.tp_sl_resolve import label_hold_bars, resolve_open_prediction_detail

    now = int(time.time())
    default_hold = label_hold_bars()
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_shadow_table(cur)
        cur.execute(
            "SELECT id, symbol, eval_ts, entry_price, target_price, stop_price, expires_at, "
            "direction, hold_bars FROM ghost_shadow_outcomes "
            "WHERE outcome IS NULL ORDER BY symbol, eval_ts"
        )
        pending = cur.fetchall()
    if not pending:
        return 0

    by_symbol: Dict[str, List[tuple]] = {}
    for row in pending:
        by_symbol.setdefault(str(row[1]).upper(), []).append(row)

    resolved = 0
    for i, (sym, rows) in enumerate(sorted(by_symbol.items())):
        if i >= max_symbols:
            break
        bars = None
        try:
            from core.signal_engine import _fetch_ohlcv

            bars = _fetch_ohlcv(sym, "stock", period="3m")
        except Exception as e:
            LOGGER.debug("shadow bars %s: %s", sym, str(e)[:80])
        if not bars:
            # No bar path means no mature outcome. Leave the row pending rather
            # than manufacturing EXPIRED evidence from a wall-clock deadline.
            # Expiry is valid only after the full promised forward bar horizon
            # is present and shows neither TP nor SL.
            continue
        for (sid, _sym, eval_ts, entry, target, stop, expires_at, direction, hold_bars) in rows:
            row_direction = str(direction or "UP").upper()
            row_hold = int(hold_bars or default_hold)
            outcome, resolved_at, evidence_price = resolve_open_prediction_detail(
                direction=row_direction,
                target=float(target),
                stop=float(stop),
                predicted_at=int(eval_ts),
                hold_bars=row_hold,
                daily_bars=bars,
                snapshot_price=None,
                now=now,
                expires_at=int(expires_at) if expires_at else None,
            )
            if not outcome or not resolved_at or resolved_at > now:
                continue
            exit_price, pnl = resolution_exit(
                outcome, row_direction, float(entry), float(target), float(stop),
                evidence_price if evidence_price is not None else float(entry),
            )
            with db_conn() as conn:
                conn.cursor().execute(
                    "UPDATE ghost_shadow_outcomes "
                    "SET outcome=%s, exit_price=%s, pnl_pct=%s, resolved_at=%s WHERE id=%s",
                    (outcome, exit_price, pnl, int(resolved_at), sid),
                )
            resolved += 1
    if resolved:
        LOGGER.info("Shadow resolve: %d virtual picks resolved", resolved)
    elif pending:
        LOGGER.debug("Shadow resolve: 0/%d pending — hold window still open or path undecided", len(pending))
    return resolved


def _bucket_for(up_prob: Optional[float]) -> str:
    if up_prob is None:
        return "unknown"
    p = float(up_prob)
    if p >= PROB_FLOOR:
        return "fireable"      # >= 0.55 — would pass the prob gate
    if p >= 0.50:
        return "near"          # 0.50–0.55 — model leaning up, below floor
    return "weak"              # < 0.50 — model leaning down/flat


def aggregate_shadow_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure aggregation: per-symbol records + prob-bucket calibration."""
    symbols: Dict[tuple, Dict[str, Any]] = {}
    buckets: Dict[str, Dict[str, Any]] = {}
    pending = 0
    for r in rows:
        outcome = r.get("outcome")
        if outcome is None:
            pending += 1
            continue
        sym = str(r.get("symbol") or "").upper()
        direction = str(r.get("direction") or "UP").upper()
        generation = (
            sym, direction, str(r.get("model_sha256") or "legacy"),
            str(r.get("feature_schema") or "legacy"),
            str(r.get("label_schema") or "legacy"),
            str(r.get("validation_schema") or "legacy"),
            r.get("hold_bars"),
        )
        s = symbols.setdefault(generation, {
            "symbol": sym, "direction": direction,
            "model_sha256": generation[2], "feature_schema": generation[3],
            "label_schema": generation[4], "validation_schema": generation[5],
            "hold_bars": generation[6],
            "n": 0, "wins": 0, "losses": 0, "expired": 0,
            "pnl_pct_sum": 0.0, "last_outcome": None, "last_eval_ts": 0,
        })
        s["n"] += 1
        if outcome == "WIN":
            s["wins"] += 1
        elif outcome == "LOSS":
            s["losses"] += 1
        else:
            s["expired"] += 1
        s["pnl_pct_sum"] += float(r.get("pnl_pct") or 0)
        if int(r.get("eval_ts") or 0) >= s["last_eval_ts"]:
            s["last_eval_ts"] = int(r.get("eval_ts") or 0)
            s["last_outcome"] = outcome

        bucket_key = f"{direction.lower()}:{_bucket_for(r.get('model_prob', r.get('up_prob')))}"
        b = buckets.setdefault(bucket_key, {
            "direction": direction, "n": 0, "wins": 0, "losses": 0, "expired": 0,
        })
        b["n"] += 1
        if outcome == "WIN":
            b["wins"] += 1
        elif outcome == "LOSS":
            b["losses"] += 1
        else:
            b["expired"] += 1

    def _wr(wins: int, losses: int, expired: int) -> Optional[float]:
        resolved = wins + losses + expired
        return round(wins / resolved * 100.0, 1) if resolved else None

    sym_out = []
    for s in symbols.values():
        sym_out.append({
            "symbol": s["symbol"],
            "direction": s["direction"],
            "model_sha256": s["model_sha256"],
            "feature_schema": s["feature_schema"],
            "label_schema": s["label_schema"],
            "validation_schema": s["validation_schema"],
            "hold_bars": s["hold_bars"],
            "n": s["n"],
            "wins": s["wins"],
            "losses": s["losses"],
            "expired": s["expired"],
            "tp_rate_pct": _wr(s["wins"], s["losses"], s["expired"]),
            "avg_pnl_pct": round(s["pnl_pct_sum"] / s["n"], 3) if s["n"] else None,
            "last_outcome": s["last_outcome"],
        })
    sym_out.sort(key=lambda x: (-(x["tp_rate_pct"] or -1), -x["n"]))

    for b in buckets.values():
        b["tp_rate_pct"] = _wr(b["wins"], b["losses"], b["expired"])

    total_resolved = sum(s["n"] for s in symbols.values())
    return {
        "resolved": total_resolved,
        "pending": pending,
        "buckets": buckets,
        "symbols": sym_out,
    }


def shadow_diagnostics() -> Dict[str, Any]:
    """Ops payload: explain pending vs resolved (hold window timing)."""
    from core.db import db_conn
    from core.tp_sl_resolve import label_hold_bars

    hold = label_hold_bars()
    now = int(time.time())
    out: Dict[str, Any] = {
        "hold_bars": hold,
        "now_ts": now,
        "pending": 0,
        "resolved_total": 0,
    }
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_shadow_table(cur)
            cur.execute("SELECT COUNT(*) FROM ghost_shadow_outcomes WHERE outcome IS NULL")
            out["pending"] = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM ghost_shadow_outcomes WHERE outcome IS NOT NULL")
            out["resolved_total"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT MIN(eval_ts), MAX(eval_ts), MIN(expires_at), MIN(trade_date), MAX(trade_date) "
                "FROM ghost_shadow_outcomes WHERE outcome IS NULL"
            )
            row = cur.fetchone()
            if row and row[0]:
                out["oldest_pending_eval_ts"] = int(row[0])
                out["newest_pending_eval_ts"] = int(row[1])
                out["earliest_expires_at"] = int(row[2]) if row[2] else None
                out["pending_trade_dates"] = {"oldest": row[3], "newest": row[4]}
    except Exception as e:
        out["error"] = str(e)[:120]
        return out

    exp = out.get("earliest_expires_at")
    if out["pending"] and exp:
        if now >= exp:
            out["resolution_status"] = (
                "waiting for complete market evidence — wall-clock expiry alone "
                "cannot resolve a virtual pick"
            )
        else:
            out["resolution_status"] = "waiting — hold window open; first batch closes after earliest expires_at"
        try:
            import pytz

            tz = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
            out["earliest_expires_at_ct"] = datetime.fromtimestamp(exp, tz).strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            note_suppressed()
    elif out["pending"]:
        out["resolution_status"] = "waiting — no expiry metadata on pending rows"
    else:
        out["resolution_status"] = "idle — no pending virtual picks"
    return out


def shadow_stats(days: int = 30) -> Dict[str, Any]:
    """Scoreboard payload for /api/shadow-stats and the MCP tool."""
    from core.db import db_conn
    from core.tp_sl_resolve import label_hold_bars

    days = max(1, min(365, int(days)))
    cutoff = int(time.time()) - days * 86400
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_shadow_table(cur)
        cur.execute(
            "SELECT symbol, eval_ts, up_prob, outcome, pnl_pct, direction, model_prob, "
            "model_sha256, label_schema, validation_schema, hold_bars "
            "FROM ghost_shadow_outcomes WHERE eval_ts >= %s",
            (cutoff,),
        )
        rows = [
            {
                "symbol": r[0], "eval_ts": r[1],
                "up_prob": r[6] if str(r[5] or "").upper() == "DOWN" else r[2],
                "outcome": r[3], "pnl_pct": r[4], "direction": r[5],
                "model_prob": r[6], "model_sha256": r[7],
                "label_schema": r[8], "validation_schema": r[9],
                "hold_bars": r[10],
            }
            for r in cur.fetchall()
        ]
    out = aggregate_shadow_stats(rows)
    diag = shadow_diagnostics()
    try:
        import json as _j
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='last_shadow_cycle'")
            row = cur.fetchone()
        if row and row[0]:
            diag["last_cycle"] = _j.loads(row[0])
    except Exception:
        note_suppressed()
    out.update({
        "ok": True,
        "days": days,
        "prob_floor": PROB_FLOOR,
        "enabled": shadow_enabled(),
        "diagnostics": diag,
        "note": (
            "Virtual picks: every scanned symbol's daily model evaluation resolved "
            "with live TP/SL bar-path rules — gates ignored. 'fireable' bucket = "
            "up_prob >= prob floor. Pending rows stay open until TP/SL hit or "
            f"{label_hold_bars()}-bar hold expires (same as live picks)."
        ),
    })
    return out


def run_shadow_cycle() -> Dict[str, int]:
    """Scheduler hook: seed new rows, then resolve what price has decided."""
    if not shadow_enabled():
        return {"seeded": 0, "resolved": 0}
    try:
        seeded = seed_shadow_rows()
    except Exception as e:
        LOGGER.warning("shadow seed failed: %s", str(e)[:100])
        seeded = 0
    try:
        resolved = resolve_shadow_rows()
    except Exception as e:
        LOGGER.warning("shadow resolve failed: %s", str(e)[:100])
        resolved = 0
    result = {"seeded": seeded, "resolved": resolved}
    try:
        import json as _j
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('last_shadow_cycle', %s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (_j.dumps({**result, "ts": int(time.time())}),),
            )
    except Exception:
        note_suppressed()
    return result
