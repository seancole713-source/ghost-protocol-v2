"""
Central configuration using Pydantic BaseSettings.
All magic numbers and env vars defined here.

P2-4 (audit): synced with live Railway production env vars as of 2026-06-19.
This file documents defaults; runtime always reads from environment variables.
"""
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # App
    APP_NAME: str = "Ghost Protocol"
    VERSION: str = "2.1.0"  # synced with wolf_app.APP_VERSION
    DEBUG: bool = False
    
    # Database
    DATABASE_URL: Optional[str] = None
    
    # API Keys
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None
    POLYGON_API_KEY: Optional[str] = None
    ALPACA_KEY_ID: Optional[str] = None
    ALPACA_SECRET_KEY: Optional[str] = None
    FINNHUB_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    
    # ── v3.2 XGBoost Engine Gates ──────────────────────────────────────
    # Live production defaults (Railway env, aggressive mode).
    MIN_ALERT_CONFIDENCE: float = 0.75
    V3_MIN_HOLDOUT_ACC: float = 0.38
    V3_MIN_WF_ACC_MEAN: float = 0.40
    V3_MIN_EDGE: float = 0.0
    V3_WF_ACC_MIN_SLACK: float = 0.15
    V3_MIN_TP_SL_WINS: int = 10
    V3_MIN_WF_FOLDS: int = 2
    V3_MIN_WIN_PROBA: float = 0.55
    V3_LABEL_HOLD_BARS: int = 3
    V3_CALIBRATION: str = "on"
    V3_CALIBRATION_METHOD: str = "auto"
    V3_POOL_TRAINING: str = "on"
    V3_SECTOR_FEATURE: str = "off"
    V3_ENSEMBLE: str = "off"
    
    # ── Objective Mode ──────────────────────────────────────────────────
    OBJECTIVE_MODE: str = "aggressive"
    OBJECTIVE_AUTO_MODE_ENABLED: str = "0"
    OBJECTIVE_ENFORCE: str = "1"
    OBJECTIVE_TARGET_WIN_RATE: float = 0.62
    OBJECTIVE_MIN_SAMPLES: int = 8
    OBJECTIVE_BOOTSTRAP_MIN_CONF: float = 0.75
    OBJECTIVE_LOOKBACK_DAYS: int = 120
    
    # ── Kill Conditions ─────────────────────────────────────────────────
    KILL_SWITCH_ENABLED: str = "1"
    KILL_WINRATE_FLOOR: float = 0.70
    KILL_WINRATE_WINDOW: int = 30
    KILL_BRIER_CEILING: float = 0.35
    KILL_BRIER_WINDOW: int = 30
    KILL_CONSEC_LOSSES: int = 3
    KILL_EXPECTANCY_WINDOW: int = 20
    KILL_COOLDOWN_MINUTES: int = 1440
    KILL_MIN_SAMPLES: int = 10
    
    # ── Trading Parameters ──────────────────────────────────────────────
    DEFAULT_TARGET_PCT: float = 0.066  # 6.6%
    DEFAULT_STOP_PCT: float = 0.033    # 3.3%
    DEFAULT_RR_RATIO: float = 2.0
    DAILY_ALERT_CAP: int = 10
    
    # ── Squeeze Monitor ─────────────────────────────────────────────────
    SQUEEZE_MONITOR_ENABLED: str = "1"
    SQUEEZE_MONITOR_INTERVAL: int = 60
    SQUEEZE_PRICE_PCT: float = 5.0
    SQUEEZE_VOL_MULT: float = 2.5
    SQUEEZE_ALERT_COOLDOWN: int = 7200
    
    # ── Scan Cadence ────────────────────────────────────────────────────
    SCAN_INTERVAL_MARKET_MIN: int = 30
    SCAN_INTERVAL_OFFHOURS_MIN: int = 60
    GHOST_PREMARKET_SCAN: str = "1"
    
    # ── Circuit Breakers (P1-3) ────────────────────────────────────────
    CB_YFINANCE_THRESHOLD: int = 5
    CB_YFINANCE_COOLDOWN_S: int = 600
    CB_FINNHUB_THRESHOLD: int = 5
    CB_FINNHUB_COOLDOWN_S: int = 300
    CB_POLYGON_THRESHOLD: int = 5
    CB_POLYGON_COOLDOWN_S: int = 300
    CB_ALPACA_THRESHOLD: int = 5
    CB_ALPACA_COOLDOWN_S: int = 300
    CB_ANTHROPIC_THRESHOLD: int = 3
    CB_ANTHROPIC_COOLDOWN_S: int = 600
    
    # ── Telegram ────────────────────────────────────────────────────────
    TELEGRAM_RETRIES: int = 3
    TELEGRAM_DAILY_MIN_CONF: float = 0.85
    TELEGRAM_WEEKLY_DAY: str = "sunday"
    TELEGRAM_WEEKLY_HOUR: int = 18
    TELEGRAM_DAILY_HOUR: int = 8
    
    # ── Cron Schedule ───────────────────────────────────────────────────
    TIMEZONE: str = "America/Chicago"
    
    # ── Data Quality ────────────────────────────────────────────────────
    MIN_PRICE_WOLF: float = 0.50
    MAX_PRICE_WOLF: float = 200.0
    PRICE_PROVIDER_TIMEOUT_S: float = 8.0
    STOCK_PRICE_TTL_S: int = 60
    INTRADAY_QUOTE_TTL_S: int = 900
    INTRADAY_MOVE_REFRESH_PCT: float = 2.0
    
    # ── Scheduler ───────────────────────────────────────────────────────
    SCHEDULER_TASK_TIMEOUT_S: float = 120.0
    
    # ── Model Coverage ──────────────────────────────────────────────────
    MODEL_COVERAGE_MIN_MODELS: int = 3
    WEEKLY_RETRAIN_MIN_INTERVAL_SEC: int = 604800  # 7 days
    
    # ── News ────────────────────────────────────────────────────────────
    NEWS_SYMBOLS_PER_CYCLE: int = 8
    
    # ── Prediction Cycle ────────────────────────────────────────────────
    PREDICTION_CYCLE_STALE_MIN: int = 2160  # 36h
    
    # ── Calibration ─────────────────────────────────────────────────────
    V3_MAX_CALIBRATION_BRIER: float = 0.31
    
    # ── Security ────────────────────────────────────────────────────────
    RATE_LIMIT_ENABLED: str = "1"
    RATE_LIMIT_RPM: int = 120
    DOCS_ENABLED: str = "0"
    ADMIN_COOKIE_SECURE: str = "1"
    
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


# Singleton instance
settings = Settings()
