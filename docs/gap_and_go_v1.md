# Gap-and-Go v1 — FROZEN

**Frozen:** 2026-09-22. **Operator decision:** Option A — $1,000 per trade.
**First card:** Wednesday 2026-09-23, 9:00am ET.

## Why this exists

Ghost ran for ~10 months without one traded prediction. Every agent tried to
*prove* a model before *trading* anything; the gates rejected everything, so no
evidence accumulated, so the next agent wrote a new plan. This document breaks
that loop: one simple, standard rule, traded small, measured honestly.

## The freeze

**Nothing in this file changes until 20 filled trades are logged.** Not the
thresholds, not the levels, not the ranking. Not because a trade lost, not
because a week was bad, not because a new agent has a better idea. Any change
is **v2**, with a **new** ledger; v1's record never transfers.

This is the whole point. A rule that is edited after every loss is not a rule
with a win rate — it is a series of guesses.

## The math that makes it viable

Target +5%, stop −3%. Break-even win rate = 3 / (5 + 3) = **37.5%** before
costs. The rule does **not** need 60–70% accuracy to make money; ~45% is a
good result. The win rate of this rule is **unknown** until it is traded.

## Eligibility — every rule must pass

| # | Rule |
|---|---|
| E1 | Premarket move **+5% to +40%** vs the prior regular-session close, measured from a **timestamped current-session** premarket price no more than **30 min** old. Long only. Over +40% = exhausted/illiquid, excluded. |
| E2 | Reference price **$2 to $500**. |
| E3 | 3-month average daily volume **≥ 500,000 shares** AND average daily **dollar** volume **≥ $5M**. |
| E4 | A **dated, company-specific catalyst** published in the last **24h**, verified with a source link: earnings/guidance, FDA/regulatory decision, material contract, M&A, index inclusion, or analyst upgrade with a price target. **Sector sympathy with no company news is NOT a catalyst.** No catalyst found → not eligible. |
| E5 | **Not mechanical or dilutive:** not an ex-dividend or split day (discovery `corporate_action`), and **no offering / ATM / dilution** announced in the last 24h — an offering is a sell catalyst. |
| E6 | Common stock on a US exchange. No OTC, ETFs, warrants, rights, units. |

**Ranking:** average daily **dollar volume**, highest first (liquidity = less
slippage). Take the **top 2**. Mechanical — no judgment calls in the ranking.

**Max 2 trades per day. Max 1 per symbol per day. Zero is a valid day.**

## The order — one bracket per name

Let `ref` = the reference premarket price at card time.

| Leg | Order | Price |
|---|---|---|
| Entry | **BUY stop-limit**, DAY | stop = `ref × 1.01`, limit = `ref × 1.02` |
| Shares | — | `floor(1000 / (ref × 1.01))` |
| Target | **SELL limit** (OCO with stop) | `entry_stop × 1.05` |
| Stop | **SELL stop** (OCO with target) | `entry_stop × 0.97` |
| Entry expiry | cancel if not filled | **10:30am ET** |
| Time exit | **SELL at market** if still open | **3:30pm ET** |

Place as one bracket (OTO + OCO) before 9:30am ET. The entry only fills if the
stock *keeps going* above its premarket level — no chasing a fade. If it gaps
past the limit, it does not fill; that is intended.

**The 3:30pm time exit must be done by hand** (or with a broker time-based
order). Bracket children are DAY orders and expire at 4:00pm, leaving an
unprotected position overnight if forgotten. Set a phone alarm.

## Risk

- Worst planned loss per trade: **~$30** (3% of $1,000). Low-priced, fast
  names can gap through a stop — real losses can exceed $30. That is recorded
  as it happened, never smoothed.
- Worst planned day: **~$60**.
- **Pause rule:** if cumulative v1 P&L reaches **−$300** before trade 20, stop
  and review. That is ten full stops — the rule is not working.

## Grading — the scoreboard

Every card is written to the ledger **before 9:30am ET** and never edited
afterwards. Graded after the close:

| Outcome | Meaning | Counts toward 20? |
|---|---|---|
| `NO_FILL` | Entry never triggered, or gapped past the limit, or expired 10:30 | No |
| `WIN` | Target hit before stop | Yes |
| `LOSS` | Stop hit before target | Yes |
| `TIME_EXIT` | Neither by 3:30pm; P&L at exit price | Yes |
| `SKIPPED` | Operator did not place it | No — recorded separately |

If one bar touches both target and stop, it is graded **LOSS** (conservative).
The operator's actual broker fills are the source of truth when reported;
otherwise the grade comes from intraday bars and is marked as such.

## Decision at 20 filled trades

- Win rate **≥ 45%** and net P&L **> $0** → continue v1 at $1,000 for 20 more,
  then consider $2,000.
- Otherwise → stop v1. Write v2 from what the 20 trades showed — a new ledger.

## Honest limits

- The win rate is unknown until traded. Gap-and-go is a common setup, not a
  proven edge.
- The operator places every order. Ghost/Claude produce the card and the
  grade; they never trade.
- Data gaps are real: Ghost's discovery lane cannot see or price some names
  (e.g. the 2026-09-22 critical-minerals movers). The card covers what can be
  verified, and says what it could not see.
