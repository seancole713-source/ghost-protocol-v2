# gap_and_go_assist@v1 and gap_and_go_orb@v1 (paper, from 2026-10-12)

Operator decision 2026-10-09. Two separately named experiments, each ONE change away from
`gap_and_go_sipref@v1`, so each record answers one question on the same days. Paper and shadow
only. Neither touches the operator's frozen Gap-and-Go v1 rule or any other experiment's record.

## gap_and_go_assist@v1 -- does Claude's morning research add edge over Ghost's keyword check?
* Same candidates, reference price (IEX, else the 15-min-delayed SIP bar), levels, sizing and
  ranking as `gap_and_go_sipref@v1`.
* The one change: rule E4's catalyst passes on the keyword classifier **or** a research-agent
  claim (`ghost_edge_catalyst` MCP tool, `edge/agent_catalysts.py`): a company-specific kind, an
  https source link and a publish time within 24h. A claim of kind `offering_dilution` fails E5.
* Point-in-time: the server stamps `first_seen_at` when the claim arrives; a claim counts only if
  received at or before the card's data cutoff (the 09:05-09:28 ET tick). Claims are append-only
  and read by no other experiment, gate or the operator's rule.
* v1 does not add the agent's symbols to the candidate list (that would change every other
  experiment's inputs); it can only supply the catalyst for names Ghost's own scan found.

## gap_and_go_orb@v1 -- does waiting for the opening-range break beat buying the premarket gap?
* Names: the 09:05 card's `gap_and_go_sipref`-eligible rows, top 2 by average dollar volume.
* Nothing is placed before the open. At the first edge tick in 09:45:00-09:47:00 ET (the tick runs
  on the clock at :x0:05/:x5:05) the buy-stop goes 0.1% above the 09:30-09:45 ET opening-range high
  from IEX 1-minute bars (>= 7 of 15 bars), limit 1.1% above it; target +5% and stop -3% from the
  trigger; the entry expires 44 minutes after the window opens (10:30:05 ET); flat 15:30 ET.
* A missed issue window or a missing range is an abstention, never a later issue.
* The range is IEX-only (one exchange) on the free plan: SIP bars are 15 minutes late.

Both are registered with pinned spec hashes (`FROZEN_HASHES`); any change is a new version.
