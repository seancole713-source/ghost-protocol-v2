"""core/engine_config.py — env-tunable v3 engine knobs (split from signal_engine PR #130).

Every getter reads the environment at call time so tests can monkeypatch.setenv
and ops can ratchet values from Railway without a code change. core.signal_engine
re-exports all of these, so existing imports and monkeypatch targets keep working.
"""
import os
import logging

LOGGER = logging.getLogger("ghost.engine_config")

V3_LABEL_HOLD_BARS = max(1, int(os.getenv("V3_LABEL_HOLD_BARS", "3")))


def _min_backtest_bars() -> int:
    """Min OHLCV rows from the feed before backtest_symbol bothers to label (was 100)."""
    return max(1, int(os.getenv("MIN_BACKTEST_BARS", "50")))


def _backtest_window() -> int:
    """Trailing-history window each labeled sample sees (was hardcoded 220).
    Smaller window = more labeled samples from limited data but noisier features."""
    return max(20, int(os.getenv("V3_BACKTEST_WINDOW", "120")))


def _v3_ohlcv_period() -> str:
    """Default OHLCV lookback for training backtests (2y gives more labeled samples)."""
    return (os.getenv("V3_OHLCV_PERIOD", "2y") or "2y").strip()


def _v3_ohlcv_fetch_retries() -> int:
    return max(1, int(os.getenv("V3_OHLCV_FETCH_RETRIES", "3")))


def _v3_train_symbol_delay_sec() -> float:
    """Pause between symbols during batch train to avoid Alpaca rate-limit empty responses."""
    return max(0.0, float(os.getenv("V3_TRAIN_SYMBOL_DELAY_SEC", "0.35")))


def _v3_scan_symbol_delay_sec() -> float:
    """Pause between symbols during live scan to avoid API rate-limit storms.

    Default 0.5s — with 43 symbols that adds ~21s to the scan but prevents
    the 429 cascade that kills all 5 feed tiers simultaneously."""
    return max(0.0, float(os.getenv("V3_SCAN_SYMBOL_DELAY_SEC", "0.5")))


def _v3_adx_trending_threshold() -> float:
    """ADX threshold for 'trending' classification in regime gate.

    Default 12 (lowered from 20). Below this, the market is
    considered choppy/sideways and BUY signals are blocked.
    Set V3_ADX_TRENDING_THRESHOLD lower to allow picks in milder trends."""
    return max(5.0, float(os.getenv("V3_ADX_TRENDING_THRESHOLD", "12")))


def _v3_watchlist_peer_pool_enabled() -> bool:
    return (os.getenv("V3_WATCHLIST_PEER_POOL", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def _v3_watchlist_peer_pool_max() -> int:
    return max(0, int(os.getenv("V3_WATCHLIST_PEER_POOL_MAX", "12")))


def _model_payload_max_bytes() -> int:
    return max(1024, int(os.getenv("V3_MODEL_MAX_BYTES", str(50 * 1024 * 1024))))


def _v3_min_holdout_acc() -> float:
    from core.accuracy_contract import resolve_float
    return resolve_float("V3_MIN_HOLDOUT_ACC", "min_holdout_acc", lo=0.30, hi=0.95)


def _v3_min_edge() -> float:
    from core.accuracy_contract import resolve_float
    return resolve_float("V3_MIN_EDGE", "min_edge", lo=0.0, hi=0.50)


def _v3_min_wf_edge() -> float:
    """Walk-forward edge floor.

    Raised from -0.05 to 0.0 (PR #135 audit): a model with negative
    out-of-time edge must never count toward a 70% system. Env can still
    tighten upward; loosening below zero requires an explicit env choice.
    """
    return float(os.getenv("V3_MIN_WF_EDGE", "0.0"))


def _v3_min_win_proba() -> float:
    from core.accuracy_contract import resolve_float
    return resolve_float("V3_MIN_WIN_PROBA", "min_win_proba", lo=0.40, hi=0.95)


def _v3_min_tp_sl_wins() -> int:
    return max(5, int(os.getenv("V3_MIN_TP_SL_WINS", "15")))


def _v3_min_wf_folds() -> int:
    from core.accuracy_contract import resolve_int
    return resolve_int("V3_MIN_WF_FOLDS", "min_wf_folds", lo=2, hi=12)


def _v3_min_wf_acc_mean() -> float:
    from core.accuracy_contract import resolve_float
    return resolve_float("V3_MIN_WF_ACC_MEAN", "min_wf_acc_mean", lo=0.30, hi=0.95)


def _v3_wf_acc_min_slack() -> float:
    return float(os.getenv("V3_WF_ACC_MIN_SLACK", "0.05"))


def _v3_split_train_frac() -> float:
    return float(os.getenv("V3_SPLIT_TRAIN", "0.70"))


def _v3_split_calib_frac() -> float:
    return float(os.getenv("V3_SPLIT_CALIB", "0.15"))


def _v3_holdout_slices(n: int) -> tuple:
    """Time-ordered train | calib | gate index bounds (calib and gate never overlap).

    Default 70/15/15. Calibration fits on the middle slice; promotion gates use
    only the final slice so holdout accuracy is not reused for Platt/isotonic.
    """
    n = int(n)
    if n < 3:
        return 1, max(1, n - 1)
    train_end = max(1, int(n * _v3_split_train_frac()))
    calib_end = max(train_end + 1, int(n * (_v3_split_train_frac() + _v3_split_calib_frac())))
    if calib_end >= n:
        calib_end = n - 1
    if calib_end <= train_end:
        calib_end = min(n - 1, train_end + 1)
    return train_end, calib_end


def _purged_holdout_bounds(n: int, train_end: int, calib_end: int, purge: int) -> tuple:
    """Purged fit-bounds for the train/calib slices (leakage guard).

    Triple-barrier labels look ahead V3_LABEL_HOLD_BARS bars, so the last
    `purge` rows of the train slice resolve on calib-period prices and the last
    `purge` rows of the calib slice resolve on gate-period prices. Fitting or
    threshold-selecting on those rows leaks the very future the precision gate
    is supposed to be proven against. Returns (train_fit_end, calib_fit_end):

        X_train = X[:train_fit_end]           # purged tail dropped
        X_calib = X[train_end:calib_fit_end]  # purged tail dropped
        X_gate  = X[calib_end:]               # untouched

    Guards keep at least 1 row per slice on tiny datasets (fallback paths
    already handle degenerate calib sizes).
    """
    purge = max(0, int(purge))
    train_fit_end = max(1, int(train_end) - purge)
    calib_fit_end = max(int(train_end) + 1, int(calib_end) - purge)
    return train_fit_end, calib_fit_end


def _v3_wf_acc_min_overrides() -> dict:
    """
    Optional per-symbol absolute floor overrides for wf_acc_min.
    Env format: V3_WF_ACC_MIN_OVERRIDES="WOLF=0.55"

    Sanity cap: no override can exceed the base wf_acc_mean floor (40% default).
    A per-symbol wf_acc_min above the wf_acc_mean requirement is nonsensical —
    it demands every fold beat the mean requirement, which thin-data symbols
    (like post-Chapter-11 WOLF) cannot satisfy.
    """
    raw = (os.getenv("V3_WF_ACC_MIN_OVERRIDES", "") or "").strip()
    out = {}
    if not raw:
        return out
    cap = _v3_min_wf_acc_mean()  # 40% default — no override can exceed this
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        sym = (k or "").strip().upper()
        if not sym:
            continue
        try:
            val = float(v.strip())
            if val > cap:
                LOGGER.info(
                    "wf_acc_min override %s=%.2f capped at base wf_acc_mean %.2f",
                    sym, val, cap,
                )
                val = cap
            out[sym] = val
        except Exception:
            continue
    return out


def _v3_holdout_acc_overrides() -> dict:
    """Optional per-symbol holdout accuracy floors (thin gate slices).

    Env format: V3_HOLDOUT_ACC_OVERRIDES=\"TLRY=0.47,SPCE=0.52\"
    """
    raw = (os.getenv("V3_HOLDOUT_ACC_OVERRIDES", "") or "").strip()
    out = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        sym = (k or "").strip().upper()
        if not sym:
            continue
        try:
            out[sym] = float(v.strip())
        except Exception:
            continue
    return out


def _v3_wf_min_train_floor() -> int:
    """Absolute minimum training-window size for walk-forward folds.

    Was hardcoded to 120, which produced ZERO folds on WOLF's
    post-restructure dataset (127 samples → 120 train + 20 test
    overshoots n). Env-tunable so small-dataset tickers can run WF;
    larger defaults can be set back on production once WOLF accumulates
    more history.
    """
    return max(20, int(os.getenv("V3_WF_MIN_TRAIN", "60")))


def _v3_wf_min_train_frac() -> float:
    """Floor as fraction of total samples (was 0.50)."""
    try:
        return float(os.getenv("V3_WF_MIN_TRAIN_FRAC", "0.40"))
    except Exception:
        return 0.40


def _v3_wf_test_size_floor() -> int:
    """Absolute minimum per-fold test-window size (was hardcoded to 20)."""
    return max(5, int(os.getenv("V3_WF_TEST_SIZE", "15")))


def _v3_wf_test_size_frac() -> float:
    """Test-window size as fraction of total samples (was 0.10, unchanged)."""
    try:
        return float(os.getenv("V3_WF_TEST_FRAC", "0.10"))
    except Exception:
        return 0.10


def _v3_calibration_enabled() -> bool:
    """Whether to wrap the trained model with probability calibration.

    Calibration turns raw XGBoost predict_proba into a true win-probability so
    the V3_MIN_WIN_PROBA firing threshold and the displayed confidence track
    the realized win-rate. On by default; set V3_CALIBRATION=off to disable.
    """
    return (os.getenv("V3_CALIBRATION", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def _v3_calibration_method() -> str:
    """Calibration map: isotonic | sigmoid | auto.

    auto picks sigmoid (Platt) for small calibration sets and isotonic once
    there is enough data for a stable non-parametric fit — isotonic overfits
    on the ~25-sample WOLF holdout, sigmoid does not.
    """
    return (os.getenv("V3_CALIBRATION_METHOD", "auto") or "auto").strip().lower()


def _v3_pool_training_enabled() -> bool:
    """Whether to pool peer/sector samples into the model's training set (W1).

    WOLF's post-Ch.11 history yields only ~127 labeled samples — too few for a
    stable model. Pooling labeled samples from sector peers multiplies the
    training data; all quality gates still judge the model on WOLF's own
    holdout. Enabling pooling also price-normalizes macd_hist (the one raw
    price-unit feature) so it is comparable across tickers. On by default; set
    V3_POOL_TRAINING=off for the prior WOLF-only behavior.
    """
    return (os.getenv("V3_POOL_TRAINING", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def _v3_down_signals_enabled() -> bool:
    """Whether DOWN-model signals may fire live (Phase 2).

    Off by default: DOWN probabilities are journaled shadow-only (scores
    carry down_prob on every scan) until the DOWN lane earns a track record.
    A stronger-but-unfireable DOWN score must never suppress a fireable UP
    pick. core.prediction honors the same flag at fire time.
    """
    return (os.getenv("V3_DOWN_SIGNALS_ENABLED", "0") or "0").strip().lower() in (
        "1", "on", "true", "yes",
    )


def _v3_wolf_sample_weight() -> float:
    """Training weight on the target's own rows relative to peer rows (W1).

    Peers broaden the fit but the model must still specialize to WOLF, so WOLF
    samples are up-weighted. Default 3.0; set V3_WOLF_SAMPLE_WEIGHT to tune.
    """
    try:
        return max(1.0, float(os.getenv("V3_WOLF_SAMPLE_WEIGHT", "3.0")))
    except Exception:
        return 3.0


def _v3_sector_feature_enabled() -> bool:
    """Whether to add the sector relative-strength feature to the model (W3).

    Off by default: it changes the model's feature vector and needs offline
    validation before it can be trusted, so the operator opts in via
    V3_SECTOR_FEATURE=on and re-validates. When off the column isn't added at
    all, so model shape and behavior are unchanged.
    """
    return (os.getenv("V3_SECTOR_FEATURE", "off") or "off").strip().lower() in (
        "1", "on", "true", "yes",
    )


def _v3_sector_proxy() -> str:
    """Ticker whose price series stands in for the sector (W3 relative strength)."""
    return (os.getenv("SECTOR_PROXY", "SMH") or "SMH").strip().upper()


def _v3_sector_lookback() -> int:
    """Bars over which sector relative strength is measured (W3)."""
    return max(2, int(os.getenv("V3_SECTOR_LOOKBACK", "20")))


def _v3_ensemble_enabled() -> bool:
    """Whether to blend a second model with XGBoost (W5).

    Off by default: it changes the persisted model and must be validated before
    it's trusted. V3_ENSEMBLE=on enables a soft-voting blend of XGBoost with a
    RandomForest, each individually probability-calibrated.
    """
    return (os.getenv("V3_ENSEMBLE", "off") or "off").strip().lower() in (
        "1", "on", "true", "yes",
    )


def _v3_prune_features() -> set:
    """Feature columns to drop from the model (W5 feature pruning).

    Operator-driven from the attribution view: set V3_PRUNE_FEATURES to a comma
    list of column names that don't separate winners from losers. Empty by
    default. Skew-safe without a schema bump because prediction reads the trained
    column set back from model meta.
    """
    raw = (os.getenv("V3_PRUNE_FEATURES", "") or "").strip()
    return {p.strip() for p in raw.split(",") if p.strip()}


def _v3_feature_schema() -> str:
    """Version tag for the feature vector's *semantics* (not just its columns).

    load_model rejects any persisted model whose feature_schema differs from the
    current one, forcing a retrain. This prevents train/serve skew when a
    feature's scale/meaning changes — e.g. the macd_hist price-normalization
    that flips with V3_POOL_TRAINING, or the W3 sector column toggling. A model
    trained under one schema must not be served features computed under another.
    """
    macd = "macd_pct_v1" if _v3_pool_training_enabled() else "macd_raw_v0"
    sector = "sec1" if _v3_sector_feature_enabled() else "sec0"
    audit = "fa1" if _v3_feature_audit_enabled() else "fa0"
    return f"{macd}+{sector}+{audit}"


def _v3_feature_audit_enabled() -> bool:
    from core.feature_audit import _v3_feature_audit_enabled as _enabled
    return _enabled()


def _v3_wf_purge() -> int:
    """Bars to purge between each walk-forward train block and its test block.

    The triple-barrier label looks ahead V3_LABEL_HOLD_BARS bars, so the last
    few training samples before a test block carry outcomes that resolve inside
    the test period — naive walk-forward leaks that future into training. Purging
    hold_bars samples at the boundary removes the overlap (López de Prado purged
    CV). Defaults to the label horizon; set V3_WF_PURGE=0 to disable.
    """
    return max(0, int(os.getenv("V3_WF_PURGE", str(V3_LABEL_HOLD_BARS))))


def _v3_max_calibration_brier() -> float:
    """Max acceptable Brier on the final holdout (gate) slice after calibration."""
    _PHASE5_FLOOR = 0.31  # tp_sl_fwd_v1 watchlist clears through ~0.305 on prod
    try:
        raw = os.getenv("V3_MAX_CALIBRATION_BRIER")
        val = float(raw) if raw not in (None, "") else _PHASE5_FLOOR
    except Exception:
        val = _PHASE5_FLOOR
    if val < _PHASE5_FLOOR:
        LOGGER.info(
            "V3_MAX_CALIBRATION_BRIER=%s below Phase 5 floor; using %s",
            val, _PHASE5_FLOOR,
        )
        return _PHASE5_FLOOR
    return val
