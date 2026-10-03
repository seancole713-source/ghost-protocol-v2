# Pilot regrade note (results.jsonl left as recorded)

- r01 (ON) `pit_ok = 0` was a grader false positive. The SEC exhibit's `published_at` was the date-only
  "2026-10-01", which the old grader read as midnight (outside 10/1 08:34 - 10/2 08:34 ET). Fixed grader:
  a date-only stamp is judged as the whole ET day. r01's dated citations were 2026-10-01, 10/1 16:54 ET and
  10/1 17:22 ET, all inside the window, so r01 `pit_ok = 1`. Corrected pilot pit_ok: 3/3.
- r07 (SSM) `unknown` is the designed rule, not a code fault: the Claude reviewer left at least one of
  entity / dilution / staleness unanswered, and an unanswered check is unknown, never clean
  (edge/research.py review_gaps). Production used the OpenAI reviewer for SSM on 10/2 and it answered all three.
- Measured pilot cost: $1.93 for 3 cases (r03 $0.28, r07 $0.72, r01 $0.93), versus $1.30 estimated.
