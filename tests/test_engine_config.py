"""Tests for core/engine_config.py's env-tunable knobs.

Focused on _v3_ohlcv_period: raised from 2y to 5y on 2026-08-31 because live
model status showed the 2y window already producing plenty of raw training
rows (~375/symbol) but too few gate-slice *picked* sessions for models with
real edge (e.g. ITRI/DOWN) to clear the precision gate's effective-session
floor. More history is a proportional lever on that count.
"""
from __future__ import annotations

from core import engine_config as ec


def test_ohlcv_period_default_is_5y(monkeypatch):
    monkeypatch.delenv("V3_OHLCV_PERIOD", raising=False)
    assert ec._v3_ohlcv_period() == "5y"


def test_ohlcv_period_env_override_still_wins(monkeypatch):
    """Ops can still ratchet this from Railway without a code change."""
    monkeypatch.setenv("V3_OHLCV_PERIOD", "3y")
    assert ec._v3_ohlcv_period() == "3y"


def test_ohlcv_period_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("V3_OHLCV_PERIOD", "")
    assert ec._v3_ohlcv_period() == "5y"


def test_ohlcv_period_strips_whitespace(monkeypatch):
    monkeypatch.setenv("V3_OHLCV_PERIOD", "  5y  ")
    assert ec._v3_ohlcv_period() == "5y"
