# Historical Options / Short-Interest Data — Provider Comparison

**Purpose.** The 2026-07-16 edge-hunt falsified every feature lever buildable
from free historical data (geometry, SEC fundamentals, momentum — see
PROJECT_STATE.py SESSION_LOG). Options-derived features (PCR, IV, skew, GEX)
are the strongest untested information source, but their *history* is
paywalled — you cannot backtest what you never stored. This doc is the costed
menu for the paid decision. The free forward collector
(`core/options_snapshots.py`, one point-in-time row per symbol per day) runs
regardless, so every week of waiting still accrues testable evidence.

**⚠️ Pricing as of the author's knowledge cutoff (Jan 2026) — verify on the
provider's site before purchase.** Plans and tiers change frequently.

## What Ghost actually needs

- **Granularity:** end-of-day per-symbol chain aggregates are enough — the
  engine trades daily bars with 3-day holds. No tick/quote data needed.
- **Fields:** put/call volume + OI, ATM IV (or IV rank), ideally per-expiry
  so near-term skew is computable. Greeks are nice-to-have (GEX needs them).
- **History depth:** ≥2 years to match `V3_OHLCV_PERIOD`; must be
  point-in-time (as-published), not backfilled/restated.
- **Universe:** the ~100-symbol watchlist, US equities only.

## Options data

| Provider | Coverage | Approx. cost | Fit |
|---|---|---|---|
| **ThetaData** | Historical EOD + intraday chains, IV, greeks; retail-quant focused; REST/Python API | ~$40–140/mo by tier | **Best first candidate** — purpose-built for exactly this backtest shape; EOD tier is the cheap entry |
| **Polygon.io options plans** | OPRA aggregates, chains, greeks/IV snapshots, trades on higher tiers; we already hold a Polygon *stocks* key | ~$99–399/mo by tier | Strong — same vendor already integrated (`POLYGON_API_KEY`), least new plumbing |
| **ORATS** | Clean computed IV surfaces, greeks, earnings-adjusted vols back to 2007; data API | ~$99–399/mo | Best data *quality* for IV features; more than Ghost needs to start |
| **CBOE DataShop** | Official exchange EOD option summaries; pay-per-file download | one-off $ per dataset | Good for a one-time 2-year backfill without a subscription; no live feed |
| **Tradier (brokerage acct)** | Live chains + greeks via API; minimal history | ~$10/mo market data | Live-only — does not solve the backtest problem |
| **Alpaca options data** | OPRA feed on paid tier; we already integrate Alpaca | ~$99/mo tier (verify) | Convenient vendor-wise; historical options depth was thin — verify before relying on it |

## Short interest / borrow data

| Provider | Coverage | Approx. cost | Fit |
|---|---|---|---|
| **SEC fails-to-deliver** | FTD files, free, twice-monthly | free | Already fetchable; weak but honest squeeze signal |
| **FINRA short interest** | Official biweekly SI; free raw files | free | Coarse (biweekly) but point-in-time and official |
| **Ortex** | Daily estimated SI, borrow rates, utilization | ~$50–100/mo retail | The real-time squeeze signal, if squeeze lane ever needs it |
| **Fintel** | SI + borrow + 13F aggregation | ~$25–75/mo | Cheaper, less granular |

## Recommendation

1. **Do nothing until the forward collector has ~4 weeks of snapshots.** Then
   run the same sweep harness with PCR/IV features joined from
   `ghost_options_snapshots` on the forward window. If the features show *any*
   discrimination on 4 weeks of honest data, that justifies the purchase; if
   they show none, we saved the money.
2. **If buying immediately:** start with **ThetaData EOD tier** (~$40/mo) or a
   **CBOE DataShop one-off backfill** — 2 years of EOD chain summaries for the
   watchlist — and run the offline harness on it *before* any live wiring.
   The pre-registered rule from `core/contract_70_verdict.py` applies: a new
   data source must clear the offline harness (serve floors + pooled 70%
   operating point) and then forward-prove before any claim changes.
3. **Skip for now:** Ortex/Fintel (squeeze lane already works and is not the
   70% contract's bottleneck), tick-level anything (wrong horizon).
