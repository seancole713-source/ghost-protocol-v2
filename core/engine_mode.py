"""CORE_ENGINE_MODE — is the old core v3 engine a trade source or research only?

Operator decision 2026-09-26 (audit 2026-09-25, F16/F17/F06, section I
"Retire"): the core v3 engine is RESEARCH-ONLY until an out-of-sample study
shows a lane that beats its base rate. Evidence at the time: 0 of 255 models
serveable; median UP holdout accuracy 54.7% vs a 57.2% natural rate; served
probabilities uncalibrated (the 70-80% band realized 56%) while position
sizing was tiered by confidence.

Env ``CORE_ENGINE_MODE``:
  * ``research`` (default, and the fallback for any unknown value): core
    picks are never sent as trade alerts and never sized. Operator-facing
    copy carries ``RESEARCH_HEADER``, drops BUY/SUPER BUY tiers, dollar and
    %-of-account sizing, and shows the raw model probability labelled
    "uncalibrated". Training, the shadow lane, the research pipeline and
    every ledger keep running unchanged so evidence still accumulates.
  * ``live``: restores the previous behaviour. Only when explicitly set.

This flag touches presentation and alert dispatch only. It does not change
model gates or thresholds. The edge engine is not affected.
"""
from __future__ import annotations

import os

RESEARCH = "research"
LIVE = "live"
MODES = (RESEARCH, LIVE)
DEFAULT_MODE = RESEARCH

RESEARCH_HEADER = "RESEARCH ONLY - not a trade (core v3 has no model that beats its base rate)"
RESEARCH_TRADE_ACTION = "RESEARCH ONLY"
RESEARCH_TRADE_NOTE = "Core v3 call is research-only - not a trade. No sizing."


def core_engine_mode() -> str:
    """Current mode, read at call time. Anything but an explicit 'live' is research."""
    raw = (os.getenv("CORE_ENGINE_MODE") or "").strip().lower()
    return LIVE if raw == LIVE else RESEARCH


def core_is_research(mode: str | None = None) -> bool:
    """True unless ``mode`` (or the env, when ``mode`` is None) is exactly 'live'."""
    if mode is None:
        return core_engine_mode() == RESEARCH
    return (str(mode).strip().lower() != LIVE)


def fmt_raw_prob(p) -> str:
    """Raw model probability as a bare 0-1 number, never a confidence percent."""
    try:
        return format(float(p), ".2f")
    except (TypeError, ValueError):
        return "--"
