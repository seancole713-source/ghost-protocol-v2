import os, sys, time, logging, threading, hmac, secrets as _secrets
import config.symbols  # noqa: F401 — pin official watchlist before engine imports
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from core.db import db_conn, init_db, ensure_ghost_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
LOGGER = logging.getLogger("ghost")

# PR #70: suppress yfinance library noise (JSON parse errors, 429s, delisted warnings).
# yfinance logs at ERROR level for transient Yahoo API issues that Ghost already
# handles via circuit breakers. Duplicate logging clutters Railway logs.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# PR #15 cache-bust banner. Logged once at module import. If this line is
# missing from Railway logs after a deploy, the container is stale (the
# Procfile boot echo is the shell-level twin of this check).
LOGGER.info(
    "[wolf_app] BOOT_BANNER PR151_WALLET_SKILL_FILTER_SHADOW_EXPIRE "
    "DEPLOY_VERSION=%s GIT_SHA=%s DEPLOY_ID=%s",
    os.getenv("DEPLOY_VERSION", "unset"),
    os.getenv("RAILWAY_GIT_COMMIT_SHA", "unset"),
    os.getenv("RAILWAY_DEPLOYMENT_ID", "unset"),
)

CRON_SECRET = os.getenv("CRON_SECRET", "")

# PR #77: refuse to boot in production without CRON_SECRET set.
# _cron_ok() fails open when the secret is empty (dev convenience), but
# production must never expose admin/cron/training routes without auth.
if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_SERVICE_NAME"):
    if not CRON_SECRET:
        raise RuntimeError(
            "CRON_SECRET is required in production (Railway). "
            "Set it in the Railway dashboard under Variables."
        )


def _cron_ok(provided: str, strict: bool = False) -> bool:
    """Constant-time check for x-cron-secret header.

    strict=False (default): if no CRON_SECRET is configured, allow (dev mode).
    strict=True: if no CRON_SECRET is configured, REJECT. Use on endpoints
                 that must never be exposed without explicit auth, even in dev.

    PR #125 audit: GHOST_DEV_MODE=1 must be explicitly set for dev-mode bypass.
    Without it, strict=False behaves like strict=True (reject when unconfigured).
    """
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        dev_mode = os.getenv("GHOST_DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on")
        if dev_mode:
            return not strict
        return False  # reject: no secret and not explicitly in dev mode
    return hmac.compare_digest((provided or "").encode("utf-8"),
                               secret.encode("utf-8"))

# Semantic app version. Bumped to 2.1.0 for the audit batch: kill conditions +
# enforcement, full pick journal, realized P&L, security hardening, regime tag,
# Telegram cards, admin lineage/audit, rate limiting, short-interest wiring.
APP_VERSION = "2.5.0"

_COVERAGE_RETRAIN_RUNNING = False
_RETRAIN_JOB_LOCK = threading.Lock()
_APP_BOOT_TS = time.time()


def _record_admin_action(action: str, detail: str = "") -> None:
    """Append an operator action to a rolling audit log in ghost_state (last 100).
    Best-effort: never raises into the calling endpoint. Audit trail for
    destructive/admin mutations (purges, training, engine resume, etc.)."""
    try:
        import json as _j
        entry = {"ts": int(time.time()), "action": str(action)[:60], "detail": str(detail)[:200]}
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='admin_audit_log'")
            row = cur.fetchone()
            log = []
            if row and row[0]:
                try:
                    log = _j.loads(row[0])
                except Exception:
                    log = []
            if not isinstance(log, list):
                log = []
            log.append(entry)
            log = log[-100:]
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('admin_audit_log', %s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (_j.dumps(log),))
    except Exception as _e:
        LOGGER.warning("admin audit log write failed: " + str(_e)[:80])


_COCKPIT_DB_CACHE = {"t": 0.0, "stats": None, "direction": None, "v3": None, "activity": None}
_COCKPIT_DB_CACHE_LOCK = threading.Lock()


def _bump_cockpit_db_cache():
    with _COCKPIT_DB_CACHE_LOCK:
        _COCKPIT_DB_CACHE["t"] = 0.0
        for _k in ("stats", "direction", "v3", "activity"):
            _COCKPIT_DB_CACHE[_k] = None


def _v32_stats_start_ts(cur):
    """Unix start of v3.2 stats window with non-drifting persistence.

    Priority:
    1) V3_STATS_START_TS env override (if set)
    2) persisted ghost_state.v32_stats_start_ts (sticky, never move forward)
    3) bootstrap candidate from model metas + recent symbol history
    """
    import json as _json

    # 1) Hard override from env
    try:
        _env_ts = int(os.getenv("V3_STATS_START_TS", "0") or 0)
        if _env_ts > 0:
            return _env_ts
    except Exception:
        pass

    # Ensure state table exists (shared with other lightweight state keys)
    try:
        ensure_ghost_state(cur)
    except Exception:
        pass

    # Correct bad persisted cutover (Apr 8 = 1775606400 -> Apr 5 = 1775347200)
    CORRECT_V32_TS = 1775347200  # 2026-04-05 00:00 UTC — v3.2 deploy date
    try:
        cur.execute("SELECT val FROM ghost_state WHERE key='v32_stats_start_ts'")
        _row = cur.fetchone()
        if _row and int(_row[0]) >= 1775606400:
            cur.execute("UPDATE ghost_state SET val=%s WHERE key='v32_stats_start_ts'", (str(CORRECT_V32_TS),))
            LOGGER.info("v32_stats_start_ts corrected to Apr 5 2026")
    except Exception: pass

    # 2) Existing sticky cutover if present
    sticky_ts = 0
    try:
        cur.execute("SELECT val FROM ghost_state WHERE key='v32_stats_start_ts'")
        _row = cur.fetchone()
        if _row and _row[0]:
            sticky_ts = int(_row[0])
    except Exception:
        sticky_ts = 0

    # 3) Bootstrap candidate (if sticky missing or to allow safe backward correction only)
    model_ts = 0
    model_syms = []
    try:
        cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%'")
        trained = []
        for key, val in cur.fetchall():
            try:
                m = _json.loads(val)
                if m.get("label_type") != "tp_sl_daily":
                    continue
                ts = int(m.get("trained_at", 0) or 0)
                if ts > 0:
                    trained.append(ts)
                sym = _strip_model_direction_suffix(
                    str(key or "").replace("meta_", "").strip()
                ).upper()
                if sym:
                    model_syms.append(sym)
            except Exception:
                continue
        if trained:
            model_ts = min(trained)
    except Exception:
        model_ts = 0

    # Recent symbol-history anchor (helps recover when model_ts drifts forward after retrain churn)
    # Scoped to recent history to avoid pulling legacy-era rows.
    hist_ts = 0
    try:
        model_syms = sorted(set(model_syms))
        if model_syms:
            placeholders = ",".join(["%s"] * len(model_syms))
            cur.execute(
                f"SELECT MIN(predicted_at) FROM predictions "
                f"WHERE predicted_at IS NOT NULL AND predicted_at >= %s "
                f"AND symbol IN ({placeholders})",
                [int(time.time()) - 90 * 86400, *model_syms],
            )
            _h = cur.fetchone()
            if _h and _h[0]:
                hist_ts = int(_h[0])
    except Exception:
        hist_ts = 0

    candidates = [t for t in (model_ts, hist_ts) if t > 0]
    candidate_ts = min(candidates) if candidates else 0

    # Never move cutover forward implicitly; allow only first set or backward correction.
    if sticky_ts > 0 and candidate_ts > 0:
        final_ts = min(sticky_ts, candidate_ts)
    else:
        final_ts = sticky_ts or candidate_ts or 0

    if final_ts > 0 and final_ts != sticky_ts:
        try:
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('v32_stats_start_ts',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (str(final_ts),),
            )
        except Exception:
            pass
    return final_ts


from core.prediction_filters import CRYPTO_JUNK_WHERE, NON_RESEARCH_WHERE, REAL_TRADE_WHERE, non_research_where, picks_where as _picks_where


def _build_symbol_universe_payload() -> dict:
    """Consolidated symbol layers: code watchlist, env scan set, portfolio, models, picks."""
    from config.symbols import OFFICIAL_WATCHLIST, watchlist_symbol_pairs

    now_ts = int(time.time())
    official = list(OFFICIAL_WATCHLIST)
    official_set = set(official)
    env_syms = [s.strip().upper() for s in os.getenv("STOCK_SYMBOLS", "").split(",") if s.strip()]
    scan_syms = [sym for sym, _atype in watchlist_symbol_pairs(include_portfolio=False)]

    portfolio_rows = []
    picks_tally = {}
    open_by_sym = {}
    models_db = []

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT symbol, quantity, buy_price, notes FROM user_portfolio ORDER BY symbol"
        )
        portfolio_rows = [
            {
                "symbol": str(r[0]).upper(),
                "quantity": float(r[1]),
                "buy_price": float(r[2]),
                "notes": r[3] or "",
            }
            for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT key, updated_at FROM ghost_v3_model WHERE key LIKE 'meta_%' ORDER BY key"
        )
        models_db = [
            {"symbol": str(k).replace("meta_", "").upper(), "updated_at": ts,
             "base_symbol": _strip_model_direction_suffix(str(k).replace("meta_", "")).upper()}
            for k, ts in cur.fetchall()
        ]
        cur.execute(
            "SELECT symbol, outcome, COUNT(*) FROM predictions WHERE "
            + REAL_TRADE_WHERE
            + " GROUP BY symbol, outcome ORDER BY symbol, outcome"
        )
        for sym, outcome, cnt in cur.fetchall():
            sym_u = str(sym).upper()
            bucket = picks_tally.setdefault(
                sym_u,
                {"total": 0, "wins": 0, "losses": 0, "expired": 0, "other": 0},
            )
            n = int(cnt)
            bucket["total"] += n
            if outcome == "WIN":
                bucket["wins"] = n
            elif outcome == "LOSS":
                bucket["losses"] = n
            elif outcome == "EXPIRED":
                bucket["expired"] = n
            else:
                bucket["other"] = bucket.get("other", 0) + n
        cur.execute(
            "SELECT symbol, COUNT(*) FROM predictions WHERE outcome IS NULL AND "
            + REAL_TRADE_WHERE
            + " GROUP BY symbol ORDER BY symbol"
        )
        open_by_sym = {str(r[0]).upper(): int(r[1]) for r in cur.fetchall()}

    portfolio_syms = sorted({p["symbol"] for p in portfolio_rows})
    portfolio_set = set(portfolio_syms)

    try:
        from core.signal_engine import get_model_status
        model_st = get_model_status() or {}
    except Exception as e:
        model_st = {"trained": False, "reason": str(e)[:120]}

    stored = model_st.get("stored_symbols") or {}
    serveable = model_st.get("symbols") or {}
    # stored_symbols keys carry the Phase 2 direction suffix (WOLF_up);
    # normalize to bare symbols for watchlist comparisons.
    stored_syms = sorted({_strip_model_direction_suffix(k) for k in stored.keys()})
    serveable_syms = sorted(serveable.keys())

    picks_enriched = {}
    for sym, stats in picks_tally.items():
        wins = int(stats.get("wins") or 0)
        losses = int(stats.get("losses") or 0)
        resolved = wins + losses
        picks_enriched[sym] = {
            **stats,
            "open": open_by_sym.get(sym, 0),
            "resolved": resolved,
            "win_rate_pct": round(wins / resolved * 100, 1) if resolved else None,
        }
    for sym, n in open_by_sym.items():
        if sym not in picks_enriched:
            picks_enriched[sym] = {
                "total": n, "wins": 0, "losses": 0, "expired": 0, "other": 0,
                "open": n, "resolved": 0, "win_rate_pct": None,
            }

    fired_syms = sorted(picks_enriched.keys())

    return {
        "ok": True,
        "checked_at": now_ts,
        "official_watchlist": {
            "count": len(official),
            "symbols": official,
            "source": "config/symbols.py OFFICIAL_WATCHLIST (no DB watchlist table)",
        },
        "stock_symbols_env": {
            "count": len(env_syms),
            "symbols": env_syms,
            "matches_official": env_syms == official,
            "scan_universe_count": len(scan_syms),
            "scan_universe_symbols": scan_syms,
        },
        "portfolio": {
            "count": len(portfolio_syms),
            "symbols": portfolio_syms,
            "positions": portfolio_rows,
            "in_official_watchlist": sorted(portfolio_set & official_set),
            "not_in_official_watchlist": sorted(portfolio_set - official_set),
            "source": "user_portfolio table (manual / Cash App imports)",
        },
        "models": {
            "stored_count": len(stored_syms),
            "serveable_count": len(serveable_syms),
            "stored_symbols": stored_syms,
            "serveable_symbols": serveable_syms,
            "stored_rows": models_db,
            "by_symbol": stored,
            "official_missing_model": sorted(official_set - set(stored_syms)),
            "official_not_serveable": sorted(
                sym for sym in official_set if sym in set(stored_syms) and sym not in serveable
            ),
        },
        "picks": {
            "by_symbol": picks_enriched,
            "symbols_with_fired_picks": fired_syms,
            "official_without_picks": sorted(official_set - set(fired_syms)),
            "filter": REAL_TRADE_WHERE,
            "source": "predictions table (engine-fired picks only)",
        },
        "summary": {
            "official_watchlist": len(official),
            "portfolio_symbols": len(portfolio_syms),
            "models_stored": len(stored_syms),
            "models_serveable": len(serveable_syms),
            "symbols_with_fired_picks": len(fired_syms),
            "prediction_engine_scan_count": len(scan_syms),
        },
        "notes": [
            "Portfolio rows do not expand the scan universe — see watchlist_symbol_pairs().",
            "Scan loop uses STOCK_SYMBOLS; picks appear only after gates pass and INSERT.",
            "WOLF-only picks history does not mean predict_live_ex is WOLF-hardcoded.",
        ],
    }


def _compute_get_stats(cur):
    """Payload for GET /api/stats using an existing cursor."""
    # Outcome-based stats exclude research picks (low-bar learning probes) so the
    # headline win rate matches what the objective gate / kill switch actually use.
    cur.execute(
        "SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS') "
        "AND predicted_at IS NOT NULL AND " + REAL_TRADE_WHERE
        + " AND " + NON_RESEARCH_WHERE + " GROUP BY outcome"
    )
    rows = {r[0]: r[1] for r in cur.fetchall()}
    wins = rows.get("WIN", 0)
    losses = rows.get("LOSS", 0)
    total = wins + losses
    cur.execute(
        "SELECT COUNT(*) FROM predictions WHERE outcome IS NULL AND " + REAL_TRADE_WHERE
    )
    open_count = cur.fetchone()[0]
    v32_start_ts = _v32_stats_start_ts(cur)
    v32_wins = v32_losses = v32_total = 0
    v32r_wins = v32r_losses = v32r_total = 0
    if v32_start_ts > 0:
        cur.execute(
            "SELECT outcome, COUNT(*) FROM predictions "
            "WHERE outcome IN ('WIN','LOSS') AND predicted_at IS NOT NULL AND predicted_at >= %s "
            "AND " + REAL_TRADE_WHERE + " AND " + NON_RESEARCH_WHERE + " GROUP BY outcome",
            (v32_start_ts,),
        )
        v32_rows = {r[0]: r[1] for r in cur.fetchall()}
        v32_wins = v32_rows.get("WIN", 0)
        v32_losses = v32_rows.get("LOSS", 0)
        v32_total = v32_wins + v32_losses
        # Closes after cutover (matches "Recent Results" feel; can include picks issued before cutover)
        cur.execute(
            "SELECT outcome, COUNT(*) FROM predictions "
            "WHERE outcome IN ('WIN','LOSS') AND resolved_at IS NOT NULL AND resolved_at >= %s "
            "AND " + REAL_TRADE_WHERE + " AND " + NON_RESEARCH_WHERE + " GROUP BY outcome",
            (v32_start_ts,),
        )
        v32r_rows = {r[0]: r[1] for r in cur.fetchall()}
        v32r_wins = v32r_rows.get("WIN", 0)
        v32r_losses = v32r_rows.get("LOSS", 0)
        v32r_total = v32r_wins + v32r_losses
    scan_stocks = [s.strip().upper() for s in os.getenv("STOCK_SYMBOLS", "WOLF").split(",") if s.strip()] or ["WOLF"]

    def _wilson_lb95(w: int, n: int) -> float:
        # 95% Wilson lower bound — the honest floor on a small-sample win rate.
        # Stops 5-of-6 luck from being quoted as "83%". (PR #135 audit)
        if n <= 0:
            return 0.0
        z = 1.96
        p = w / n
        denom = 1 + z * z / n
        center = p + z * z / (2 * n)
        margin = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
        return round(max(0.0, (center - margin) / denom) * 100, 1)

    return {
        "ok": True,
        "wins": wins,
        "losses": losses,
        "total": total,
        "win_rate_pct": round(wins / total * 100, 1) if total else 0,
        "win_rate_wilson_lb95_pct": _wilson_lb95(wins, total),
        "sample_note": "win_rate under N=30 resolved picks is statistically weak — quote the Wilson lower bound",
        "open_positions": open_count,
        "post_v32": {
            "start_ts": v32_start_ts,
            "wins": v32_wins,
            "losses": v32_losses,
            "total": v32_total,
            "win_rate_pct": round(v32_wins / v32_total * 100, 1) if v32_total else 0.0,
        },
        "post_v32_resolved": {
            "start_ts": v32_start_ts,
            "wins": v32r_wins,
            "losses": v32r_losses,
            "total": v32r_total,
            "win_rate_pct": round(v32r_wins / v32r_total * 100, 1) if v32r_total else 0.0,
        },
        "scan_symbols": {"stocks": scan_stocks},
    }


def _cockpit_activity_on_cursor(cur):
    """Summary counts embedded in /api/cockpit/context."""
    cur.execute(
        "SELECT COUNT(*) FROM predictions WHERE outcome IS NULL AND expires_at > extract(epoch from now())"
    )
    open_predictions = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM predictions WHERE resolved_at > extract(epoch from now()) - 86400"
    )
    resolved_24h = cur.fetchone()[0]
    cur.execute(
        "SELECT outcome, COUNT(*) FROM predictions WHERE resolved_at > extract(epoch from now()) - 604800 GROUP BY outcome"
    )
    weekly_outcomes = {r[0]: r[1] for r in cur.fetchall()}
    return {
        "open_predictions": open_predictions,
        "resolved_24h": resolved_24h,
        "weekly_outcomes": weekly_outcomes,
    }


def _has_loadable_v3_model() -> bool:
    """True only if at least one configured symbol has a model that actually
    LOADS — passing load_model's label_type / feature_schema / age guards — not
    merely a row present in ghost_v3_model.

    A stored-but-rejected model (e.g. after a feature_schema bump from a
    model-shape change) must still trigger the startup retrain; otherwise the
    engine sits dormant with no usable model until someone retrains by hand.
    This closes the gap between "a row exists" and "a model is serveable" that
    left the engine down after the W1 feature_schema guard shipped (the old
    rows kept label_type=tp_sl_daily, so the prior existence check thought a
    model was present while load_model rejected every one).
    """
    try:
        from core.signal_engine import load_model
        syms = [s.strip().upper() for s in os.getenv("STOCK_SYMBOLS", "WOLF").split(",") if s.strip()] or ["WOLF"]
        for s in syms:
            model, _cols, _meta = load_model(s)
            if model is not None:
                return True
        return False
    except Exception:
        return False


def _purge_v3_stale_or_weak():
    """Remove off-watchlist and pre-v3.2 models only.

    Do not second-guess train gates on accuracy/edge/wf — that deleted symbols
    like NOK immediately after a successful retrain (train passes at 38% holdout,
    purge required 55%).
    """
    import json as _j
    from core.signal_engine import model_serve_guard

    try:
        from config.symbols import watchlist_symbols
        allowed = watchlist_symbols(include_portfolio=True)
    except Exception:
        allowed = None
    purged = 0
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%'")
            for key, val in cur.fetchall():
                raw = key.replace("meta_", "")
                # Phase 2 keys carry a direction suffix (meta_WOLF_up); the
                # watchlist check must use the bare symbol or WOLF_UP would
                # read as off-watchlist and purge WOLF's own models.
                sym = _strip_model_direction_suffix(raw)
                try:
                    meta = _j.loads(val)
                    if model_serve_guard(meta) is None:
                        continue
                    off_watchlist = allowed is not None and sym.upper() not in allowed
                    legacy = meta.get("label_type") != "tp_sl_daily"
                    if off_watchlist or legacy:
                        cur.execute(
                            "DELETE FROM ghost_v3_model WHERE key IN (%s,%s)",
                            (f"model_{raw}", f"meta_{raw}"),
                        )
                        purged += 1
                except Exception:
                    pass
        if purged:
            try:
                from core.signal_engine import invalidate_model_cache
                invalidate_model_cache()
            except Exception:
                pass
        return purged
    except Exception:
        return 0


def _strip_model_direction_suffix(raw_key: str) -> str:
    """Bare symbol from a ghost_v3_model key stem (Phase 2 directional keys).

    'WOLF_up' / 'WOLF_down' -> 'WOLF'; legacy 'WOLF' passes through.
    """
    if raw_key.endswith("_up"):
        return raw_key[:-3]
    if raw_key.endswith("_down"):
        return raw_key[:-5]
    return raw_key


def _expire_open_picks_without_v3_model():
    """Expire active picks for symbols that currently have no v3 TP/SL model."""
    expired = 0
    now = int(time.time())
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key FROM ghost_v3_model WHERE key LIKE 'meta_%'")
            # Normalize Phase 2 directional keys (meta_WOLF_up) to bare
            # symbols, or every open pick would mass-expire once the legacy
            # meta_WOLF row ages out.
            model_syms = {
                _strip_model_direction_suffix(row[0].replace("meta_", ""))
                for row in cur.fetchall()
            }
            cur.execute(
                "SELECT id, symbol FROM predictions "
                "WHERE outcome IS NULL AND expires_at > %s",
                (now,),
            )
            rows = cur.fetchall()
            for pid, sym in rows:
                if (sym or "").upper() not in model_syms:
                    cur.execute(
                        "UPDATE predictions SET outcome='EXPIRED', resolved_at=%s WHERE id=%s",
                        (now, pid),
                    )
                    expired += 1
        return expired
    except Exception:
        return 0


# ── Telegram card assembly (feat/telegram-cards) ─────────────────────────

def _daily_min_conf() -> float:
    """High-conviction threshold below which the daily card goes to SILENCE."""
    try:
        return float(os.getenv("TELEGRAM_DAILY_MIN_CONF", "0.85"))
    except Exception:
        return 0.85


def _wolf_track_record() -> dict:
    """All-time W/L, win rate, last-5 (newest first), and current streak for
    WOLF v3.2-era resolved picks."""
    out = {"wins": 0, "losses": 0, "win_rate_pct": 0, "last5": [], "streak": "--"}
    try:
        with db_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT outcome FROM predictions WHERE symbol='WOLF' AND id >= %s "
                "AND outcome IN ('WIN','LOSS') ORDER BY resolved_at DESC NULLS LAST, id DESC",
                (_V32_ERA_MIN_ID,))
            outs = [r[0] for r in cur.fetchall()]
        wins = outs.count("WIN")
        losses = outs.count("LOSS")
        tot = wins + losses
        out["wins"], out["losses"] = wins, losses
        out["win_rate_pct"] = round(wins / tot * 100, 1) if tot else 0
        out["last5"] = ["W" if o == "WIN" else "L" for o in outs[:5]]
        if outs:
            first = outs[0]
            n = 0
            for o in outs:
                if o == first:
                    n += 1
                else:
                    break
            out["streak"] = str(n) + ("W" if first == "WIN" else "L")
    except Exception:
        pass
    return out


def _wolf_week_rate_bounds():
    """(highest, lowest) confidence pct among WOLF picks predicted in last 7d."""
    try:
        with db_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT MAX(confidence), MIN(confidence) FROM predictions "
                "WHERE symbol='WOLF' AND predicted_at >= %s",
                (int(time.time()) - 7 * 86400,))
            r = cur.fetchone()
        if r and r[0] is not None:
            return int(round(float(r[0]) * 100)), int(round(float(r[1]) * 100))
    except Exception:
        pass
    return None, None


def _wolf_retrain_in_days():
    """Days until the WOLF model goes stale (14d window from trained_at)."""
    try:
        with db_conn() as c:
            cur = c.cursor()
            # Phase 2 directional key first, legacy key as fallback.
            cur.execute(
                "SELECT value FROM ghost_v3_model WHERE key IN ('meta_WOLF_up','meta_WOLF') "
                "ORDER BY key DESC LIMIT 1"
            )
            r = cur.fetchone()
        if r and r[0]:
            ta = json.loads(r[0]).get("trained_at")
            if ta:
                return max(0, int(round(14 - (time.time() - float(ta)) / 86400)))
    except Exception:
        pass
    return None


def _build_daily_card_data(pick: dict) -> dict:
    """Assemble the daily-card payload from a saved pick + DB-derived context."""
    import datetime as _dt, pytz as _tz
    from core.telegram_cards import conviction_from_confidence, compute_news_influence
    tz = _tz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    conf = float(pick.get("confidence") or 0)
    entry = float(pick.get("entry_price") or 0)
    target = float(pick.get("target_price") or 0)
    stop = float(pick.get("stop_price") or 0)
    direction = pick.get("direction", "UP")
    exp_move = ((target - entry) / entry * 100) if entry else 0.0
    feats = pick.get("features") or {}
    conf_raw = feats.get("confidence_raw", conf)
    news = compute_news_influence(conf, conf_raw)
    if news["influence_pct"] > 0:
        news["summary"] = _wolf_news_summary()
    hi, lo = _wolf_week_rate_bounds()
    conf_pct = int(round(conf * 100))
    gs_score = None
    try:
        from api.wolf_endpoints import ghost_score_payload_sync
        _gs = ghost_score_payload_sync(use_cache=True)
        if _gs.get("ok"):
            gs_score = float(_gs.get("score") or 0)
    except Exception:
        pass
    from core.risk_discipline import position_sizing_plan, pick_action_tier
    sizing = position_sizing_plan(entry, stop, confidence=conf)
    return {
        "date": _dt.datetime.now(tz).strftime("%A %b %d, %Y"),
        "model_version": "v3.2",
        "direction": direction,
        "confidence": conf,
        "conviction": conviction_from_confidence(conf),
        "pick_action": pick_action_tier(conf, gs_score),
        "position_sizing": sizing,
        "current_price": entry,
        "buy_point": entry,
        "sell_target": target,
        "stop_loss": stop,
        "expected_move_pct": round(exp_move, 1),
        "news": news,
        "rates": {"today_pct": conf_pct,
                  "week_high_pct": hi if hi is not None else conf_pct,
                  "week_low_pct": lo if lo is not None else conf_pct},
        "track_record": _wolf_track_record(),
    }


def _wolf_news_summary():
    """Best-effort 1-line catalyst headline for the news-influence section.
    Returns None on any failure (formatter then shows just the influence split)."""
    try:
        from core.wolf_context import _get_catalyst_news_score
        _score, headlines = _get_catalyst_news_score("UP")
        if headlines:
            return str(headlines[0])[:160]
    except Exception:
        pass
    return None


def _build_silence_card_data(diag: dict) -> dict:
    """Assemble the SILENCE card from the cycle diagnostics + a Ghost Score."""
    reason = "No qualifying signal — gates not cleared"
    try:
        floor = diag.get("confidence_floor")
        label = diag.get("top_reason_label") or diag.get("top_reason_code")
        if label:
            reason = str(label)
        if floor:
            reason += " (floor " + str(int(round(float(floor) * 100))) + "%)"
    except Exception:
        pass
    score = "--"
    gs_score_f = None
    try:
        from api.wolf_endpoints import ghost_score_payload_sync
        gs = ghost_score_payload_sync(use_cache=False)
        if gs.get("ok") and gs.get("score") is not None:
            gs_score_f = float(gs["score"])
            score = int(round(gs_score_f))
    except Exception:
        pass
    from core.risk_discipline import (
        bias_label_from_score,
        combined_trading_block,
        is_daily_loss_locked,
        trade_action_from_context,
    )
    from core.prediction import engine_pause_state
    pause = engine_pause_state()
    action_ctx = trade_action_from_context(
        has_official_pick=False,
        ghost_score=gs_score_f,
        gates_blocked=True,
        engine_paused=bool(pause.get("paused")),
        daily_locked=is_daily_loss_locked(),
    )
    from core.telegram_cards import next_scan_note

    out = {
        "ghost_score": score,
        "bias_label": bias_label_from_score(float(gs_score_f or 50)),
        "reason": reason,
        "next_scan_note": next_scan_note(),
        "top_candidates": _top_scan_candidates(),
        **action_ctx,
    }
    block = combined_trading_block()
    if block.get("blocked") and block.get("reasons"):
        out["risk_block"] = "; ".join(block["reasons"])
    return out


def _build_daily_summary():
    """Aggregate the day's engine activity (roadmap #3b): scans + candidates +
    saves from the per-cycle gate history, today's resolutions, and the engine
    pause state. Pure of scheduling — callable any time."""
    import datetime as _dt, pytz as _tz, json as _j
    tz = _tz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    now_ct = _dt.datetime.now(tz)
    day_start = int(now_ct.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    s = {"date": now_ct.strftime("%Y-%m-%d"), "ts": int(time.time()),
         "scans": 0, "candidates": 0, "saved": 0, "would_fire_cycles": 0,
         "resolved": {"wins": 0, "losses": 0, "pnl_pct": 0.0}, "engine_paused": False}
    try:
        with db_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='gate_outcome_history'")
            row = cur.fetchone()
            hist = []
            if row and row[0]:
                try:
                    hist = _j.loads(row[0])
                except Exception:
                    hist = []
            for h in hist if isinstance(hist, list) else []:
                if (h.get("ts") or 0) >= day_start:
                    s["scans"] += 1
                    s["candidates"] += h.get("candidates", 0) or 0
                    s["saved"] += h.get("saved", 0) or 0
                    if h.get("would_fire"):
                        s["would_fire_cycles"] += 1
            cur.execute(
                "SELECT outcome, pnl_pct FROM predictions WHERE symbol='WOLF' "
                "AND resolved_at >= %s AND outcome IN ('WIN','LOSS') "
                "AND " + NON_RESEARCH_WHERE, (day_start,))
            for o, p in cur.fetchall():
                if o == "WIN":
                    s["resolved"]["wins"] += 1
                elif o == "LOSS":
                    s["resolved"]["losses"] += 1
                s["resolved"]["pnl_pct"] += float(p or 0)
            s["resolved"]["pnl_pct"] = round(s["resolved"]["pnl_pct"], 3)
    except Exception as e:
        LOGGER.warning("daily summary build failed: " + str(e)[:80])
    try:
        from core.prediction import engine_pause_state
        s["engine_paused"] = bool(engine_pause_state().get("paused"))
    except Exception:
        pass
    return s


def _daily_summary_job():
    """Store one daily summary per CT day at DAILY_SUMMARY_HOUR (default 16, after
    close). Registered hourly with an ISO-date dedup so it fires once/day across
    restarts. Appends to ghost_state.daily_summary_history (last 30)."""
    import datetime, pytz, json as _j
    if os.getenv("DAILY_SUMMARY_ENABLED", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return
    ct = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    now_ct = datetime.datetime.now(ct)
    try:
        want_hour = int(os.getenv("DAILY_SUMMARY_HOUR", "16"))
    except Exception:
        want_hour = 16
    if now_ct.hour != want_hour:
        return
    date_str = now_ct.strftime("%Y-%m-%d")
    try:
        with db_conn() as c:
            cur = c.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='last_daily_summary_date'")
            row = cur.fetchone()
            if row and row[0] == date_str:
                return  # already stored today
    except Exception:
        pass
    summary = _build_daily_summary()
    try:
        with db_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='daily_summary_history'")
            row = cur.fetchone()
            hist = []
            if row and row[0]:
                try:
                    hist = _j.loads(row[0])
                except Exception:
                    hist = []
            if not isinstance(hist, list):
                hist = []
            hist.append(summary)
            hist = hist[-30:]
            cur.execute("INSERT INTO ghost_state(key,val) VALUES('daily_summary_history',%s) "
                        "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (_j.dumps(hist),))
            cur.execute("INSERT INTO ghost_state(key,val) VALUES('last_daily_summary_date',%s) "
                        "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (date_str,))
        LOGGER.info("Daily summary stored %s: scans=%d saved=%d", date_str, summary["scans"], summary["saved"])
    except Exception as e:
        LOGGER.error("daily summary store failed: " + str(e)[:100])


def _market_scan_gap_s(now_et=None):
    """Required seconds between scans: shorter during US RTH + pre-market (ET)."""
    try:
        market_min = int(os.getenv("SCAN_INTERVAL_MARKET_MIN", "30"))
        off_min = int(os.getenv("SCAN_INTERVAL_OFFHOURS_MIN", "60"))
    except Exception:
        market_min, off_min = 30, 60
    from core.market_hours import is_us_premarket, is_us_rth
    from core.prediction import _premarket_scan_enabled

    is_premarket = is_us_premarket(now_et) and _premarket_scan_enabled()
    is_market = is_us_rth(now_et) or is_premarket
    return (market_min if is_market else off_min) * 60, is_market


def _market_scan_job():
    """Run the prediction cycle on a market-aware cadence (roadmap #3a).

    Registered at the short (market) interval; self-gates via ghost_state so it
    actually scans every SCAN_INTERVAL_MARKET_MIN during market hours and only
    every SCAN_INTERVAL_OFFHOURS_MIN otherwise. This is the scan loop the engine
    lacked — previously it only ran once/day with the morning card. Saving is
    deduped inside run_prediction_cycle (one open WOLF pick at a time), and any
    pick that fires is pushed through the alert sweep (Telegram + email/SMS)."""
    if os.getenv("MARKET_SCAN_ENABLED", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return
    # P3 (audit): degraded mode — evaluate before scan
    try:
        from core.degraded_mode import check_degraded
        check_degraded()
    except Exception:
        pass
    import datetime as _dt, pytz as _tz
    from core.prediction import run_prediction_cycle
    now = int(time.time())
    now_ct = _dt.datetime.now(_tz.timezone("America/Chicago"))
    gap, is_market = _market_scan_gap_s(now_ct)
    try:
        with db_conn() as c:
            cur = c.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='last_market_scan_ts'")
            row = cur.fetchone()
            last = int(row[0]) if row and row[0] else 0
        if now - last < gap - 30:   # 30s slack for scheduler tick jitter
            return
    except Exception as _ge:
        LOGGER.warning("market scan gate failed: " + str(_ge)[:80])
    try:
        picks = run_prediction_cycle()
        with db_conn() as c:
            c.cursor().execute(
                "INSERT INTO ghost_state(key,val) VALUES('last_market_scan_ts',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (str(now),))
        LOGGER.info("Market scan: %d pick(s) saved (market_hours=%s)", len(picks or []), is_market)
        # Notify on any freshly-fired pick (Telegram + email/SMS), deduped internally.
        try:
            wolf_signal_alert_check(x_cron_secret=os.getenv("CRON_SECRET", ""))
        except Exception:
            pass
    except Exception as e:
        LOGGER.error("Market scan failed: " + str(e)[:120])


def _morning_card_job():
    """Run prediction cycle and send morning Telegram card."""
    import datetime as _dt, pytz as _pytz, time as _t2
    from core.prediction import run_prediction_cycle
    from core.db import db_conn
    _cycle_diag = {}
    # Dedup: Telegram morning card only once per CT day — but always run prediction cycle
    # (redeploys used to return [] here and skipped inserts until next calendar day).
    _ct_tz = _pytz.timezone("America/Chicago")
    _today_ct = _dt.datetime.now(_ct_tz).strftime("%Y-%m-%d")
    _skip_telegram = False
    try:
        with db_conn() as _dc2:
            _cur_d = _dc2.cursor()
            _cur_d.execute("SELECT val FROM ghost_state WHERE key='last_morning_card_date'")
            _row = _cur_d.fetchone()
            if _row and _row[0] == _today_ct:
                _skip_telegram = True
                LOGGER.info(
                    "Morning card already sent today (" + _today_ct + ") — will run prediction cycle, skip duplicate Telegram"
                )
    except Exception as _de:
        LOGGER.warning("Dedup check failed: "+str(_de)[:60])
    picks, _cycle_diag = run_prediction_cycle(with_diag=True)
    # PR #80: only claim the CT-day dedup slot AFTER a successful send.
    # Previously the slot was claimed before the send, so a dead-lettered
    # card would consume the slot and prevent automatic retry that day.
    if _skip_telegram:
        LOGGER.info("Morning card: Telegram skipped (same CT day); cycle returned %s saved picks", len(picks or []))
        return picks
    # Overhauled cards (feat/telegram-cards): a high-conviction pick gets the
    # full daily card; otherwise the SILENCE card. The once-per-CT-day dedup
    # above (last_morning_card_date) prevents duplicate sends on restart/self-heal.
    min_conf = _daily_min_conf()
    top = max(picks, key=lambda p: float(p.get("confidence") or 0)) if picks else None
    if top and float(top.get("confidence") or 0) >= min_conf:
        try:
            from core.telegram import send_daily_card
            ok = send_daily_card(_build_daily_card_data(top))
            if ok:
                _record_morning_card_sent()
                LOGGER.info("Daily card sent: %s %s @ %.0f%%", top.get("symbol"),
                            top.get("direction"), float(top.get("confidence") or 0) * 100)
            else:
                LOGGER.error("Daily card FAILED to send (dead-lettered): %s", top.get("symbol"))
        except Exception as _ce:
            LOGGER.error("Daily card send failed: " + str(_ce)[:120])
    else:
        try:
            from core.telegram import send_silence_card
            ok = send_silence_card(_build_silence_card_data(_cycle_diag if isinstance(_cycle_diag, dict) else {}))
            if ok:
                _record_morning_card_sent()
                LOGGER.info("Silence card sent (no pick >= %.0f%%)", min_conf * 100)
            else:
                LOGGER.error("Silence card FAILED to send (dead-lettered)")
        except Exception as _se:
            LOGGER.error("Silence card send failed: " + str(_se)[:120])
    return picks


def _record_morning_card_sent():
    """PR #80: record morning card sent date AFTER successful send, not before."""
    try:
        import datetime as _dt2, pytz as _pytz2
        _ct2 = _pytz2.timezone("America/Chicago")
        _date_str = _dt2.datetime.now(_ct2).strftime("%Y-%m-%d")
        with db_conn() as _tc:
            _cur_tc = _tc.cursor()
            _cur_tc.execute(
                "INSERT INTO ghost_state(key,val) VALUES('last_morning_card_ts',%s) ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (str(int(time.time())),),
            )
            _cur_tc.execute(
                "INSERT INTO ghost_state(key,val) VALUES('last_morning_card_date',%s) ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (_date_str,),
            )
    except Exception:
        pass

_WEEKDAY_INDEX = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                  "friday": 4, "saturday": 5, "sunday": 6}


def _build_weekly_card_data() -> dict:
    """Assemble the overhauled weekly-summary payload: followed-pick P&L over the
    week (via core.pnl), all-time record, retrain countdown, top/weakest pick by
    confidence, and how many of the week's picks were news-driven."""
    import datetime as _dt, pytz as _tz
    tz = _tz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    now = int(time.time())
    cutoff = now - 7 * 86400

    pnl_trades = []
    wk_wins = wk_losses = 0
    prows = []
    try:
        with db_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT resolved_at,outcome,pnl_pct,entry_price,exit_price FROM predictions "
                "WHERE symbol='WOLF' AND resolved_at >= %s AND outcome IN ('WIN','LOSS') "
                "AND " + NON_RESEARCH_WHERE + " ORDER BY resolved_at ASC",
                (cutoff,))
            for r in cur.fetchall():
                if r[1] == "WIN":
                    wk_wins += 1
                elif r[1] == "LOSS":
                    wk_losses += 1
                if r[2] is not None:
                    pnl_trades.append({"resolved_at": r[0], "outcome": r[1],
                                       "pnl_pct": float(r[2]), "entry_price": r[3], "exit_price": r[4]})
            cur.execute(
                "SELECT predicted_at,confidence,features FROM predictions "
                "WHERE symbol='WOLF' AND predicted_at >= %s ORDER BY confidence DESC",
                (cutoff,))
            prows = cur.fetchall()
    except Exception:
        pass

    from core.pnl import realized_pnl
    pnl = realized_pnl(pnl_trades)

    def _day(ts):
        try:
            return _dt.datetime.fromtimestamp(float(ts), tz=_tz.utc).astimezone(tz).strftime("%A")
        except Exception:
            return "--"

    top = weak = {}
    news_driven = 0
    total_week = len(prows)
    if prows:
        hi, lo = prows[0], prows[-1]
        top = {"day": _day(hi[0]), "confidence_pct": int(round(float(hi[1]) * 100))}
        weak = {"day": _day(lo[0]), "confidence_pct": int(round(float(lo[1]) * 100))}
        for pr in prows:
            try:
                f = pr[2]
                if isinstance(f, str):
                    f = json.loads(f)
                if isinstance(f, dict):
                    cr = f.get("confidence_raw")
                    if cr is not None and abs(float(pr[1]) - float(cr)) > 1e-9:
                        news_driven += 1
            except Exception:
                pass

    tr = _wolf_track_record()
    wk_tot = wk_wins + wk_losses
    start = _dt.datetime.now(tz) - _dt.timedelta(days=6)
    week_range = start.strftime("%b %d") + " - " + _dt.datetime.now(tz).strftime("%b %d")
    retrain = _wolf_retrain_in_days()
    return {
        "week_range": week_range,
        "followed": {"wins": wk_wins, "losses": wk_losses,
                     "win_rate_pct": round(wk_wins / wk_tot * 100, 1) if wk_tot else 0,
                     "pnl_usd": pnl["realized_pnl_usd"]},
        "alltime": {"win_rate_pct": tr["win_rate_pct"], "wins": tr["wins"], "losses": tr["losses"]},
        "retrain_in_days": retrain if retrain is not None else "--",
        "top_pick": top,
        "weakest_pick": weak,
        "news_driven": {"count": news_driven, "total": total_week},
    }


def _weekly_summary_job():
    """Fire the weekly summary once on the configured day/hour CT (default Sunday
    6 PM). Registered hourly; an ISO-week dedup in ghost_state guarantees a single
    send per week even across restarts."""
    import datetime, pytz
    ct = pytz.timezone(os.getenv("GHOST_TZ", "America/Chicago"))
    now_ct = datetime.datetime.now(ct)
    want_day = _WEEKDAY_INDEX.get(os.getenv("TELEGRAM_WEEKLY_DAY", "sunday").strip().lower(), 6)
    try:
        want_hour = int(os.getenv("TELEGRAM_WEEKLY_HOUR", "18"))
    except Exception:
        want_hour = 18
    if not (now_ct.weekday() == want_day and now_ct.hour == want_hour):
        return  # not the configured slot

    iso = now_ct.isocalendar()
    week_tag = str(iso[0]) + "-W" + str(iso[1])
    try:
        with db_conn() as c:
            cur = c.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='last_weekly_summary_week'")
            row = cur.fetchone()
            if row and row[0] == week_tag:
                return  # already sent this ISO week
    except Exception:
        pass

    from core.telegram import send_weekly_card
    try:
        send_weekly_card(_build_weekly_card_data())
        with db_conn() as c:
            c.cursor().execute(
                "INSERT INTO ghost_state(key,val) VALUES('last_weekly_summary_week',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (week_tag,))
        LOGGER.info("Weekly summary sent (%s)", week_tag)
    except Exception as e:
        LOGGER.error("Weekly summary failed: " + str(e))


def _build_train_symbol_list():
    """Training symbol universe from STOCK_SYMBOLS + portfolio holdings."""
    from config.symbols import watchlist_symbol_pairs
    return watchlist_symbol_pairs(include_portfolio=True)


def _watchlist_missing_symbol_pairs() -> list:
    """Watchlist symbols that currently lack a loadable v3 model."""
    try:
        from core.signal_engine import get_model_status
        expected = _build_train_symbol_list()
        loaded = set((get_model_status() or {}).get("symbols", {}).keys())
        return [(sym, atype) for sym, atype in expected if sym not in loaded]
    except Exception:
        return []


def _coverage_maintenance_job():
    """
    Keep model coverage above a floor.
    If loaded model count is too low, run a rate-limited retrain pass.
    """
    global _COVERAGE_RETRAIN_RUNNING
    if os.getenv("AUTO_COVERAGE_RETRAIN_ENABLED", "1").strip() not in ("1", "true", "TRUE", "yes", "on"):
        return
    if _COVERAGE_RETRAIN_RUNNING:
        LOGGER.info("Coverage maintenance: retrain already running, skip")
        return

    min_models = max(1, int(os.getenv("MODEL_COVERAGE_MIN_MODELS", "3")))
    cooldown_s = max(900, int(os.getenv("COVERAGE_RETRAIN_COOLDOWN_SEC", "21600")))
    boot_grace_s = max(0, int(os.getenv("COVERAGE_BOOT_GRACE_SEC", "600")))
    low_yield_ratio = max(0.0, min(1.0, float(os.getenv("COVERAGE_LOW_YIELD_RATIO", "0.25"))))
    low_yield_backoff_s = max(3600, int(os.getenv("COVERAGE_LOW_YIELD_BACKOFF_SEC", "43200")))
    now = int(time.time())
    _lock_acquired = False
    if (time.time() - _APP_BOOT_TS) < boot_grace_s:
        LOGGER.info("Coverage maintenance: boot grace active, defer (%ss)", int(boot_grace_s - (time.time() - _APP_BOOT_TS)))
        return

    last_ts = 0
    low_yield_until_ts = 0
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            cur.execute("SELECT val FROM ghost_state WHERE key='last_coverage_retrain_ts'")
            row = cur.fetchone()
            last_ts = int(row[0]) if row and row[0] else 0
            cur.execute("SELECT val FROM ghost_state WHERE key='last_coverage_low_yield_until_ts'")
            row2 = cur.fetchone()
            low_yield_until_ts = int(row2[0]) if row2 and row2[0] else 0
    except Exception as e:
        LOGGER.warning("Coverage maintenance state read failed: %s", str(e)[:80])

    if low_yield_until_ts and now < low_yield_until_ts:
        LOGGER.info("Coverage maintenance: low-yield backoff active (%ss left)", low_yield_until_ts - now)
        return

    if last_ts and now - last_ts < cooldown_s:
        LOGGER.info("Coverage maintenance: cooldown active (%ss left)", cooldown_s - (now - last_ts))
        return

    try:
        from core.signal_engine import get_model_status, train_and_validate
        st = get_model_status() or {}
        loaded = int(st.get("models", 0)) if st.get("trained") else 0
        missing = _watchlist_missing_symbol_pairs()
        if loaded >= min_models and not missing:
            LOGGER.info("Coverage maintenance: loaded models %s >= floor %s, watchlist complete", loaded, min_models)
            return

        syms = missing if missing else _build_train_symbol_list()
        if not syms:
            LOGGER.warning("Coverage maintenance: empty symbol universe, skip retrain")
            return

        if not _RETRAIN_JOB_LOCK.acquire(blocking=False):
            LOGGER.info("Coverage maintenance: retrain lock busy, skip this run")
            return
        _lock_acquired = True
        _COVERAGE_RETRAIN_RUNNING = True
        LOGGER.warning(
            "Coverage maintenance: loaded=%s floor=%s missing=%s — retraining %s symbols",
            loaded, min_models, len(missing), len(syms)
        )
        _, acc_ratio, _ok = train_and_validate(syms)
        trained = int(round(acc_ratio * len(syms))) if syms else 0
        failed = len(syms) - trained
        try:
            purged = _auto_purge_bad_models()
            pv = _purge_v3_stale_or_weak()
            LOGGER.info("Coverage maintenance purge: legacy=%s v3=%s", purged, pv)
        except Exception as e:
            LOGGER.warning("Coverage maintenance purge failed: %s", str(e)[:80])
        _bump_cockpit_db_cache()
        LOGGER.info(
            "Coverage maintenance retrain complete: %s trained, %s failed (acc_ratio=%.3f)",
            trained, failed, float(acc_ratio or 0.0)
        )
        if float(acc_ratio or 0.0) < low_yield_ratio:
            try:
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO ghost_state(key,val) VALUES('last_coverage_low_yield_until_ts',%s) "
                        "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                        (str(int(time.time()) + low_yield_backoff_s),),
                    )
                LOGGER.warning(
                    "Coverage maintenance: low-yield retrain (acc_ratio=%.3f < %.3f), backoff %ss",
                    float(acc_ratio or 0.0), low_yield_ratio, low_yield_backoff_s
                )
            except Exception as e:
                LOGGER.warning("Coverage maintenance low-yield backoff write failed: %s", str(e)[:80])
    except Exception as e:
        LOGGER.warning("Coverage maintenance retrain failed: %s", str(e)[:120])
    finally:
        if _lock_acquired:
            try:
                _RETRAIN_JOB_LOCK.release()
            except Exception:
                pass
        _COVERAGE_RETRAIN_RUNNING = False
        if _lock_acquired:
            try:
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO ghost_state(key,val) VALUES('last_coverage_retrain_ts',%s) "
                        "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                        (str(int(time.time())),),
                    )
            except Exception:
                pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    LOGGER.info("Ghost Protocol v2 starting...")
    if os.getenv("GHOST_TEST_MODE", "0").strip().lower() in ("1", "true", "yes", "on"):
        LOGGER.info("Ghost Protocol v2 test mode startup: skipping DB init and schedulers")
        yield
        return
    init_db()
    try:
        from core.news_store import ensure_news_tables
        ensure_news_tables()
    except Exception as _nte:
        LOGGER.warning("News tables init failed: " + str(_nte)[:80])
    # Purge weak / legacy-schema models on startup
    try:
        purged = _auto_purge_bad_models()
        if purged: LOGGER.info(f"Boot purge: removed {purged} legacy ghost_models below floor")
        pv = _purge_v3_stale_or_weak()
        if pv: LOGGER.info(f"Boot v3 purge: removed {pv} stale or sub-floor TP/SL models")
        expired_orphans = _expire_open_picks_without_v3_model()
        if expired_orphans:
            LOGGER.info("Boot pick cleanup: expired %s active picks with no model", expired_orphans)
    except Exception as _bpe:
        LOGGER.warning("Boot purge failed: "+str(_bpe)[:60])

    # PR #26: auto-purge ghost/test rows from user_portfolio on every boot.
    # The /admin "Purge Ghost Portfolio" button (PR #23) was never run —
    # the ZZE2E* probe-ticker rows persisted. Self-healing: deletes rows
    # matching the ghost patterns on each startup so they can't pollute
    # the investor portfolio totals. Legit WOLF (and any deliberately-added
    # non-ghost symbol) is untouched.
    try:
        with db_conn() as _pc:
            _pcur = _pc.cursor()
            _pcur.execute("SELECT id, symbol FROM user_portfolio")
            _prows = _pcur.fetchall()
            _purged_ids = []
            for _rid, _sym in _prows:
                _up = str(_sym or "").strip().upper()
                if any(_up.startswith(p) or _up == p for p in _GHOST_PORTFOLIO_PATTERNS):
                    _pcur.execute("DELETE FROM user_portfolio WHERE id=%s", (int(_rid),))
                    _purged_ids.append(_rid)
            if _purged_ids:
                LOGGER.info("Boot portfolio purge: removed %s ghost rows %s",
                            len(_purged_ids), _purged_ids[:10])
    except Exception as _ppe:
        LOGGER.warning("Boot portfolio purge failed: " + str(_ppe)[:80])

    # Self-healing: if app restarts within the morning card window (TELEGRAM_DAILY_HOUR
    # .. +4h CT) and last card was >8h ago, fire now. Prevents silent card misses
    # when Railway restarts during the cron window.
    try:
        import datetime as _sdt, pytz as _stz
        _ct = _stz.timezone("America/Chicago")
        _now_ct = _sdt.datetime.now(_ct)
        _hour_ct = _now_ct.hour
        try:
            _daily_hour = int(os.getenv("TELEGRAM_DAILY_HOUR", "8"))
        except Exception:
            _daily_hour = 8
        if _daily_hour <= _hour_ct < _daily_hour + 4:  # morning window
            with db_conn() as _sc:
                _scur = _sc.cursor()
                _scur.execute("SELECT val FROM ghost_state WHERE key='last_morning_card_ts'")
                _row = _scur.fetchone()
                _last_ts = int(_row[0]) if _row else 0
                _hours_ago = (time.time() - _last_ts) / 3600
            if _hours_ago > 8:
                LOGGER.warning(f"Startup recovery: last card {_hours_ago:.1f}h ago, firing now (hour={_hour_ct} CT)")
                import asyncio as _aio
                _aio.get_event_loop().run_in_executor(None, _morning_card_job)
    except Exception as _se:
        LOGGER.warning(f"Startup card recovery failed: {_se}")

    from core import scheduler
    from core.prediction import reconcile_outcomes
    from core.news import run_news_cycle
    scheduler.register("morning_card", _morning_card_job, interval_s=86400)
    # Market-hours scan loop (roadmap #3a): tick at the market interval; the job
    # self-gates to SCAN_INTERVAL_MARKET_MIN / SCAN_INTERVAL_OFFHOURS_MIN.
    try:
        _scan_tick = max(300, int(os.getenv("SCAN_INTERVAL_MARKET_MIN", "30")) * 60)
    except Exception:
        _scan_tick = 1800
    scheduler.register("market_scan", _market_scan_job, interval_s=_scan_tick)
    # Watchdog: real-time hit alerts every 5 minutes
    from core.watchdog import run_watchdog
    scheduler.register("watchdog", run_watchdog, interval_s=300)
    # Weekly summary: every Friday at 4 PM CT = 22:00 UTC = 79200s from midnight
    # Approximated as 7-day interval - fires on first Friday after deploy
    scheduler.register("weekly_summary", _weekly_summary_job, interval_s=3600)
    # Daily summary (roadmap #3b): hourly tick, fires once/day at DAILY_SUMMARY_HOUR.
    scheduler.register("daily_summary", _daily_summary_job, interval_s=3600)
    from core.squeeze_outcomes import run_squeeze_eod_job as _squeeze_eod_job

    scheduler.register("squeeze_eod", _squeeze_eod_job, interval_s=3600)
    # PR #84: Super Ghost Truth Ledger — resolve logged predictions vs realized
    # price at 1/5/20-day horizons so accuracy + if-followed stats accrue.
    from core.super_ghost_ledger import run_resolver_job as _super_ghost_resolver

    def _super_ghost_ledger_job():
        try:
            _super_ghost_resolver()
        except Exception as _e:
            LOGGER.warning("super ghost ledger job failed: %s", str(_e)[:80])

    scheduler.register("super_ghost_ledger", _super_ghost_ledger_job, interval_s=3600)
    scheduler.register("reconcile", reconcile_outcomes, interval_s=900)
    # T19: Auto-refresh portfolio stock prices every 15 min
    from core.portfolio_routes import auto_refresh_portfolio_prices
    scheduler.register("portfolio_price_refresh", auto_refresh_portfolio_prices, interval_s=900)
    from core.risk_discipline import run_risk_discipline_cycle

    def _risk_discipline_job():
        try:
            run_risk_discipline_cycle(notify=True)
        except Exception as _e:
            LOGGER.warning("risk discipline job failed: %s", str(_e)[:80])

    scheduler.register("risk_discipline", _risk_discipline_job, interval_s=300)
    scheduler.register("news", run_news_cycle, interval_s=1800)
    # PR #134: structured news-event ingestion (Alpaca+Finnhub → typed events)
    # and the defensive tripwire (dark until NEWS_DEFENSE_ENABLED=1).
    from core.news_ingest import run_news_ingest_cycle as _news_ingest_cycle
    from core.news_defense import run_defense_check as _news_defense_check

    def _news_ingest_job():
        try:
            _news_ingest_cycle()
        except Exception as _e:
            LOGGER.warning("news ingest job failed: %s", str(_e)[:100])

    def _news_defense_job():
        try:
            _news_defense_check()
        except Exception as _e:
            LOGGER.warning("news defense job failed: %s", str(_e)[:100])

    scheduler.register("news_event_ingest", _news_ingest_job, interval_s=900)
    scheduler.register("news_defense", _news_defense_job, interval_s=300)
    # PR #138: paper wallet — fake-money mirror of Ghost's signals (gated +
    # shadow books) so fill-level evidence accrues with zero real risk.
    from core.paper_wallet import run_wallet_cycle as _paper_wallet_cycle

    def _paper_wallet_job():
        try:
            _paper_wallet_cycle()
        except Exception as _e:
            LOGGER.warning("paper wallet job failed: %s", str(_e)[:100])

    scheduler.register("paper_wallet", _paper_wallet_job, interval_s=300)
    # Shadow scoring: resolve every silenced model eval as a virtual pick so
    # per-symbol live hit rates accrue without firing (core.shadow_outcomes).
    from core.shadow_outcomes import run_shadow_cycle as _shadow_cycle

    def _shadow_outcomes_job():
        try:
            _shadow_cycle()
        except Exception as _e:
            LOGGER.warning("shadow outcomes job failed: %s", str(_e)[:80])

    scheduler.register("shadow_outcomes", _shadow_outcomes_job, interval_s=3600)
    # Coverage maintenance: if too few loadable v3 models, run rate-limited retrain.
    scheduler.register(
        "coverage_maintenance",
        _coverage_maintenance_job,
        interval_s=max(900, int(os.getenv("COVERAGE_CHECK_INTERVAL_SEC", "3600"))),
    )
    # Weekly model retrain — keeps models fresh as market conditions change
    from core.signal_engine import train_and_validate as _tv
    def _weekly_retrain():
        _lock_acquired = False
        try:
            if not _RETRAIN_JOB_LOCK.acquire(blocking=False):
                LOGGER.info("Weekly retrain skipped: retrain lock busy")
                return
            _lock_acquired = True
            min_interval_s = max(3600, int(os.getenv("WEEKLY_RETRAIN_MIN_INTERVAL_SEC", "604800")))
            now_ts = int(time.time())
            last_ts = 0
            try:
                with db_conn() as _wc:
                    _wcur = _wc.cursor()
                    ensure_ghost_state(_wcur)
                    _wcur.execute("SELECT val FROM ghost_state WHERE key='last_weekly_retrain_ts'")
                    _wr = _wcur.fetchone()
                    last_ts = int(_wr[0]) if _wr and _wr[0] else 0
            except Exception as _wse:
                LOGGER.warning("Weekly retrain state read failed: %s", str(_wse)[:80])
            if last_ts and (now_ts - last_ts) < min_interval_s:
                LOGGER.info(
                    "Weekly retrain skipped: last run %ss ago (<%ss)",
                    now_ts - last_ts, min_interval_s
                )
                return
            try:
                with db_conn() as _wc2:
                    _wcur2 = _wc2.cursor()
                    _wcur2.execute(
                        "INSERT INTO ghost_state(key,val) VALUES('last_weekly_retrain_ts',%s) "
                        "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                        (str(now_ts),),
                    )
            except Exception as _wse2:
                LOGGER.warning("Weekly retrain state write failed: %s", str(_wse2)[:80])
            from core.prediction import STOCK_SYMBOLS
            syms = _v3_train_collect_symbols()
            trained, failed = 0, len(syms)
            try:
                # train_and_validate expects one list of (symbol, asset_type), not per-symbol calls
                _, acc_ratio, _ok = _tv(syms)
                trained = int(round(acc_ratio * len(syms))) if syms else 0
                failed = len(syms) - trained
            except Exception as _e:
                LOGGER.warning("Weekly retrain failed: " + str(_e)[:80])
            LOGGER.info("Weekly retrain complete: " + str(trained) + " trained, " + str(failed) + " failed")
            try:
                purged = _auto_purge_bad_models()
                pv = _purge_v3_stale_or_weak()
                LOGGER.info("Weekly retrain purge: legacy=%s v3=%s", purged, pv)
            except Exception as _pe:
                LOGGER.warning("Weekly purge failed: "+str(_pe)[:60])
        except Exception as _e:
            LOGGER.warning("Weekly retrain error: "+str(_e)[:80])
        finally:
            if _lock_acquired:
                try:
                    _RETRAIN_JOB_LOCK.release()
                except Exception:
                    pass
    scheduler.register("weekly_retrain", _weekly_retrain, interval_s=604800)
    scheduler.start()
    # Intraday monitors (must run in lifespan — engines/startup._on_startup is not invoked).
    import asyncio as _aio
    _bg_loop = _aio.get_running_loop()
    try:
        from core.wolf_monitor import start_wolf_monitor
        _bg_loop.create_task(start_wolf_monitor())
        LOGGER.info("[GHOST STARTUP] WOLF autonomous monitor started")
    except Exception as _wme:
        LOGGER.warning("WOLF monitor start failed: %s", str(_wme)[:80])
    try:
        from core.squeeze_monitor import start_squeeze_monitor
        _bg_loop.create_task(start_squeeze_monitor())
        LOGGER.info("[GHOST STARTUP] Watchlist squeeze monitor started (44 symbols)")
    except Exception as _sqe:
        LOGGER.warning("Squeeze monitor start failed: %s", str(_sqe)[:80])
    # Ghost v3: auto-train on startup if no model in DB
    def _startup_train():
        _lock_acquired = False
        try:
            from core.signal_engine import train_and_validate
            import os
            if not _has_loadable_v3_model():
                if not _RETRAIN_JOB_LOCK.acquire(blocking=False):
                    LOGGER.info("Startup training skipped: retrain lock busy")
                    return
                _lock_acquired = True
                LOGGER.info("No loadable v3.2 TP/SL model found — training on startup...")
                _record_v3_train_state(
                    ts=int(time.time()), state="started", force="startup",
                    accuracy="", passed="", error="", models_before="", models_after="",
                )
                stocks = _v3_train_collect_symbols()
                _record_v3_train_state(state="running", stocks=str(stocks))
                m, acc, passed = train_and_validate(stocks)
                LOGGER.info(f"Startup training: acc={round((acc or 0)*100,1)}% passed={passed}")
                _record_v3_train_state(
                    state="passed" if passed else "failed",
                    accuracy=f"{(acc or 0):.4f}", passed=str(bool(passed)).lower(),
                    finished_at=int(time.time()), error="",
                )
                try:
                    purged = _auto_purge_bad_models()
                    pv = _purge_v3_stale_or_weak()
                    LOGGER.info(f"Startup purge: legacy={purged} v3={pv}")
                except Exception as _spe:
                    LOGGER.warning("Startup purge failed: "+str(_spe)[:60])
            else:
                LOGGER.info("v3 TP/SL model loaded from DB — ready")
                try:
                    purged = _auto_purge_bad_models()
                    pv = _purge_v3_stale_or_weak()
                    if purged or pv:
                        LOGGER.info(f"Startup cleanup: legacy={purged} v3={pv}")
                except Exception:
                    pass
                missing = _watchlist_missing_symbol_pairs()
                if missing and _RETRAIN_JOB_LOCK.acquire(blocking=False):
                    _lock_acquired = True
                    LOGGER.warning(
                        "Startup coverage gap: %s watchlist symbols missing models — training",
                        len(missing),
                    )
                    _record_v3_train_state(
                        ts=int(time.time()), state="started", force="startup_missing",
                        accuracy="", passed="", error="", models_before="", models_after="",
                    )
                    _record_v3_train_state(state="running", stocks=str(missing))
                    m, acc, passed = train_and_validate(missing)
                    LOGGER.info(
                        "Startup missing-model training: acc=%s%% passed=%s symbols=%s",
                        round((acc or 0) * 100, 1), passed, len(missing),
                    )
                    _record_v3_train_state(
                        state="passed" if passed else "failed",
                        accuracy=f"{(acc or 0):.4f}", passed=str(bool(passed)).lower(),
                        finished_at=int(time.time()), error="",
                    )
                    try:
                        purged = _auto_purge_bad_models()
                        pv = _purge_v3_stale_or_weak()
                        if purged or pv:
                            LOGGER.info(f"Post-startup-missing purge: legacy={purged} v3={pv}")
                    except Exception:
                        pass
        except Exception as _te:
            LOGGER.warning("Startup training failed: " + str(_te))
            try:
                _record_v3_train_state(
                    state="exception", error=str(_te)[:300], finished_at=int(time.time()),
                )
            except Exception:
                pass
        finally:
            if _lock_acquired:
                try:
                    _RETRAIN_JOB_LOCK.release()
                except Exception:
                    pass
    import threading as _th
    _th.Thread(target=_startup_train, daemon=True).start()
    LOGGER.info("Ghost Protocol v2 ready.")
    yield
    scheduler.stop()

# Security (audit): /docs (Swagger UI), /redoc, and the OpenAPI schema are
# disabled unless DOCS_ENABLED is explicitly truthy. When the schema IS exposed,
# every /api/admin/* route sets include_in_schema=False so destructive endpoints
# never appear in openapi.json or "Try it out".
_DOCS_ENABLED = os.getenv("DOCS_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
APP = FastAPI(
    title="Ghost Protocol v2", version=APP_VERSION, lifespan=lifespan,
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)
# CORS: the cockpit/picks pages are served same-origin, so no origin needs
# cross-site access by default. GHOST_CORS_ORIGINS (comma-separated) can widen
# it; "*" keeps the legacy wildcard. Auth is bearer/cookie(SameSite=Lax) and
# allow_credentials stays False, so this is exposure-narrowing, not auth.
_CORS_ORIGINS = [o.strip() for o in os.getenv("GHOST_CORS_ORIGINS", "*").split(",") if o.strip()]
APP.add_middleware(CORSMiddleware, allow_origins=_CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


# ── Global exception handlers (audit: DB outage → 503, bad input → 422) ──
# Without these, a Postgres outage or a hand-parsed bad query param surfaces
# as an opaque 500 from every endpoint that touches the failure.
try:
    import psycopg2 as _psycopg2

    @APP.exception_handler(_psycopg2.OperationalError)
    @APP.exception_handler(_psycopg2.InterfaceError)
    async def _db_unavailable_handler(request: Request, exc: Exception):
        LOGGER.error("[db] unavailable on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "database_unavailable",
                     "detail": "Database is unreachable. Retry shortly."},
        )
except Exception:  # psycopg2 absent in some test contexts
    pass


# A ValueError is only client input if it was raised in a route-handler file
# (hand-parsed query params: int(...), float(...), date parsing). One raised
# deeper — core/, engines, third-party libs — is an internal bug and must stay
# a 500 with a logged traceback, not masquerade as "invalid_input" (PR #131).
_ROUTE_FILE_MARKERS = ("wolf_app.py", f"{os.sep}api{os.sep}", "portfolio_routes.py")


def _valueerror_origin_is_route(exc: BaseException) -> bool:
    tb = exc.__traceback__
    origin = ""
    while tb:
        origin = tb.tb_frame.f_code.co_filename
        tb = tb.tb_next
    return any(m in origin for m in _ROUTE_FILE_MARKERS)


@APP.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError):
    if _valueerror_origin_is_route(exc):
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": "invalid_input", "detail": str(exc)[:300]},
        )
    LOGGER.error("unhandled ValueError on %s", request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "internal_error",
                 "detail": "Unexpected server error. The failure has been logged."},
    )


# ── Public-endpoint rate limiting (audit) ────────────────────────────────
# In-process per-IP sliding window (60s). The app runs single-instance on
# Railway, so process-local state is sufficient. Admin/cron routes have their
# own auth and are exempt; /api/health is exempt for uptime monitors.
import collections as _collections

_RL_LOCK = threading.Lock()
_RL_HITS = _collections.defaultdict(_collections.deque)  # ip -> deque[ts]
_RL_EXEMPT_PREFIXES = ("/api/admin", "/api/cron", "/api/v3/train")
_RL_EXEMPT_PATHS = ("/api/health",)


def _rate_limit_cfg():
    enabled = os.getenv("RATE_LIMIT_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
    try:
        rpm = max(1, int(os.getenv("RATE_LIMIT_RPM", "300")))
    except Exception:
        rpm = 120
    return enabled, rpm


def _client_ip(request: Request) -> str:
    # PR #77: prefer request.client.host (set by Railway's trusted proxy).
    # Only fall back to X-Forwarded-For when client.host is unavailable.
    # This prevents XFF spoofing from bypassing the per-IP rate limiter.
    if request.client and request.client.host:
        return request.client.host
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return "unknown"


@APP.middleware("http")
async def _rate_limit_mw(request: Request, call_next):
    enabled, rpm = _rate_limit_cfg()
    path = request.url.path
    if (enabled and request.method != "OPTIONS" and path.startswith("/api/")
            and not path.startswith(_RL_EXEMPT_PREFIXES) and path not in _RL_EXEMPT_PATHS):
        ip = _client_ip(request)
        now = time.time()
        with _RL_LOCK:
            dq = _RL_HITS[ip]
            cutoff = now - 60
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= rpm:
                retry = int(60 - (now - dq[0])) + 1
                return JSONResponse(
                    {"ok": False, "error": "rate_limited", "retry_after_s": retry},
                    status_code=429, headers={"Retry-After": str(retry)})
            dq.append(now)
            # Bound memory: drop emptied buckets when the table grows large.
            if len(_RL_HITS) > 4096:
                for _k in [k for k, v in list(_RL_HITS.items()) if not v]:
                    _RL_HITS.pop(_k, None)
    return await call_next(request)


# ── Security headers + CSP (audit v2 #6/#7) ──────────────────────────────
# CSP allows the cockpit's CDN (Chart.js from jsdelivr) and the inline
# <style>/<script>/onclick the pages rely on ('unsafe-inline'); frame-ancestors
# 'none' + X-Frame-Options DENY block clickjacking. HSTS only on HTTPS.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


@APP.middleware("http")
async def _security_headers_mw(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


# P3-5 (audit): latency SLO tracking middleware — records p50/p95/p99 per route.
# Excludes long-running training endpoints (v3_train, v3_train_sync) which
# would inflate p95/p99 and make SLOs meaningless for normal API routes.
_SLO_EXCLUDE_PREFIXES = ("/api/v3/train",)

@APP.middleware("http")
async def _latency_slo_mw(request: Request, call_next):
    t0 = time.time()
    resp = await call_next(request)
    elapsed_ms = (time.time() - t0) * 1000
    try:
        path = request.url.path
        if not any(path.startswith(p) for p in _SLO_EXCLUDE_PREFIXES):
            from core.latency_slo import record
            record(path, elapsed_ms)
    except Exception:
        pass
    return resp


# ── Static/SEO + version routes (audit v2 #1/#2/#3/#9) ───────────────────
_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://ghost-protocol-v2-production.up.railway.app").rstrip("/")


@APP.get("/robots.txt", include_in_schema=False)
def robots_txt():
    body = ("User-agent: *\n"
            "Allow: /\n"
            "Allow: /cockpit\n"
            "Disallow: /admin\n"
            "Disallow: /api/\n"
            "Sitemap: " + _BASE_URL + "/sitemap.xml\n")
    return Response(content=body, media_type="text/plain")


@APP.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml():
    urls = ["/", "/picks", "/cockpit"]
    items = "".join("<url><loc>" + _BASE_URL + u + "</loc></url>" for u in urls)
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + items + "</urlset>")
    return Response(content=body, media_type="application/xml")


@APP.get("/version")
def version_public():
    """Public deploy-metadata endpoint (audit v2 #1). Same payload as
    /api/_version — app version + Railway git/deploy IDs for one-curl checks."""
    return deploy_version()


@APP.get("/api/v1/ghost-score")
async def v1_ghost_score():
    """Stable /api/v1 alias for the WOLF Ghost Score (audit v2 #9)."""
    from api.wolf_endpoints import get_ghost_score
    return await get_ghost_score()


# Mount portfolio router — WOLF position tracking, price refresh, ghost predictions
from core.portfolio_routes import portfolio_router
from core.stats_direction import compute_stats_by_direction
APP.include_router(portfolio_router)

# Phase 4: WOLF Intel endpoints
try:
    from api.wolf_endpoints import router as wolf_router
    APP.include_router(wolf_router)
    LOGGER.info("[INIT] WOLF Intel endpoints loaded")
except Exception as _we:
    LOGGER.warning(f"[INIT] wolf_endpoints unavailable: {_we}")

try:
    from mcp.routes import router as mcp_router
    APP.include_router(mcp_router)
    LOGGER.info("[INIT] Ghost MCP Phase 1.6 routes loaded at /mcp")
except Exception as _mcp:
    LOGGER.warning(f"[INIT] MCP routes unavailable: {_mcp}")

try:
    from mcp.oauth_routes import router as oauth_router
    APP.include_router(oauth_router)
    LOGGER.info("[INIT] Ghost MCP OAuth discovery loaded")
except Exception as _oauth:
    LOGGER.warning(f"[INIT] MCP OAuth routes unavailable: {_oauth}")






def _auto_purge_bad_models():
    """Purge all sub-52% accuracy models from DB. Called after every retrain."""
    try:
        MIN_ACC = 0.52
        from core.db import db_conn as _dbc
        import json as _j
        with _dbc() as _c:
            cur = _c.cursor()
            # Legacy table may not exist on newer deployments; skip quietly if absent.
            cur.execute("SELECT to_regclass('public.ghost_models')")
            reg = cur.fetchone()
            if not reg or not reg[0]:
                return 0
            cur.execute("SELECT id, symbol, metadata FROM ghost_models")
            rows = cur.fetchall()
            purged = 0
            for rid, sym, meta in rows:
                try:
                    m = _j.loads(meta) if isinstance(meta, str) else (meta or {})
                    acc = float(m.get('accuracy', 1.0))
                    if acc < MIN_ACC:
                        cur.execute("DELETE FROM ghost_models WHERE id=%s", (rid,))
                        purged += 1
                except Exception: pass
        return purged
    except Exception: return 0




_GHOST_PORTFOLIO_PATTERNS = ("ZZE2E", "STOCK GHOST", "GHOST", "ZZ", "TEST")




# Synthetic/test symbols that pollute the predictions ledger (e2e roundtrips
# create ZZE2E<ts> rows; ZZ/TEST/GHOST are manual probes). Real tickers never
# match these prefixes. user_portfolio is already self-healed on boot; this
# covers the predictions table, which feeds stats and the pick journal.
_TEST_PREDICTION_PATTERNS = ("ZZE2E%", "ZZ%", "TEST%", "GHOST%", "STOCK GHOST%")








def health():
    import os, time as _t
    from core.prices import check_feeds
    from core import scheduler
    issues = []
    warnings = []

    # 1. DB
    db_ok = False
    try:
        with db_conn() as conn: conn.cursor().execute("SELECT 1")
        db_ok = True
    except Exception as e:
        issues.append("DB failed: " + str(e)[:60])

    # 2. Price feeds
    feeds = {"alpaca_stock": False, "yfinance": False, "summary": "0/2 feeds responding"}
    try:
        feeds = check_feeds()
        feeds_ok = sum(1 for k,v in feeds.items() if k != "summary" and v)
        if feeds_ok < 2:
            warnings.append(feeds.get("summary", "<2 feeds responding"))
    except Exception as _fe:
        LOGGER.warning("health.check_feeds failed: " + str(_fe)[:120])

    # 3. Prediction freshness vs cycle freshness
    freshness_min = None
    cycle_freshness_min = None
    cycle_last_saved = None
    cycle_last_scanned = None
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT predicted_at FROM predictions WHERE predicted_at IS NOT NULL ORDER BY predicted_at DESC LIMIT 1")
            row = cur.fetchone()
            if row and row[0]:
                freshness_min = int((_t.time() - float(row[0])) / 60)
            cur.execute("SELECT val FROM ghost_state WHERE key='last_prediction_cycle_ts'")
            cyc = cur.fetchone()
            if cyc and cyc[0]:
                cycle_freshness_min = int((_t.time() - float(cyc[0])) / 60)
            cur.execute("SELECT val FROM ghost_state WHERE key='last_prediction_cycle_saved'")
            cyc_saved = cur.fetchone()
            if cyc_saved and cyc_saved[0] is not None:
                cycle_last_saved = int(cyc_saved[0])
            cur.execute("SELECT val FROM ghost_state WHERE key='last_prediction_cycle_scanned'")
            cyc_scan = cur.fetchone()
            if cyc_scan and cyc_scan[0] is not None:
                cycle_last_scanned = int(cyc_scan[0])

        cycle_stale_min = max(60, int(os.getenv("PREDICTION_CYCLE_STALE_MIN", "2160")))  # default 36h
        if cycle_freshness_min is None:
            warnings.append("Prediction cycle heartbeat missing")
        elif cycle_freshness_min > cycle_stale_min:
            issues.append("Prediction cycle stale: " + str(cycle_freshness_min) + "m")

        # No-pick periods are normal when gates block trades; do not hard-fail if cycle is alive.
        if freshness_min and freshness_min > 2880:
            if cycle_freshness_min is not None and cycle_freshness_min <= cycle_stale_min:
                warnings.append("No picks inserted recently: " + str(freshness_min) + "m (cycle alive)")
            else:
                issues.append("Predictions stale: " + str(freshness_min) + "m")
    except Exception as _pe:
        LOGGER.warning("health.prediction_freshness_block failed: " + str(_pe)[:120])

    # 4. Telegram
    tg_ok = bool(os.getenv("TELEGRAM_BOT_TOKEN","") and os.getenv("TELEGRAM_CHAT_ID",""))
    if not tg_ok:
        issues.append("Telegram credentials missing")

    # 5. Open picks + dedup
    open_picks = 0
    dedup_blocked = False
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM predictions WHERE outcome IS NULL AND expires_at > %s", (_t.time(),))
            open_picks = cur.fetchone()[0]
        total_syms = len([s for s in os.getenv("STOCK_SYMBOLS","WOLF").split(",") if s.strip()]) or 1
        if open_picks >= total_syms > 0:
            dedup_blocked = True
            warnings.append("Dedup blocking all " + str(total_syms) + " symbols")
        if dedup_blocked:
            try:
                with db_conn() as _fc:
                    _fc.cursor().execute(
                        "UPDATE predictions SET outcome='EXPIRED', resolved_at=%s WHERE outcome IS NULL AND predicted_at < %s",
                        (int(_t.time()), int(_t.time() - 50*3600))
                    )
            except Exception as _de:
                LOGGER.warning("health.dedup_expire_update failed: " + str(_de)[:120])
    except Exception as _oe:
        LOGGER.warning("health.open_picks_block failed: " + str(_oe)[:120])

    # 6. Confidence floor
    conf_floor = float(os.getenv("MIN_ALERT_CONFIDENCE","0.75"))
    if conf_floor < 0.70:
        warnings.append("Confidence floor " + str(conf_floor) + " is low")

    # 7. Tasks
    tasks = []
    last_card_min = None
    try:
        tasks = scheduler.status()
        mc = next((t for t in tasks if t["name"] == "morning_card"), None)
        if mc:
            last_card_min = int(mc.get("last_run_ago_s", 0) / 60)
            if last_card_min > 1440:
                issues.append("Morning card last ran " + str(last_card_min) + "m ago")
    except Exception as _se:
        LOGGER.warning("health.scheduler_status failed: " + str(_se)[:120])

    # 8. Degraded mode (P3 audit)
    degraded = False
    degraded_reasons = []
    try:
        from core.degraded_mode import check_degraded
        d = check_degraded()
        degraded = d.get("degraded", False)
        if degraded:
            degraded_reasons = d.get("reasons", [])
            warnings.append("DEGRADED MODE: " + ", ".join(degraded_reasons[:3]))
    except Exception:
        pass

    # 8b. Auto-recover stale breakers (PR #114)
    try:
        from core.circuit_breaker import auto_recover_breakers
        ar = auto_recover_breakers()
        if ar.get("recovered"):
            warnings.append("Auto-recovered breakers: " + ", ".join(ar["recovered"]))
    except Exception:
        pass

    # 9. Dead-letter queue (P1-2 audit)
    dead_letter_count = 0
    try:
        from core.telegram import get_dead_letter_queue
        dlq = get_dead_letter_queue()
        dead_letter_count = len(dlq)
        if dead_letter_count > 0:
            warnings.append(f"Telegram dead-letter queue: {dead_letter_count} undelivered alerts")
    except Exception:
        pass

    score = max(0, min(100, 100 - len(issues)*20 - len(warnings)*5))
    status_str = "healthy" if score >= 80 and not issues else "degraded" if score >= 50 else "critical"
    return {
        "status": status_str, "score": score, "db": db_ok,
        "telegram_configured": tg_ok, "predictions_freshness_min": freshness_min,
        "prediction_cycle_freshness_min": cycle_freshness_min,
        "last_prediction_cycle_saved": cycle_last_saved,
        "last_prediction_cycle_scanned": cycle_last_scanned,
        "open_picks": open_picks, "dedup_blocked": dedup_blocked,
        "last_morning_card_min": last_card_min, "confidence_floor": conf_floor,
        "price_feeds": feeds, "tasks": tasks, "issues": issues, "warnings": warnings,
        "degraded": degraded, "degraded_reasons": degraded_reasons,
        "dead_letter_count": dead_letter_count,
    }

def _health_public():
    """Slim public health (audit v2 #10): liveness only — no internals
    (telegram config, confidence floor, dedup, freshness, tasks, price feeds).
    Full detail moved to the cookie-gated /admin/health."""
    full = health()
    return {"status": full.get("status"), "score": full.get("score"), "ts": int(time.time())}


@APP.get("/health")
def health_public_route():
    return _health_public()


@APP.get("/api/health")
def api_health():
    """Public liveness probe for external monitors — slimmed (no internals)."""
    return _health_public()




@APP.post("/api/health/audit")
def health_audit(x_cron_secret: str = Header(default=""), auto_fix: bool = True):
    """
    Deep reliability audit with persistent findings and optional auto-fix hooks.

    Returns structured PASS/FAIL records for each check:
    status, location, evidence, impact, auto_fix, fix_result.
    """
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)

    import asyncio as _asyncio
    from core.health_audit import run_health_audit

    stage = "init"
    try:
        stage = "health"
        h = health()

        stage = "diagnostics"
        d = {"score": 0, "checks_passed": 0, "warnings": 0, "errors": 1, "details": {"errors": [{"check": "diagnostics.fallback", "detail": "diagnostics fallback used"}]}}
        try:
            # Avoid creating an un-awaited coroutine if we are already inside a running loop.
            _loop_running = False
            try:
                _asyncio.get_running_loop()
                _loop_running = True
            except RuntimeError:
                _loop_running = False
            if _loop_running:
                d = {
                    "score": 0,
                    "checks_passed": 0,
                    "warnings": 0,
                    "errors": 1,
                    "details": {"errors": [{"check": "diagnostics.loop", "detail": "running loop detected; fallback diagnostics used"}]},
                }
            else:
                d = _asyncio.run(diagnostics())
        except Exception as _de:
            d = {
                "score": 0,
                "checks_passed": 0,
                "warnings": 0,
                "errors": 1,
                "details": {"errors": [{"check": "diagnostics.error", "detail": str(_de)[:160]}]},
            }

        stage = "stats"
        try:
            with db_conn() as conn:
                cur = conn.cursor()
                s = _compute_get_stats(cur)
        except Exception as _se:
            s = {
                "ok": False,
                "wins": 0,
                "losses": 0,
                "total": 0,
                "open_positions": 0,
                "error": "stats_unavailable: " + str(_se)[:120],
            }

        stage = "cockpit"
        try:
            c = cockpit_context()
            if isinstance(c, JSONResponse):
                c = {"ok": False, "error": "cockpit_context returned JSONResponse error"}
        except Exception as _ce:
            c = {"ok": False, "error": "cockpit_context_failed: " + str(_ce)[:120]}

        stage = "audit"
        report = run_health_audit(
            app=APP,
            db_conn=db_conn,
            health_payload=h,
            diagnostics_payload=d,
            stats_payload=s,
            cockpit_payload=c,
            auto_fix=bool(auto_fix),
        )
        return {"ok": True, "audit": report}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200], "stage": stage}, status_code=500)


@APP.get("/api/health/audit/history")
def health_audit_history(limit: int = 20):
    """Persistent audit run history for recurrence analysis."""
    lim = max(1, min(200, int(limit)))
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS health_audit_runs (
                    id SERIAL PRIMARY KEY,
                    run_ts BIGINT NOT NULL,
                    status TEXT NOT NULL,
                    coverage_pct FLOAT NOT NULL,
                    unresolved_count INT NOT NULL,
                    resolved_count INT NOT NULL,
                    payload JSONB NOT NULL
                )
                """
            )
            cur.execute(
                """
                SELECT id, run_ts, status, coverage_pct, unresolved_count, resolved_count
                FROM health_audit_runs
                ORDER BY id DESC
                LIMIT %s
                """,
                (lim,),
            )
            rows = cur.fetchall()
        out = [
            {
                "id": int(r[0]),
                "run_ts": int(r[1]),
                "status": r[2],
                "coverage_pct": float(r[3]),
                "unresolved_count": int(r[4]),
                "resolved_count": int(r[5]),
            }
            for r in rows
        ]
        return {"ok": True, "runs": out}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)









def _norm_pred(r):
    _conf = r.get("confidence") or r.get("confidence_score") or 0
    if _conf >= 0.90:   _pos = 5.0
    elif _conf >= 0.85: _pos = 4.0
    elif _conf >= 0.80: _pos = 3.0
    elif _conf >= 0.75: _pos = 2.0
    else:               _pos = 1.0
    return {
        "id": r.get("id"),
        "symbol": r.get("symbol",""),
        "direction": r.get("direction",""),
        "confidence": _conf,
        "pos_size_pct": _pos,
        "entry_price": r.get("entry_price") or r.get("entry") or 0,
        "target_price": r.get("target_price") or r.get("target") or 0,
        "stop_price": r.get("stop_price") or r.get("stop") or 0,
        "predicted_at": r.get("predicted_at") or r.get("run_at") or 0,
        "expires_at": r.get("expires_at") or 0,
        "outcome": r.get("outcome") or r.get("result"),
        "exit_price": r.get("exit_price"),
        "pnl_pct": r.get("pnl_pct") or r.get("pnl"),
        "asset_type": r.get("asset_type","stock"),
    }



def _is_wolf_relevant(a: dict) -> bool:
    """True only if the article TEXT mentions WOLF/Wolfspeed/SiC. The Finnhub
    company-news feed tags every article ['WOLF'] including market-roundup
    pieces about other tickers, so the tag is unreliable — trust the text."""
    title = a.get("title") or a.get("headline") or ""
    body = a.get("summary") or a.get("description") or ""
    blob = (title + " " + body).upper()
    words = set(blob.replace(",", " ").replace(".", " ").replace(":", " ")
                .replace(";", " ").replace("(", " ").replace(")", " ").split())
    return ("WOLFSPEED" in blob or "WOLF" in words or "SIC" in words
            or "SILICON CARBIDE" in blob)












@APP.get("/api/diag/data-sources")
def diag_data_sources(x_cron_secret: str = Header(default=""), symbol: str = "WOLF", period: str = "1y"):
    """Probe each OHLCV data source independently and report results.

    Lets you see in-browser exactly which sources return bars and which
    fail, without grep'ing training logs. Mirrors the chain order in
    core/signal_engine._fetch_ohlcv (Alpaca SIP → IEX → Polygon → yfinance
    → Stooq).

    Each entry includes: ok, bar count, first/last timestamp on success,
    error string on failure, and request latency in ms.
    """
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)

    try:
        from core.signal_engine import (
            _try_polygon_ohlcv,
            _try_yfinance_ohlcv,
            _try_stooq_ohlcv,
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": "import failed: " + str(e)[:200]}, status_code=500)

    results = []

    def _probe(name, fn):
        t0 = time.time()
        try:
            rows = fn()
            elapsed_ms = int((time.time() - t0) * 1000)
            if rows:
                results.append({
                    "source": name,
                    "ok": True,
                    "bars": len(rows),
                    "first_ts": rows[0].get("ts"),
                    "last_ts": rows[-1].get("ts"),
                    "elapsed_ms": elapsed_ms,
                })
            else:
                results.append({
                    "source": name,
                    "ok": False,
                    "bars": 0,
                    "error": "returned no data (see Railway logs for per-branch detail)",
                    "elapsed_ms": elapsed_ms,
                })
        except Exception as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            results.append({
                "source": name,
                "ok": False,
                "bars": 0,
                "error": str(exc)[:300],
                "elapsed_ms": elapsed_ms,
            })

    _probe("polygon", lambda: _try_polygon_ohlcv(symbol, period))
    _probe("yfinance", lambda: _try_yfinance_ohlcv(symbol, period))
    _probe("stooq", lambda: _try_stooq_ohlcv(symbol, period))

    working = [r["source"] for r in results if r["ok"]]
    broken = [r["source"] for r in results if not r["ok"]]
    return {
        "ok": True,
        "symbol": symbol,
        "period": period,
        "results": results,
        "summary": {"working": working, "broken": broken, "total_working": len(working)},
        "note": "Alpaca SIP/IEX are nested inside _fetch_ohlcv and not directly probed; check Railway logs for those.",
    }












































def _top_scan_candidates(limit: int = 5) -> list:
    """Ranked up_prob leaderboard from the most recent scan cycle's evals."""
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT symbol, up_prob, skip_code, min_win_proba, fired "
                "FROM ghost_perf_symbol_evals "
                "WHERE cycle_id = (SELECT MAX(id) FROM ghost_perf_cycles) "
                "AND up_prob IS NOT NULL ORDER BY up_prob DESC LIMIT %s",
                (max(1, min(10, int(limit))),),
            )
            rows = cur.fetchall()
        return [
            {
                "symbol": r[0],
                "up_prob": float(r[1]),
                "skip_code": r[2],
                "min_win_proba": float(r[3]) if r[3] is not None else None,
                "fired": bool(r[4]),
            }
            for r in rows
        ]
    except Exception as e:
        LOGGER.debug("top candidates failed: %s", str(e)[:80])
        return []


# v3.2 era marker — predictions with id >= this are Ghost's high-conviction
# v3.2-engine picks. Used across the codebase (core.stats_direction, core.prediction)
# to exclude ~223k legacy v1 rows from credibility stats.
from core.prediction_filters import V32_ERA_MIN_ID as _V32_ERA_MIN_ID  # single source of truth


def _pick_journal_scope(symbol: str):
    """Return (sql_prefix, params, label) for pick-journal symbol filter."""
    sym = str(symbol or "ALL").strip().upper()
    if sym in ("ALL", "*", ""):
        return "", (), "ALL"
    return "symbol = %s AND ", (sym,), sym


def _coerce_json(v):
    """psycopg2 may hand back JSONB as dict already, or as text. Normalise to obj."""
    if v is None:
        return {}
    if isinstance(v, (dict, list)):
        return v
    try:
        import json as _j
        return _j.loads(v)
    except Exception:
        return {}







































_HTML_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


def _serve_html_page(filename: str) -> HTMLResponse:
    """Serve a repo HTML page with build stamp + no-cache headers (avoid stale cockpit)."""
    import os as _os
    _path = _os.path.join(_os.path.dirname(__file__), filename)
    with open(_path, encoding="utf-8") as _f:
        html = _f.read()
    meta = _deploy_meta()
    short = meta.get("git_sha_short") or "dev"
    stamp = f'<meta name="ghost-build" content="{short}">'
    if 'name="ghost-build"' not in html:
        html = html.replace("<head>", "<head>\n  " + stamp, 1)
    else:
        import re as _re
        html = _re.sub(
            r'<meta name="ghost-build" content="[^"]*">',
            stamp,
            html,
            count=1,
        )
    return HTMLResponse(html, headers=_HTML_NO_CACHE)


@APP.get("/cockpit", include_in_schema=False)
def cockpit():
    return _serve_html_page("cockpit.html")


@APP.get("/", include_in_schema=False)
def root_console():
    """Unified Liquid Glass prediction console (PR #86)."""
    return _serve_html_page("ghost_console.html")


@APP.get("/picks", include_in_schema=False)
def picks_page():
    """Unified Liquid Glass prediction console (PR #86).

    Merges the old Ghost Picks consumer tracker and the operator dashboard into
    one clean sidebar-based command center. The old picks page remains available
    at /legacy-picks during rollout.
    """
    return _serve_html_page("ghost_console.html")


# Tiny inline ghost favicon (SVG). PR #92: browsers auto-request /favicon.ico on
# every page load; without this route that request 404s (flagged in the launch
# review). Served as an SVG data-equivalent with long cache so it costs nothing.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='8' fill='#0b1424'/>"
    "<text x='16' y='23' font-size='20' text-anchor='middle' fill='#5eead4' "
    "font-family='Arial'>G</text></svg>"
)


@APP.get("/favicon.ico", include_in_schema=False)
@APP.get("/favicon.svg", include_in_schema=False)
def favicon():
    """Serve a small ghost favicon so /favicon.ico no longer 404s."""
    return Response(
        content=_FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@APP.get("/legacy-picks", include_in_schema=False)
def legacy_picks_page():
    """Legacy Ghost Picks page kept as a rollout fallback for PR #86."""
    return _serve_html_page("picks.html")


# ────────────────────────────────────────────────────────────────
# /admin — cookie-login operator console (PR #28)
# ────────────────────────────────────────────────────────────────
# Replaced HTTP Basic Auth (PR #23) which rendered blank on production —
# browsers/edge proxies mishandle the 401 Basic challenge. Cookie login is
# a plain HTML form → no browser auth dialog, no proxy quirks. The cookie
# is an HMAC-signed {expiry}.{sig} token so it can't be forged client-side.
_ADMIN_COOKIE = "gp_admin"
_ADMIN_TTL_S = 28800  # 8 hours


def _admin_mint_token(ttl_s: int = _ADMIN_TTL_S) -> str:
    secret = os.environ.get("CRON_SECRET", "")
    exp = str(int(time.time()) + ttl_s)
    sig = hmac.new(secret.encode("utf-8"), exp.encode("utf-8"), "sha256").hexdigest()
    return exp + "." + sig


def _admin_token_valid(token: str) -> bool:
    """True if the cookie token is a non-expired, correctly-signed value.

    Dev mode (no CRON_SECRET) requires explicit GHOST_DEV_MODE=1 — mirrors
    _cron_ok semantics. Without the flag, an unset secret fails CLOSED so a
    deploy on any platform (not just Railway, whose boot guard refuses to
    start) never exposes /admin, /api/portfolio, or /api/my-picks.
    """
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return os.getenv("GHOST_DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on")
    if not token or "." not in token:
        return False
    try:
        exp_str, sig = token.rsplit(".", 1)
        if int(exp_str) < int(time.time()):
            return False
        expected = hmac.new(secret.encode("utf-8"), exp_str.encode("utf-8"), "sha256").hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


_ADMIN_LOGIN_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ghost Protocol — Admin Login</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{background:#0a0a0a;color:#fff;
font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;min-height:100vh;
display:flex;align-items:center;justify-content:center}
.box{background:#111;border:1px solid #1e1e1e;border-radius:14px;padding:32px;width:340px;max-width:90vw}
.logo{font-size:16px;font-weight:800;letter-spacing:2px;margin-bottom:6px}.logo span{color:#ff3b3b}
.sub{font-size:12px;color:#666;margin-bottom:20px}
input{width:100%;background:#0a0a0a;border:1px solid #2a2a2a;color:#fff;padding:11px 12px;
border-radius:8px;font-size:14px;font-family:ui-monospace,Menlo,monospace;margin-bottom:12px}
button{width:100%;background:#ff3b3b;color:#fff;border:none;padding:11px;border-radius:8px;
font-size:13px;font-weight:700;letter-spacing:.5px;cursor:pointer}button:hover{background:#e03333}
.err{color:#ff3b3b;font-size:12px;min-height:16px;margin-top:10px}</style></head><body>
<div class="box"><div class="logo">&#128123; GHOST <span>ADMIN</span></div>
<div class="sub">Enter the cron secret to access the operator console.</div>
<input type="password" id="secret" placeholder="CRON_SECRET" autocomplete="off" autofocus
onkeydown="if(event.key==='Enter')doLogin()">
<button onclick="doLogin()">Sign in</button><div class="err" id="err"></div></div>
<script>
async function doLogin(){
  var s=document.getElementById('secret').value||'';
  var e=document.getElementById('err');e.textContent='Signing in...';
  try{
    var r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({secret:s})});
    if(r.ok){location.reload();}
    else{e.textContent='Wrong secret. Try again.';}
  }catch(_){e.textContent='Network error.';}
}
</script></body></html>"""




# /admin/login sits outside the /api/* rate-limit middleware, so it needs its
# own throttle — the admin secret is the whole security boundary and must not
# be brute-forceable online.
_LOGIN_ATTEMPTS = _collections.defaultdict(_collections.deque)  # ip -> deque[ts]
_LOGIN_ATTEMPTS_LOCK = threading.Lock()


def _login_throttled(ip: str) -> bool:
    """True if this IP exceeded LOGIN_ATTEMPTS_PER_MIN (default 5) in 60s."""
    try:
        limit = max(1, int(os.getenv("LOGIN_ATTEMPTS_PER_MIN", "5")))
    except Exception:
        limit = 5
    now = time.time()
    with _LOGIN_ATTEMPTS_LOCK:
        dq = _LOGIN_ATTEMPTS[ip]
        cutoff = now - 60
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return True
        dq.append(now)
        if len(_LOGIN_ATTEMPTS) > 4096:
            for _k in [k for k, v in list(_LOGIN_ATTEMPTS.items()) if not v]:
                _LOGIN_ATTEMPTS.pop(_k, None)
    return False






if os.path.exists("static"):
    APP.mount("/static", StaticFiles(directory="static"), name="static")

# ════════════════════════════════════════════════════════════
# GHOST v3 ENDPOINTS — Backtested signal engine
# ════════════════════════════════════════════════════════════

def _v3_system_health(model_status: dict) -> dict:
    """Aggregate, DB-cheap system health (audit). Composes engine heartbeat,
    kill-condition + pause state, model coverage, recent activity and realized
    P&L into one snapshot with a healthy/degraded/critical roll-up. Every block
    is independently guarded so a single failure degrades gracefully rather than
    500-ing the status endpoint."""
    now = int(time.time())
    issues = []

    db_ok = True
    try:
        with db_conn() as c:
            c.cursor().execute("SELECT 1")
    except Exception:
        db_ok = False
        issues.append("db_unreachable")

    cycle = {"ts": None, "saved": None, "scanned": None, "age_min": None}
    try:
        with db_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT key,val FROM ghost_state WHERE key IN "
                "('last_prediction_cycle_ts','last_prediction_cycle_saved','last_prediction_cycle_scanned')")
            kv = {k: v for k, v in cur.fetchall()}
        if kv.get("last_prediction_cycle_ts"):
            cycle["ts"] = int(kv["last_prediction_cycle_ts"])
            cycle["age_min"] = int((now - cycle["ts"]) / 60)
        if kv.get("last_prediction_cycle_saved") is not None:
            cycle["saved"] = int(kv["last_prediction_cycle_saved"])
        if kv.get("last_prediction_cycle_scanned") is not None:
            cycle["scanned"] = int(kv["last_prediction_cycle_scanned"])
    except Exception:
        pass

    pause = {"paused": False}
    try:
        from core.prediction import engine_pause_state
        pause = engine_pause_state()
    except Exception:
        pass
    if pause.get("paused"):
        issues.append("engine_paused")

    kill = {"enabled": None, "any_triggered": None, "resolved_available": None}
    try:
        from core.prediction import evaluate_kill_conditions
        ev = evaluate_kill_conditions()
        kill = {"enabled": ev.get("enabled"), "any_triggered": ev.get("any_triggered"),
                "resolved_available": ev.get("resolved_available")}
        if ev.get("any_triggered"):
            issues.append("kill_condition_triggered")
    except Exception:
        pass

    trained = bool(model_status.get("trained"))
    if not trained:
        issues.append("no_model")
    min_models = max(1, int(os.getenv("MODEL_COVERAGE_MIN_MODELS", "3")))
    loaded = int(model_status.get("models", 0)) if trained else 0
    expected = sorted({sym for sym, _atype in _v3_train_collect_symbols()})
    missing_models = [sym for sym in expected if sym not in (model_status.get("symbols") or {})]
    if missing_models:
        issues.append("watchlist_models_missing")
    if loaded < min_models:
        issues.append("coverage_below_floor")

    watchlist_syms = expected or ["WOLF"]
    activity = {"open": None, "resolved_24h": None}
    pnl = None
    try:
        with db_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM predictions WHERE symbol = ANY(%s) "
                "AND outcome IS NULL AND expires_at > %s",
                (watchlist_syms, now),
            )
            activity["open"] = int(cur.fetchone()[0])
            cur.execute(
                "SELECT COUNT(*) FROM predictions WHERE symbol = ANY(%s) AND resolved_at >= %s",
                (watchlist_syms, now - 86400),
            )
            activity["resolved_24h"] = int(cur.fetchone()[0])
            cur.execute(
                "SELECT resolved_at,symbol,outcome,pnl_pct,entry_price,exit_price FROM predictions "
                "WHERE symbol = ANY(%s) AND id >= %s AND outcome IS NOT NULL AND pnl_pct IS NOT NULL "
                "AND " + NON_RESEARCH_WHERE + " "
                "ORDER BY resolved_at ASC NULLS LAST, id ASC",
                (watchlist_syms, _V32_ERA_MIN_ID),
            )
            rows = cur.fetchall()
        from core.pnl import realized_pnl
        trades = [{"resolved_at": r[0], "symbol": r[1], "outcome": r[2],
                   "pnl_pct": float(r[3]) if r[3] is not None else None,
                   "entry_price": r[4], "exit_price": r[5]} for r in rows]
        full = realized_pnl(trades)
        pnl = {k: full[k] for k in ("count", "wins", "losses", "win_rate",
                                    "realized_pnl_usd", "total_return_pct",
                                    "profit_factor", "max_drawdown_pct")}
    except Exception:
        pass

    if not db_ok or not trained:
        status = "critical"
    elif issues:
        status = "degraded"
    else:
        status = "healthy"

    return {
        "status": status,
        "issues": issues,
        "db_ok": db_ok,
        "engine": {
            "last_cycle": cycle,
            "paused": bool(pause.get("paused")),
            "pause_reason": pause.get("reason"),
            "pause_auto_resume_at": pause.get("auto_resume_at"),
        },
        "kill": kill,
        "coverage": {
            "loaded_models": loaded,
            "min_models_floor": min_models,
            "below_floor": loaded < min_models,
            "expected_symbols": expected,
            "missing_models": missing_models,
        },
        "activity": activity,
        "pnl": pnl,
        "checked_at": now,
    }






















def _cockpit_cached_db_payload():
    """
    Stats + direction + activity in one DB connection; v3 JSON cached with them.
    Health and regime stay fresh per request. TTL: COCKPIT_CONTEXT_CACHE_SEC (0 = off).
    """
    ttl = float(os.getenv("COCKPIT_CONTEXT_CACHE_SEC", "8"))
    now = time.time()
    if (
        ttl > 0
        and _COCKPIT_DB_CACHE["stats"] is not None
        and (now - _COCKPIT_DB_CACHE["t"]) < ttl
    ):
        return (
            _COCKPIT_DB_CACHE["stats"],
            _COCKPIT_DB_CACHE["direction"],
            _COCKPIT_DB_CACHE["v3"],
            _COCKPIT_DB_CACHE["activity"],
        )
    with db_conn() as conn:
        cur = conn.cursor()
        stats = _compute_get_stats(cur)
        direction = compute_stats_by_direction(cur)
        activity = _cockpit_activity_on_cursor(cur)
    v3 = v3_status()
    if ttl > 0:
        _COCKPIT_DB_CACHE["t"] = now
        _COCKPIT_DB_CACHE["stats"] = stats
        _COCKPIT_DB_CACHE["direction"] = direction
        _COCKPIT_DB_CACHE["v3"] = v3
        _COCKPIT_DB_CACHE["activity"] = activity
    return stats, direction, v3, activity


@APP.get("/api/cockpit/context", include_in_schema=False)
def cockpit_context():
    """Single fetch for /cockpit: health, stats, direction, regime, v3, activity summary."""
    try:
        stats, direction, v3, activity = _cockpit_cached_db_payload()
        # WOLF-only mode: regime gate is a no-op.
        regime = {"ok": True, "block_crypto_buys": False, "reduce_size": False, "reason": "", "btc_24h_pct": 0.0}
        # P1-3: circuit breaker status for cockpit visibility
        cb_status = {}
        try:
            from core.circuit_breaker import all_breaker_status
            cb_status = all_breaker_status()
        except Exception:
            pass
        # P3: degraded mode status
        degraded = {}
        try:
            from core.degraded_mode import check_degraded
            degraded = check_degraded()
        except Exception:
            pass
        # P3: latency SLO summary
        latency = {}
        try:
            from core.latency_slo import all_stats
            latency = all_stats()
        except Exception:
            pass
        return {
            "ok": True,
            "health": health(),
            "stats": stats,
            "direction": direction,
            "regime": regime,
            "v3": v3,
            "activity": activity,
            "deploy": _deploy_meta(),
            "circuit_breakers": cb_status,
            "degraded": degraded,
            "latency": latency.get("overall", {}),
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:120]}, status_code=500)


# P3-3 (audit): WebSocket live feed for cockpit — pushes price updates,
# squeeze alerts, and prediction changes without polling.
# Also provides an HTTP polling fallback (/api/cockpit/live) for when
# the websockets library isn't available in the deployment environment.
_WS_CLIENTS: set = set()


@APP.get("/api/cockpit/live", include_in_schema=False)
def cockpit_live_poll():
    """HTTP polling fallback for cockpit live data (WebSocket alternative).

    Returns the same payload the WebSocket would push on a ping, so the
    cockpit can use this when /ws/cockpit is unavailable (e.g. nixpacks
    build cache preventing websockets from installing).
    """
    payload = {"type": "pong", "ts": int(time.time())}
    try:
        from core.squeeze_monitor import get_squeeze_picks
        sp = get_squeeze_picks()
        payload["squeeze"] = {
            "picks": sp.get("picks", [])[:5],
            "radar_active": sp.get("radar_active"),
        }
    except Exception:
        payload["squeeze"] = {"error": "unavailable"}
    try:
        from core.prices import get_stock_price
        price = get_stock_price("WOLF")
        payload["wolf_price"] = price       # PR #77: canonical key
        payload["wol f_price"] = price      # legacy typo — remove after frontend migration
    except Exception:
        payload["wolf_price"] = None
        payload["wol f_price"] = None
    return payload


@APP.websocket("/ws/cockpit")
async def ws_cockpit(ws: WebSocket):
    await ws.accept()
    _WS_CLIENTS.add(ws)
    try:
        while True:
            try:
                data = await ws.receive_text()
                if data == "ping":
                    # Push a lightweight snapshot: latest squeeze picks + WOLF price
                    payload = {"type": "pong", "ts": int(time.time())}
                    try:
                        from core.squeeze_monitor import get_squeeze_picks
                        sp = get_squeeze_picks()
                        payload["squeeze"] = {
                            "picks": sp.get("picks", [])[:5],
                            "radar_active": sp.get("radar_active"),
                        }
                    except Exception:
                        payload["squeeze"] = {"error": "unavailable"}
                    try:
                        from core.prices import get_stock_price
                        price = get_stock_price("WOLF")
                        payload["wolf_price"] = price       # PR #77: canonical key
                        payload["wol f_price"] = price      # legacy typo
                    except Exception:
                        payload["wolf_price"] = None
                        payload["wol f_price"] = None
                    await ws.send_json(payload)
            except WebSocketDisconnect:
                break
            except Exception:
                await ws.send_json({"type": "error", "detail": "invalid message"})
    finally:
        _WS_CLIENTS.discard(ws)


async def _ws_broadcast(payload: dict) -> None:
    """Best-effort broadcast to all connected cockpit WebSocket clients."""
    dead: list = []
    for ws in list(_WS_CLIENTS):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _WS_CLIENTS.discard(ws)


def _v3_train_collect_symbols() -> list:
    """Collect symbols for v3 training from env + user portfolio."""
    from config.symbols import watchlist_symbol_pairs
    return watchlist_symbol_pairs(include_portfolio=True)


def _record_v3_train_state(**fields) -> None:
    """Write v3_train phase markers into ghost_state for /api/v3/train/last.

    Fields are keyed as last_v3_train_<name>; each call upserts only the
    provided fields so partial updates work across the train phases.
    """
    if not fields:
        return
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_ghost_state(cur)
            for name, value in fields.items():
                cur.execute(
                    "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
                    "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                    (f"last_v3_train_{name}", "" if value is None else str(value)),
                )
    except Exception as _e:
        LOGGER.warning("v3_train state write failed: " + str(_e)[:120])




# PR #19 deploy-version constant. Bump on every "did Railway pick up
# the new code?" PR so /api/_version reveals the truth in one curl.
_RUNNING_PR_VERSION = 151


def _deploy_meta() -> dict:
    """Railway-injected deploy metadata for cockpit/admin verification."""
    sha = os.getenv("RAILWAY_GIT_COMMIT_SHA", "unset")
    short = sha[:7] if sha and sha != "unset" else "unset"
    meta = {
        "git_sha": sha,
        "git_sha_short": short,
        "deploy_id": os.getenv("RAILWAY_DEPLOYMENT_ID", "unset"),
        "deploy_version_env": os.getenv("DEPLOY_VERSION", "unset"),
        "app_version": APP_VERSION,
        "_pr_version": _RUNNING_PR_VERSION,
    }
    try:
        from core.accuracy_contract import contract_summary
        meta["accuracy_contract"] = contract_summary()
    except Exception:
        pass
    return meta


@APP.get("/api/_version")
def deploy_version():
    """Return the running code version + Railway-injected git/deploy IDs.

    Lets the operator verify from a single curl whether the deployed
    container is running the expected commit. No auth required —
    nothing sensitive in the response.
    """
    return {
        "ok": True,
        "ts": int(time.time()),
        "endpoints_present": {
            "v3_train_force_param": True,    # PR #18
            "v3_train_last": True,            # PR #18
            "v3_train_sync": True,            # PR #19
            "diag_data_sources": True,        # PR #17
            "wolf_signal_alert_check": True,  # PR #8
        },
        **_deploy_meta(),
    }








# ── PR #130: endpoint groups split into api/routes_* modules ──
# Routers import at the very end so every helper they late-import already
# exists. Names are re-exported so wolf_app.<endpoint> imports, monkeypatch
# targets, and inspect.getsource contract tests keep working.
from api.routes_admin import router as _routes_admin_router  # noqa: E402
APP.include_router(_routes_admin_router)
from api.routes_admin import (  # noqa: E402,F401 — facade re-exports
    admin_page,
    admin_health,
    admin_login,
    admin_logout,
    admin_audit_log,
    delete_model,
    fix_stock_expiry,
    news_import,
    news_import_format,
    purge_crypto_junk,
    purge_ghost_portfolio,
    purge_test_predictions,
    admin_reset_breakers,
    admin_resume_engine,
    admin_shadow_cycle,
    admin_squeeze_resolve,
    admin_squeeze_scan,
    admin_symbol_universe,
    admin_telegram_dead_letter,
    admin_telegram_dead_letter_replay,
    diagnostics,
    clean_garbage,
    dedup_picks,
    migrate_outcomes,
    run_watchdog,
    test_alert,
)
from api.routes_ghost_system import router as _routes_ghost_system_router  # noqa: E402
APP.include_router(_routes_ghost_system_router)
from api.routes_ghost_system import (  # noqa: E402,F401 — facade re-exports
    ghost_blueprint_endpoint,
    ghost_contract_endpoint,
    ghost_drift_endpoint,
    ghost_options_endpoint,
    ghost_regime_endpoint,
    ghost_score_spec_endpoint,
    ghost_sentiment_endpoint,
    shadow_stats_endpoint,
    squeeze_daily_log_endpoint,
    squeeze_picks_endpoint,
    squeeze_status_endpoint,
    system_breakers_endpoint,
    system_degraded_endpoint,
    system_latency_endpoint,
    api_regime,
    api_objective,
    api_objective_report,
)
from api.routes_v3 import router as _routes_v3_router  # noqa: E402
APP.include_router(_routes_v3_router)
from api.routes_v3 import (  # noqa: E402,F401 — facade re-exports
    v3_backtest,
    v3_explain,
    v3_lineage,
    v3_status,
    v3_train,
    v3_train_last,
    v3_train_sync,
    retrain,
)
from api.routes_wolf_ops import router as _routes_wolf_ops_router  # noqa: E402
APP.include_router(_routes_wolf_ops_router)
from api.routes_wolf_ops import (  # noqa: E402,F401 — facade re-exports
    wolf_gate_status,
    wolf_gate_history,
    wolf_kill_status,
    wolf_pnl,
    wolf_daily_summary,
    wolf_pick_journal,
    wolf_perf_log_cycles,
    wolf_perf_log_cycle_detail,
    wolf_perf_log_events,
    wolf_perf_log_progress,
    wolf_perf_log_symbol,
    wolf_signal_alert_check,
    cron_signal_check,
)
from api.routes_data import router as _routes_data_router  # noqa: E402
APP.include_router(_routes_data_router)
from api.routes_data import (  # noqa: E402,F401 — facade re-exports
    get_picks,
    get_history,
    get_news,
    get_schema,
    get_stats,
    get_stats_v32,
    get_stats_confidence_buckets,
    symbol_accuracy,
    telegram_status,
    coverage_status,
    db_probe,
    research_status_endpoint,
    debug_signal,
    get_market_session_endpoint,
    get_price_endpoint,
    trigger_predictions,
    trigger_morning_card,
    trigger_reconcile,
)
