# Scheduled agents (routines)

Each routine is a scheduled wake-up of the operator's Claude Code session, which holds the
Ghost and Railway connectors and this repo. The wake-up message names a playbook here;
the session follows it. Playbooks are versioned like code: change them by PR.

| Routine | When (CT) | Playbook | Writes code? |
|---|---|---|---|
| Miss investigator | 7:20am weekdays | `miss_investigator.md` | no |
| Premarket briefer | 8:12am weekdays | `briefer.md` | no |
| Ops watchdog | hourly 8:20am-3:20pm weekdays | `watchdog.md` | small, tested fixes only |
| Evening reporter | 3:40pm weekdays | `reporter.md` | no |
| Research scientist | Saturday 9:00am | `scientist.md` | no (proposals only) |

## Where the data comes from
1. **Ghost connector** (`ghost_edge_report`, `ghost_edge_note`) when the session's tool list
   has them (search tools for `ghost_edge`).
2. Otherwise **Railway logs** (project `f910dbba-dc10-4a8b-b654-28001e64f4ec`, service
   `98593080-065d-43ef-840c-4a3d36a1b572`, env `a036418b-07aa-424d-8b66-8b3bc5be9a11`):
   Ghost logs each stage's views once a day as `EDGE_VIEW <view> <day> <json>` --
   morning `today`/`paper`/`research` (~8:05-8:28 CT), `misses` (~6:00-8:05 CT),
   evening `radar`/`paper`/`experiments`/`scorecard` (~3:20 CT). Also `EDGE_SHADOW`
   (every noteworthy tick), `EDGE_PROBE_CAP` (daily data probe), `EDGE_BACKTEST`.
   Filter the logs by those prefixes; never dump raw logs into the reply.

Delegate raw log reading to a subagent and keep only its summary, so the session stays small.

## Rules every routine follows
- Research, shadow and paper only. Never place, change or recommend real-money orders.
- Never print, copy or log secret values. Railway variables are read by NAME only.
- Never disable TLS verification or unset HTTPS_PROXY.
- Never change a frozen experiment's spec, thresholds, gates or promotion criteria.
  A different rule is a new version, proposed, not edited in.
- Small samples are called small; every win rate comes with its interval.
- Times: Central first (market opens 8:30am CT), Eastern in brackets.
- Finish with a phone-sized message. Send it with PushNotification only when the playbook
  says so.
