# Premarket briefer -- 8:12am CT (9:12 ET), weekdays

The shadow card is issued 8:05-8:28am CT and PAPER orders are placed right after it.

1. Read today's `today`, `paper` and `research` views (connector, else `EDGE_VIEW` logs since 7:55am CT).
   - Not a trading day (holiday): reply "market closed today", no push, stop.
   - Trading day but no card: check `EDGE_SHADOW` and error logs, report it as a
     PROBLEM, push.
2. For each forecast: symbol, experiment, entry trigger / limit, target, stop, shares,
   paper order state (flag rejections with the broker's message).
3. For each card name: what the research found that survived review, what was quarantined
   and why, which reviewer checked it. Research is off or empty: say so.
4. Context: the latest miss investigation, any open watchdog problem.
5. If `ghost_edge_note` is available: one note, kind=brief, author=premarket-briefer.
6. Reply (and push a one-line summary): the names with levels, the health banner, one line
   of research each, any problem. If the operator trades the Gap-and-Go card themself:
   "cancel any UNFILLED entry by 9:30am CT (10:30 ET)" and "SELL anything still open by
   2:30pm CT (3:30 ET)". Always: shadow/paper research, not a trade recommendation.
   An empty card is a valid answer: "no qualifying setup today".
