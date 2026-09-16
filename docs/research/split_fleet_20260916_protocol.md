# Paired data repair evaluation: split-adjusted history

Declared after detecting raw split jumps during the still-running first fleet
experiment. The first experiment remains unchanged at commit e1cdd0e, using
engine source 8fe00d7. Its complete results, including failures and exclusions,
must remain in the final report. This second run evaluates a data repair, not
a model-parameter search. Both runs are exploratory historical evidence.

- Same 107 symbols, UP/DOWN, dates, IEX feed, peer pool and fixed model settings.
- Only the verified split-adjusted input repair and its required new feature/
  contract identity change. Source code hashes are frozen and enforced.
- Re-fetch bars with explicit `adjustment=split`. Hash the completed snapshot
  before any second-run fitting. No raw/split mixing or fallback feeds.
- The same production admission gates and isolated 70% proof target apply.
  Production's staged 55% configuration remains unchanged.
- Apply the Bonferroni screen across 428 total attempted hypotheses (214 per
  run), not just the second run. This screen does not establish independence
  or erase earlier research/model-selection bias.
- Missing latest sessions are excluded; all attempted hypotheses are reported.
  Large remaining jumps require corporate-action investigation, not automatic
  removal. Any hypothetical candidate also requires peer/proxy data review.
- No registration, promotion, stored production model changes, trading, or
  proven-accuracy claim. No second-run threshold/parameter tuning or reruns
  after seeing results. Raw vendor bars stay local and are not redistributed.

The existing daily five-bar model remains a daily model. A repaired historical
fit does not establish an intraday strategy or promise today's accuracy.
