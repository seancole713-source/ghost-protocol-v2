# edge

An independent forecasting system built **beside** Ghost, not from it. Nothing in
`edge/` imports Ghost's engine (`core/`); it shares only hosting and the database,
and moves to its own repository with one copy.

**Goal:** find developing squeeze and momentum opportunities, explain which are worth
acting on, and manage each decision from entry to exit. A 60–70% hit rate is a
*research objective to be measured*, not a promise.

## Status — be exact about what is and isn't true

| Layer | Module | Built | Verified how |
|---|---|---|---|
| Forecast contract | `contracts` | ✅ | unit tests |
| Frozen forward ledger (3 records) | `ledger`, `store_pg` | ✅ | unit tests; Postgres store not yet run live |
| Outcome resolver | `resolver` | ✅ | unit tests on synthetic minute bars |
| Real-fill reconciliation | `fills`, `broker_alpaca` | ✅ | unit tests; **not connected to a broker** |
| Radar state machine | `radar` | ✅ | unit tests |
| Data health | `health` | ✅ | unit tests |
| Risk (trade / day / open / theme) | `risk` | ✅ | unit tests |
| Statistics | `stats` | ✅ | unit tests; Wilson checked against an outside calculation |
| Detectors, catalysts, strategies | `detectors`, `catalysts`, `setups` | ✅ | unit tests; **every strategy threshold is an unvalidated v0 hypothesis** |
| Miss review | `miss_audit` | ✅ | unit tests |
| Research-claim contract | `research` | ✅ | unit tests; **no worker writes to it yet** |
| Phone cards | `cards` | ✅ | unit tests |
| Data providers | `providers/*` | ✅ | written to vendor docs; **the build machine cannot reach them** |
| Readiness probe | `probe` | ✅ | runs daily **in production**; results are `EDGE_PROBE_SUMMARY` log lines |
| **Shadow pipeline** | `pipeline` | ✅ | **runs every market day in production**: card 09:05–09:28 ET, grading and miss review 16:20–20:00 ET. Experiment `gap_and_go_auto@v1`. Logs `EDGE_SHADOW`. |

Nothing here trades, and nothing here is validated yet. The shadow pipeline records
forecasts on free data (Alpaca movers, IEX premarket prices, SIP bars >15 min old,
Alpaca news); every name it cannot price is recorded as data-unavailable, not skipped. The only rule being traded is
Gap-and-Go v1 (`docs/gap_and_go_v1.md`), by the operator, at $1,000 per trade.

## Before buying data

Read the latest `EDGE_PROBE_SUMMARY` lines. Each strategy reads `READY`, `DEGRADED` or
`BLOCKED`, and a blocked one names what would unblock it. Plan names come from vendor
documentation. **Get written quotes and licence terms before buying.**

## Rules this code enforces

- A forecast is recorded before its window opens, or it isn't a forecast.
- A spec is hashed. Any change is a new version with a new record.
- Final outcomes are immutable. Missing data is `UNRESOLVED`, never a win or a loss.
- Forecast outcome, simulated execution and actual fills are three separate records.
- Unknown is never zero. Only a strategy's *required* inputs can block it.
- Research claims need citations. Workers never state probabilities, and two models agreeing is not proof.
- A win rate is always shown with its interval. 45% vs a 37.5% break-even needs about 263 trades.
