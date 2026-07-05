"""api/routes_admin.py — endpoint group split out of wolf_app.py (PR #130).

Endpoint bodies late-import shared helpers from wolf_app at request time so
tests that monkeypatch wolf_app attributes (db_conn, _cron_ok, ...) keep
working, and so this module never imports wolf_app at import time (no cycle).
wolf_app re-exports every endpoint name for backward compatibility.
"""
import os, sys, time, json, logging, threading, hmac, math, asyncio, base64  # noqa: F401,E401

from fastapi import APIRouter, Header, HTTPException, Request, Depends  # noqa: F401
from fastapi.responses import JSONResponse, HTMLResponse, Response, PlainTextResponse  # noqa: F401

router = APIRouter()

@router.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    """Serve admin.html when the cookie is valid; else the login page."""
    from wolf_app import _ADMIN_COOKIE, _ADMIN_LOGIN_HTML, _HTML_NO_CACHE, _admin_token_valid, _serve_html_page  # late import — shared state + monkeypatch-safe
    token = request.cookies.get(_ADMIN_COOKIE, "")
    if not _admin_token_valid(token):
        return HTMLResponse(_ADMIN_LOGIN_HTML, headers=_HTML_NO_CACHE)
    return _serve_html_page("admin.html")


@router.get("/admin/health", include_in_schema=False)
def admin_health(request: Request):
    """Full health detail, cookie-gated like /api/diagnostics — 404 when
    unauthenticated so internals are not publicly discoverable (audit v2 #10)."""
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid, health  # late import — shared state + monkeypatch-safe
    if not _admin_token_valid(request.cookies.get(_ADMIN_COOKIE, "")):
        raise HTTPException(status_code=404)
    return health()


@router.post("/admin/login", include_in_schema=False)
async def admin_login(request: Request):
    """Validate the posted secret against CRON_SECRET; set the signed cookie.

    JSON body {"secret": "..."} (no python-multipart dependency). On success
    sets an HttpOnly, SameSite=Lax cookie valid for 8h and returns {ok:true}.
    """
    from wolf_app import _ADMIN_COOKIE, _ADMIN_TTL_S, _admin_mint_token, _client_ip, _login_throttled  # late import — shared state + monkeypatch-safe
    if _login_throttled(_client_ip(request)):
        return JSONResponse(
            {"ok": False, "error": "too many attempts"},
            status_code=429, headers={"Retry-After": "60"},
        )
    expected = os.environ.get("CRON_SECRET", "")
    provided = ""
    try:
        body = await request.json()
        provided = str(body.get("secret", "") or "")
    except Exception:
        provided = ""
    if expected and not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        return JSONResponse({"ok": False, "error": "invalid secret"}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        _ADMIN_COOKIE, _admin_mint_token(),
        max_age=_ADMIN_TTL_S, httponly=True, samesite="lax",
        secure=os.getenv("ADMIN_COOKIE_SECURE", "1").strip() in ("1", "true", "yes", "on"),
    )
    return resp


@router.post("/admin/logout", include_in_schema=False)
def admin_logout():
    from wolf_app import _ADMIN_COOKIE  # late import — shared state + monkeypatch-safe
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_ADMIN_COOKIE)
    return resp


@router.get("/api/admin/audit-log", include_in_schema=False)
def admin_audit_log(request: Request, limit: int = 100):
    """Operator action audit log (audit) — purges, training, engine resume, etc.
    Gated behind the admin cookie like /api/diagnostics; 404 when unauthenticated
    so it is undiscoverable. Newest first."""
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid, db_conn, ensure_ghost_state  # late import — shared state + monkeypatch-safe
    if not _admin_token_valid(request.cookies.get(_ADMIN_COOKIE, "")):
        raise HTTPException(status_code=404)
    try:
        import json as _j
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
        lim = max(1, min(200, int(limit)))
        recent = list(reversed(log))[:lim]
        return {"ok": True, "count": len(recent), "actions": recent}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/admin/delete-model", include_in_schema=False)
async def delete_model(x_cron_secret: str = Header(None), non_wolf_only: bool = False):
    """Delete v3 models from ghost_v3_model.

    Default mode: delete models with accuracy < V3_MIN_HOLDOUT_ACC (cleanup
    of weak models below the deploy gate).

    non_wolf_only=true mode: delete every model whose symbol is not WOLF,
    regardless of accuracy. Use to clean up stale rows from the pre-WOLF
    crypto / multi-stock era that v3_status already filters out at read
    time (per PR #7 WOLF-only hardening) but still occupy DB rows.
    """
    from wolf_app import _cron_ok, _strip_model_direction_suffix  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403, detail="Forbidden")
    import json as _j
    from core.db import db_conn
    deleted = []
    kept = []
    ACCURACY_FLOOR = float(os.getenv("V3_MIN_HOLDOUT_ACC", "0.55"))
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%'")
            rows = cur.fetchall()
            for key, val in rows:
                raw = key.replace("meta_", "")
                # Phase 2 directional keys (meta_WOLF_up): compare on the bare
                # symbol so non_wolf_only never deletes WOLF's own models, but
                # delete by the raw key stem.
                sym = _strip_model_direction_suffix(raw)
                if non_wolf_only:
                    if str(sym).upper() == "WOLF":
                        kept.append(f"{raw}(WOLF)")
                        continue
                    cur.execute("DELETE FROM ghost_v3_model WHERE key IN (%s, %s)",
                               (f"model_{raw}", f"meta_{raw}"))
                    deleted.append(f"{raw}(non-WOLF)")
                    continue
                try:
                    meta = _j.loads(val)
                    acc = meta.get("accuracy", 0)
                    if acc < ACCURACY_FLOOR:
                        cur.execute("DELETE FROM ghost_v3_model WHERE key IN (%s, %s)",
                                   (f"model_{raw}", f"meta_{raw}"))
                        deleted.append(f"{raw}(acc={round(acc*100,1)}%)")
                    else:
                        kept.append(f"{raw}(acc={round(acc*100,1)}%)")
                except Exception:
                    pass
        if deleted:
            try:
                from core.signal_engine import invalidate_model_cache
                invalidate_model_cache()
            except Exception:
                pass
        return {"ok": True, "mode": "non_wolf_only" if non_wolf_only else "low_accuracy",
                "deleted": deleted, "kept": kept}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/admin/fix-stock-expiry", include_in_schema=False)
async def fix_stock_expiry(x_cron_secret: str = Header(None)):
    """Fix stock picks that were created before the weekend-expiry fix and expire before market open."""
    from wolf_app import _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403, detail="Forbidden")
    import time as _ft, datetime as _fdt, pytz as _ftz
    from core.db import db_conn
    _ct = _ftz.timezone("America/Chicago")
    _now = int(_ft.time())
    updated = []
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            # Find open stock picks expiring before 4 PM on their expiry day
            cur.execute("""SELECT id, symbol, expires_at FROM predictions
                           WHERE outcome IS NULL AND asset_type='stock'
                           AND expires_at > %s""", (_now,))
            picks = cur.fetchall()
            for pid, sym, exp_ts in picks:
                exp_dt = _fdt.datetime.fromtimestamp(exp_ts, tz=_ct)
                # If expiry hour is before 16 (4 PM), push to 4 PM same day
                if exp_dt.hour < 16:
                    fixed_dt = exp_dt.replace(hour=16, minute=0, second=0, microsecond=0)
                    # Skip weekends
                    if fixed_dt.weekday() == 5: fixed_dt += _fdt.timedelta(days=2)
                    elif fixed_dt.weekday() == 6: fixed_dt += _fdt.timedelta(days=1)
                    fixed_ts = int(fixed_dt.timestamp())
                    cur.execute("UPDATE predictions SET expires_at=%s WHERE id=%s", (fixed_ts, pid))
                    updated.append(f"{sym}: {exp_dt.strftime('%a %I:%M %p')} -> {fixed_dt.strftime('%a %I:%M %p')} CT")
        return {"ok": True, "fixed": len(updated), "details": updated}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/admin/news/import", include_in_schema=False)
async def news_import(request: Request, x_cron_secret: str = Header(default="")):
    """Import watchlist news articles from JSON (paste or file upload)."""
    from wolf_app import _cron_ok, _record_admin_action  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    try:
        from core.news_store import import_articles_payload
        out = import_articles_payload(body, watchlist_only=True)
        _record_admin_action(
            "news_import",
            f"inserted={out.get('inserted')} updated={out.get('updated')} skipped={out.get('skipped')}",
        )
        return out
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=400)


@router.get("/api/admin/news/import-format", include_in_schema=False)
def news_import_format():
    """JSON schema/help for manual news imports."""
    from core.news_store import import_format_doc
    return import_format_doc()


@router.post("/api/admin/purge-crypto-junk", include_in_schema=False)
async def purge_crypto_junk(x_cron_secret: str = Header(None), dry_run: bool = True):
    """Hard-delete crypto-era / zero-entry prediction rows (not real stock trades).

    Targets rows where asset_type != stock or entry_price <= 0 — the legacy crypto
    phase left EXPIRED picks with $0 entry/target/stop that pollute lifetime stats.
    dry_run defaults TRUE; pass dry_run=false to delete.
    """
    from wolf_app import CRYPTO_JUNK_WHERE, _bump_cockpit_db_cache, _cron_ok, _record_admin_action, db_conn  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COALESCE(asset_type, 'stock'), COUNT(*) FROM predictions "
                "WHERE " + CRYPTO_JUNK_WHERE + " GROUP BY 1 ORDER BY 2 DESC"
            )
            by_type = [{"asset_type": r[0], "count": int(r[1])} for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) FROM predictions WHERE " + CRYPTO_JUNK_WHERE)
            total = int(cur.fetchone()[0])
            deleted = 0
            if not dry_run and total:
                cur.execute("DELETE FROM predictions WHERE " + CRYPTO_JUNK_WHERE)
                deleted = cur.rowcount
        if not dry_run:
            _record_admin_action("purge_crypto_junk", f"deleted={deleted} matched={total}")
            _bump_cockpit_db_cache()
        return {
            "ok": True,
            "dry_run": dry_run,
            "filter": CRYPTO_JUNK_WHERE,
            "by_asset_type": by_type,
            "total_matched": total,
            "deleted": deleted,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/admin/purge-ghost-portfolio", include_in_schema=False)
async def purge_ghost_portfolio(x_cron_secret: str = Header(None), dry_run: bool = False):
    """Hard-delete ghost / test rows from user_portfolio.

    Targets symbols matching one of _GHOST_PORTFOLIO_PATTERNS (case-
    insensitive prefix or exact match). Common pollutants:
      - 'ZZE2E*' — yfinance probe tickers (PR #13/14 left visible by mistake)
      - 'STOCK GHOST', 'GHOST*' — test rows
      - 'ZZ*', 'TEST*' — manual test entries

    dry_run=true: report what would be deleted without deleting.
    """
    from wolf_app import _GHOST_PORTFOLIO_PATTERNS, _cron_ok, _record_admin_action, db_conn  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403, detail="Forbidden")
    deleted = []
    would_delete = []
    kept_count = 0
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, symbol FROM user_portfolio")
            rows = cur.fetchall()
            for rid, sym in rows:
                up = (str(sym or "").strip().upper())
                hit = any(up.startswith(p) or up == p for p in _GHOST_PORTFOLIO_PATTERNS)
                if not hit:
                    kept_count += 1
                    continue
                if dry_run:
                    would_delete.append({"id": int(rid), "symbol": sym})
                else:
                    cur.execute("DELETE FROM user_portfolio WHERE id=%s", (int(rid),))
                    deleted.append({"id": int(rid), "symbol": sym})
        if not dry_run:
            _record_admin_action("purge_ghost_portfolio", f"deleted={len(deleted)}")
        return {
            "ok": True,
            "dry_run": dry_run,
            "patterns": list(_GHOST_PORTFOLIO_PATTERNS),
            "deleted": deleted,
            "would_delete": would_delete,
            "kept": kept_count,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/admin/purge-test-predictions", include_in_schema=False)
async def purge_test_predictions(x_cron_secret: str = Header(None), dry_run: bool = True):
    """Hard-delete synthetic/test rows from the predictions table (audit).

    Targets symbols matching _TEST_PREDICTION_PATTERNS — chiefly the 'ZZE2E*'
    probe tickers the e2e roundtrip leaves behind. dry_run defaults to TRUE: it
    reports the per-symbol counts that WOULD be deleted so the operator can
    confirm before running with dry_run=false. Destructive and irreversible.
    """
    from wolf_app import _TEST_PREDICTION_PATTERNS, _cron_ok, _record_admin_action, db_conn  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403, detail="Forbidden")
    patterns = list(_TEST_PREDICTION_PATTERNS)
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT symbol, COUNT(*) FROM predictions WHERE symbol ILIKE ANY(%s) GROUP BY symbol",
                (patterns,))
            matched = [{"symbol": s, "count": int(c)} for s, c in cur.fetchall()]
            total = sum(m["count"] for m in matched)
            deleted = 0
            if not dry_run and total:
                cur.execute("DELETE FROM predictions WHERE symbol ILIKE ANY(%s)", (patterns,))
                deleted = cur.rowcount
        if not dry_run:
            _record_admin_action("purge_test_predictions", f"deleted={deleted} matched={total}")
        return {
            "ok": True,
            "dry_run": dry_run,
            "patterns": patterns,
            "matched": matched,
            "total_matched": total,
            "deleted": deleted,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/admin/reset-breakers", include_in_schema=False)
def admin_reset_breakers(request: Request):
    """Force-close all circuit breakers. Admin cookie-gated. P3 audit."""
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid  # late import — shared state + monkeypatch-safe
    if not _admin_token_valid(request.cookies.get(_ADMIN_COOKIE, "")):
        raise HTTPException(status_code=404)
    try:
        from core.circuit_breaker import reset_all_breakers
        result = reset_all_breakers()
        return {"ok": True, **result}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/admin/resume-engine", include_in_schema=False)
def admin_resume_engine(x_cron_secret: str = Header(default="")):
    """Clear a kill-condition pause and resume firing (audit §2 enforcement).
    Manual recovery for pause/degrade/halt trips that do not auto-resume."""
    from wolf_app import _cron_ok, _record_admin_action  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403)
    try:
        from core.prediction import resume_engine
        out = resume_engine()
        _record_admin_action("resume_engine", "kill-condition pause cleared")
        return out
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/admin/shadow-cycle", include_in_schema=False)
def admin_shadow_cycle(request: Request, x_cron_secret: str = Header(default=""), dry_run: bool = False):
    """Run shadow seed + resolve now (ops). Gated by cron secret or admin cookie."""
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid, _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret) and not _admin_token_valid(
        request.cookies.get(_ADMIN_COOKIE, "")
    ):
        raise HTTPException(status_code=404)
    if dry_run:
        from core.shadow_outcomes import shadow_diagnostics, shadow_stats
        return {"ok": True, "dry_run": True, "stats": shadow_stats(), "diagnostics": shadow_diagnostics()}
    try:
        from core.shadow_outcomes import run_shadow_cycle, shadow_diagnostics, shadow_stats
        result = run_shadow_cycle()
        return {"ok": True, **result, "stats": shadow_stats(), "diagnostics": shadow_diagnostics()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/admin/squeeze-resolve", include_in_schema=False)
async def admin_squeeze_resolve(
    request: Request,
    x_cron_secret: str = Header(default=""),
    session_date: str = "",
):
    """Force EOD resolution for squeeze daily log (ops / backfill)."""
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid, _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret) and not _admin_token_valid(
        request.cookies.get(_ADMIN_COOKIE, "")
    ):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    try:
        from core.squeeze_outcomes import resolve_pending_squeeze_days, resolve_squeeze_outcomes

        sd = session_date.strip() or None
        if sd:
            n = resolve_squeeze_outcomes(sd)
        else:
            n = resolve_pending_squeeze_days(14)
        return {"ok": True, "resolved": n, "session_date": sd}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/admin/squeeze-scan", include_in_schema=False)
async def admin_squeeze_scan(request: Request, x_cron_secret: str = Header(default="")):
    """Force one squeeze watchlist scan now (stress test / ops)."""
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid, _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret) and not _admin_token_valid(
        request.cookies.get(_ADMIN_COOKIE, "")
    ):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    try:
        from core.squeeze_monitor import _run_watchlist_scan, get_squeeze_status

        await _run_watchlist_scan()
        return {"ok": True, **get_squeeze_status()}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/admin/symbol-universe", include_in_schema=False)
def admin_symbol_universe(request: Request, x_cron_secret: str = Header(default="")):
    """Operator map of symbol layers: code watchlist vs portfolio vs models vs picks.

    Read-only. Gated by admin cookie or X-Cron-Secret (404 when unauthenticated).
    """
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid, _build_symbol_universe_payload, _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret) and not _admin_token_valid(
        request.cookies.get(_ADMIN_COOKIE, "")
    ):
        raise HTTPException(status_code=404)
    try:
        return _build_symbol_universe_payload()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/admin/telegram/dead-letter", include_in_schema=False)
def admin_telegram_dead_letter(request: Request):
    """View the Telegram dead-letter queue (P1-2 audit).

    Gated by admin cookie — 404 when unauthenticated.
    """
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid  # late import — shared state + monkeypatch-safe
    if not _admin_token_valid(request.cookies.get(_ADMIN_COOKIE, "")):
        raise HTTPException(status_code=404)
    try:
        from core.telegram import get_dead_letter_queue
        queue = get_dead_letter_queue()
        return {"ok": True, "count": len(queue), "entries": queue}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/admin/telegram/dead-letter/replay", include_in_schema=False)
async def admin_telegram_dead_letter_replay(request: Request):
    """Replay one dead-letter entry by index (0 = oldest). P1-2 audit.

    JSON body: {"index": 0}. Gated by admin cookie.
    """
    from wolf_app import _ADMIN_COOKIE, _admin_token_valid  # late import — shared state + monkeypatch-safe
    if not _admin_token_valid(request.cookies.get(_ADMIN_COOKIE, "")):
        raise HTTPException(status_code=404)
    try:
        body = await request.json()
        idx = int(body.get("index", 0))
        from core.telegram import replay_dead_letter
        result = replay_dead_letter(idx)
        return {"ok": True, **result}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/diagnostics", include_in_schema=False)
async def diagnostics(request: Request = None):
    """Full logic correctness check — catches bugs /health misses.

    Security (audit): leaks scheduler intervals, Telegram gate hashes, model
    internals and health-check details, so it is gated behind the same admin
    cookie as /admin. Returns 404 (not 403) when unauthenticated so the endpoint
    is undiscoverable. FastAPI always injects `request` for HTTP calls; trusted
    internal callers invoke diagnostics() with no request and bypass the gate.
    """
    from wolf_app import REAL_TRADE_WHERE, _ADMIN_COOKIE, _admin_token_valid, _strip_model_direction_suffix  # late import — shared state + monkeypatch-safe
    if request is not None and not _admin_token_valid(request.cookies.get(_ADMIN_COOKIE, "")):
        raise HTTPException(status_code=404)
    import time as _t, json as _j2, datetime as _dt, pytz as _tz
    _now = int(_t.time())
    _passed = []
    _warnings = []
    _errors = []
    _score = 100

    # helpers — plain list appends, no nonlocal/closures
    def _ok(name, detail=""):
        _passed.append({"check": name, "detail": detail})

    def _warn(name, detail):
        _warnings.append({"check": name, "detail": detail})
        return 5

    def _fail(name, detail, deduct=10):
        _errors.append({"check": name, "detail": detail})
        return deduct

    try:
        from core.db import db_conn
        from core import scheduler as _sched
        from core.prices import check_feeds
        from core.signal_engine import FEATURE_COLS

        # ── 1. Scheduler integrity ────────────────────────────────────────
        _ws = _sched._tasks.get("weekly_summary")
        if not _ws:
            _score -= _fail("scheduler.weekly_summary", "not registered")
        elif _ws.interval_s != 3600:
            _score -= _fail("scheduler.weekly_summary", f"interval={_ws.interval_s}s want 3600 — hourly check + ISO-week dedup")
        else:
            _ok("scheduler.weekly_summary", f"hourly check at {_ws.interval_s}s (week-deduped)")

        # Check for duplicate weekly_summary registrations
        _ws_count = sum(1 for k in _sched._tasks if k == "weekly_summary")
        if _ws_count > 1:
            _score -= _fail("scheduler.weekly_summary_dup", f"registered {_ws_count}x — hourly spam bug")

        _wd = _sched._tasks.get("watchdog")
        if not _wd:
            _score -= _fail("scheduler.watchdog", "not registered — picks never resolve", 20)
        else:
            _ok("scheduler.watchdog", f"every {_wd.interval_s}s")

        _mc = _sched._tasks.get("morning_card")
        if not _mc:
            _score -= _fail("scheduler.morning_card", "not registered — no 8 AM picks", 20)
        else:
            _ok("scheduler.morning_card", f"every {_mc.interval_s}s")

        # ── 2. Telegram dedup state ───────────────────────────────────────
        try:
            with db_conn() as _conn:
                _cur = _conn.cursor()
                _cur.execute("SELECT key, val FROM ghost_state WHERE key IN ('last_open_pos_hash','last_no_picks_sent')")
                _state = {r[0]: r[1] for r in _cur.fetchall()}
            if "last_open_pos_hash" in _state:
                _ok("telegram.open_pos_gate", f"hash={_state['last_open_pos_hash']}")
            else:
                _score -= _warn("telegram.open_pos_gate", "hash missing — open positions may spam on restart")
        except Exception as _e:
            _score -= _warn("telegram.open_pos_gate", f"state check failed: {_e}")

        # ── 3. Active pick expiry ─────────────────────────────────────────
        with db_conn() as _conn:
            _cur = _conn.cursor()
            _cur.execute("""SELECT symbol, asset_type, expires_at, predicted_at
                            FROM predictions WHERE outcome IS NULL AND expires_at > %s""", (_now,))
            _active = _cur.fetchall()

        _weekend = []
        _stale = []
        for _sym, _atype, _exp, _pred in _active:
            if _atype == "stock":
                _exp_dt = _dt.datetime.fromtimestamp(_exp, tz=_tz.timezone("America/Chicago"))
                if _exp_dt.weekday() in (5, 6):
                    _weekend.append(f"{_sym} expires {_exp_dt.strftime('%a')}")
            if (_now - _pred) > 96 * 3600:
                _stale.append(f"{_sym} open {int((_now-_pred)/3600)}h")

        if _weekend:
            _score -= _fail("picks.weekend_expiry", f"Stock picks expiring on weekend: {_weekend}", 15)
        else:
            _ok("picks.weekend_expiry", "no stock picks expiring on weekend")

        if _stale:
            _score -= _warn("picks.stale_open", f"Picks open >96h: {_stale}")
        else:
            _ok("picks.stale_open", "all picks within 96h window")

        # ── 4. Resolution rate (7-day window) ────────────────────────────
        _7d = _now - 7 * 86400
        with db_conn() as _conn:
            _cur = _conn.cursor()
            _cur.execute("""SELECT outcome, COUNT(*) FROM predictions
                            WHERE outcome IN ('WIN','LOSS','EXPIRED')
                            AND predicted_at > %s
                            AND """ + REAL_TRADE_WHERE + """
                            GROUP BY outcome""", (_7d,))
            _7d_rows = {r[0]: r[1] for r in _cur.fetchall()}
            # All-time win rate (real stock trades only)
            _cur.execute(
                "SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS') "
                "AND " + REAL_TRADE_WHERE + " GROUP BY outcome"
            )
            _at_rows = {r[0]: r[1] for r in _cur.fetchall()}

        _7w = _7d_rows.get("WIN", 0)
        _7l = _7d_rows.get("LOSS", 0)
        _7e = _7d_rows.get("EXPIRED", 0)
        _7tot = _7w + _7l + _7e
        _7res = _7w + _7l

        if _7tot > 0:
            _res_rate = round(_7res / _7tot * 100, 1)
            if _res_rate < 10:
                _score -= _fail("resolution.rate", f"Last 7d: {_res_rate}% resolve ({_7w}W/{_7l}L/{_7e}E) — feed/expiry broken", 20)
            elif _res_rate < 30:
                _score -= _warn("resolution.rate", f"Last 7d: {_res_rate}% resolve ({_7w}W/{_7l}L/{_7e}E)")
            else:
                _ok("resolution.rate", f"Last 7d: {_res_rate}% ({_7w}W/{_7l}L/{_7e}E)")
        else:
            _score -= _warn("resolution.rate", "No resolved picks in last 7 days")

        _atw = _at_rows.get("WIN", 0)
        _atl = _at_rows.get("LOSS", 0)
        _at_wr = round(_atw / (_atw + _atl) * 100, 1) if (_atw + _atl) > 0 else 0
        _ok("win_rate.alltime", f"{_at_wr}% WIN/(WIN+LOSS) all-time ({_atw}W/{_atl}L)")

        # ── 5. Loss streak ────────────────────────────────────────────────
        with db_conn() as _conn:
            _cur = _conn.cursor()
            _cur.execute("""SELECT outcome FROM predictions
                            WHERE outcome IN ('WIN','LOSS')
                            ORDER BY resolved_at DESC LIMIT 10""")
            _recent_outcomes = [r[0] for r in _cur.fetchall()]

        _streak = 0
        for _o in _recent_outcomes:
            if _o == "LOSS":
                _streak += 1
            else:
                break
        if _streak >= 5:
            _score -= _fail("signal.loss_streak", f"{_streak} consecutive losses — retrain needed", 15)
        elif _streak >= 3:
            _score -= _warn("signal.loss_streak", f"{_streak} consecutive losses")
        else:
            _ok("signal.loss_streak", f"{_streak} consecutive losses" if _streak else "no streak")

        # ── 6. Model freshness and engine version ─────────────────────────
        with db_conn() as _conn:
            _cur = _conn.cursor()
            _cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%'")
            _model_rows = _cur.fetchall()

        _stale_models = []
        _old_engine = []
        _drift = []
        _weak_wf = []
        _expected_features = len(FEATURE_COLS)
        for _k, _v in _model_rows:
            _sym = _k.replace("meta_", "")
            _m = _j2.loads(_v)
            _age = (_now - _m.get("trained_at", 0)) / 86400
            if _age > 14:
                _stale_models.append(f"{_sym} ({_age:.0f}d)")
            _engine = _m.get("engine_version", "v3.0")
            if ("v3.1_ema_adx_atr_obv_stoch" not in _engine) and ("v3.2_tp_sl_daily" not in _engine):
                _old_engine.append(_sym)
            _fc = _m.get("feature_cols", [])
            if len(_fc) != _expected_features:
                _drift.append(f"{_sym}: {len(_fc)} vs {_expected_features}")
            _wf_folds = int(_m.get("wf_fold_count", 0))
            _wf_acc = float(_m.get("wf_acc_mean", _m.get("accuracy", 0)))
            _wf_edge = float(_m.get("wf_edge_mean", _m.get("edge", 0)))
            _wf_min_folds = max(2, int(os.getenv("V3_MIN_WF_FOLDS", "3")))
            _wf_floor = float(os.getenv("V3_MIN_WF_ACC_MEAN", "0.60"))
            _wf_edge_floor = float(os.getenv("V3_MIN_EDGE", "0.05"))
            if _wf_folds < _wf_min_folds or _wf_acc < _wf_floor or _wf_edge < _wf_edge_floor:
                _weak_wf.append(_sym)

        if _stale_models:
            _score -= _warn("models.freshness", f"Stale models (>14d): {_stale_models}")
        else:
            _ok("models.freshness", f"{len(_model_rows)} models within 14 days")

        if _old_engine:
            _score -= _warn("models.engine", f"Unrecognized engine version: {_old_engine}")
        else:
            _ok("models.engine", f"all {len(_model_rows)} models on accepted engines (v3.1/v3.2)")

        if _drift:
            _score -= _warn("models.feature_drift", f"Feature mismatch: {_drift}")
        else:
            _ok("models.feature_drift", f"all models match {_expected_features}-feature engine")

        if _weak_wf:
            _score -= _warn("models.walk_forward", f"Models below walk-forward floor: {_weak_wf}")
        else:
            _ok("models.walk_forward", "all models pass walk-forward floor")

        # Active picks with no model (normalize Phase 2 directional keys)
        _active_syms = set(r[0] for r in _active)
        _model_syms = set(
            _strip_model_direction_suffix(k.replace("meta_", "")) for k, _ in _model_rows
        )
        _no_model = _active_syms - _model_syms
        if _no_model:
            _score -= _warn("models.coverage", f"Active picks with no model: {list(_no_model)}")
        else:
            _ok("models.coverage", "all active picks have v3 models")

        # ── 7. Confidence calibration ─────────────────────────────────────
        with db_conn() as _conn:
            _cur = _conn.cursor()
            _cur.execute("""SELECT confidence, outcome FROM predictions
                            WHERE outcome IN ('WIN','LOSS') AND confidence IS NOT NULL
                            ORDER BY resolved_at DESC LIMIT 100""")
            _cal = _cur.fetchall()

        if len(_cal) >= 10:
            _hi = [(c,o) for c,o in _cal if c >= 0.9]
            _lo = [(c,o) for c,o in _cal if c < 0.9]
            _hi_wr = round(sum(1 for c,o in _hi if o=="WIN")/len(_hi)*100) if _hi else None
            _lo_wr = round(sum(1 for c,o in _lo if o=="WIN")/len(_lo)*100) if _lo else None
            if _hi_wr is not None and _lo_wr is not None:
                if _hi_wr < _lo_wr:
                    _score -= _warn("confidence.calibration",
                        f"HIGH conf {_hi_wr}% WR < LOW conf {_lo_wr}% WR — confidence not meaningful")
                else:
                    _ok("confidence.calibration", f"high {_hi_wr}% WR vs low {_lo_wr}% WR — calibrated")

        # ── 8. Price feeds ────────────────────────────────────────────────
        _feeds = check_feeds()
        _working = sum(1 for v in _feeds.values() if v is True)
        _total = sum(1 for v in _feeds.values() if isinstance(v, bool))
        if _working == 0:
            _score -= _fail("price_feeds", "0 feeds responding — watchdog blind", 20)
        elif _working < 2:
            _score -= _warn("price_feeds", f"Only {_working}/{_total} feeds")
        else:
            _ok("price_feeds", f"{_working}/{_total} feeds responding")

    except Exception as _ex:
        _errors.append({"check": "diagnostics.crashed", "detail": str(_ex)})

    # morning_card.today: flag if no card today after 9AM CT
    try:
        import datetime as _mcdt, pytz as _mcpytz
        _mc_ct = _mcpytz.timezone("America/Chicago")
        _mc_now = _mcdt.datetime.now(_mc_ct)
        _mc_today = _mc_now.strftime("%Y-%m-%d")
        _mc_last = None
        try:
            with db_conn() as _mc_conn:
                _mc_cur = _mc_conn.cursor()
                _mc_cur.execute("SELECT val FROM ghost_state WHERE key='last_morning_card_date'")
                _mc_row = _mc_cur.fetchone()
                _mc_last = _mc_row[0] if _mc_row else None
        except Exception: pass
        if _mc_now.hour >= 9:
            if _mc_last == _mc_today:
                _passed.append({"check":"morning_card.today","detail":"Card sent today "+_mc_today,"status":"pass"})
            else:
                _errors.append({"check":"morning_card.today","detail":"No card today ("+_mc_today+") last:"+str(_mc_last),"status":"error"})
                _score -= 10
        else:
            _passed.append({"check":"morning_card.today","detail":"Before 9AM CT — OK","status":"pass"})
    except Exception as _mc_ex:
        _warnings.append({"check":"morning_card.today","detail":"Cannot verify: "+str(_mc_ex)[:60],"status":"warning"})

    _score = max(0, _score)
    return {
        "score": _score,
        "status": "healthy" if _score >= 80 else "degraded" if _score >= 50 else "critical",
        "checks_passed": len(_passed),
        "warnings": len(_warnings),
        "errors": len(_errors),
        "details": {"passed": _passed, "warnings": _warnings, "errors": _errors},
        "timestamp": _now,
    }


@router.post("/api/clean-garbage")
def clean_garbage(x_cron_secret: str = Header(default="")):
    """Delete broken predictions: absurd entry/target combos and crypto-era junk."""
    from wolf_app import CRYPTO_JUNK_WHERE, REAL_TRADE_WHERE, _cron_ok, db_conn  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM predictions WHERE entry_price > 50 AND target_price < 1 "
            "AND predicted_at IS NOT NULL"
        )
        absurd_count = cur.fetchone()[0]
        cur.execute(
            "DELETE FROM predictions WHERE entry_price > 50 AND target_price < 1 "
            "AND predicted_at IS NOT NULL"
        )
        absurd_deleted = cur.rowcount
        cur.execute("SELECT COUNT(*) FROM predictions WHERE " + CRYPTO_JUNK_WHERE)
        junk_count = cur.fetchone()[0]
        cur.execute("DELETE FROM predictions WHERE " + CRYPTO_JUNK_WHERE)
        junk_deleted = cur.rowcount
        cur.execute(
            "SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS') "
            "AND predicted_at IS NOT NULL AND " + REAL_TRADE_WHERE + " GROUP BY outcome"
        )
        counts = {r[0]: r[1] for r in cur.fetchall()}
    return {
        "ok": True,
        "absurd_deleted": absurd_deleted,
        "absurd_matched": absurd_count,
        "crypto_junk_deleted": junk_deleted,
        "crypto_junk_matched": junk_count,
        "deleted": absurd_deleted + junk_deleted,
        "remaining": counts,
    }


@router.post("/api/dedup-picks", include_in_schema=False)
def dedup_picks(x_cron_secret: str = Header(None)):
    """Expire duplicate open picks per symbol (keep highest confidence). Requires CRON_SECRET header."""
    from wolf_app import _cron_ok, db_conn  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403, detail="Forbidden")
    now = int(time.time())
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, symbol, confidence FROM predictions WHERE outcome IS NULL AND expires_at > %s "
                "ORDER BY symbol, confidence DESC",
                (now,),
            )
            rows = cur.fetchall()
            seen = {}
            to_expire = []
            for pid, sym, conf in rows:
                if sym not in seen:
                    seen[sym] = pid
                else:
                    to_expire.append(pid)
            if to_expire:
                cur.execute(
                    "UPDATE predictions SET outcome='EXPIRED', resolved_at=%s WHERE id = ANY(%s)",
                    (now, to_expire),
                )
        return {"ok": True, "expired": len(to_expire), "kept": len(seen)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/migrate-outcomes")
def migrate_outcomes(x_cron_secret: str = Header(default="")):
    """INSERT from ghost_prediction_outcomes (13k rows) into predictions."""
    from wolf_app import _cron_ok, db_conn  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO predictions
                    (symbol, direction, confidence, entry_price, target_price, stop_price,
                     run_at, predicted_at, expires_at, resolved_at, outcome, exit_price, pnl_pct, asset_type)
                SELECT
                    gpo.symbol,
                    COALESCE(gpo.predicted_direction, 'UP'),
                    COALESCE(gpo.predicted_confidence, 0.5),
                    gpo.price_at_prediction,
                    gpo.price_at_prediction * 1.06,
                    gpo.price_at_prediction * 0.97,
                    EXTRACT(EPOCH FROM gpo.created_at)::BIGINT,
                    EXTRACT(EPOCH FROM gpo.created_at)::BIGINT,
                    EXTRACT(EPOCH FROM COALESCE(gpo.closed_at, gpo.created_at + INTERVAL '48 hours'))::BIGINT,
                    EXTRACT(EPOCH FROM gpo.closed_at)::BIGINT,
                    CASE WHEN gpo.hit_direction = 1 THEN 'WIN' ELSE 'LOSS' END,
                    gpo.price_at_resolution,
                    gpo.realized_move_pct,
                    'stock'
                FROM ghost_prediction_outcomes gpo
                WHERE gpo.hit_direction IS NOT NULL
                AND gpo.price_at_prediction IS NOT NULL
                AND gpo.price_at_prediction > 0
                AND gpo.closed_at IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM predictions p2
                    WHERE p2.symbol = gpo.symbol
                    AND p2.resolved_at = EXTRACT(EPOCH FROM gpo.closed_at)::BIGINT
                    AND p2.outcome IS NOT NULL
                )
            """)
            inserted = cur.rowcount
            cur.execute("SELECT outcome, COUNT(*) FROM predictions WHERE outcome IS NOT NULL GROUP BY outcome")
            counts = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM ghost_prediction_outcomes WHERE hit_direction IS NOT NULL")
            source_rows = cur.fetchone()[0]
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "inserted": inserted, "source_rows": source_rows, "outcome_counts": counts}


@router.post("/api/watchdog")
def run_watchdog(x_cron_secret: str = Header(default="")):
    """Check open picks vs live prices. Send Telegram alert if target or stop hit."""
    from wolf_app import LOGGER, _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret):
        raise HTTPException(status_code=403)
    from core.prediction import reconcile_outcomes
    from core.telegram import send_position_alert
    from core.prices import get_price
    from core.db import db_conn
    import time
    alerted = []
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id,symbol,direction,entry_price,target_price,stop_price,asset_type,confidence FROM predictions"
                " WHERE outcome IS NULL AND predicted_at IS NOT NULL AND entry_price > 0"
                " AND target_price IS NOT NULL AND stop_price IS NOT NULL LIMIT 50"
            )
            open_picks = cur.fetchall()
        for pred_id, symbol, direction, entry, target, stop, asset_type, conf in open_picks:
            price = get_price(symbol, asset_type or "stock")
            if not price: continue
            hit = None
            if direction == "UP":
                if price >= target: hit = "WIN"
                elif price <= stop: hit = "LOSS"
            else:
                if price <= target: hit = "WIN"
                elif price >= stop: hit = "LOSS"
            if hit:
                from core.pnl import resolution_exit
                exit_price, pnl = resolution_exit(hit, direction, entry, target, stop, price)
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE predictions SET outcome=%s,exit_price=%s,pnl_pct=%s,resolved_at=%s WHERE id=%s AND outcome IS NULL",
                        (hit, exit_price, pnl, int(time.time()), pred_id))
                    if cur.rowcount == 0:
                        continue  # already resolved by another path
                try:
                    usd_out = round(100 * (1 + pnl / 100), 2)
                    send_position_alert(symbol, direction, hit, entry, exit_price, pnl, usd_out)
                except Exception as e:
                    LOGGER.error("watchdog alert " + symbol + ": " + str(e))
                alerted.append({"symbol":symbol,"outcome":hit,"pnl":round(pnl,2)})
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)
    return {"ok": True, "alerted": len(alerted), "hits": alerted}


@router.post("/api/test-alert")
def test_alert(x_cron_secret: str = Header(default="")):
    """Send test message to Telegram to verify connection."""
    from wolf_app import _cron_ok  # late import — shared state + monkeypatch-safe
    if not _cron_ok(x_cron_secret, strict=True):
        raise HTTPException(status_code=403)
    from core.telegram import send_test
    ok = send_test()
    return {"ok": ok, "message": "Test alert sent to Telegram + Discord"}
