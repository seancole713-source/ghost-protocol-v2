"""
Ghost Score v1.0 — Composite Intelligence Rating Specification
==============================================================
Version: 1.0.0
Last updated: 2026-06-19 (PR #65 audit batch)
Status: PRODUCTION

This document is the single source of truth for the Ghost Score formula.
Any change to weights, thresholds, or signal labels MUST bump the version
and be recorded in the changelog below.

─── Formula ───────────────────────────────────────────────────────

  Ghost Score = clamp(raw_score × regime_modifier × squeeze_modifier, 0, 100)

  raw_score = Σ(component_value) for each component below
  regime_modifier ∈ [0.85, 1.15] — from core.regime_calibration
  squeeze_modifier ∈ [1.00, 1.08] — from short-float risk tag

─── Components ─────────────────────────────────────────────────────

  Component         Weight   Range    Computation
  ───────────────   ──────   ─────    ───────────────────────────────────────
  model_confidence    40     [0,40]   BUY  → confidence × 40
                                      SELL → (1 − confidence) × 40
                                      none → 20 (neutral midpoint)

  volume_signal       20     [0,20]   min(20, volume_ratio × 10)
                                      volume_ratio = session_vol / avg_daily_vol
                                      none → 10 (neutral)

  sector_alignment    15     [0,15]   wolf_lagging_up   → 15  (peers up, WOLF flat)
                                      wolf_holding_down → 12  (peers down, WOLF holds)
                                      else              → 7.5 (neutral)

  price_momentum      15     [0,15]   Δ = (price − SMA_5d) / SMA_5d
                                      Δ ≥ +3%  → 15
                                      Δ ≥ +1%  → 12
                                      Δ ∈ ±1%  → 7.5
                                      Δ < −1%  → 3
                                      Δ < −3%  → 0
                                      no data  → 7.5

  freshness           10     [0,10]   Hours since last engine scan cycle:
                                      0–2h   → 10
                                      2–6h   → 8
                                      6–12h  → 6
                                      12–24h → 4
                                      24–48h → 2
                                      >48h   → 0

─── Signal Labels ──────────────────────────────────────────────────

  Score Range    Label         Interpretation
  ───────────    ─────         ──────────────────────────────
  80–100         STRONG_BUY    All components aligned bullish
  60–79          BUY           Majority bullish
  40–59          HOLD          Mixed / neutral
  20–39          SELL          Majority bearish
  0–19           STRONG_SELL   All components aligned bearish

─── Modifiers ──────────────────────────────────────────────────────

  Regime Modifier (multiplicative, applied after raw_score):
    strong_uptrend   → 1.10
    uptrend          → 1.05
    choppy           → 1.00
    downtrend        → 0.95
    strong_downtrend → 0.90
    unknown          → 1.00

  Squeeze Modifier (multiplicative, applied after regime):
    extreme short float (>40% short, DTC <1) → 1.08
    high short float   (>25% short)           → 1.05
    medium short float (>15% short)           → 1.02
    low/unknown                                → 1.00

─── Data Sources ───────────────────────────────────────────────────

  Component          Primary Source          Fallback            Deterministic?
  ─────────          ──────────────          ────────            ─────────────
  model_confidence   signal_engine v3.2      None                YES
  volume_signal      squeeze_monitor         yfinance avg vol    YES
  sector_alignment    wolf_context            None                YES
  price_momentum     prices.get_stock_price  yfinance close      YES
  freshness          ghost_state scan ts     last pick ts        YES
  regime_modifier    regime_classifier       regime_calibration  YES
  squeeze_modifier   yfinance short data     None (→ 1.00)       YES

  NOTE: Claude Haiku sentiment is NOT a Ghost Score component.
  It feeds the news-influence display on the daily card only.

─── Caching ────────────────────────────────────────────────────────

  Ghost Score is cached for 60 seconds in-process.
  Cache key: "ghost-score" in api/wolf_endpoints._CACHE.
  Force-refresh: pass use_cache=False to ghost_score_payload_sync().

─── Changelog ──────────────────────────────────────────────────────

  v1.0.0  2026-06-19  Initial spec. Extracted from api/wolf_endpoints.py
                      compute_ghost_score(). Weights and thresholds
                      unchanged from the live production formula.
                      Added determinism audit: all components are
                      deterministic; Claude sentiment is excluded.
"""

# Re-export the weights dict for programmatic access.
GHOST_SCORE_SPEC_VERSION = "1.0.0"
GHOST_WEIGHTS = {
    "model_confidence": 40,
    "volume_signal": 20,
    "sector_alignment": 15,
    "price_momentum": 15,
    "freshness": 10,
}
GHOST_SIGNAL_LABELS = {
    (80, 100): "STRONG_BUY",
    (60, 79): "BUY",
    (40, 59): "HOLD",
    (20, 39): "SELL",
    (0, 19): "STRONG_SELL",
}
SQUEEZE_MODIFIER = {"extreme": 1.08, "high": 1.05, "medium": 1.02, "low": 1.00}
REGIME_MODIFIER = {
    "strong_uptrend": 1.10,
    "uptrend": 1.05,
    "choppy": 1.00,
    "downtrend": 0.95,
    "strong_downtrend": 0.90,
    "unknown": 1.00,
}
