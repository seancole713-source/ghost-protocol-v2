# Split-adjusted model history repair

## Verified defect

The Alpaca branch of `core.signal_engine._fetch_ohlcv_once` did not specify
`adjustment`. Alpaca defaults to raw bars. Five-year training therefore treated
stock splits as price moves; the shared SMH sector context had the same defect.
The Polygon fallback already requested adjusted bars, so model inputs also
changed basis depending on the successful provider.

Read-only IEX queries on September 16 compared the same dates and feed:

| Symbol / dates | Raw close-to-close | Split-adjusted close-to-close |
| --- | --- | --- |
| NVDA, June 7-10, 2024 | 1208.42 -> 121.93 (-89.91%) | 120.84 -> 121.93 (+0.90%) |
| SMH, May 4-5, 2023 | 243.515 -> 124.375 (-48.93%) | 121.76 -> 124.375 (+2.15%) |

Provider documentation: [Alpaca historical bars](https://docs.alpaca.markets/us/reference/stockbars).
`split` adjusts price and volume for forward and reverse splits. It is not a
dividend-inclusive total-return series, and `asof` controls symbol mapping,
not a historical corporate-action knowledge cutoff.

## Repair and evidence boundaries

- Target training, peer training, sector context, live model inference,
  research model inference, and the model's SMA comparison explicitly request
  split-adjusted Alpaca history.
- Cache and in-flight keys include adjustment, preventing a prior raw outcome
  request from poisoning a model request or the reverse.
- Legacy outcome callers retain their existing default. Their immutable
  issuance reference is an observed price: changing only subsequent bars to
  another price basis would corrupt outcomes. Cross-split outcomes still need
  an explicit corporate-action/reference-price policy; this repair does not
  certify those old outcomes or homogenize every fallback provider.
- Feature schema adds `alpaca_split_v1`. Old models cannot silently serve under
  changed input semantics. `tp_sl_swing/v4` is a new contract lineage; v1-v3
  hashes are preserved under the same configuration and v3 is retired.
- No accuracy thresholds, trade gates, production variables, stored model
  payloads, prediction histories, or registered forward experiments are
  manually rewritten. Automatic future retraining uses the corrected inputs.

## Validation

Six regression cases cover SIP/IEX requests, split-adjusted volume, raw/split
cache isolation, invalid modes, matching target/sector requests, and historical
contract identity. The live-score test also checks the split request alongside
the already enforced completed-daily-bar/no-premarket-overlay rules.

Full local unit suite: 2,060 passed; 43 integration tests deselected. Historical
contract v1/v2/v3 SHA comparisons against the pre-change code are identical.
CI and exact production verification are recorded on the repair PR after release.

## What this does not prove

Fixing corporate-action artifacts is not a measured accuracy improvement.
The concurrently frozen 214-candidate evaluation deliberately retains its
original raw snapshot and source code; it is an imperfect baseline, not an
evaluation of this new split-adjusted lineage. No winner from that run can
establish valid 70% accuracy. A new, explicitly declared corrected-data study
and independent forward outcomes remain necessary. Daily five-bar labels are
still not premarket/intraday labels.
