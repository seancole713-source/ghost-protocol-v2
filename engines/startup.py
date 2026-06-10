# ══════════════════════════════════════════════════════════════
# FILE: engines/startup.py
# PURPOSE: Startup event handler — runs on app boot. Initializes DB tables,
#          loads models, starts the prediction loop, paper trading,
#          accuracy tracking, and background tasks.
# STATUS: STABLE (recently patched)
# LINES: ~911
# ──────────────────────────────────────────────────────────────
# CHANGE LOG:
#   2026-03-19 — Briefing header added (Browser Agent)
#   2026-03-19 — Bug #23 fixed: wrapped _get_conn() and _get_connection()
#                in proper 'with' context managers (lines ~361, ~472)
# ──────────────────────────────────────────────────────────────
# KNOWN ISSUES:
#   - Market mood update fails when SPY data unavailable (weekends/after hours)
# ──────────────────────────────────────────────────────────────
# DO NOT CHANGE (frozen interfaces):
#   startup_event()            — registered in wolf_app.py as on_event("startup")
#   run_ghost_loop()           — main prediction loop, called from startup
#   IMPORTANT: All get_sync_connection() / _get_conn() / _get_connection()
#              calls MUST use 'with' statement — they are @contextmanager
# ══════════════════════════════════════════════════════════════
"""Event handler: startup — extracted from wolf_app.py (Step 12)"""
# fmt: off
# ruff: noqa

import asyncio
import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone, timedelta

LOGGER = logging.getLogger("ghost")

# ── Inject all app-config constants (STAGE1_ENABLED, SIM_MODE, etc.) ─────
# engines/startup.py is a thin module extracted from wolf_app.py (Step 12).
# _on_startup() and helper functions reference many module-level constants
# (STAGE1_ENABLED, DATABASE_URL, SIM_MODE, DEFAULT_STOCKS, …) that live in
# engines/app_config.py.  Injecting them here mirrors the pattern used in
# wolf_app.py and all 16 route modules, and fixes the NameError cascade
# that was keeping all 9 background workers from starting.
try:
    import engines.app_config as _ac
    globals().update({k: v for k, v in vars(_ac).items() if not k.startswith("__")})
    del _ac
except Exception as _ac_err:
    LOGGER.warning(f"[STARTUP] Could not inject app_config globals: {_ac_err}")

# ── Missing imports that were lost when wolf_app.py was split (Step 12) ──
# _post_startup_init: launches alert-worker, accuracy-tracker, autopilot,
#   price-recorder, doctor-cron, news-analysis, self-improvement, etc.
# _heartbeat_pulse:  records task aliveness for the Health tab.
try:
    from wolf_helpers import _post_startup_init
except Exception as _wh_err:
    LOGGER.warning(f"[STARTUP] Could not import _post_startup_init: {_wh_err}")
    async def _post_startup_init():  # type: ignore[misc]
        pass

# ── Also inject wolf_helpers functions ─────────────────────────────────
# Helper functions used by _on_startup() body: _ensure_startup_dirs,
# _ensure_metrics_registered, _init_forecast_tables, _get_redis, etc.
# These lived in wolf_app.py before Step 12 extraction.
try:
    import wolf_helpers as _wh
    globals().update({k: v for k, v in vars(_wh).items() if not k.startswith("__")})
    del _wh
except Exception as _wh2_err:
    LOGGER.warning(f"[STARTUP] Could not inject wolf_helpers globals: {_wh2_err}")

try:
    from core.heartbeat import pulse as _heartbeat_pulse
except Exception as _hb_err:
    LOGGER.warning(f"[STARTUP] Could not import heartbeat.pulse: {_hb_err}")
    def _heartbeat_pulse(name: str, **kw) -> None:  # type: ignore[misc]
        pass

async def _on_startup():
    """
    Startup handler with comprehensive error protection.
    Each initialization step is wrapped in try/except to prevent cascading failures.
    """
    import os as _os_module  # Import locally to avoid UnboundLocalError

    # Railway debugging: Log immediately to confirm app is starting
    print("[RAILWAY DEBUG] ==========================================")
    print("[RAILWAY DEBUG] GHOST STARTING - Python import successful")
    print(f"[RAILWAY DEBUG] PORT: {_os_module.getenv('PORT', 'NOT_SET')}")
    print(f"[RAILWAY DEBUG] RAILWAY_ENVIRONMENT: {_os_module.getenv('RAILWAY_ENVIRONMENT', 'NOT_SET')}")
    print(f"[RAILWAY DEBUG] REDIS_URL: {'SET' if _os_module.getenv('REDIS_URL') else 'NOT_SET'}")
    print("[RAILWAY DEBUG] ==========================================")

    LOGGER.info("[GHOST STARTUP] Beginning initialization...")

    # #21: Initialize shared asyncpg connection pool (replaces 20+ psycopg2.connect calls)
    try:
        from core.db_pool import init_pool
        await init_pool()
        LOGGER.info("[GHOST STARTUP] ✅ Shared asyncpg pool initialized")
    except Exception as e:
        LOGGER.error(f"[GHOST STARTUP] ⚠️ DB pool init failed (non-fatal): {e}")

    # Log critical environment configuration at boot
    try:
        env_config = {
            "STOCKS_ENABLED": _os_module.getenv("STOCKS_ENABLED", "1"),
            "PRICE_STRICT_LIVE": _os_module.getenv("PRICE_STRICT_LIVE", "0"),
            "PRICE_REQUIRE_QUORUM": _os_module.getenv("PRICE_REQUIRE_QUORUM", "0"),
            "PREDICT_REQUIRE_PRICE_QUORUM": _os_module.getenv("PREDICT_REQUIRE_PRICE_QUORUM", "0"),
            "STOCK_PRICE_SOURCE": _os_module.getenv("STOCK_PRICE_SOURCE", "alpaca"),
            "REDIS_URL_SET": bool(_os_module.getenv("REDIS_URL")),
            "OPENAI_KEY_SET": bool(_os_module.getenv("OPENAI_API_KEY")),
            "TELEGRAM_TOKEN_SET": bool(_os_module.getenv("TELEGRAM_BOT_TOKEN")),
        }
        LOGGER.info(f"[GHOST BOOT] Environment flags: {json.dumps(env_config)}")
    except Exception:
        LOGGER.warning("Failed to log env config", exc_info=False)

    # Ensure Prometheus metrics registered
    try:
        _ensure_metrics_registered()
        LOGGER.info("prometheus_metrics_registered", extra={"component": "startup"})
    except Exception as e:
        LOGGER.error(f"metrics_registration_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup

    # Log OpenAI/AI provider config for debugging
    try:
        key_mask = (
            (OPENAI_API_KEY[:8] + "..." + OPENAI_API_KEY[-4:]) if OPENAI_API_KEY else "(not set)"
        )
        LOGGER.info(
            f"AI startup config: provider={AI_PROVIDER}, model={AGENT_MODEL}, OPENAI_API_KEY={key_mask}",
            extra={"component": "startup"},
        )
    except Exception as e:
        LOGGER.warning(f"Failed to log AI config: {e}", extra={"component": "startup"})
    # Ensure required directories exist
    try:
        _ensure_startup_dirs()
        LOGGER.info("[GHOST STARTUP] Directories created")
    except Exception as e:
        LOGGER.error(f"startup_dirs_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Critical failure - but try to continue

    # Run database migrations (personal watchlist, etc.)
    # NOTE: Wrapped in try/except, non-blocking, has 5s timeout on PostgreSQL connection
    try:
        from core.migration_runner import run_migrations, ensure_personal_watchlist_table, ensure_checkpoint_columns
        success, messages = run_migrations()
        for msg in messages:
            LOGGER.info(msg)
        if success:
            LOGGER.info("[GHOST STARTUP] ✅ Database migrations complete")
        else:
            LOGGER.warning("[GHOST STARTUP] ⚠️  Some migrations failed (see logs above)")
        
        # Double-check personal watchlist table exists (creates if missing)
        if ensure_personal_watchlist_table():
            LOGGER.info("[GHOST STARTUP] ✅ Personal watchlist table ready")
        else:
            LOGGER.warning("[GHOST STARTUP] ⚠️  Personal watchlist table could not be verified")
        
        # Ensure checkpoint columns exist for Trust Ladder multi-checkpoint system
        if ensure_checkpoint_columns():
            LOGGER.info("[GHOST STARTUP] ✅ Paper trades checkpoint columns ready")
        else:
            LOGGER.warning("[GHOST STARTUP] ⚠️  Paper trades checkpoint columns could not be verified")
    except Exception as e:
        LOGGER.error(f"migrations_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup

    # Initialize forecast tables
    try:
        _init_forecast_tables()
        LOGGER.info("[GHOST STARTUP] Forecast tables initialized")
    except Exception as e:
        LOGGER.error(f"forecast_tables_init_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup
    
    # Initialize Telegram alerts module (CRITICAL for VIP scanner, movers, daily reports)
    try:
        from core import telegram_alerts
        
        # NOTE: core.telegram_alerts expects TELEGRAM_SEND_FUNC(chat_id, text) -> bool.
        # Use the local HTML sender to avoid signature mismatches.
        
        # Inject dependencies
        telegram_alerts.REDIS_CLIENT = _get_redis()
        telegram_alerts.TELEGRAM_SEND_FUNC = _tg_send_chat_message
        telegram_alerts.TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID
        telegram_alerts.LOGGER = LOGGER
        
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            LOGGER.info("[GHOST STARTUP] ✅ Telegram alerts module initialized")
        else:
            LOGGER.warning("[GHOST STARTUP] ⚠️  Telegram disabled (missing BOT_TOKEN or CHAT_ID)")
    except Exception as e:
        LOGGER.error(f"telegram_alerts_init_failed: {e}", extra={"component": "startup"}, exc_info=False)
    
    # Initialize AI Memory (persistent long-term memory with PostgreSQL)
    try:
        from core.ai_memory import AIMemory
        import state
        
        memory_db_path = _os_module.getenv("AI_MEMORY_DB_PATH", 
                                           _os_module.path.join(_os_module.getenv("DATA_DIR", "data"), "ai_memory.db"))
        vector_store = _os_module.getenv("VECTOR_SOURCE", "chromadb")
        
        state.AI_MEMORY_STORE = AIMemory(db_path=memory_db_path, vector_store=vector_store)
        state.AI_MEMORY_RING = []  # Legacy ring buffer (deprecated but still referenced)
        
        LOGGER.info(f"[GHOST STARTUP] ✅ AI Memory initialized: {memory_db_path} (vector: {vector_store})")
    except Exception as e:
        LOGGER.error(f"ai_memory_init_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup
    
    # Initialize graceful shutdown handler (Phase 5.8)
    try:
        from core.shutdown_handler import install_shutdown_handler, register_cleanup
        
        # Install signal handlers
        install_shutdown_handler()
        
        # Register cleanup callbacks
        def stop_prediction_loop():
            try:
                from core.auto_prediction_loop import stop_auto_prediction_loop
                stop_auto_prediction_loop()
                LOGGER.info("[SHUTDOWN] Prediction loop stopped")
            except Exception as e:
                LOGGER.error(f"[SHUTDOWN] Failed to stop prediction loop: {e}")
        
        register_cleanup(stop_prediction_loop)
        LOGGER.info("[GHOST STARTUP] ✅ Graceful shutdown handler installed")
    except Exception as e:
        LOGGER.error(f"shutdown_handler_init_failed: {e}", extra={"component": "startup"}, exc_info=False)
    
    # Initialize goals from environment
    try:
        from core.goals_tracker import GoalsTracker
        tracker = GoalsTracker()
        existing = tracker.get_all_goals()
        
        # Check if goals are already set
        has_goals = any(g.get('target', 0) > 0 for g in existing.values())
        
        if not has_goals:
            # Initialize from environment variable
            weekly_target = float(_os_module.getenv("TARGET_WEEKLY_PROFIT_USD", "300"))
            
            # Calculate other periods based on weekly target
            daily_target = weekly_target / 5  # 5 trading days per week
            monthly_target = weekly_target * 4  # ~4 weeks per month
            yearly_target = weekly_target * 52  # 52 weeks per year
            
            tracker.set_goal("daily", daily_target)
            tracker.set_goal("weekly", weekly_target)
            tracker.set_goal("monthly", monthly_target)
            tracker.set_goal("yearly", yearly_target)
            
            LOGGER.info(f"[GHOST STARTUP] Goals initialized: weekly=${weekly_target}, yearly=${yearly_target}")
        else:
            LOGGER.info("[GHOST STARTUP] Goals already configured (skipping initialization)")
    except Exception as e:
        LOGGER.error(f"goals_init_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup
    
    # ── DISABLED: PostgreSQL model overwrite ──────────────────────────────
    # ROOT CAUSE of 0% feature quality (f0-f27 bug):
    #   1. retrain_production_model.py saved a BARE XGBClassifier to PostgreSQL
    #      (no feature_names dict wrapper)
    #   2. On EVERY startup, this code loaded that bare model and overwrote
    #      the git pkl (which has 59 named features in dict format)
    #   3. XGBoostModel._load_trained_model() then got the bare model → f0-f27
    #   4. Pipeline features (RSI_14, MACD_LINE, etc.) couldn't match → ALL defaulted
    #   5. Model was literally predicting on zeros every single cycle
    #
    # FIX: Use the git-committed pkl directly (59-feature dict, 84.8% test acc).
    # The retrain script has also been fixed to save dict format going forward.
    # ────────────────────────────────────────────────────────────────────────
    LOGGER.info(
        "[GHOST STARTUP] Using git-committed XGBoost model (59 features, dict format). "
        "PostgreSQL model load DISABLED — was overwriting good model with bare XGBClassifier."
    )
    
    # Stage 1: Initialize Context Awareness Layer
    if STAGE1_ENABLED:
        try:
            task = initialize_stage1()
            if task:
                LOGGER.info(
                    "[GHOST STARTUP] Stage 1 initialized: world_context, market_mood",
                    extra={
                        "component": "startup",
                        "features": "world_context,market_mood",
                        "update_interval": "5min",
                    },
                )
            else:
                LOGGER.warning("stage1_init_no_task", extra={"component": "startup"})
        except Exception as e:
            LOGGER.error(f"stage1_init_failed: {e}", extra={"component": "startup"}, exc_info=False)
            # Non-critical - continue startup
    # Stage 2: Initialize Self-Evaluation System
    if STAGE2_ENABLED:
        try:
            get_accuracy_tracker()
            learning = get_learning_loop()
            LOGGER.info(
                "[GHOST STARTUP] Stage 2 initialized: accuracy_tracker, learning_loop",
                extra={
                    "component": "startup",
                    "features": "accuracy_tracker,learning_loop",
                    "mape_threshold": learning.mape_threshold,
                },
            )
        except Exception as e:
            LOGGER.error(f"stage2_init_failed: {e}", extra={"component": "startup"}, exc_info=False)
            # Non-critical - continue startup

    # Confidence calibration auto-builder (non-blocking)
    # Keeps calibration curves fresh so outcomes influence future signal confidence.
    try:
        from core.confidence_calibrator import get_confidence_calibrator

        calib_enabled = _os_module.getenv("CONFIDENCE_CALIBRATION_AUTOBUILD_ENABLED", "1").strip() not in ("0", "false", "False")
        calib_interval_s = int(_os_module.getenv("CONFIDENCE_CALIBRATION_AUTOBUILD_INTERVAL_S", "21600"))  # 6h
        calib_min_predictions = int(_os_module.getenv("CONFIDENCE_CALIBRATION_MIN_PREDICTIONS", "50"))

        async def _calibration_builder_loop():
            if not calib_enabled:
                LOGGER.info("[CALIBRATION] Auto-build disabled")
                return

            await asyncio.sleep(60)  # let DB/services come up
            while True:
                try:
                    calibrator = get_confidence_calibrator()
                    res = await calibrator.build_calibration(min_predictions=calib_min_predictions)
                    if res.get("ok"):
                        LOGGER.info(
                            f"[CALIBRATION] ✅ Updated curves: total={res.get('total_predictions')} "
                            f"quality_threshold={res.get('quality_threshold')}"
                        )
                    else:
                        LOGGER.info(f"[CALIBRATION] Skipped: {res.get('error') or 'not ready'}")
                except Exception as e:
                    LOGGER.error(f"[CALIBRATION] Auto-build error: {e}", exc_info=False)

                await asyncio.sleep(max(600, calib_interval_s))

        loop = asyncio.get_running_loop()
        loop.create_task(_calibration_builder_loop())
        LOGGER.info("[GHOST STARTUP] ✅ Confidence calibration auto-builder scheduled")
    except Exception as e:
        LOGGER.error(f"calibration_autobuilder_start_failed: {e}", extra={"component": "startup"}, exc_info=False)

    # NOTE: Auto-prediction loop is started in wolf_helpers.py initialization
    # Do NOT start it here to avoid duplicate loop instances

    # Stage 3: Initialize Continuous Improvement System
    if STAGE3_ENABLED:
        try:
            get_ensemble_forecaster()
            regime = get_regime_detector()
            risk = get_risk_engine()
            LOGGER.info(
                "[GHOST STARTUP] Stage 3 initialized: ensemble, regime, risk",
                extra={
                    "component": "startup",
                    "features": "ensemble_forecaster,regime_detector,risk_engine",
                    "ensemble_models": 4,
                    "current_regime": regime.current_regime,
                    "risk_limits": {
                        "max_drawdown_pct": risk.max_drawdown_pct,
                        "max_position_pct": risk.max_single_position_pct,
                    },
                },
            )
        except Exception as e:
            LOGGER.error(f"stage3_init_failed: {e}", extra={"component": "startup"}, exc_info=False)
            # Non-critical - continue startup

    # Stage 3.5: Start Accuracy Evaluator + Feedback Loop Background Task (Task #4)
    try:
        import asyncio as _asyncio_module
        from core.prediction_evaluator import evaluate_pending_predictions
        from core.feedback_loop import get_feedback_loop, PredictionOutcome
        from core.learning_loop import get_learning_loop
        
        async def _accuracy_evaluator_loop():
            """Background task to evaluate prediction outcomes every hour + feed to learning system"""
            # Run once immediately on startup, then hourly
            first_run = True
            while True:
                try:
                    if not first_run:
                        await _asyncio_module.sleep(3600)  # Run every hour (skip sleep on first iteration)
                    first_run = False
                    
                    LOGGER.info("[ACCURACY] Running prediction evaluator + feedback loop...")
                    
                    # Run in thread pool to avoid blocking asyncio
                    loop = _asyncio_module.get_event_loop()
                    await loop.run_in_executor(None, evaluate_pending_predictions)
                    
                    # Task #4: Check for completed predictions and learn from them
                    def _process_outcomes():
                        try:
                            from core.accuracy_tracker import get_accuracy_tracker
                            tracker = get_accuracy_tracker()
                            feedback = get_feedback_loop()
                            
                            # AccuracyTracker uses PostgreSQL — query via its connection
                            if not getattr(tracker, '_enabled', False):
                                return
                            
                            try:
                                with tracker._get_conn() as conn:
                                    cur = conn.cursor()
                                    cur.execute("""
                                        SELECT 
                                            id, symbol, direction, confidence,
                                            entry_price, exit_price, was_correct,
                                            pnl_pct, metadata, 
                                            EXTRACT(EPOCH FROM created_at) as ts
                                        FROM accuracy_forecasts
                                        WHERE was_correct IS NOT NULL
                                        AND created_at > NOW() - INTERVAL '24 hours'
                                        ORDER BY created_at DESC
                                        LIMIT 100
                                    """)
                                    completed = cur.fetchall()
                            except Exception as db_err:
                                LOGGER.debug(f"[FEEDBACK LOOP] DB query failed: {db_err}")
                                return
                            
                            outcomes_processed = 0
                            for row in completed:
                                (forecast_id, symbol, direction, conf,
                                 pred_price, actual_price, was_correct,
                                 pnl_pct, metadata_json, ts) = row
                                
                                # Parse metadata
                                try:
                                    metadata = json.loads(metadata_json) if isinstance(metadata_json, str) else (metadata_json or {})
                                except (ValueError, TypeError):
                                    metadata = {}
                                
                                signals = metadata.get("signals", [])
                                features = metadata.get("features", {})
                                
                                # Create outcome for feedback loop
                                outcome = PredictionOutcome(
                                    prediction_id=forecast_id,
                                    symbol=symbol,
                                    direction=direction or "FLAT",
                                    confidence=conf or 0.5,
                                    predicted_price=pred_price or 0,
                                    actual_price=actual_price or 0,
                                    was_correct=bool(was_correct),
                                    accuracy_pct=100 - abs(pnl_pct or 0),
                                    signals_used=signals,
                                    features=features,
                                    timestamp=ts or time.time()
                                )
                                
                                # Feed to learning system
                                feedback.record_outcome(outcome)
                                outcomes_processed += 1
                            
                            if outcomes_processed > 0:
                                LOGGER.info(f"[FEEDBACK LOOP] ✅ Processed {outcomes_processed} outcomes for learning")
                                
                                # Trigger learning loop to update weights if enough outcomes
                                try:
                                    learning = get_learning_loop()
                                    check = learning.check_performance(symbol=None, days=7)
                                    if check.get("needs_tuning"):
                                        analysis = learning.analyze_bias(check["metrics"])
                                        recs = analysis.get("recommendations", [])
                                        if recs:
                                            learning.adjust_parameters(recs, auto_apply=True)
                                            LOGGER.info(f"[LEARNING] ✅ Applied {len(recs)} parameter adjustments")
                                except Exception as learn_err:
                                    LOGGER.debug(f"[LEARNING] Learning cycle skipped: {learn_err}")
                        
                        except Exception as feedback_err:
                            LOGGER.error(f"[FEEDBACK LOOP] Error processing outcomes: {feedback_err}", exc_info=False)
                    
                    await loop.run_in_executor(None, _process_outcomes)
                    LOGGER.info("[ACCURACY] Prediction evaluation + feedback complete")
                    
                except Exception as eval_err:
                    LOGGER.error(f"[ACCURACY] Evaluator error: {eval_err}", exc_info=False)
        
        _asyncio_module.create_task(_accuracy_evaluator_loop())
        LOGGER.info("[GHOST STARTUP] ✅ Accuracy evaluator + feedback loop scheduled (hourly)")
    except Exception as e:
        LOGGER.error(f"accuracy_evaluator_start_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup

    # 📈 Start Paper Trade Reconciliation Scheduler
    # Runs every hour to check if any paper trades have reached their 48h target time
    try:
        import asyncio as _asyncio_module
        
        async def _paper_trade_reconciler_loop():
            """Background task to reconcile paper trades every 15 minutes."""
            # Wait 60 seconds on startup before first run (was 300s - too long)
            await _asyncio_module.sleep(60)
            
            while True:
                try:
                    from core.paper_tracker import get_paper_tracker
                    tracker = get_paper_tracker()
                    price_data = {}
                    
                    # CRITICAL FIX: Use PaperTracker's own connection abstraction
                    # instead of raw psycopg2 (which crashes when DATABASE_URL is SQLite)
                    conn = None
                    try:
                        with tracker._get_connection() as conn:
                            now_str = datetime.utcnow().isoformat()
                            cur = tracker._execute(conn, """
                                SELECT DISTINCT symbol FROM paper_trades 
                                WHERE outcome = 'PENDING' 
                                AND target_time <= ?
                            """, (now_str,))
                            rows = tracker._fetchall(cur)
                            symbols = [(row["symbol"],) for row in rows]
                    except Exception as query_err:
                        LOGGER.error(f"[PAPER] Failed to query pending trades: {query_err}")
                        symbols = []
                    
                    if symbols:
                        LOGGER.info(f"[PAPER] Found {len(symbols)} symbols with due trades, fetching prices...")
                        
                        # Fetch current prices for symbols with due trades (WOLF-only: stocks)
                        failed_symbols = []
                        for (symbol,) in symbols:
                            try:
                                # WOLF-only mode: stock prices only
                                stock_result = turbo_stock_price(symbol, max_budget_s=2.0)
                                if stock_result and stock_result.get("ok") and stock_result.get("price"):
                                    price_data[symbol] = stock_result["price"]
                                    LOGGER.info(f"[PAPER] Stock price for {symbol}: ${stock_result['price']:.2f}")
                                else:
                                    failed_symbols.append(symbol)
                            except Exception as price_err:
                                failed_symbols.append(symbol)
                                LOGGER.debug(f"[PAPER] Price fetch failed for {symbol}: {price_err}")
                        
                        if failed_symbols:
                            LOGGER.warning(
                                f"[PAPER] ⚠️ Price fetch failed for {len(failed_symbols)} symbols "
                                f"(will retry next cycle): {failed_symbols[:10]}"
                            )
                        
                        if price_data:
                            resolved = tracker.check_all_pending(price_data)
                            if resolved:
                                LOGGER.info(f"[PAPER] ✅ Resolved {len(resolved)} paper trades")
                                
                                # Alert on first significant resolution batch
                                if len(resolved) >= 10:
                                    try:
                                        stats = tracker.get_stats(days=365)
                                        win_rate = stats.get("win_rate", 0)
                                        LOGGER.info(
                                            f"[PAPER] 📊 Stats update: "
                                            f"resolved={stats.get('resolved_trades', 0)}, "
                                            f"win_rate={win_rate:.1%}"
                                        )
                                    except Exception as e:
                                        LOGGER.warning(f"paper_trade_stats_log_failed: {e}")
                    else:
                        LOGGER.debug("[PAPER] No paper trades due for resolution yet")
                
                except Exception as paper_err:
                    LOGGER.error(f"[PAPER] Reconciler error: {paper_err}", exc_info=False)
                
                # Sleep for 15 minutes (was 1 hour - too long, trades expire between cycles)
                await _asyncio_module.sleep(900)
        
        _asyncio_module.create_task(_paper_trade_reconciler_loop())
        LOGGER.info("[GHOST STARTUP] ✅ Paper trade reconciler scheduled (every 15 min)")
    except Exception as e:
        LOGGER.error(f"paper_trade_reconciler_start_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup

    # Phase 4/5: Start Watchlist Prediction Scheduler (Market Open/Close + Big Move Alerts)
    try:
        from core.watchlist_prediction_scheduler import start_watchlist_scheduler
        start_watchlist_scheduler()
        LOGGER.info("[GHOST STARTUP] ✅ Watchlist prediction scheduler started (market open/close + big moves)")
    except Exception as e:
        LOGGER.error(f"watchlist_scheduler_start_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup

    # Start Outcome Reconciler (70% Accuracy Goal)
    try:
        from services.outcome_reconciler_v2 import start_reconciler_background_task
        start_reconciler_background_task()
        LOGGER.info("[GHOST STARTUP] ✅ Outcome reconciler started (48h accuracy tracking)")
    except Exception as e:
        LOGGER.error(f"outcome_reconciler_start_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup

    # 🧠 Start Learning Cycle Scheduler (Ghost learns from mistakes!)
    # THIS IS THE KEY FIX - Without this, Ghost never improves over time
    try:
        from core.feedback_loop import start_learning_scheduler
        start_learning_scheduler()
        LOGGER.info("[GHOST STARTUP] ✅ Learning cycle scheduler started (continuous improvement)")
    except Exception as e:
        LOGGER.error(f"learning_scheduler_start_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup

    # 🎯 Start V2 Quality Auto-Updater (symbol whitelist/blacklist)
    # Updates daily from PostgreSQL-verified performance data
    try:
        from core.v2_quality import get_quality_system
        import json
        quality_system = get_quality_system()
        
        # FIX (Jan 27, 2026): If PostgreSQL is empty but JSON has data, sync JSON to PostgreSQL
        # This fixes the issue where trial_stocks/whitelist don't persist after deploy
        if not quality_system._whitelist and not quality_system._trial_stocks:
            try:
                if os.path.exists("ghost_v2_quality.json"):
                    with open("ghost_v2_quality.json", 'r') as f:
                        data = json.load(f)
                    if data.get('whitelist') or data.get('trial_stocks'):
                        quality_system._whitelist = set(data.get('whitelist', []))
                        quality_system._blacklist = set(data.get('blacklist', []))
                        quality_system._trial_stocks = set(data.get('trial_stocks', []))
                        quality_system._config = data.get('config', {})
                        quality_system._save_config()  # Sync to PostgreSQL
                        LOGGER.info(f"[V2-STARTUP] ✅ Synced JSON to PostgreSQL: {len(quality_system._whitelist)} whitelist, {len(quality_system._trial_stocks)} trial_stocks")
            except Exception as sync_err:
                LOGGER.warning(f"[V2-STARTUP] JSON sync failed: {sync_err}")
        
        quality_system.start_auto_update_scheduler(interval_hours=24)
        LOGGER.info("[GHOST STARTUP] ✅ V2 Quality auto-scheduler started (daily whitelist updates)")
    except Exception as e:
        LOGGER.error(f"v2_quality_scheduler_start_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup

    # 🔄 Start Weekly Auto-Calibration Scheduler (Ghost tunes itself!)
    # Runs every Sunday at 5:00 AM CT to find optimal strategies
    try:
        from core.auto_calibrate_scheduler import start_weekly_calibration_scheduler
        start_weekly_calibration_scheduler()
        LOGGER.info("[GHOST STARTUP] ✅ Weekly auto-calibration scheduler started (Sundays 5AM CT)")
    except Exception as e:
        LOGGER.warning(f"auto_calibration_scheduler_start_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup

    # 🧠 Start Model Retrain Scheduler (Ghost learns from its mistakes!)
    # Retrains XGBoost every 14 days on recent outcome data
    try:
        from core.ml_trainer import start_retrain_scheduler
        start_retrain_scheduler()
        LOGGER.info("[GHOST STARTUP] ✅ Model retrain scheduler started (every 14 days)")
    except Exception as e:
        LOGGER.warning(f"retrain_scheduler_start_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup

    # 🔄 Start Online Calibrator Scheduler (Feb 25, 2026)
    # Periodically recalibrates horizon/strategy weights based on actual performance.
    # Runs every 6 hours, checks for model drift, and auto-adjusts weights.
    try:
        import threading
        
        def _online_calibration_loop():
            """Background thread: run calibration every 6 hours."""
            try:
                import time as _time
                _heartbeat_pulse("online-calibrator")  # Pulse immediately so Health tab shows alive
                LOGGER.info("[CALIBRATOR] ⏳ Waiting 10 min before first calibration...")
                _time.sleep(600)  # Wait 10 min after startup for data to be available
                
                while True:
                    _heartbeat_pulse("online-calibrator")
                    try:
                        from core.online_calibrator import get_online_calibrator
                        calibrator = get_online_calibrator()
                        
                        # Calibrate horizon weights
                        horizon_result = calibrator.calibrate_horizon_weights()
                        if horizon_result:
                            LOGGER.info(
                                f"[CALIBRATOR] ✅ Horizon weights recalibrated: "
                                f"gain={horizon_result.performance_gain:.1%}"
                            )
                        
                        # Calibrate strategy weights
                        strategy_result = calibrator.calibrate_strategy_weights()
                        if strategy_result:
                            LOGGER.info(
                                f"[CALIBRATOR] ✅ Strategy weights recalibrated: "
                                f"gain={strategy_result.performance_gain:.1%}"
                            )
                        
                    except Exception as e:
                        LOGGER.warning(f"[CALIBRATOR] Calibration cycle failed: {e}")
                    
                    _time.sleep(6 * 3600)  # Every 6 hours
            except Exception as _thread_err:
                LOGGER.error(f"[CALIBRATOR] ❌ Thread crashed: {_thread_err}", exc_info=True)
        
        _calibrator_thread = threading.Thread(
            target=_online_calibration_loop,
            daemon=True,
            name="online-calibrator"
        )
        _calibrator_thread.start()
        LOGGER.info("[GHOST STARTUP] ✅ Online calibrator scheduler started (every 6 hours)")
    except Exception as e:
        LOGGER.warning(f"online_calibrator_scheduler_start_failed: {e}", extra={"component": "startup"}, exc_info=False)
        # Non-critical - continue startup

    # CRITICAL: Initialize prediction store tables EARLY to prevent "table not found" errors
    # Do NOT wait for full pool init - just ensure tables exist
    try:
        from core.prediction_store import get_prediction_store
        store = get_prediction_store()
        # Force table initialization (quick operation, ~50ms)
        if hasattr(store.backend, '_init_db'):
            store.backend._init_db()
        LOGGER.info("[GHOST STARTUP] ✅ Prediction store tables initialized")
    except Exception as e:
        LOGGER.warning(f"[GHOST STARTUP] Prediction store init skipped: {e}", exc_info=False)
    
    # Pool initialization will happen in background during first query
    LOGGER.info("[GHOST STARTUP] ⚠️  Prediction store pool will initialize on first use (non-blocking)")

    # CRITICAL: Pre-populate _LATEST_PREDICTIONS cache to prevent cold-start slowness
    # RE-ENABLED with timeout protection (2s max) to prevent blocking startup
    try:
        async def _warmup_cache_with_timeout():
            try:
                from core.prediction_store import get_prediction_store
                store = get_prediction_store()
                LOGGER.info("[GHOST STARTUP] Warming _LATEST_PREDICTIONS cache (2s max)...")
                
                # Get latest 50 predictions from database with timeout
                recent_preds = await asyncio.wait_for(
                    asyncio.to_thread(store.get_recent_predictions, limit=50),
                    timeout=2.0
                )
                warmup_count = 0
                
                # EDGE WHITELIST (Feb 10, 2026): Only cache edge symbols on startup
                # Previously loaded 50 random predictions which polluted cache.
                _warmup_edge_enabled = os.getenv("EDGE_WHITELIST_ENABLED", "1") == "1"
                _warmup_edge_set = get_edge_set()
                
                # Populate cache with most recent prediction per symbol
                warmup_blocked = 0
                for pred in recent_preds:
                    symbol = pred.get("symbol")
                    # Skip non-edge symbols during warmup
                    if _warmup_edge_enabled and symbol and symbol.upper() not in _warmup_edge_set:
                        warmup_blocked += 1
                        continue
                    _warmup_dir = pred.get("direction", "")
                    # FIX (Mar 1, 2026): Skip predictions with no direction or non-actionable
                    # Old code defaulted to "FLAT" which created garbage cache entries
                    if symbol and symbol not in _LATEST_PREDICTIONS and _warmup_dir in ("UP", "DOWN"):
                        _LATEST_PREDICTIONS[symbol] = {
                            "prediction_id": pred.get("id"),
                            "symbol": symbol,
                            "run_at": pred.get("run_at", time.time()),
                            "confidence": pred.get("confidence", 0.5),
                            "direction": _warmup_dir,
                            "horizon_h": pred.get("horizon_h", 6),
                            "method": pred.get("method", "unknown"),
                            "price_at_prediction": pred.get("price_at_prediction"),
                            "price": pred.get("price_at_prediction"),  # FIX (Feb 24): cockpit expects "price"
                            "expected_move": pred.get("expected_move"),
                            "engine": pred.get("engine", "turbo"),  # FIX (Feb 24): was missing
                            "intel_applied": pred.get("intel_applied", False),  # FIX (Feb 24): was missing
                            "market": pred.get("market", "unknown"),  # FIX (Feb 24): was missing
                        }
                        warmup_count += 1
                
                LOGGER.info(f"[GHOST STARTUP] ✅ Cache warmed with {warmup_count} edge predictions ({warmup_blocked} non-edge blocked)")
            except asyncio.TimeoutError:
                LOGGER.warning("[GHOST STARTUP] Cache warmup timeout (2s) - endpoints will use DB fallback")
            except Exception as e:
                LOGGER.error(f"cache_warmup_failed: {e}", extra={"component": "startup"}, exc_info=False)
        
        # Run warmup in background (non-blocking)
        loop = asyncio.get_running_loop()
        loop.create_task(_warmup_cache_with_timeout())
    except Exception as e:
        LOGGER.error(f"cache_warmup_schedule_failed: {e}", extra={"component": "startup"}, exc_info=False)

    # ========================================================================
    # V2 CLEANUP: Remove non-whitelisted predictions from memory cache
    # This prevents old DASH/LRC predictions from persisting across restarts
    # ONLY runs when Money Game is OFF - Money Game uses its own rankings
    # ========================================================================
    use_money_game = os.getenv("USE_MONEY_GAME", "1") == "1"  # DEFAULT ON!
    
    if not use_money_game:
        try:
            from core.v2_quality import get_quality_system
            v2_quality = get_quality_system()
            
            symbols_to_remove = []
            for symbol in list(_LATEST_PREDICTIONS.keys()):
                should_keep, reason = v2_quality.should_predict(symbol, 1.0)
                if not should_keep:
                    symbols_to_remove.append(symbol)
            
            for symbol in symbols_to_remove:
                del _LATEST_PREDICTIONS[symbol]
            
            if symbols_to_remove:
                LOGGER.info(f"[V2-CLEANUP] 🧹 Removed {len(symbols_to_remove)} non-whitelisted predictions from cache: {symbols_to_remove}")
            else:
                LOGGER.info(f"[V2-CLEANUP] ✅ All cached predictions ({len(_LATEST_PREDICTIONS)}) are whitelisted")
        except Exception as e:
            LOGGER.error(f"v2_cleanup_failed: {e}", extra={"component": "startup"}, exc_info=False)
    else:
        LOGGER.info(f"[MONEY-GAME] ✅ V2 cleanup SKIPPED - Money Game mode uses profit rankings, not blacklists")

    # ========================================================================
    # ACCOUNTABILITY SYSTEMS - Killswitch status + Outcome Reconciler
    # ========================================================================
    try:
        from core.prediction_killswitch import get_killswitch
        killswitch = get_killswitch()
        status = killswitch.get_status()
        if status['predictions_enabled']:
            LOGGER.info("[GHOST STARTUP] ✅ Killswitch: Predictions ENABLED")
        else:
            LOGGER.warning(f"[GHOST STARTUP] ⛔ Killswitch: Predictions BLOCKED - {status['reason']}")
    except Exception as e:
        LOGGER.error(f"killswitch_init_failed: {e}", extra={"component": "startup"}, exc_info=False)
    
    # Start outcome reconciler background task (runs hourly)
    # NOTE: DISABLED - Using outcome_reconciler_v2 instead (started at line 3973)
    LOGGER.info("[GHOST STARTUP] ⚠️ Old outcome reconciler DISABLED - using V2 at line 3973")

    # ========================================================================
    # WOLF-only: Auto-trigger predictions on startup
    # Railway ephemeral storage loses predictions on redeploy.
    # ========================================================================
    try:
        async def _startup_predictions():
            """Trigger WOLF predictions 60s after startup."""
            await asyncio.sleep(60)  # Wait for app to fully initialize

            from config.symbols import get_edge_set

            edge_set = get_edge_set()
            EDGE_STOCKS = sorted(edge_set)

            TOP_STOCKS = EDGE_STOCKS[:5]

            LOGGER.info(f"[STARTUP PREDS] Edge symbols: {len(EDGE_STOCKS)} stocks")
            LOGGER.info(f"[STARTUP PREDS] Triggering predictions for {len(TOP_STOCKS)} stocks...")

            import httpx

            # Get base URL from environment or default to localhost
            port = _os_module.getenv("PORT", "8080")
            base_url = f"http://localhost:{port}"
            auth_token = _os_module.getenv("API_AUTH_TOKEN", "ghost-prod-2024")

            stocks_triggered = 0

            async with httpx.AsyncClient(timeout=30.0) as client:
                for symbol in TOP_STOCKS:
                    try:
                        resp = await client.post(
                            f"{base_url}/api/predict/run",
                            params={"symbol": symbol, "horizon": "SHORT"},
                            headers={"Authorization": f"Bearer {auth_token}"}
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            if data.get("ok") or data.get("direction"):
                                stocks_triggered += 1
                                LOGGER.info(f"[STARTUP PREDS] ✅ {symbol}: {data.get('direction')} conf={data.get('confidence', 0):.2f}")
                        await asyncio.sleep(5)
                    except Exception as e:
                        LOGGER.warning(f"[STARTUP PREDS] ⚠️ {symbol} failed: {e}")

            LOGGER.info(f"[STARTUP PREDS] ✅ Done: {stocks_triggered}/{len(TOP_STOCKS)} stocks")
        
        loop = asyncio.get_running_loop()
        loop.create_task(_startup_predictions())
        LOGGER.info("[GHOST STARTUP] 📈 Startup predictions scheduled (30s after boot)")
    except Exception as e:
        LOGGER.error(f"startup_predictions_schedule_failed: {e}", extra={"component": "startup"}, exc_info=False)

    # Final startup confirmation
    LOGGER.info("[GHOST STARTUP] ✅ Initialization complete - server ready")
    
    # Schedule post-startup initialization in background (non-blocking)
    # Use asyncio.get_running_loop() to ensure task is created in the right loop
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_post_startup_init())
        LOGGER.info("[GHOST STARTUP] 📋 Post-startup tasks scheduled (will run in 5s)")
    except Exception as task_err:
        LOGGER.error(f"[GHOST STARTUP] ❌ Failed to schedule post-startup tasks: {task_err}", exc_info=True)
    
    # Start AI Advisor if enabled
    if _os_module.getenv("AI_ADVISOR_ENABLED", "0") == "1":
        try:
            from core.ai_advisor.scanner import start_scanner
            loop = asyncio.get_running_loop()
            loop.create_task(start_scanner())
            LOGGER.info("[GHOST STARTUP] 🤖 AI Advisor autonomous scanner started (30s intervals)")
        except Exception as advisor_err:
            LOGGER.error(f"[GHOST STARTUP] AI Advisor start failed: {advisor_err}", exc_info=False)

    # ── Phase 5: WOLF Autonomous Monitor ─────────────────────────────────
    try:
        from core.wolf_monitor import start_wolf_monitor
        loop = asyncio.get_running_loop()
        loop.create_task(start_wolf_monitor())
        LOGGER.info("[GHOST STARTUP] 🐺 WOLF autonomous monitor started")
    except Exception as wolf_mon_err:
        LOGGER.warning(f"[GHOST STARTUP] WOLF monitor start failed: {wolf_mon_err}")

    # ── Watchlist squeeze radar (all 44 — intraday RVOL, not v3 picks) ───
    try:
        from core.squeeze_monitor import start_squeeze_monitor
        loop = asyncio.get_running_loop()
        loop.create_task(start_squeeze_monitor())
        LOGGER.info("[GHOST STARTUP] 🎯 Watchlist squeeze monitor started")
    except Exception as sq_err:
        LOGGER.warning(f"[GHOST STARTUP] Squeeze monitor start failed: {sq_err}")

