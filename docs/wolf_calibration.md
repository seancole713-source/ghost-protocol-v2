# WOLF v3.2 calibration log

Operator sheet for post-overhaul picks (`id >= 223438`). Display confidence is **not** P(win); target **n ≥ 8** resolved WIN/LOSS before sizing or gate changes.

## Engine state (updated 2026-06-01)

| Field | Value |
|-------|-------|
| Kill switch | `consecutive_losses` → 24h cooldown |
| Paused since (CT) | **Monday Jun 1, 2026 11:37:26 AM CDT** |
| Auto-resume (CT) | **Tuesday Jun 2, 2026 11:37:26 AM CDT** |
| UTC resume | `2026-06-02T16:37:26Z` (`auto_resume_at=1780418246`) |
| Last logged pick | `224034` |
| WIN/LOSS toward n≥8 | **2W / 6L** (8 of 8) — calibration count met; keep logging for drift |

## Resolved picks

| pick_id | fired (CT) | resolved (CT) | outcome | conf | pnl% | entry | exit | target | stop | counts_n | notes |
|---------|------------|---------------|---------|------|------|-------|------|--------|------|----------|-------|
| 223970 | 2026-03-27 | — | LOSS | 90% | −1.36 | 15.82 | 15.60 | — | — | yes | pre-May cluster; legacy price scale |
| 224027 | 2026-05-27 | 2026-05-27 | WIN | 75% | +3.65 | 60.80 | 63.02 | 62.32 | — | yes | exit overshoot vs target (+2.5% designed); reconcile cap deployed after |
| 224028 | 2026-05-27 | 2026-05-27 | LOSS | 75% | −2.65 | 63.36 | 61.68 | — | — | yes | |
| 224029 | 2026-05-28 | 2026-05-28 | EXPIRED | 95% | 0 | 67.20 | — | — | — | no | duplicate-fire window; no fill |
| 224030 | 2026-05-28 | 2026-05-28 | LOSS | 95% | −1.63 | 67.20 | 66.11 | 68.88 | 66.11 | yes | display conf at ceiling, not P(win) |
| 224031 | 2026-05-28 | 2026-05-28 | WIN | 95% | +2.44 | 66.56 | 68.18 | — | — | yes | capped at target fill |
| 224032 | 2026-05-28 | 2026-05-28 | LOSS | 95% | −1.59 | 68.92 | 67.82 | — | — | yes | 1st of 3L kill streak |
| 224033 | 2026-05-28 | 2026-05-28 | LOSS | 85% | −1.53 | 67.69 | 66.66 | — | — | yes | 2nd of 3L kill streak |
| 224034 | 2026-05-28 | 2026-05-28 | LOSS | 75% | −1.48 | 65.22 | 64.25 | — | — | yes | 3rd of 3L → cooldown until resume CT above |

## Next resolve (pending)

_Watcher: `scripts/watch_wolf_calibration.py` polls production and appends rows when `pick_id > 224034` resolves._

| pick_id | fired (CT) | resolved (CT) | outcome | conf | pnl% | entry | exit | target | stop | counts_n | notes |
|---------|------------|---------------|---------|------|------|-------|------|--------|------|----------|-------|
| _pending_ | — | — | — | — | — | — | — | — | — | — | log on first post-cooldown resolve |

## Bucket snapshot (production backtest, $1k deploy)

- **70–80%:** 3 picks, 33.3% WR  
- **80–90%:** 1 pick  
- **90%+:** 4 picks, mixed (2W/2L among 95% fires)  
- **All v3.2 trades:** 25% WR, −$42.31 compounded  

Manual size until buckets stabilize: **0.5–1%** recommended; engine `pos_size_pct` tiers are advisory.
