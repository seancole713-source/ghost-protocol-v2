# edge

An independent forecasting system built **beside** Ghost, not from it. Nothing in
`edge/` imports Ghost's engine (`core/`); it shares only hosting and the database,
and moves to its own repository with one copy.

**Goal:** find developing squeeze and momentum opportunities, explain which are worth
acting on, and manage each decision from entry to exit. A 60–70% hit rate is a
*research objective to be measured*, not a promise.

## Status — what is built, what runs, how it is verified

| Layer | Module | Runs in production | Verified how |
|---|---|---|---|
| Forecast contract (premarket + intraday) | `contracts` | yes | unit tests |
| Frozen forward ledger, 3 records | `ledger`, `store_pg` | yes (`edge_rows`) | unit tests |
| Outcome resolver (market / simulated) | `resolver` | yes, 16:20 ET | unit tests on minute bars |
| Real fills (paper broker) | `paper`, `fills`, `broker_alpaca` | yes, **paper only** (`EDGE_PAPER_ENABLED`) | unit tests; live paper fills from the first shadow card |
| Premarket shadow card | `pipeline` | yes, 09:05–09:28 ET | end-to-end simulated day |
| Baseline + verified experiments | `pipeline` | baseline yes; verified only with research on | unit tests |
| Intraday radar, 3 strategies | `intraday` | yes, 09:45–14:30 ET | end-to-end simulated session |
| Universe snapshot | `universe` | yes, 06:00–07:00 ET | unit tests |
| Miss review (full market) | `miss_audit`, `pipeline` | yes, 07:00–09:04 ET for the prior session | unit tests |
| Historical backtest | `backtest` | yes, once, overnight | point-in-time tests |
| Readiness probe | `probe`, `providers/*` | yes, daily | **production-verified 2026-09-22** |
| Research worker (Claude Opus 5.5) | `research_worker` | **off** until `EDGE_RESEARCH_ENABLED` (costs API money; cap `EDGE_RESEARCH_DAILY_USD`) | fake-client tests |
| Phone messages | `notify` | yes (`EDGE_TELEGRAM_ENABLED`) | unit tests |
| Promotion gate | `promotion` | yes (in every readout) | unit tests |
| Readout / MCP | `readout`, `ghost_edge_report` | yes | unit tests |
| Model layer | `features`, `models` | trains after the backtest; forecasts ONLY if it beats the base rate AND the baseline out of sample | planted-signal vs noise tests |
| Replay guard | `replay` | yes, 20:00 ET | simulated-refactor test |
| Independent reviewer | `research_openai` | when `OPENAI_API_KEY` works and `EDGE_OPENAI_MODEL` is set (probe lists usable models) | fallback tests |
| Streaming | `stream` | off until `EDGE_STREAM_ENABLED` (worth it with SIP data: `EDGE_STREAM_FEED=sip`) | protocol tests |
| AI scorecard | `scorecard` | yes, 16:20 ET: every priced card candidate graded as a labelled counterfactual; compares what research/keywords/model approved vs rejected | unit + simulated-day tests |
| Agent notes | `agent_notes`, `ghost_edge_note` | yes: append-only notes from the scheduled agents; never evidence | unit tests |
| Standalone service | `service`, `requirements.txt`, `railway.json` | ready; `EDGE_STANDALONE=1` after switching Ghost's edge jobs off | isolated CI (`.github/workflows/edge.yml`) |

Every strategy except the frozen Gap-and-Go v1 levels is an **unvalidated v0 hypothesis**.
Nothing here trades real money: the operator places every live order.

### Production facts (probe + logs, 2026-09-22)

- Works on current keys: full-market daily bars, minute bars, reference data, splits,
  dividends, news, Alpaca movers, SEC filings, FINRA short volume.
- Plan-limited: real-time SIP quotes (Alpaca 403), Polygon all-tickers snapshot (403).
- The Polygon key has a **per-minute request allowance** that Ghost's own signal engine
  exhausts; every edge Polygon call waits out a 429 rather than failing.
- IBKR's borrow FTP is unreachable from Railway; iBorrowDesk (HTTPS) is probed instead.

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
