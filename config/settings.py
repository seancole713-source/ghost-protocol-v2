"""
Central configuration using Pydantic BaseSettings.
All magic numbers and env vars defined here.
"""
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # App
    APP_NAME: str = "Ghost Protocol"
    VERSION: str = "3.0.0"
    DEBUG: bool = False
    
    # Database
    DATABASE_URL: Optional[str] = None
    
    # API Keys
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None
    POLYGON_API_KEY: Optional[str] = None
    
    # V3 Thresholds
    V3_MIN_CONFIDENCE: float = 0.78
    V3_DEFAULT_HOLD_HOURS: int = 72
    V3_ENABLED: bool = True
    
    # Trading Parameters
    DEFAULT_TARGET_PCT: float = 0.066  # 6.6%
    DEFAULT_STOP_PCT: float = 0.033    # 3.3%
    DEFAULT_RR_RATIO: float = 2.0
    
    # Cron Schedule
    TOP10_HOUR: int = 8
    TOP10_MINUTE: int = 0
    TIMEZONE: str = "America/Chicago"
    
    # Data Quality - WOLF (Wolfspeed, SiC semiconductor stock)
    # Price range covers post-bankruptcy emergence + high-growth scenarios
    MIN_PRICE_WOLF: float = 0.50
    MAX_PRICE_WOLF: float = 200.0
    
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


# Singleton instance
settings = Settings()
