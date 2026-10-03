# Ghost research eval: proposed cases (DRAFT, nothing has been run)

Flow under test: `edge.research_worker.research_symbol` (author = claude-opus-5-5 with web search/fetch,
then reviewer). One case = one symbol on one day with a research cutoff (US/Eastern).
Expected output per case is a gold label, not a reference text: `catalyst` (yes/no), `dilution`
(yes/no/unknown), `stale_trap` (an in-window-looking item that is really outside the window),
`entity_trap` (a same-ticker or similar-name company the author must not use).

**Where the gold came from.** Column `gold` says: `ghost-record` = taken from Ghost's own stored research
for that day, which was produced by the SAME model family under test (so it can only be trusted once a
human has checked it); `miss-review` = from the daily miss review (price/volume truth plus a stored
catalyst headline, not a research label); `to-source` = I have a lead, no label yet.

## A. Cases with a stored research record (7 symbol-days, measured cost $0.29-$0.54 each)

| id | symbol / day | cutoff (ET) | expected | what it tests | gold |
|---|---|---|---|---|---|
| r01 | ON 2026-10-02 | 08:34 | catalyst YES (m_and_a: revised Synaptics deal, published 10/1 4:34pm ET, in window); dilution NO (all-cash removes the planned ON share issuance) | positive case; the stored run labeled that cash switch `offering_dilution`, so this also checks the kind label | ghost-record |
| r02 | ABAT 2026-10-01 | 08:38 | catalyst YES (Commerce/BIS export license, up to $100M recycled black mass); dilution NO ($250M shelf is outside window) | positive case; reviewer said "authorization, not a completed sale" | ghost-record |
| r03 | SDEV 2026-10-01 | 08:33 | catalyst NO (only stablecoin sector commentary and price-action articles) | negative; sector news must not count | ghost-record |
| r04 | SDEV 2026-10-02 | 08:34 | catalyst NO (9/29 "no new developments" 8-K is before the window); entity trap: one article calls it "Security Devices International" | negative + entity trap + stale trap | ghost-record |
| r05 | LIFE 2026-10-02 | 09:14 | catalyst NO; entity trap: LIFE was aTyr Pharma's ticker, now Ethos Technologies; undated analyst targets must not be used | negative + entity trap | ghost-record |
| r06 | FPS 2026-10-02 | 09:24 | catalyst NO (9/15 earnings and 9/24 coverage start are outside the window); dilution NO | negative; many dated but stale events | ghost-record |
| r07 | SSM 2026-10-02 | 09:09 | catalyst NO: Alpine Fox 13D was published 10/1 1:25pm ET, before the prior close, so stale; arithmetic in the claim is off by ~$130; entity trap: SSM.AX (Service Stream) | stale + entity + bad-arithmetic trap | ghost-record |

## B. Cases from the 2026-10-01 miss review (price truth known, research label NOT yet verified)

| id | symbol | what the miss review shows | expected (proposed) | gold |
|---|---|---|---|---|
| m01 | VEEA | +88% day; stored catalyst "Agreement With TROLLEE Holdings For Phased Deployment Of VeeaONE Solutions Across 1,000 Unattended Stores" | catalyst YES (contract) | miss-review; need the publish time to set the cutoff |
| m02 | MEDS | +37%; Ghost's classifier said "no dated company-specific catalyst" but the release came ~06:00 ET (Corexa) | catalyst YES | miss-review; I need to re-open the release to confirm time and wording |
| m03 | XRPN | +46%; premarket shareholder vote | to-source: what kind of event, and is it "specific"? | to-source |
| m04 | HOOD | product news ("earnings contracts") that is not an earnings report | catalyst must NOT be labeled `earnings` | to-source (date needed) |
| m05 | RZAI | +62%, Ghost classifier: no dated catalyst | unknown | to-source |
| m06 | AISP | +54%, Ghost classifier: no dated catalyst | unknown | to-source |
| m07 | BENF | +31%, Ghost classifier: no dated catalyst | unknown | to-source |

## C. Gaps to fill before this is a real set

- **Dilution-positive cases: zero so far.** Every stored run says dilution NO, so "always answer no" would score
  100% on dilution. I would source 4-6 historical cases (offering / ATM / reverse split announced in the window)
  from SEC EDGAR, point-in-time.
- **Total today: 7 usable candidates + 7 unverified = 14.** The checklist wants 15-100. Target for the first
  run: ~24 (adds the dilution cases and the verified m-cases), run twice.
- **Hindsight leakage.** Web search now returns pages published after each cutoff. Grading therefore rejects any
  citation whose `published_at` is after the cutoff or more than 24h before it, and counts those as failures
  of the point-in-time rule, not as correct finds.

## D. Grading (all programmatic, no LLM judge)

1. JSON valid and in the expected shape (a harness error is NOT a zero; it goes to errors.jsonl).
2. `catalyst`: any non-quarantined specific-kind claim after the real review = YES, else NO; compared to gold.
3. `dilution`: reviewer flag or dilution-kind claim vs gold (unknown counts as its own outcome, not "no").
4. Point-in-time: every cited `published_at` inside [cutoff-24h, cutoff].
5. Trap checks: no claim whose citation URL/quote names the entity trap; stale item not reported as fresh.
6. Reported as a confusion matrix (precision/recall on catalyst-YES, specificity on catalyst-NO, dilution
   recall), plus cost, searches used, and not_researched rate.

## E. Cost (measured from Ghost's own production records, not from a pilot)

7 stored runs: $0.29 / 0.29 / 0.35 / 0.38 / 0.46 / 0.49 / 0.54, mean $0.42.
24 cases x 2 reps x $0.42 = about $20 (range about $14-$26). A pilot of 3 cases (~$1.3) would replace this
with a measured number before the full run.
