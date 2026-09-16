# Ghost prediction mission: September 16 evaluation report

## Result

**The repairs are deployed. The accurate premarket/regular-session prediction
objective is not achieved. No 70% live accuracy has been demonstrated.**

We evaluated the entire declared watchlist, not selected winners: 107 symbols,
UP and DOWN, with one fixed configuration and two explicitly recorded data
versions. The first run fixed sector-history coverage but still used Alpaca's
raw bars; its data audit uncovered split-induced fake crashes. The second used
the deployed split-adjusted model-input repair, with unchanged model settings.

| Result | Raw-price baseline | Split-adjusted repair |
| --- | ---: | ---: |
| Declared symbol/direction hypotheses | 214 | 214 |
| Models evaluated | 204 | 204 |
| Insufficient/stale exclusions | 10 | 10 |
| Passing existing training admission | 0 | 0 |
| Qualified candidates | 0 | 0 |
| Execution errors | 0 | 0 |
| Production model writes by experiment | 0 | 0 |

These are **408 offline model evaluations, not 408 independent forward
predictions**. Every attempted hypothesis, including exclusions, is retained
in `fleet_20260916/raw/results.jsonl` and `fleet_20260916/split/results.jsonl`.
Zero candidates passed even before the additional family-adjusted screening
or data-warning exclusions could promote/reject them. This fixed family has
not supplied a release candidate; it does not prove every other strategy must fail.

## Why candidates failed

First binding admission failures (counts sum to 204 per run):

| Gate category | Raw | Split-adjusted |
| --- | ---: | ---: |
| `edge` | 49 | 51 |
| `holdout_acc` | 153 | 152 |
| `wf_acc_mean` | 2 | 1 |

The full reasons and gate statistics are in the JSONL records. Categories include
the existing thin-data research admission behavior; no thresholds were changed
to get a model through. Passing a training gate alone would not establish a
precision proof, forward performance, executable liquidity or net profitability.

## Exclusions and data integrity

- APGE, GPS and SATS: no latest required session (September 15) in this IEX
  snapshot; both directions excluded rather than filled with stale prices.
- BTGO: 37 labeled rows per direction; OGC: 20. Both below the candidate
  trainer's minimum of 50. These exclusions still count in the declared family.
- Split adjustment removed large-jump warnings for AMZN, AVGO, DELL, GOOG,
  GOOGL, MCHP, NFLX, NVDA, SHOP, SMH, TSLA and WMT.
- 27 symbols retain large-move warnings. Those may be genuine market moves,
  reorganizations or data issues; they are not all assumed to be erroneous.
  Any future candidate needs a corporate-action audit of target, peers and proxy.
- IEX is a limited venue feed, not complete consolidated-market coverage.
- Original snapshots are retained locally and hash-bound to the committed
  manifests. Raw vendor data is not redistributed with this report.

## Reproducibility and limits

The original pre-fit program was published at `e1cdd0e` against engine source
`8fe00d7`. The second pre-fit manifest and source hashes were published at
`6169308`, using repair source `910a21c` (released unchanged as `f8015ff`).
Source hashes were rechecked after synchronizing the audit branch with main.
Runtime versions and feature/label schemas are preserved for both runs.

The second screen counts all 428 attempted hypotheses, not just the corrected
run. Both runs used sequential, thread-limited local fitting, blocked HTTP/DB
access during fitting, the actual production trainer and peer pool, purged
walk-forward evaluation, and the fixed isolated 70% target. Production remains
on its deliberate staged 55% contract; no production variable was changed.

This is exploratory historical research, not an untouched dataset relative to
prior project research. Effective-count Wilson screening is not proof of
statistical independence. No reruns, threshold tuning, model activation,
registered-experiment rewrites or live trades were performed. The daily model
still predicts five-bar TP/SL outcomes; it is not an intraday/premarket model.

For baseline reproduction, use its original commit: the newer evaluator
requires source hashes and refuses to reuse an old manifest under changed code.

## Verified production repair

PR #200: https://github.com/seancole713-source/ghost-protocol-v2/pull/200

- Exact release `f8015ff257d6e6457350ed9c88caa040c8a9801c`, Railway deployment
  `8fe31f9e-2853-4aac-8ed8-30d4a124da6f`: SUCCESS and matching `/api/_version`.
- Deployed bar-fetch checks: NVDA's apparent -89.91% split crash becomes
  +0.902%; SMH's -48.9251% becomes +2.1477%. Raw requests remain separately cached.
- Model-input schema now includes `alpaca_split_v1`; new evidence lineage v4.
  Historical contract v1-v3 hashes are preserved under the same configuration.
- Old raw-schema models cannot silently serve on changed inputs. Their weights
  were not rewritten; no valid corrected candidate was activated.
- Production health healthy, score 95. Model, research-forecast and shadow-stats
  endpoints returned HTTP 200. **Fireable models: 0.**
- Release CI 35111862613 passed: 2,060 unit, 43 PostgreSQL integration and
  67 browser tests; 10 browser tests skipped. The GitHub health-audit POST used
  its documented public fallback because CRON_SECRET is not configured there.
- Expanded local audit-tool suite: 2,068 unit tests passed, 43 integration
  deselected; it does not substitute for the separately executed release DB tests.

## Honest handoff

Do not lower gates or relabel research output as approved picks. A healthy
server does not mean a profitable or 70%-accurate strategy. This particular
corrected model family did not qualify; simply leaving it running does not
promise that it eventually will.

A genuine next release needs a qualified, preregistered candidate and new
independent forward outcomes, with the intended session/horizon, corporate-action
resolution policy, coverage and execution-cost assumptions explicitly frozen.
Today's premarket cannot be backfilled as if predictions had been issued then,
and future outcomes cannot be validated today. The mission remains incomplete.
