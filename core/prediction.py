"""
core/prediction.py - Ghost v2 prediction engine (multi-symbol watchlist).
Signal source: v3 XGBoost trained on TP/SL outcomes (see core.signal_engine v3.2).
Rules:
  - 30+ resolved picks AND win_rate > 55%: predict dominant direction
  - 30+ resolved picks AND win_rate < 45%: predict inverse
  - Less than 30 picks: momentum-based (price vs 7-day average)
  - SELL signals blocked: 1.9% win rate across 211 trades
  - Confidence floor 0.80 by default (MIN_ALERT_CONFIDENCE)
  - Features logged on every prediction for future ML training
"""
import os, time, logging, json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from core.db import db_conn
from core.vol_targets import base_vol_pct, stop_pct_from_vol
try:
    from core.prices import get_price
except ImportError:
    def get_price(s, t=None): return None

LOGGER = logging.getLogger("ghost.prediction")

# Serialize prediction saves across concurrent cycles (market scan + cron overlap).
_PREDICTION_SAVE_LOCK_ID = 8723491

CONFIDENCE_FLOOR = float(os.getenv("MIN_ALERT_CONFIDENCE", "0.80"))  # raised: filter weak signals
DAILY_CAP        = int(os.getenv("DAILY_ALERT_CAP", "10"))
TARGET_PCT       = float(os.getenv("TARGET_PCT", "0.06"))
STOP_PCT         = float(os.getenv("STOP_PCT", "0.03"))
MIN_SAMPLES      = int(os.getenv("MIN_SAMPLES", "10"))
EDGE_THRESHOLD   = 0.55
INVERSE_THRESHOLD = 0.40

# Kill condition — honesty-layer falsification gate (blueprint §10).
# Pre-registered BEFORE the data so the stop is a decision, not a rationalization:
# once we have >= min_samples resolved high-conviction picks, if the win rate is
# below win_rate_floor AND the 95% CI upper bound is below north_star (i.e. the CI
# excludes 80%), the 80% claim is falsified — abandon it and reposition the system
# as a lower-confidence directional aid rather than moving the goalposts.
# Surfaced via GET /api/wolf/pick-journal -> verdict.falsification.
FALSIFICATION_THRESHOLD: Dict[str, float] = {
    "min_samples": 30,      # N: resolved high-conviction picks required before judging
    "win_rate_floor": 0.70, # below this realized win rate ...
    "north_star": 0.80,     # ... and 95% CI upper-bound below this => abandon the 80% claim
    "ci_level": 0.95,
}

# Symbol universe — official watchlist (config/symbols.py); env override in tests only.
from config.symbols import _env_stock_symbols

CRYPTO_SYMBOLS: List[str] = []
STOCK_SYMBOLS: List[str] = _env_stock_symbols() or ["WOLF"]

# Priority order for binding-gate resolution. Higher items win over raw skip_counts
# tallies — e.g. 17× no_v3_model (untrained watchlist symbols) must not mask
# v3_prob_low on WOLF when the model actually ran and emitted up_prob.
_SKIP_PRIORITY: List[str] = [
    "dedup_blocked",
    "below_confidence_floor",
    "objective_gate",
    "objective_bootstrap_conf",
    "v3_regime_gate",
    "v3_meta_gate",
    "v3_prob_low",
    "v3_intraday_data",
    "v3_engine_error",
    "v3_no_signal",
    "no_v3_model",
    "no_price",
    "sell_blocked",
    "excluded",
]

_SKIP_LABELS: Dict[str, str] = {
    "dedup_blocked": "dedup (open pick already exists)",
    "below_confidence_floor": "below confidence floor",
    "objective_gate": "objective gate (symbol WR below target)",
    "objective_bootstrap_conf": "objective bootstrap (confidence below bootstrap minimum)",
    "v3_regime_gate": "v3 live regime gate blocked BUY",
    "v3_meta_gate": "v3 model metadata failed live thresholds",
    "v3_prob_low": "v3 model prob below BUY floor",
    "v3_intraday_data": "v3 intraday bars missing/short",
    "v3_engine_error": "v3 engine error",
    "v3_no_signal": "v3 returned no signal (other)",
    "no_v3_model": "no v3 model trained for symbol (coverage gap — not WOLF unload)",
    "no_price": "missing price",
    "sell_blocked": "SELL/DOWN blocked",
    "excluded": "symbol excluded",
}


def enrich_near_miss(near_miss: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Add prob/conf/bootstrap gaps so UI can show the gate that actually blocked."""
    if not near_miss:
        return None
    nm = dict(near_miss)
    up = nm.get("up_prob")
    min_p = nm.get("min_win_proba")
    if up is not None and min_p is not None:
        nm["prob_gap"] = round(float(up) - float(min_p), 4)
    conf = nm.get("confidence")
    floor = nm.get("confidence_floor")
    if conf is not None and floor is not None:
        nm["conf_gap"] = round(float(conf) - float(floor), 4)
    boot = nm.get("bootstrap_min_conf")
    if conf is not None and boot is not None:
        nm["bootstrap_gap"] = round(float(conf) - float(boot), 4)
    return nm


def backfill_near_miss_for_display(near_miss: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Fill missing bootstrap fields on historical gate rows recorded before f02a1e2."""
    if not near_miss:
        return None
    nm = dict(near_miss)
    if nm.get("skip") == "objective_bootstrap_conf":
        if nm.get("bootstrap_min_conf") is None:
            nm["bootstrap_min_conf"] = float(_objective_effective_config().get("bootstrap_min_conf", 0.75))
        if nm.get("confidence") is None and nm.get("up_prob") is not None:
            nm["confidence"] = float(nm["up_prob"])
    return enrich_near_miss(nm)


def resolve_binding_skip(
    skip_counts: Dict[str, int],
    *,
    dedup_blocked: int = 0,
    near_miss: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Pick the semantically binding skip for this cycle.

    When the best near-miss symbol ran the model (up_prob present), its per-symbol
    skip wins over bulk no_v3_model counts from untrained watchlist tickers.
    Otherwise fall back to priority order (same as Telegram diagnostics).
    """
    if dedup_blocked > 0:
        return "dedup_blocked"
    if near_miss and near_miss.get("up_prob") is not None and near_miss.get("skip"):
        return str(near_miss["skip"])
    for code in _SKIP_PRIORITY:
        if skip_counts.get(code):
            return code
    if skip_counts:
        return max(skip_counts.items(), key=lambda kv: kv[1])[0]
    return None


# Kill conditions — env-tunable safety thresholds (audit §2). Each evaluates over
# a rolling window of resolved high-conviction picks and maps to a degrade action.
# All thresholds are env vars (read at call time so ops can retune without deploy).
# This module only EVALUATES; enforcement (pause/degrade/Telegram) is wired separately.
#   win_rate  < floor   over N  -> auto_pause          (KILL_WINRATE_FLOOR / _WINDOW)
#   brier     > ceiling over N  -> degrade_watching    (KILL_BRIER_CEILING / _WINDOW)
#   consecutive losses  >= K    -> cooldown            (KILL_CONSEC_LOSSES)
#   expectancy (mean pnl%) < 0  over N -> halt_manual_review (KILL_EXPECTANCY_WINDOW)
def _kill_cfg() -> Dict[str, Any]:
    g = os.getenv
    return {
        "enabled": g("KILL_SWITCH_ENABLED", "1") not in ("0", "false", "False", ""),
        "winrate_floor": float(g("KILL_WINRATE_FLOOR", "0.70")),
        "winrate_window": max(1, int(g("KILL_WINRATE_WINDOW", "30"))),
        "brier_ceiling": float(g("KILL_BRIER_CEILING", "0.35")),
        "brier_window": max(1, int(g("KILL_BRIER_WINDOW", "30"))),
        "consec_losses": max(1, int(g("KILL_CONSEC_LOSSES", "3"))),
        "expectancy_window": max(1, int(g("KILL_EXPECTANCY_WINDOW", "20"))),
        "cooldown_minutes": max(1, int(g("KILL_COOLDOWN_MINUTES", "1440"))),
        "min_samples": max(1, int(g("KILL_MIN_SAMPLES", "10"))),
    }


def _kill_symbol_universe() -> List[str]:
    """Symbols included in kill-condition rollups (watchlist + portfolio)."""
    try:
        from config.symbols import watchlist_symbols
        return sorted(watchlist_symbols(include_portfolio=True))
    except Exception:
        return list(STOCK_SYMBOLS) or ["WOLF"]


def evaluate_kill_conditions() -> Dict[str, Any]:
    """Read-only evaluation of the kill conditions over the rolling resolved-pick
    history (v3.2 era, watchlist symbols). Returns per-condition status with current
    value vs threshold and a green/red/insufficient flag. Does NOT enforce. During
    cold start every gated condition reads 'insufficient' and never trips."""
    cfg = _kill_cfg()
    need = max(cfg["winrate_window"], cfg["brier_window"],
               cfg["expectancy_window"], cfg["consec_losses"], cfg["min_samples"])
    symbols = _kill_symbol_universe()
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT confidence, outcome, pnl_pct FROM predictions "
                "WHERE symbol = ANY(%s) AND id >= 223438 AND outcome IS NOT NULL "
                "ORDER BY resolved_at DESC NULLS LAST, id DESC LIMIT %s",
                (symbols, need))
            rows = cur.fetchall()   # newest first
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}

    def _status(triggered: bool, enough: bool) -> str:
        if not enough:
            return "insufficient"
        return "red" if triggered else "green"

    conds = []

    # 1. Win rate over rolling window -> auto_pause
    wr_rows = rows[: cfg["winrate_window"]]
    wr_n = len(wr_rows)
    wr_wins = sum(1 for _c, o, _p in wr_rows if o == "WIN")
    wr = (wr_wins / wr_n) if wr_n else None
    wr_enough = wr_n >= cfg["winrate_window"]
    wr_trig = bool(wr_enough and wr is not None and wr < cfg["winrate_floor"])
    conds.append({
        "name": "win_rate", "action": "auto_pause", "window": cfg["winrate_window"],
        "samples": wr_n, "current": round(wr, 4) if wr is not None else None,
        "threshold": cfg["winrate_floor"], "comparator": "<",
        "triggered": wr_trig, "status": _status(wr_trig, wr_enough),
    })

    # 2. Brier score over rolling window -> degrade_watching
    br_terms = [(float(c) - (1.0 if o == "WIN" else 0.0)) ** 2
                for c, o, _p in rows[: cfg["brier_window"]] if c is not None]
    br_n = len(br_terms)
    brier = (sum(br_terms) / br_n) if br_n else None
    br_enough = br_n >= cfg["brier_window"]
    br_trig = bool(br_enough and brier is not None and brier > cfg["brier_ceiling"])
    conds.append({
        "name": "brier", "action": "degrade_watching", "window": cfg["brier_window"],
        "samples": br_n, "current": round(brier, 4) if brier is not None else None,
        "threshold": cfg["brier_ceiling"], "comparator": ">",
        "triggered": br_trig, "status": _status(br_trig, br_enough),
    })

    # 3. Consecutive losses (most recent K resolved all LOSS) -> cooldown
    streak = 0
    for _c, o, _p in rows:
        if o == "LOSS":
            streak += 1
        else:
            break
    cl_enough = len(rows) >= max(cfg["consec_losses"], cfg["min_samples"])
    cl_trig = bool(cl_enough and streak >= cfg["consec_losses"])
    conds.append({
        "name": "consecutive_losses", "action": "cooldown", "window": cfg["consec_losses"],
        "samples": len(rows), "current": streak, "threshold": cfg["consec_losses"],
        "comparator": ">=", "triggered": cl_trig,
        "status": _status(cl_trig, cl_enough),
    })

    # 4. Expectancy (mean realized pnl%) over rolling window -> halt_manual_review
    ex_pnls = [float(p) for _c, _o, p in rows[: cfg["expectancy_window"]] if p is not None]
    ex_n = len(ex_pnls)
    expectancy = (sum(ex_pnls) / ex_n) if ex_n else None
    ex_enough = ex_n >= cfg["expectancy_window"]
    ex_trig = bool(ex_enough and expectancy is not None and expectancy < 0)
    conds.append({
        "name": "expectancy", "action": "halt_manual_review", "window": cfg["expectancy_window"],
        "samples": ex_n, "current": round(expectancy, 4) if expectancy is not None else None,
        "threshold": 0.0, "comparator": "<",
        "triggered": ex_trig, "status": _status(ex_trig, ex_enough),
    })

    return {
        "ok": True,
        "enabled": cfg["enabled"],
        "any_triggered": any(c["triggered"] for c in conds),
        "resolved_available": len(rows),
        "conditions": conds,
    }


_ENGINE_PAUSE_KEYS = (
    "engine_paused", "engine_pause_reason", "engine_pause_ts",
    "engine_pause_auto_resume_at", "engine_pause_alerted",
)


def _clear_engine_pause():
    try:
        with db_conn() as c:
            cur = c.cursor()
            cur.execute(
                "DELETE FROM ghost_state WHERE key IN ("
                + ",".join(["%s"] * len(_ENGINE_PAUSE_KEYS)) + ")",
                _ENGINE_PAUSE_KEYS,
            )
    except Exception as e:
        LOGGER.warning("clear engine pause failed: " + str(e)[:80])


def engine_pause_state() -> Dict[str, Any]:
    """Current kill-condition pause state. A cooldown-only pause auto-resumes
    once its window elapses; harder trips require manual resume."""
    try:
        with db_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT key,val FROM ghost_state WHERE key IN ("
                + ",".join(["%s"] * len(_ENGINE_PAUSE_KEYS)) + ")",
                _ENGINE_PAUSE_KEYS,
            )
            st = {k: v for k, v in cur.fetchall()}
    except Exception:
        return {"paused": False}
    if st.get("engine_paused") != "1":
        return {"paused": False}
    auto = st.get("engine_pause_auto_resume_at")
    if auto:
        try:
            if int(time.time()) >= int(auto):
                _clear_engine_pause()
                return {"paused": False, "auto_resumed": True}
        except Exception:
            pass
    return {
        "paused": True,
        "reason": st.get("engine_pause_reason") or "",
        "since": int(st["engine_pause_ts"]) if st.get("engine_pause_ts") else None,
        "auto_resume_at": int(auto) if auto else None,
    }


def resume_engine() -> Dict[str, Any]:
    """Manual resume — clears any kill-condition pause."""
    _clear_engine_pause()
    LOGGER.warning("ENGINE RESUMED (manual): kill-condition pause cleared")
    return {"ok": True, "resumed": True}


def enforce_kill_conditions() -> Dict[str, Any]:
    """Evaluate the kill conditions and, if any tripped (and the switch is
    enabled), pause the engine + fire a one-time Telegram alert. Returns the
    resulting pause state. Cooldown-only trips auto-resume after
    KILL_COOLDOWN_MINUTES; harder trips (pause/degrade/halt) need manual resume.
    Inert during cold start: with too few resolved picks nothing trips."""
    cfg = _kill_cfg()
    if not cfg["enabled"]:
        return {"paused": False, "enabled": False}
    ev = evaluate_kill_conditions()
    if not ev.get("ok"):
        return engine_pause_state()
    tripped = [c for c in ev["conditions"] if c["triggered"]]
    if not tripped:
        prev = engine_pause_state()
        if prev.get("paused"):
            _clear_engine_pause()
            LOGGER.info("Kill conditions cleared — engine auto-resumed")
            return {"paused": False, "cleared": True}
        return {"paused": False}

    actions = sorted({c["action"] for c in tripped})
    reason = "; ".join(c["name"] + "->" + c["action"] for c in tripped)
    cooldown_only = actions == ["cooldown"]
    now = int(time.time())
    auto_resume_at = (now + cfg["cooldown_minutes"] * 60) if cooldown_only else None
    prev = engine_pause_state()

    try:
        with db_conn() as c:
            cur = c.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            pairs = [("engine_paused", "1"), ("engine_pause_reason", reason),
                     ("engine_pause_ts", str(now))]
            for k, v in pairs:
                cur.execute(
                    "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
                    "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (k, v))
            if auto_resume_at:
                cur.execute(
                    "INSERT INTO ghost_state(key,val) VALUES('engine_pause_auto_resume_at',%s) "
                    "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (str(auto_resume_at),))
            else:
                cur.execute("DELETE FROM ghost_state WHERE key='engine_pause_auto_resume_at'")
    except Exception as e:
        LOGGER.error("engine pause write failed: " + str(e)[:80])

    # One-time Telegram alert per new trip reason.
    if not prev.get("paused") or prev.get("reason") != reason:
        LOGGER.warning("KILL CONDITION TRIPPED — engine paused: %s", reason)
        try:
            from core.telegram import send_health_alert
            msg = ("KILL CONDITION TRIPPED — engine paused: " + reason
                   + (" (auto-resume in " + str(cfg["cooldown_minutes"]) + "m)"
                      if auto_resume_at else " (manual resume required)"))
            send_health_alert(msg)
        except Exception as e:
            LOGGER.error("kill alert failed: " + str(e)[:80])
        try:
            with db_conn() as c2:
                cur2 = c2.cursor()
                cur2.execute(
                    "INSERT INTO ghost_state(key,val) VALUES('engine_pause_alerted',%s) "
                    "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val", (str(now),))
        except Exception:
            pass

    return {"paused": True, "reason": reason, "since": now,
            "auto_resume_at": auto_resume_at, "actions": actions}


def _is_market_hours():
    """Returns True if US market is open (9:30 AM - 4:00 PM CT, Mon-Fri)."""
    import datetime as _dt, pytz as _tz
    ct = _tz.timezone("America/Chicago")
    now = _dt.datetime.now(ct)
    if now.weekday() >= 5: return False  # weekend
    mkt_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    mkt_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return mkt_open <= now <= mkt_close

def _is_premarket():
    """Returns True if pre-market (4 AM - 9:30 AM CT, Mon-Fri)."""
    import datetime as _dt, pytz as _tz
    ct = _tz.timezone("America/Chicago")
    now = _dt.datetime.now(ct)
    if now.weekday() >= 5: return False
    pre_open = now.replace(hour=4, minute=0, second=0, microsecond=0)
    mkt_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return pre_open <= now < mkt_open


def _premarket_scan_enabled() -> bool:
    """When True, watchlist scans run during US pre-market (4:00–9:30 AM CT)."""
    return os.getenv("GHOST_PREMARKET_SCAN", "1").strip().lower() in ("1", "true", "yes", "on")


def _premarket_floor_bump() -> float:
    """Extra confidence required for pre-market fires (noisier tape)."""
    raw = os.getenv("GHOST_PREMARKET_FLOOR_BUMP", "0.03")
    try:
        return max(0.0, min(0.15, float(raw)))
    except Exception:
        return 0.03


def _watchlist_scan_enabled() -> bool:
    """True when run_prediction_cycle should scan the configured watchlist."""
    if _is_premarket():
        return _premarket_scan_enabled()
    return True


EXCLUDE = set(s for s in os.getenv("EXCLUDE_SYMBOLS","").split(",") if s.strip())
_OBJECTIVE_RUNTIME_MODE_CACHE: Dict[str, Any] = {"mode": None, "ts": 0.0}


def _objective_mode() -> str:
    if _objective_auto_enabled():
        cached_mode = _objective_runtime_mode()
        if cached_mode in ("aggressive", "balanced", "precision"):
            return cached_mode
    mode = (os.getenv("OBJECTIVE_MODE", "precision") or "").strip().lower()
    if mode in ("aggressive", "balanced", "precision"):
        return mode
    return "precision"


def _objective_mode_defaults(mode: str) -> Dict[str, float]:
    if mode == "aggressive":
        return {
            "target_wr": 0.62,
            "min_samples": 8.0,
            # Cold-start firing bar, lowered 0.78 -> 0.75 by operator decision so
            # the engine can bootstrap its first fires once recovered + calibrated
            # (up_prob ~0.58 mapping to conf 0.75). Only gates while in bootstrap
            # (total < min_samples); after that the win-rate gate takes over.
            # Overridable per-deploy via OBJECTIVE_BOOTSTRAP_MIN_CONF.
            "bootstrap_min_conf": 0.75,
            "lookback_days": 120.0,
        }
    if mode == "balanced":
        return {
            "target_wr": 0.70,
            "min_samples": 12.0,
            "bootstrap_min_conf": 0.85,
            "lookback_days": 150.0,
        }
    # precision mode (default)
    return {
        "target_wr": 0.80,
        "min_samples": 20.0,
        "bootstrap_min_conf": 0.90,
        "lookback_days": 180.0,
    }


def _objective_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(raw)
    except Exception:
        return default
    return max(lo, min(hi, val))


def _objective_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = int(raw)
    except Exception:
        return default
    return max(lo, min(hi, val))


def _objective_effective_config() -> Dict[str, Any]:
    mode = _objective_mode()
    defaults = _objective_mode_defaults(mode)
    target_wr = _objective_float("OBJECTIVE_TARGET_WIN_RATE", float(defaults["target_wr"]), 0.50, 0.95)
    min_samples = _objective_int("OBJECTIVE_MIN_SAMPLES", int(defaults["min_samples"]), 5, 5000)
    bootstrap_min_conf = _objective_float("OBJECTIVE_BOOTSTRAP_MIN_CONF", float(defaults["bootstrap_min_conf"]), 0.50, 0.99)
    lookback_days = _objective_int("OBJECTIVE_LOOKBACK_DAYS", int(defaults["lookback_days"]), 7, 3650)
    return {
        "mode": mode,
        "target_wr": target_wr,
        "min_samples": min_samples,
        "bootstrap_min_conf": bootstrap_min_conf,
        "lookback_days": lookback_days,
    }


def _objective_auto_enabled() -> bool:
    return os.getenv("OBJECTIVE_AUTO_MODE_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")


def _objective_auto_window_days() -> int:
    return max(7, int(os.getenv("OBJECTIVE_AUTO_WINDOW_DAYS", "30")))


def _objective_runtime_mode(cache_ttl_s: int = 45) -> str:
    """
    Runtime-selected objective mode from ghost_state.
    Falls back to env OBJECTIVE_MODE when unavailable.
    """
    now = time.time()
    cached = _OBJECTIVE_RUNTIME_MODE_CACHE.get("mode")
    cached_ts = float(_OBJECTIVE_RUNTIME_MODE_CACHE.get("ts") or 0.0)
    if cached and (now - cached_ts) < cache_ttl_s:
        return str(cached)

    env_mode = (os.getenv("OBJECTIVE_MODE", "precision") or "").strip().lower()
    fallback_mode = env_mode if env_mode in ("aggressive", "balanced", "precision") else "precision"
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            cur.execute("SELECT val FROM ghost_state WHERE key='objective_mode_runtime'")
            row = cur.fetchone()
            if row and row[0]:
                db_mode = str(row[0]).strip().lower()
                if db_mode in ("aggressive", "balanced", "precision"):
                    _OBJECTIVE_RUNTIME_MODE_CACHE["mode"] = db_mode
                    _OBJECTIVE_RUNTIME_MODE_CACHE["ts"] = now
                    return db_mode
    except Exception:
        pass
    _OBJECTIVE_RUNTIME_MODE_CACHE["mode"] = fallback_mode
    _OBJECTIVE_RUNTIME_MODE_CACHE["ts"] = now
    return fallback_mode


def _objective_target_win_rate() -> float:
    return float(_objective_effective_config()["target_wr"])


def _objective_min_samples() -> int:
    return int(_objective_effective_config()["min_samples"])


def _objective_lookback_days() -> int:
    return int(_objective_effective_config()["lookback_days"])


def _objective_bootstrap_min_conf() -> float:
    return float(_objective_effective_config()["bootstrap_min_conf"])


def _objective_enforced() -> bool:
    return os.getenv("OBJECTIVE_ENFORCE", "1").strip().lower() in ("1", "true", "yes", "on")


def _safe_wr(wins: int, total: int) -> float:
    return float(wins) / float(total) if total > 0 else 0.0


def _direction_aliases(direction: str) -> Tuple[str, ...]:
    d = (direction or "").upper()
    if d in ("UP", "BUY"):
        return ("UP", "BUY")
    return ("DOWN", "SELL")


def _objective_symbol_stats(symbol: str, direction: str) -> Dict[str, Any]:
    """
    Blend recent v2 outcomes + legacy outcomes to estimate direction precision.
    """
    cutoff = int(time.time()) - _objective_lookback_days() * 86400
    aliases = _direction_aliases(direction)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END), 0)
            FROM predictions
            WHERE symbol=%s
              AND direction = ANY(%s)
              AND outcome IN ('WIN','LOSS')
              AND COALESCE(resolved_at, predicted_at, run_at, 0) >= %s
            """,
            (symbol, list(aliases), cutoff),
        )
        v2_total, v2_wins = cur.fetchone() or (0, 0)

        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(CASE WHEN hit_direction=1 THEN 1 ELSE 0 END), 0)
            FROM ghost_prediction_outcomes
            WHERE symbol=%s
              AND predicted_direction = %s
              AND hit_direction IN (0,1)
              AND EXTRACT(EPOCH FROM created_at)::BIGINT >= %s
            """,
            (symbol, "UP" if aliases[0] in ("UP", "BUY") else "DOWN", cutoff),
        )
        gpo_total, gpo_wins = cur.fetchone() or (0, 0)

    v2_total = int(v2_total or 0)
    v2_wins = int(v2_wins or 0)
    gpo_total = int(gpo_total or 0)
    gpo_wins = int(gpo_wins or 0)
    combined_total = v2_total + gpo_total
    combined_wins = v2_wins + gpo_wins
    return {
        "v2_total": v2_total,
        "v2_wins": v2_wins,
        "v2_wr": round(_safe_wr(v2_wins, v2_total), 4),
        "gpo_total": gpo_total,
        "gpo_wins": gpo_wins,
        "gpo_wr": round(_safe_wr(gpo_wins, gpo_total), 4),
        "combined_total": combined_total,
        "combined_wins": combined_wins,
        "combined_wr": round(_safe_wr(combined_wins, combined_total), 4),
    }


def _objective_gate(symbol: str, direction: str, confidence: float) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Precision gate for the 80% objective:
    - If enough evidence exists, require historical direction WR >= target.
    - If not enough evidence, require higher confidence bootstrap threshold.
    """
    cfg = _objective_effective_config()
    target_wr = float(cfg["target_wr"])
    min_samples = int(cfg["min_samples"])
    bootstrap_min_conf = float(cfg["bootstrap_min_conf"])

    if not _objective_enforced():
        return True, "", {"enforced": False, **cfg}

    try:
        stats = _objective_symbol_stats(symbol, direction)
    except Exception as e:
        LOGGER.warning("objective gate stats failed for %s: %s", symbol, str(e)[:120])
        # Fail-open on telemetry failure to avoid hard outage.
        return True, "", {"enforced": True, "error": "stats_failed", **cfg}

    meta = {
        "enforced": True,
        **cfg,
        **stats,
    }
    total = int(stats.get("combined_total", 0))
    wr = float(stats.get("combined_wr", 0.0))

    if total >= min_samples:
        if wr < target_wr:
            return False, "objective_gate", meta
        return True, "", meta

    # Low evidence: only allow stronger-confidence picks.
    if confidence < bootstrap_min_conf:
        return False, "objective_bootstrap_conf", meta
    return True, "", meta


def _objective_recent_v2_stats(window_days: int) -> Dict[str, Any]:
    cutoff = int(time.time()) - int(window_days) * 86400
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END), 0)
            FROM predictions
            WHERE direction IN ('UP','BUY')
              AND outcome IN ('WIN','LOSS')
              AND COALESCE(resolved_at, predicted_at, run_at, 0) >= %s
            """,
            (cutoff,),
        )
        total, wins = cur.fetchone() or (0, 0)
    total_i = int(total or 0)
    wins_i = int(wins or 0)
    losses_i = max(0, total_i - wins_i)
    wr = _safe_wr(wins_i, total_i)
    return {"window_days": int(window_days), "wins": wins_i, "losses": losses_i, "total": total_i, "win_rate": wr}


def _objective_pick_mode(stats: Dict[str, Any]) -> Tuple[str, str]:
    """
    Choose objective mode based on recent realized BUY precision.
    """
    total = int(stats.get("total", 0))
    wr = float(stats.get("win_rate", 0.0))
    min_precision_n = max(10, int(os.getenv("OBJECTIVE_AUTO_MIN_PRECISION_SAMPLES", "40")))
    min_balanced_n = max(6, int(os.getenv("OBJECTIVE_AUTO_MIN_BALANCED_SAMPLES", "20")))
    wr_precision = max(0.55, min(0.95, float(os.getenv("OBJECTIVE_AUTO_PROMOTE_TO_PRECISION_WR", "0.68"))))
    wr_balanced = max(0.50, min(0.90, float(os.getenv("OBJECTIVE_AUTO_PROMOTE_TO_BALANCED_WR", "0.55"))))

    if total >= min_precision_n and wr >= wr_precision:
        return "precision", f"recent_wr={wr:.3f} on n={total} >= precision thresholds"
    if total >= min_balanced_n and wr >= wr_balanced:
        return "balanced", f"recent_wr={wr:.3f} on n={total} >= balanced thresholds"
    return "aggressive", f"recent_wr={wr:.3f} on n={total} below promotion thresholds"


def objective_autotune_mode() -> Dict[str, Any]:
    """
    Auto-step objective mode based on recent realized outcomes.
    Persists selected mode in ghost_state.objective_mode_runtime.
    """
    if not _objective_auto_enabled():
        mode = _objective_runtime_mode()
        return {"enabled": False, "mode": mode, "reason": "auto disabled"}

    try:
        stats = _objective_recent_v2_stats(_objective_auto_window_days())
        target_mode, reason = _objective_pick_mode(stats)
        now = int(time.time())
        changed = False
        prev_mode = None
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            cur.execute("SELECT val FROM ghost_state WHERE key='objective_mode_runtime'")
            row = cur.fetchone()
            prev_mode = str(row[0]).strip().lower() if row and row[0] else None
            if prev_mode != target_mode:
                changed = True
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('objective_mode_runtime',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (target_mode,),
            )
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('objective_mode_runtime_reason',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (reason[:240],),
            )
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('objective_mode_runtime_updated_ts',%s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (str(now),),
            )

        _OBJECTIVE_RUNTIME_MODE_CACHE["mode"] = target_mode
        _OBJECTIVE_RUNTIME_MODE_CACHE["ts"] = time.time()
        if changed:
            LOGGER.info("OBJECTIVE AUTO MODE: %s -> %s (%s)", prev_mode or "unset", target_mode, reason)
        return {
            "enabled": True,
            "mode": target_mode,
            "changed": changed,
            "previous_mode": prev_mode,
            "reason": reason,
            "stats": stats,
        }
    except Exception as e:
        LOGGER.warning("objective auto mode failed: %s", str(e)[:120])
        mode = _objective_runtime_mode()
        return {"enabled": True, "mode": mode, "error": str(e)[:120]}


def _check_regime():
    """WOLF-only mode: regime gate is a no-op. Returns benign defaults for back-compat."""
    return {"block_crypto_buys": False, "reduce_size": False, "reason": "", "btc_24h_pct": 0.0}


def _get_sentiment(symbol):
    """Get cached news sentiment for symbol. Returns float -1..+1."""
    try:
        from core.news import get_sentiment_for_symbol
        return get_sentiment_for_symbol(symbol)
    except Exception:
        return 0.0


def _get_symbol_signal(symbol, current_price):
    """
    v3: Use XGBoost model trained on real price data.
    Returns None if no v3 model exists — legacy fallback disabled.
    """
    # Try v3 model first
    try:
        from core.signal_engine import predict_live_ex
        _atype = "stock"  # WOLF-only mode
        result, _reason = predict_live_ex(symbol, _atype)
        if result is not None:
            LOGGER.info("v3 signal for " + symbol + ": " + str(result[0]) + " " + str(round(result[1]*100,1)) + "%")
            return result
    except Exception as _v3e:
        LOGGER.warning("v3 engine error for " + symbol + ": " + str(_v3e))
        return None

    # predict_live_ex returned None — distinguish missing/unloadable model vs live gating.
    try:
        from core.signal_engine import load_model as _lm
        _m, _fc, _meta = _lm(symbol)
        _has_model = _m is not None
    except Exception:
        _has_model = False
    if not _has_model:
        LOGGER.info("No v3 model for " + symbol + " — model missing/unloadable, skipping")
    else:
        LOGGER.info("No v3 signal for " + symbol + " — model loaded but live filters returned no trade")
    return None

def _legacy_signal(symbol, current_price):
    """Legacy v2 signal — kept as fallback until v3 model is trained and validated."""

    # T22: Skip symbols with < 15% WR after 20+ v2 picks
    try:
        with db_conn() as _ac:
            _c = _ac.cursor()
            _c.execute(
                "SELECT COUNT(*), SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) FROM predictions WHERE symbol=%s AND outcome IN ('WIN','LOSS') AND direction='UP' AND id >= 223438",
                (symbol,))
            _r = _c.fetchone()
            if _r and _r[0] and _r[0] >= 20:
                _wr = (_r[1] or 0) / _r[0]
                if _wr < 0.15:
                    LOGGER.info("T22 SKIP " + symbol + " poor WR: " + str(round(_wr*100,1)) + "% on " + str(_r[0]) + " picks")
                    return None
    except Exception: pass
    """
    Core signal logic. Returns (direction, confidence) or None.
    Uses v2 resolved picks + legacy ghost_prediction_outcomes as fallback.
    """
    rows = []
    v2_rows = []
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            # v2 resolved picks
            cur.execute(
                "SELECT direction, CASE WHEN outcome='WIN' THEN 1 ELSE 0 END FROM predictions WHERE symbol=%s AND outcome IN ('WIN','LOSS') ORDER BY id DESC LIMIT 50",
                (symbol,))
            v2_rows = cur.fetchall()
            # Legacy ghost_prediction_outcomes
            cur.execute("""
                SELECT predicted_direction, hit_direction
                FROM ghost_prediction_outcomes
                WHERE symbol = %s AND hit_direction IN (0, 1)
                ORDER BY id DESC LIMIT 100
            """, (symbol,))
            rows = cur.fetchall()
    except Exception as e:
        LOGGER.warning("signal query failed for " + symbol + ": " + str(e))
        return None

    if len(v2_rows) >= 8:
        # Circuit breaker: 8 consecutive v2 losses
        last8 = [o for _, o in v2_rows[:8]]
        if sum(last8) == 0:
            try:
                with db_conn() as conn2:
                    cur2 = conn2.cursor()
                    cur2.execute(
                        "SELECT COUNT(*), SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) FROM ghost_prediction_outcomes WHERE symbol=%s AND hit_direction IN (0,1)",
                        (symbol,))
                    r = cur2.fetchone()
                    gpo_total = r[0] or 0
                    gpo_wins  = r[1] or 0
                    gpo_wr = gpo_wins / gpo_total if gpo_total > 0 else 0
            except Exception:
                gpo_wr = 0
            if gpo_wr <= 0.60:
                LOGGER.info("CIRCUIT BREAKER: " + symbol + " benched (8 v2 losses, gpo_wr=" + str(round(gpo_wr,2)) + ")")
                return None
            else:
                LOGGER.info("CB SKIPPED: " + symbol + " has gpo_wr=" + str(round(gpo_wr,2)) + " overrides 8 v2 losses")

    rows = list(v2_rows) + list(rows)

    if len(rows) >= MIN_SAMPLES:
        total = len(rows)
        wins = sum(1 for _, o in rows if o == 1 or o == "WIN")
        win_rate = wins / total
        up_picks = sum(1 for d, _ in rows if d == "UP")
        dominant_dir = "UP" if up_picks >= total / 2 else "DOWN"
        up_win_rate = sum(1 for d, o in rows if d == "UP" and (o == 1 or o == "WIN")) / max(up_picks, 1)
        down_picks = total - up_picks
        down_win_rate = sum(1 for d, o in rows if d == "DOWN" and (o == 1 or o == "WIN")) / max(down_picks, 1)

        # Cap confidence at 0.82 for legacy-only symbols (no v2 validation)
        _v2_count = len(v2_rows) if v2_rows else 0
        _conf_cap = 1.0 if _v2_count >= 5 else 0.82 if _v2_count > 0 else 0.79
        if up_win_rate > down_win_rate and up_win_rate > EDGE_THRESHOLD:
            return ("UP", round(min(up_win_rate, _conf_cap), 3))
        elif down_win_rate > up_win_rate and down_win_rate > EDGE_THRESHOLD:
            return ("DOWN", round(min(down_win_rate, _conf_cap), 3))
        elif win_rate < INVERSE_THRESHOLD:
            inv_dir = "DOWN" if dominant_dir == "UP" else "UP"
            # Confidence scales with distance from 50/50.
            # WR=0.40 (barely inverse) → 0.20; WR=0.10 (strong inverse) → 0.80.
            # Capped at 0.65 because inverse signals are second-class evidence.
            inv_conf = round(min(abs(win_rate - 0.5) * 2.0, 0.65), 3)
            return (inv_dir, inv_conf)
        else:
            return None  # No edge (45-55% zone)
    else:
        # Not enough history - use price momentum vs last recorded price
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT entry_price, predicted_at FROM predictions WHERE symbol=%s AND entry_price > 0 ORDER BY id DESC LIMIT 5",
                (symbol,))
            price_rows = cur.fetchall()
        if len(price_rows) >= 2:
            oldest_price = price_rows[-1][0]
            age_hours = (time.time() - (price_rows[-1][1] or 0)) / 3600
            if oldest_price > 0 and age_hours < 72:
                pct_change = (current_price - oldest_price) / oldest_price
                if abs(pct_change) > 0.01:  # >1% move
                    direction = "UP" if pct_change > 0 else "DOWN"
                    conf = min(round(0.55 + abs(pct_change) * 2, 3), 0.70)
                    return (direction, conf)
        return None


def _predict_symbol_ex(symbol, asset_type, regime, scores_out=None):
    """
    Like predict_symbol but returns (pick_or_None, skip_code_or_None).
    skip_code is for morning-card diagnostics only (not an API contract).
    If `scores_out` (a dict) is passed, the model score vector — up_prob,
    min_win_proba, and (on a confidence-floor miss) confidence vs floor — is
    written into it so callers can log how close a silent cycle came to firing.
    """
    sym = symbol.strip()
    if sym in EXCLUDE or not sym:
        return None, "excluded"
    price = get_price(symbol, asset_type)
    if (not price or price <= 0) and asset_type == "stock":
        try:
            import yfinance as _yf
            _hist = _yf.Ticker(symbol).history(period="2d")
            if not _hist.empty:
                price = float(_hist["Close"].iloc[-1])
                LOGGER.info("Stock prev-close for " + symbol + ": $" + str(round(price,2)))
        except Exception as _pe:
            LOGGER.warning("Prev-close fallback failed " + symbol + ": " + str(_pe))
    if not price or price <= 0:
        return None, "no_price"
    score_vector = scores_out if scores_out is not None else {}
    try:
        from core.signal_engine import predict_live_ex
        signal, v3_reason = predict_live_ex(symbol, "stock", scores=score_vector)
    except Exception as _pe:
        LOGGER.warning("v3 engine error for " + symbol + ": " + str(_pe))
        return None, "v3_engine_error"

    if not signal:
        if v3_reason == "no_model":
            return None, "no_v3_model"
        if v3_reason == "regime_gate":
            return None, "v3_regime_gate"
        if v3_reason == "intraday_data":
            return None, "v3_intraday_data"
        if v3_reason == "meta_gate":
            return None, "v3_meta_gate"
        if v3_reason == "prob_low":
            return None, "v3_prob_low"
        return None, "v3_no_signal"
    direction, confidence = signal

    _floor = regime.get('confidence_floor_override', CONFIDENCE_FLOOR) if isinstance(regime, dict) else CONFIDENCE_FLOOR
    if _is_premarket() and _premarket_scan_enabled():
        _floor = min(0.98, _floor + _premarket_floor_bump())
        score_vector["premarket_floor_bump"] = _premarket_floor_bump()
    if confidence < _floor:
        score_vector["confidence"] = float(confidence)
        score_vector["confidence_floor"] = float(_floor)
        return None, "below_confidence_floor"

    # SELL signals blocked: 1.9% win rate across 211 trades (data as of 2026-03-25)
    if direction == "DOWN":
        LOGGER.info("SELL blocked: " + symbol + " — DOWN signals 1.9% wr historically")
        return None, "sell_blocked"

    objective_ok, objective_skip, objective_meta = _objective_gate(sym, direction, float(confidence))
    if not objective_ok:
        LOGGER.info(
            "OBJECTIVE GATE blocked %s %s: wr=%s total=%s target=%s conf=%.3f",
            sym,
            direction,
            objective_meta.get("combined_wr"),
            objective_meta.get("combined_total"),
            objective_meta.get("target_wr"),
            float(confidence),
        )
        return None, objective_skip

    now = int(time.time())
    # Align live hold with v3.2 training labels (N daily forward bars, not 48 calendar hours).
    from core.tp_sl_resolve import expires_at_nth_trading_close, label_hold_bars
    hold = expires_at_nth_trading_close(now, label_hold_bars()) - now
    # Dynamic targets — same base_vol_pct as training labels (core.vol_targets).
    _vol_pct = base_vol_pct(symbol, asset_type)
    _stop_pct = stop_pct_from_vol(_vol_pct)  # ~1.5:1 reward:risk vs target move
    target = price * (1 + _vol_pct) if direction == "UP" else price * (1 - _vol_pct)
    stop   = price * (1 - _stop_pct) if direction == "UP" else price * (1 + _stop_pct)

    # Capture raw confidence before sentiment nudge (for ML features)
    confidence_raw = confidence

    # Price change vs ~4h ago (for ML features)
    price_4h_pct = 0.0
    try:
        with db_conn() as fc:
            fc_cur = fc.cursor()
            fc_cur.execute(
                "SELECT entry_price FROM predictions WHERE symbol=%s AND predicted_at > %s AND entry_price > 0 ORDER BY id ASC LIMIT 1",
                (symbol, int(time.time()) - 14400))
            old_row = fc_cur.fetchone()
            if old_row and old_row[0] and float(old_row[0]) > 0:
                price_4h_pct = round((price - float(old_row[0])) / float(old_row[0]) * 100, 3)
    except Exception:
        pass

    # Claude news sentiment: nudges confidence +-10% based on news alignment
    sentiment_score = 0.0
    try:
        sent = _get_sentiment(symbol)
        sentiment_score = float(sent)
        if abs(sent) > 0.1:
            dir_mult = 1.0 if direction in ("UP", "BUY") else -1.0
            adj = round(sent * dir_mult * 0.10, 3)
            confidence = round(max(CONFIDENCE_FLOOR, min(0.98, confidence + adj)), 3)
            LOGGER.info("[SENTIMENT] " + symbol + " news=" + str(round(sent,2)) + " adj=" + str(adj) + " conf=" + str(confidence))
    except Exception:
        pass

    # Build feature vector — stored in DB for future ML training
    now_dt = datetime.now(timezone.utc)
    from core.feature_schema import FEATURE_ASOF_KEY, feature_asof_unix
    v3_feats = score_vector.get("features") if isinstance(score_vector, dict) else {}
    feature_asof = None
    if isinstance(v3_feats, dict):
        feature_asof = v3_feats.get(FEATURE_ASOF_KEY)
    features = {
        "hour_of_day":      now_dt.hour,
        "day_of_week":      now_dt.weekday(),
        "symbol_win_rate":  round(confidence_raw, 3),
        "confidence_raw":   round(confidence_raw, 3),
        "sentiment_score":  round(sentiment_score, 3),
        "price_4h_pct":     price_4h_pct,
        FEATURE_ASOF_KEY:   int(feature_asof if feature_asof is not None else feature_asof_unix(now)),
    }

    if confidence >= 0.90:   pos_pct = 5.0
    elif confidence >= 0.85: pos_pct = 4.0
    elif confidence >= 0.80: pos_pct = 3.0
    elif confidence >= 0.75: pos_pct = 2.0
    else:                    pos_pct = 1.0
    # Journal a ghost-score component snapshot (roadmap #4b/B) so true component
    # attribution accrues going forward. Best-effort — never blocks a pick.
    try:
        from core.attribution import ghost_components
        if isinstance(score_vector, dict):
            score_vector["ghost_components"] = ghost_components(
                confidence, direction, score_vector.get("features"), now, now)
    except Exception:
        pass
    return {
        "symbol":       symbol,
        "direction":    direction,
        "confidence":   confidence,
        "entry_price":  price,
        "target_price": round(target, 6),
        "stop_price":   round(stop, 6),
        "predicted_at": now,
        "expires_at":   now + hold,
        "asset_type":   asset_type,
        "features":     features,
        "scores":       score_vector,
        "pos_size_pct": pos_pct,
        "objective_expected_wr": objective_meta.get("combined_wr"),
        "objective_samples": objective_meta.get("combined_total"),
    }, None


def predict_symbol(symbol, asset_type, regime):
    pick, _skip = _predict_symbol_ex(symbol, asset_type, regime)
    return pick


def _circuit_breaker_floor():
    """T23 circuit breaker (roadmap #1c). After a streak of consecutive UP losses,
    raise the confidence floor. Now env-tunable AND recency-guarded: a STALE loss
    streak no longer suppresses firing forever. The old hard-coded version could
    deadlock the engine at 0.90 — if it stopped firing, the last-N resolved never
    refreshed, so the breaker stayed latched indefinitely.

    Env: CB_LOSS_STREAK(5), CB_FLOOR_DELTA(0.10), CB_FLOOR_CAP(0.92),
         CB_RECENCY_DAYS(14, 0 disables the recency guard).
    Returns (floor, active, detail)."""
    base = CONFIDENCE_FLOOR
    try:
        streak_n = max(1, int(os.getenv("CB_LOSS_STREAK", "5")))
        delta = float(os.getenv("CB_FLOOR_DELTA", "0.10"))
        cap = float(os.getenv("CB_FLOOR_CAP", "0.92"))
        recency_days = max(0, int(os.getenv("CB_RECENCY_DAYS", "14")))
    except Exception:
        streak_n, delta, cap, recency_days = 5, 0.10, 0.92, 14
    try:
        with db_conn() as _cb:
            _cc = _cb.cursor()
            _cc.execute(
                "SELECT outcome, resolved_at FROM predictions WHERE outcome IN ('WIN','LOSS') "
                "AND direction='UP' AND id >= 223438 ORDER BY resolved_at DESC LIMIT %s",
                (streak_n,))
            rows = _cc.fetchall()
        if len(rows) == streak_n and all(r[0] == 'LOSS' for r in rows):
            newest = int(rows[0][1] or 0)
            if recency_days > 0 and newest and (int(time.time()) - newest) > recency_days * 86400:
                LOGGER.info("T23 breaker: %d-loss streak but stale (>%dd) — floor not raised",
                            streak_n, recency_days)
                return base, False, "stale_streak"
            floor = min(cap, base + delta)
            LOGGER.warning("T23 CIRCUIT BREAKER: %d consecutive losses — floor -> %.3f", streak_n, floor)
            return floor, True, str(streak_n) + "_loss_streak"
    except Exception as e:
        LOGGER.warning("circuit breaker check failed: " + str(e)[:80])
    return base, False, "clear"


def _symbol_has_open_pick(cur, symbol: str, now_ts: int = None) -> bool:
    """True if symbol already has an unresolved, unexpired pick."""
    now_ts = int(time.time()) if now_ts is None else now_ts
    cur.execute(
        "SELECT 1 FROM predictions WHERE symbol=%s AND outcome IS NULL AND expires_at > %s LIMIT 1",
        (symbol, now_ts),
    )
    return cur.fetchone() is not None


def run_prediction_cycle(with_diag: bool = False):
    """Run predictions. Returns list of saved picks. Does NOT send Telegram.

    If with_diag=True, returns (saved_picks, diag_dict) for Telegram copy.
    """
    _cycle_started = time.time()
    # T23 circuit breaker (roadmap #1c): env-tunable + recency-guarded (no deadlock).
    _cb_floor, _cb_active, _cb_detail = _circuit_breaker_floor()
    # Kill-condition enforcement (audit §2): pause/alert if a safety threshold
    # tripped. Scanning + gate recording still run while paused so the operator
    # sees where cycles land; only firing (saving picks) is suppressed.
    _pause = {"paused": False}
    try:
        _pause = enforce_kill_conditions()
    except Exception as _ke:
        LOGGER.error("Kill enforcement failed: " + str(_ke)[:80])
    engine_paused = bool(_pause.get("paused"))

    auto_mode_state = objective_autotune_mode()
    regime = _check_regime()
    regime['confidence_floor_override'] = _cb_floor
    # Scan configured watchlist only (no portfolio expansion). Pre-market scans are
    # opt-out via GHOST_PREMARKET_SCAN=0; extended-session price is overlaid in
    # predict_live_ex so daily-trained features stay valid before the cash open.
    symbols = ([(s.strip(), "stock") for s in STOCK_SYMBOLS if s.strip()]
               if _watchlist_scan_enabled() else [])
    skip_counts = {}
    all_picks = []
    closest = None   # silence logging (audit §3): track the highest-up_prob candidate
    symbol_evals = []
    _eval_ts = int(time.time())
    _obj_cfg = _objective_effective_config()
    for symbol, asset_type in symbols:
        _sv = {}
        pick, skip = _predict_symbol_ex(symbol, asset_type, regime, scores_out=_sv)
        if pick:
            all_picks.append(pick)
        elif skip:
            skip_counts[skip] = skip_counts.get(skip, 0) + 1
        try:
            from core.performance_log import symbol_eval_from_scan
            symbol_evals.append(symbol_eval_from_scan(symbol, pick, skip, _sv, _eval_ts))
        except Exception:
            pass
        _up = _sv.get("up_prob")
        if _up is not None and (closest is None or _up > closest["up_prob"]):
            closest = {
                "symbol": symbol,
                "up_prob": _up,
                "min_win_proba": (_sv.get("model_meta") or {}).get("min_win_proba"),
                "confidence": _sv.get("confidence"),
                "confidence_floor": _sv.get("confidence_floor"),
                "bootstrap_min_conf": float(_obj_cfg.get("bootstrap_min_conf", 0.75)),
                "objective_mode": auto_mode_state.get("mode") if isinstance(auto_mode_state, dict) else None,
                "skip": skip,
            }

    all_picks.sort(key=lambda x: x["confidence"], reverse=True)
    top = all_picks[:DAILY_CAP]
    _suppress_reason = None
    _suppressed = 0
    _risk_block = None
    if engine_paused:
        LOGGER.warning(
            "ENGINE PAUSED (kill condition) — suppressing %d candidate(s): %s",
            len(top), _pause.get("reason"))
        _suppressed = len(top)
        _suppress_reason = "engine_paused"
        top = []   # scan + record continue; firing suppressed
    try:
        from core.risk_discipline import combined_trading_block, refresh_daily_loss_lock
        refresh_daily_loss_lock(notify=False)
        _rb = combined_trading_block()
        _risk_block = _rb if isinstance(_rb, dict) else None
        if _rb.get("blocked") and top:
            LOGGER.warning(
                "RISK DISCIPLINE — suppressing %d candidate(s): %s",
                len(top), "; ".join(_rb.get("reasons") or []))
            _suppressed = len(top)
            _suppress_reason = "risk_discipline"
            top = []
    except Exception as _rde:
        LOGGER.warning("risk discipline gate failed: %s", str(_rde)[:80])
    saved = []
    dedup_blocked = 0
    withdrawn_picks: List[Dict[str, Any]] = []
    now_ts = int(time.time())
    with db_conn() as conn:
        cur = conn.cursor()
        # One writer at a time — prevents duplicate open picks when market scan,
        # morning card, and /api/run-predictions overlap in the same second.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_PREDICTION_SAVE_LOCK_ID,))
        if not engine_paused:
            try:
                from core.pick_review import open_pick_review_enabled, notify_withdrawals, review_open_picks
                if open_pick_review_enabled():
                    withdrawn_picks = review_open_picks(cur, symbol_evals, all_picks, now_ts)
            except Exception as _wre:
                LOGGER.warning("Open pick review failed: %s", str(_wre)[:100])
        for pick in top:
            try:
                sym = pick["symbol"]
                if _symbol_has_open_pick(cur, sym, now_ts):
                    LOGGER.info("DEDUP: skipping " + sym)
                    dedup_blocked += 1
                    continue
                cur.execute(
                    "INSERT INTO predictions (symbol,direction,confidence,entry_price,target_price,stop_price,run_at,predicted_at,expires_at,asset_type,features,scores) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (pick["symbol"], pick["direction"], pick["confidence"], pick["entry_price"],
                     pick["target_price"], pick["stop_price"], pick["predicted_at"],
                     pick["predicted_at"], pick["expires_at"], pick["asset_type"],
                     json.dumps(pick.get("features", {})), json.dumps(pick.get("scores", {})))
                )
                pred_id = cur.fetchone()[0]
                pick["id"] = pred_id
                try:
                    from core.feature_schema import FEATURE_ASOF_KEY, persist_feature_snapshot
                    persist_feature_snapshot(
                        cur,
                        symbol=sym,
                        feature_asof_ts=int(pick.get("features", {}).get(FEATURE_ASOF_KEY, now_ts)),
                        payload={"scores": pick.get("scores"), "features": pick.get("features")},
                        prediction_id=pred_id,
                    )
                except Exception as _fse:
                    LOGGER.debug("feature snapshot persist skipped: %s", str(_fse)[:80])
                saved.append(pick)
                for _ev in symbol_evals:
                    if _ev.get("symbol") == sym:
                        _ev["saved"] = True
                        _ev["prediction_id"] = pred_id
            except Exception as e:
                import psycopg2
                if isinstance(e, psycopg2.errors.UniqueViolation):
                    LOGGER.info("DEDUP: unique index blocked " + pick["symbol"])
                    dedup_blocked += 1
                    continue
                LOGGER.error("INSERT " + pick["symbol"] + ": " + str(e))
                raise
    if withdrawn_picks:
        try:
            from core.pick_review import notify_withdrawals
            notify_withdrawals(withdrawn_picks)
        except Exception:
            pass
    LOGGER.info(
        "Cycle: %d/%d picks saved | %d withdrawn | regime: %s",
        len(saved), len(all_picks), len(withdrawn_picks), regime.get("reason") or "OK",
    )
    _saved_ids = [p["id"] for p in saved if p.get("id")]
    # Persist cycle heartbeat even when zero picks are saved.
    try:
        with db_conn() as _hc:
            _cur = _hc.cursor()
            _cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            _now = int(time.time())
            _cur.execute(
                """
                INSERT INTO ghost_state(key,val) VALUES
                    ('last_prediction_cycle_ts', %s),
                    ('last_prediction_cycle_saved', %s),
                    ('last_prediction_cycle_scanned', %s)
                ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val
                """,
                (str(_now), str(len(saved)), str(len(symbols))),
            )
    except Exception as _he:
        LOGGER.warning("Cycle heartbeat write failed: " + str(_he)[:80])

    # PR #29: per-cycle gate-outcome recorder. Rolling history (last 50
    # cycles) in ghost_state so ops can review "did any cycle clear the
    # gates today, and which gate was binding?" without watching the live
    # /admin monitor. `would_fire` = a candidate cleared ALL gates (it may
    # still have been dedup-blocked from saving).
    try:
        import json as _gj
        _binding = resolve_binding_skip(
            skip_counts, dedup_blocked=dedup_blocked, near_miss=closest,
        )
        _top_skip = _binding
        # Silence logging (audit §3): on a quiet cycle, how close did the best
        # candidate come? prob_gap = up_prob - min_win_proba (>=0 cleared the
        # prob gate); conf_gap = confidence - floor (only set on a floor miss).
        _near_miss = enrich_near_miss(closest)
        _entry = {
            "ts": int(time.time()),
            "scanned": len(symbols),
            "candidates": len(all_picks),
            "saved": len(saved),
            "dedup_blocked": dedup_blocked,
            "withdrawn": len(withdrawn_picks),
            "would_fire": len(all_picks) > 0,
            "top_skip": _top_skip,
            "binding_skip": _binding,
            "skip_counts": dict(skip_counts),
            "paused": engine_paused,
            "pause_reason": _pause.get("reason") if engine_paused else None,
            "near_miss": _near_miss,
        }
        with db_conn() as _gc:
            _gcur = _gc.cursor()
            _gcur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            _gcur.execute("SELECT val FROM ghost_state WHERE key='gate_outcome_history'")
            _grow = _gcur.fetchone()
            _hist = []
            if _grow and _grow[0]:
                try:
                    _hist = _gj.loads(_grow[0])
                except Exception:
                    _hist = []
            if not isinstance(_hist, list):
                _hist = []
            _hist.append(_entry)
            _hist = _hist[-50:]
            _gcur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('gate_outcome_history', %s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (_gj.dumps(_hist),),
            )
    except Exception as _ge:
        LOGGER.warning("Gate-outcome record failed: " + str(_ge)[:80])

    # Full-detail performance log (Postgres — retained ~90d by default).
    try:
        from core.performance_log import log_prediction_cycle
        _binding = resolve_binding_skip(
            skip_counts, dedup_blocked=dedup_blocked, near_miss=closest,
        )
        _near_miss_log = enrich_near_miss(closest)
        with db_conn() as _plc:
            _plcur = _plc.cursor()
            log_prediction_cycle(
                _plcur,
                cycle_ts=int(time.time()),
                duration_ms=int((time.time() - _cycle_started) * 1000),
                scanned=len(symbols),
                candidates=len(all_picks),
                saved=len(saved),
                dedup_blocked=dedup_blocked,
                would_fire=len(all_picks) > 0,
                binding_skip=_binding,
                paused=engine_paused,
                pause_reason=_pause.get("reason") if engine_paused else None,
                suppressed=_suppressed,
                suppress_reason=_suppress_reason,
                skip_counts=dict(skip_counts),
                near_miss=_near_miss_log,
                regime=dict(regime) if isinstance(regime, dict) else {},
                circuit_breaker={
                    "active": _cb_active,
                    "detail": _cb_detail,
                    "floor": _cb_floor,
                },
                objective_mode=dict(auto_mode_state) if isinstance(auto_mode_state, dict) else {},
                risk_block=_risk_block,
                saved_prediction_ids=_saved_ids,
                symbol_evals=symbol_evals,
            )
    except Exception as _ple:
        LOGGER.warning("Performance log write failed: " + str(_ple)[:120])

    if not with_diag:
        return saved
    # --- diagnostics for Telegram "no picks" accuracy ---
    _prio = _SKIP_PRIORITY
    _labels = _SKIP_LABELS
    top_reason = resolve_binding_skip(
        skip_counts, dedup_blocked=dedup_blocked, near_miss=closest,
    )
    diag = {
        "symbols_scanned": len(symbols),
        "candidates": len(all_picks),
        "saved": len(saved),
        "dedup_blocked": dedup_blocked,
        "withdrawn": len(withdrawn_picks),
        "skip_counts": dict(sorted(skip_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "regime": regime.get("reason") or "",
        "confidence_floor": regime.get("confidence_floor_override", CONFIDENCE_FLOOR),
        "circuit_breaker": {"active": _cb_active, "detail": _cb_detail, "floor": _cb_floor},
        "objective_mode": _objective_mode(),
        "objective_mode_auto": auto_mode_state,
        "top_reason_code": top_reason,
        "top_reason_label": _labels.get(top_reason, top_reason or "unknown"),
    }
    parts = []
    if dedup_blocked:
        parts.append(_labels["dedup_blocked"] + "=" + str(dedup_blocked))
    for k in _prio:
        if k == "dedup_blocked":
            continue
        c = skip_counts.get(k, 0)
        if c:
            parts.append(_labels.get(k, k) + "=" + str(c))
    diag["skip_summary"] = "; ".join(parts) if parts else ""
    return saved, diag


def get_objective_status() -> Dict[str, Any]:
    """
    Report progress toward the configured objective win rate target.
    """
    auto_state = objective_autotune_mode()
    cfg = _objective_effective_config()
    target_wr = float(cfg["target_wr"])
    lookback_days = int(cfg["lookback_days"])
    cutoff = int(time.time()) - lookback_days * 86400

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END), 0)
            FROM predictions
            WHERE direction IN ('UP','BUY')
              AND outcome IN ('WIN','LOSS')
              AND COALESCE(resolved_at, predicted_at, run_at, 0) >= %s
            """,
            (cutoff,),
        )
        v2_total, v2_wins = cur.fetchone() or (0, 0)

        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(CASE WHEN hit_direction=1 THEN 1 ELSE 0 END), 0)
            FROM ghost_prediction_outcomes
            WHERE predicted_direction='UP'
              AND hit_direction IN (0,1)
              AND EXTRACT(EPOCH FROM created_at)::BIGINT >= %s
            """,
            (cutoff,),
        )
        gpo_total, gpo_wins = cur.fetchone() or (0, 0)

        cur.execute(
            """
            SELECT symbol,
                   COUNT(*)::INT AS total,
                   COALESCE(SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END),0)::INT AS wins
            FROM predictions
            WHERE direction IN ('UP','BUY')
              AND outcome IN ('WIN','LOSS')
              AND COALESCE(resolved_at, predicted_at, run_at, 0) >= %s
            GROUP BY symbol
            HAVING COUNT(*) >= %s
            ORDER BY (COALESCE(SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END),0)::FLOAT / COUNT(*)) DESC, COUNT(*) DESC
            LIMIT 15
            """,
            (cutoff, _objective_min_samples()),
        )
        rows = cur.fetchall()

    v2_total = int(v2_total or 0)
    v2_wins = int(v2_wins or 0)
    gpo_total = int(gpo_total or 0)
    gpo_wins = int(gpo_wins or 0)
    combined_total = v2_total + gpo_total
    combined_wins = v2_wins + gpo_wins
    current_wr = _safe_wr(combined_wins, combined_total)

    top_symbols = []
    for sym, total, wins in rows:
        total_i = int(total or 0)
        wins_i = int(wins or 0)
        top_symbols.append(
            {
                "symbol": str(sym or "").upper(),
                "wins": wins_i,
                "losses": max(0, total_i - wins_i),
                "total": total_i,
                "win_rate_pct": round(_safe_wr(wins_i, total_i) * 100.0, 1),
            }
        )

    runtime_reason = ""
    runtime_updated_ts = None
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='objective_mode_runtime_reason'")
            rr = cur.fetchone()
            runtime_reason = str(rr[0]) if rr and rr[0] else ""
            cur.execute("SELECT val FROM ghost_state WHERE key='objective_mode_runtime_updated_ts'")
            rt = cur.fetchone()
            runtime_updated_ts = int(rt[0]) if rt and rt[0] else None
    except Exception:
        runtime_reason = ""
        runtime_updated_ts = None

    return {
        "objective_enforced": _objective_enforced(),
        "objective_mode": cfg["mode"],
        "objective_mode_reason": runtime_reason,
        "objective_mode_updated_ts": runtime_updated_ts,
        "objective_mode_auto": auto_state,
        "target_win_rate_pct": round(target_wr * 100.0, 1),
        "min_samples": int(cfg["min_samples"]),
        "bootstrap_min_conf_pct": round(float(cfg["bootstrap_min_conf"]) * 100.0, 1),
        "lookback_days": lookback_days,
        "current_win_rate_pct": round(current_wr * 100.0, 1),
        "gap_to_target_pct": round((target_wr - current_wr) * 100.0, 1),
        "combined": {
            "wins": combined_wins,
            "losses": max(0, combined_total - combined_wins),
            "total": combined_total,
        },
        "v2_recent": {
            "wins": v2_wins,
            "losses": max(0, v2_total - v2_wins),
            "total": v2_total,
            "win_rate_pct": round(_safe_wr(v2_wins, v2_total) * 100.0, 1),
        },
        "legacy_recent": {
            "wins": gpo_wins,
            "losses": max(0, gpo_total - gpo_wins),
            "total": gpo_total,
            "win_rate_pct": round(_safe_wr(gpo_wins, gpo_total) * 100.0, 1),
        },
        "top_symbols": top_symbols,
    }


def get_objective_daily_report(days: int = 14) -> Dict[str, Any]:
    """
    Day-by-day realized BUY precision trend.
    """
    days_i = max(3, min(90, int(days)))
    cutoff = int(time.time()) - days_i * 86400
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
              TO_CHAR(DATE_TRUNC('day', TO_TIMESTAMP(COALESCE(resolved_at, predicted_at, run_at))), 'YYYY-MM-DD') AS d,
              COUNT(*)::INT AS total,
              COALESCE(SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END), 0)::INT AS wins
            FROM predictions
            WHERE direction IN ('UP','BUY')
              AND outcome IN ('WIN','LOSS')
              AND COALESCE(resolved_at, predicted_at, run_at, 0) >= %s
            GROUP BY 1
            ORDER BY 1 ASC
            """,
            (cutoff,),
        )
        rows = cur.fetchall()

    series = []
    wins_total = 0
    losses_total = 0
    for day, total, wins in rows:
        total_i = int(total or 0)
        wins_i = int(wins or 0)
        losses_i = max(0, total_i - wins_i)
        wins_total += wins_i
        losses_total += losses_i
        series.append(
            {
                "date": str(day),
                "wins": wins_i,
                "losses": losses_i,
                "total": total_i,
                "win_rate_pct": round(_safe_wr(wins_i, total_i) * 100.0, 1),
            }
        )
    total = wins_total + losses_total
    return {
        "days": days_i,
        "wins": wins_total,
        "losses": losses_total,
        "total": total,
        "win_rate_pct": round(_safe_wr(wins_total, total) * 100.0, 1),
        "series": series,
    }


def reconcile_outcomes():
    """Check open v2 predictions. Bar-path TP/SL first (v3.2 label parity), snapshot fallback."""
    from core.tp_sl_resolve import label_hold_bars, resolve_open_prediction

    resolved = 0
    now = int(time.time())
    hold_bars = label_hold_bars()
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id,symbol,direction,entry_price,target_price,stop_price,expires_at,predicted_at,asset_type "
            "FROM predictions WHERE outcome IS NULL AND predicted_at IS NOT NULL "
            "AND entry_price IS NOT NULL AND entry_price > 0 "
            "AND target_price IS NOT NULL AND stop_price IS NOT NULL"
        )
        open_preds = cur.fetchall()
    for pred_id, symbol, direction, entry, target, stop, expires_at, predicted_at, asset_type in open_preds:
        if None in (entry, target, stop):
            continue
        daily_bars = None
        try:
            from core.signal_engine import _fetch_ohlcv
            daily_bars = _fetch_ohlcv(symbol, asset_type or "stock", period="3m")
        except Exception as _fe:
            LOGGER.debug("reconcile bar fetch %s: %s", symbol, str(_fe)[:80])
        price = get_price(symbol)
        outcome = resolve_open_prediction(
            direction=direction,
            target=float(target),
            stop=float(stop),
            predicted_at=int(predicted_at or 0),
            hold_bars=hold_bars,
            daily_bars=daily_bars,
            snapshot_price=float(price) if price else None,
            now=now,
            expires_at=int(expires_at) if expires_at else None,
        )
        if not outcome:
            continue
        from core.pnl import resolution_exit
        exit_price, pnl = resolution_exit(
            outcome, direction, entry, target, stop, price if price else entry)
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE predictions SET outcome=%s,exit_price=%s,pnl_pct=%s,resolved_at=%s WHERE id=%s",
                (outcome, exit_price, pnl, now, pred_id))
        resolved += 1
        try:
            from core.performance_log import record_pick_resolution
            record_pick_resolution(
                pred_id, symbol, outcome,
                exit_price=exit_price, pnl_pct=pnl, source="reconcile",
            )
        except Exception:
            pass
        LOGGER.info("Resolved " + symbol + " " + direction + ": " + outcome + " " + str(round(pnl,2)) + "%")
        # Watchdog: fire Telegram alert immediately when pick resolves
        if outcome in ("WIN", "LOSS"):
            try:
                from core.telegram import send_position_alert
                usd_out = round(100 * (1 + pnl/100), 2)
                send_position_alert(symbol, direction, outcome, entry, exit_price, pnl, usd_out)
            except Exception as te:
                LOGGER.error("Watchdog alert failed: " + str(te))
    if resolved:
        try:
            from core.risk_discipline import run_risk_discipline_cycle
            run_risk_discipline_cycle(notify=True)
        except Exception as _re:
            LOGGER.warning("risk discipline post-resolve failed: %s", str(_re)[:80])
    return resolved