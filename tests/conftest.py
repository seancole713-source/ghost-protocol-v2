"""Pytest bootstrap — allow STOCK_SYMBOLS env overrides in tests only."""
import os

os.environ.setdefault("GHOST_ALLOW_ENV_WATCHLIST", "1")
