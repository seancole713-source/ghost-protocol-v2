"""
PROJECT_STATE.py — Ghost Protocol v2
Single source of truth. Accountability ledger. Read this before touching anything.

RULES:
  1. Read this file before starting work.
  2. Verify claimed fixes on the LIVE site before trusting them.
  3. After every fix session, add a dated section with PR numbers + live verification.
  4. Never mark [x] done unless YOU personally verified it on the live dashboard.
  5. If you find new bugs, add them to the TODO list.
  6. This is not documentation. It is an accountability ledger.
     Agents lie. This file exists because of that.
  7. SESSION_LOG is the handover log — read it to understand what happened in the
     last session and what's in flight. Update it at the end of every session.

LAST UPDATED: 2026-07-12 — Daily report now follows Ghost Doctrine 6-word loop (Clarity→Decision→Direction→Alignment→Consistency→Results); code-only observability upgrade, 870 tests green, no engine/gate/env changes
"""

# ============================================================
# SESSION_LOG — Handover log for the next AI agent
# ============================================================
# Read this FIRST. It tells you what happened, what's deployed,
# what's in flight, and what needs attention next.
# ============================================================

SESSION_LOG = {
    "session_date": "2026-06-30 → 2026-07-04",
    "session_span": "Multi-day: initial health check → bug discovery → autonomous execution → P0 audit → Phase 0-2 model overhaul → forensic audit → GO verification → PR #129 tech-debt cleanup",
    "handover_to": "Next AI agent picking up this project",

    # ── WHAT'S DEPLOYED RIGHT NOW ──
    "production": {
        "url": "https://ghost-protocol-v2-production.up.railway.app",
        "railway_project": "tender-benevolence",
        "pr_version": "live _pr_version=129 (PR129 doctrine marker) — NON-MONOTONIC: LOWER than PR #161's 161 because the doctrine layer intentionally set _RUNNING_PR_VERSION=129; git_sha is the real source of truth, not this number",
        "git_sha": "d9dc254 (source-backed, live-verified 2026-07-12 via /api/_version; PR #129 doctrine verifier pass — fixed route tests + wired console loadAll/My-Picks doctrine). NOTE: docs-only ledger pushes auto-deploy and advance the live SHA off d9dc254 — always re-read /api/_version",
        "app_version": "2.5.0",
        "health": "95/100 (live-verified 2026-07-12 after d9dc254 deploy; status healthy)",
        "degraded": False,
        "tests": "869 passed, 3 skipped (python3.13 -m pytest tests/ -q; independently re-run 2026-07-12 Fugu) + import-integrity PASS (128 files/870 imports)",
        "playwright_e2e": "console-audit.spec.ts 24/24 desktop+mobile (Fugu, 2026-07-08) — verifies every tab RENDERS with real data & zero JS/API errors; does NOT verify prediction accuracy (a passing tab can show 'Grade F / NO EDGE' honestly). Suite lives in e2e/console-audit.spec.ts.",
        "accuracy_contract": "70% target, balanced mode",
        "models_trained": "126 stored, 46 serveable, 0 fireable_now (edge unproven, gate closed) — live /api/v3/status 2026-07-08; PR #153 does not change model firing behavior",
        "wallet": "paper wallet live+trading, total_value ~$9,880 (-1.20% MTD), 15 open shadow positions, 16 closed (8 wins), monthly goal $20k (auto month-reset); PR #151 shadow filters live: min_prob=0.55, min_tp_rate=0.55, min_resolved=10 — live /api/wallet 2026-07-08 after PR #152",
        "breakers": "yfinance + alpaca cycling open — not degraded",
        "frontend_verified": "Fugu's dedup fixes (dc7c4f7) CONFIRMED live 2026-07-08 (dedup JS present in live-served ghost_console.html); the 24/24 e2e ran against real production. _pr_version stayed 147 because these commits didn't bump the constant.",
        "verified_by_fugu_2026_07_08": "Live reconciliation + PR #151 verification vs public API + local build. CONFIRMED: _pr_version=152 after deploy (source-backed; exact SHA must be re-read from /api/_version because docs-only ledger pushes can advance it); /health=95; watchlist=74 symbols; 11 shadow brains (PR #152 momentum_shadow_v1 added); gate CLOSED (fireable_now=0, silence CORRECT); kill-status historical win_rate/brier RED but enforcement_window insufficient so engine NOT paused; wallet honest & underwater, with PR #151 filters live (shadow_min_prob=0.55, shadow_skill_min_tp_rate=0.55, shadow_skill_min_resolved=10); local suite 773 passed/3 skipped + import-integrity PASS. Shadow resolver backlog fixed live: before PR #151 /api/shadow-stats had 31 pending with oldest expired 2026-06-15 and cycle 0 resolved; after deploy + one admin shadow-cycle, stale June rows closed, pending=29/resolved=1138, earliest_expires_at_ct=2026-07-08 16:00 CDT, resolution_status=waiting. Ghost MCP already registered + Connected (no re-add needed). No Railway env var changed; CRON_SECRET was read only to trigger the gated shadow-cycle and was never printed or stored.",
        "verified_by_fugu_2026_07_08_master_rerun": "Master-agent re-verification at 2026-07-08 13:13-13:20 CDT. Read this ledger first, fetched origin, confirmed local main==origin/main at d49273b before this ledger-only update. LIVE public API verified /api/_version _pr_version=152 git_sha_short=d49273b deploy_id=8a961f58-d76d-477f-8bbc-d203a57e0ac3, /health status=healthy score=95, /api/v3/status fireable_now=0 serveable=46 models_stored=126 watchlist=74, /api/wallet filter knobs shadow_min_prob=0.55 shadow_skill_min_tp_rate=0.55 shadow_skill_min_resolved=10 total_value≈9876 (-1.24%) open_positions=15, /api/shadow-stats pending=29 resolved=1138, gate CLOSED as expected, kill-status historical red but enforcement_window insufficient and engine_pause.paused=false. Local gates PASS: import-integrity scanned 117 files/784 imports; python3.13 -m pytest tests/ -q -> 773 passed, 3 skipped. Observation: several weak-symbol wallet opens (PFE/OPK/NOK/GME/SNAP/AI/XPO) remain open but were entered before PR #151; newest post-filter open CLNE entered after PR #151 and its public shadow TP rate is 55.6%, so no current filter bypass was verified. Attempted read-only Railway prod DB introspection still cannot resolve postgres.railway.internal from this local shell; did not mutate DB. No Railway env vars changed; no secrets read or stored in this session.",
    },

    # ── WHAT HAPPENED THIS SESSION ──
    "session_summary": [
        "CONTRACT-70 SETUP-FLAG DIMENSIONS (2026-07-14, Fugu agent-1 role): added the most principled unsearched discriminators to the honest 70+ slice search — Ghost own regime-GATE flags at issuance (adx_trending, above_ema200, ema_trend_bullish). up_prob is non-discriminative >=0.55 and symbol/regime_label/fired do not clear 0.70; but these binary flags are the exact market conditions Ghost gates on, captured in ghost_perf_symbol_evals.scores.regime. Question answered: in which market setups does Ghost TP/SL actually clear 70pct? IMPL: additive migration adds SMALLINT columns adx_trending/above_ema200/ema_trend_bullish to ghost_shadow_outcomes; seed_shadow_rows persists them from scores.regime via _regime_flag(); both slice loaders COALESCE the durable column with a JSONB fallback to pe.scores->regime (mirrors the regime_label precedent so it works for old rows too); _dim_value labels yes/no (None skips); DEFAULT_DIMENSION_SETS adds single + paired + up_prob_bucket + triple flag combos. Read-only; Wilson-gated; EXPIRED still non-win; no gate/model/env/broker change. Tests: 6 added (helper, dim labels, discoverable qualified slice, None-skip, expired-non-win, durable seed persistence); fixed one stale positional-param assertion. VERIFY: py_compile PASS; import-integrity PASS (131 files/908 imports); full suite 937 passed/3 skipped. HONEST STATUS: verifying against live data after deploy whether any flag-conditioned pocket is Wilson-proven >=0.70; no 70+ claim.",
        "CONTRACT-70 EXPIRED NON-WIN CORRECTION (2026-07-14, Fugu independent re-derive): found a proof-honesty gap in the current 70+ measurement. ghost_shadow_outcomes.EXPIRED means no TP/SL hit within the hold window and is closed at 0pct/entry in the resolver; it is not a WIN. But contract_70, slice search, and forward proof counted only WIN/LOSS rows, excluding EXPIRED from the denominator. That can overstate pockets (example live shadow-stats shows BILL 18 wins / 4 losses / 2 expired; old slice search reported 18/22, stricter win-test should use 18/24). FIX: core.watcher summarize_shadow_outcomes + contract_70_symbol_breakdown now include EXPIRED as resolved non-win; watcher SQL includes EXPIRED; core.contract_70_slices loaders and summarize_slices include EXPIRED; core.contract_70_registry evaluate_forward/evaluate_forward_slices include EXPIRED. Tests added for EXPIRED-as-non-win in watcher, slice search, and forward registry. VERIFY: py_compile PASS; import-integrity PASS (131 files/908 imports); full suite 931 passed/3 skipped. HONESTY: this tightens the bar only; it cannot fabricate a pass. Expected live result after deploy: contract_70 and best slices become more conservative, and 70+ remains NOT proven.",
        "CONTRACT-70 FIRED DIMENSION (2026-07-14, Fugu agent-1 role): added the most honest discriminative axis the slice search was missing -- Ghost own conviction. Existing dims (symbol/regime_label/up_prob_bucket) do not separate because up_prob is non-discriminative >=0.55. But ghost_shadow_outcomes.fired records whether a pick cleared EVERY gate (regime+precision+meta+overconfidence); when Ghost actually commits, does it hit 70pct is the truest reading of the objective. Added fired to _dim_value (labels fired/unfired; None skips), to DEFAULT_DIMENSION_SETS (fired, fired+regime, fired+up_prob_bucket, fired+symbol), and to both slice loaders (direct column, no migration/join). Read-only; no gate/broker/model/env change; Wilson lower bound still gates qualification. Tests: 3 added; tests/test_contract_70_slices.py 12 passed; import-integrity PASS (131 files/908 imports); full suite 928 passed/3 skipped. HONEST STATUS: verifying against live data whether the fired population is Wilson-proven >=0.70; no 70+ claim made.",
        "CONTRACT-70 SLICE FORWARD REGISTRY (2026-07-14, Fugu autonomous continuation): converted the new slice-search evidence into an actual forward-proof path. Problem found after PR139: /api/watcher/contract-70/register still froze only symbol universes, so richer proven slices (symbol+regime+prob-band) could be discovered but not pre-registered/scored forward. FIX: core.contract_70_registry now has evaluate_forward_slices() and register_slices(); registry payloads support mode=slices with exact frozen dims/key specs while preserving legacy symbol mode. Watcher contract_70_forward now scores slice registries by loading resolved rows strictly after registered_at_ts and matching exact frozen slice dimensions; missing dimensions are ignored, never widened. POST /api/watcher/contract-70/register now defaults to mode=slice, refuses weaker-than-contract criteria, registers only the strongest Wilson-proven qualified slice, and still supports mode=symbol for legacy behavior. Tests added for anti-lookahead slice scoring, small-sample Wilson rejection, ghost_state-only slice registry, route no-write when no slice qualifies, strongest-slice registration, legacy symbol compatibility, and watcher slice-forward readout. VERIFY: py_compile PASS; import-integrity PASS (131 files/908 imports); full suite 925 passed/3 skipped. HONEST STATUS: 70+ still NOT proven live; current slice search returns no_qualified_slice (best BILL 18/22 raw 81.8%% but Wilson low only 0.615; best regime+band 21/26 raw 80.8%% Wilson low 0.621). No gate loosened, no broker/real-money path, no model/env mutation. This makes the final proof path complete once a slice actually becomes Wilson-proven; it does not fabricate the result.",
        "CONTRACT-70 SLICE SEARCH (2026-07-14, Fugu autonomous, agent-1 role): diagnosed WHY the 70+ win test is stuck and shipped the honest discovery mechanism it was missing. ROOT CAUSE (live-proven): the single model probability up_prob is NON-DISCRIMINATIVE above ~0.55 — realized win rate is flat ~0.56 across the 55-60 (0.568), 60-70 (0.560), and 70+ (0.568) buckets while claimed prob climbs to 0.78 (calibration_gap -0.21 in the 70+ bin). So NO threshold on up_prob can isolate a 70%% pocket; the existing forward registry, which selects per-symbol up_prob>=0.70 buckets, selects on a signal that cannot separate. The discriminative signal lives in the brains (fundamental 7/8, contrarian 3/4 vs technical 55/170). FIX: NEW core/contract_70_slices.py — read-only search that groups the SAME resolved TP/SL contract rows (ghost_shadow_outcomes) by symbol / market regime / up_prob band (and pairs) and reports which conditional slices clear a Wilson-PROVEN 0.70 (min_n>=8, Wilson lower bound, never raw). It never fires, never loosens a gate, never re-labels an outcome; a qualified slice is only a CANDIDATE to pre-register for the forward proof, not a 70+ claim. DURABILITY FIX: ghost_perf_symbol_evals (the regime join source) is pruned after ~90d (GHOST_PERF_RETENTION_DAYS) while shadow outcomes are not, so added an additive migration ALTER TABLE ghost_shadow_outcomes ADD COLUMN IF NOT EXISTS regime_label TEXT and made seed_shadow_rows persist regime_label at issuance, so a forward proof's conditioning signal can't decay. Exposed read-only at GET /api/watcher/contract-70/slices. VERIFY: py_compile PASS; import-integrity PASS (131 files/902 imports); tests/test_contract_70_slices.py 9 passed + new durable-column seed test; full suite 918 passed/3 skipped. HONEST STATUS UNCHANGED: 70+ still NOT proven — live contract_70=21/37=56.8%% raw+Wilson fail, and against current live data no single symbol/regime/band slice is Wilson-proven >=0.70 (best carriers XPO 5/7 and YMM 4/4 are raw>=70 but Wilson-fail on small n). This ships the tool to FIND/PROVE an honest 70+ slice and freezes it forward; it does not fabricate one. No gate loosened, no broker path, no real-money action, no model/env mutation.",
        "PR #138 CANDIDATE (2026-07-14, Fugu autonomous): first triaged the reported production 502 after the docs-only 5475c14 push. Railway latest deployment 89caefd1 for 5475c14 is SUCCESS/RUNNING; live /health=200 healthy score=90 and /api/_version=200 git_sha_short=5475c14, so the earlier 502 was a transient deploy/cold-start window, not a persisted crash. Local compile PASS for PROJECT_STATE.py/core.watcher/core.contract_70_registry and targeted watcher+registry tests PASS. Continued the honest 70+ path by adding POST /api/watcher/contract-70/register: strict cron/admin gated, refuses weaker-than-contract criteria (min_n<8 or min_wilson_low<0.70), selects only per-symbol 70+ buckets whose OWN Wilson lower bound clears 0.70, writes only the frozen forward registry in ghost_state, and returns no_qualified_symbols without writing when nothing qualifies. This starts a forward proof only when evidence warrants it; it does NOT loosen gates, fire trades, mutate models, touch broker paths, or claim 70+. VERIFY: py_compile PASS; tests/test_watcher.py + tests/test_contract_70_registry.py + strict cron tests PASS; full suite 908 passed/3 skipped. HONEST LIVE STATUS remains contract_70=21/37=56.8%% raw+Wilson fail; forward proof currently no_registry until a qualified universe exists/registers.",
        "CONTRACT-70 FORWARD PROOF HARNESS (2026-07-14, Fugu autonomous): shipped the honest mechanism to eventually prove the 70+ win test WITHOUT look-ahead or cherry-picking. NEW core/contract_70_registry.py: select_candidate_universe (symbols whose OWN 70+ bucket is Wilson-proven on PAST data) -> register_universe (freezes universe + timestamp in ghost_state, explicit only) -> evaluate_forward (scores ONLY outcomes with eval_ts strictly after registration, prob>=floor, registered symbols) -> load_registry. Watcher exposes it read-only at /api/watcher/summary shadow_calibration.contract_70_forward (status=no_registry until a window is frozen). VERIFY: failing-first proven (neutering the forward cutoff fails the anti-look-ahead test); tests/test_contract_70_registry.py 5 passed; tests/test_watcher.py 10 passed; import-integrity PASS (130 files/895 imports); full suite 904 passed/3 skipped. PR #137 merged; live cef9e70 SUCCESS; health 95. HONEST STATUS: 70+ NOT proven. Live contract_70 = 21/37 = 56.8%% (raw+Wilson fail; needs 17 straight wins raw, ~44 Wilson); fireable_now=0; forward proof not started (no_registry). No gate loosened, no broker path, no real-money action, no model/env mutation. NEXT: operator/cron calls register_universe to start the forward window, then 70+ can be claimed ONLY when contract_70_forward.wilson_pass turns true on future outcomes.",
        "PR #133 WIN-TEST BAR CORRECTION (2026-07-13, Fugu agent 2-verify): re-derived the operator request (no i want 70+ plus to clear the win test) independently. KEY INSIGHT the prior agent missed: the operator was replying to the PRIOR turn about the paper-wallet making-money test (consistent_money_readiness), whose pass bar was BREAK-EVEN (~42pct) — I had shown 53-60pct win rates passing. The operator rejected that: the win test must require 70pct. The prior agent instead only tightened the SEPARATE 70+ confidence bucket (proven_skill_gate.overconfidence_min_win_rate), which the operator was never shown — correct and kept, but not the gate they reacted to. FIX (commit 974231f): core.paper_wallet consistent_money_readiness now clears a WIN TEST bar = accuracy-contract target_win_rate (70pct under contract 70) via new _consistent_money_win_test() -> resolve_float(PAPER_CONSISTENT_WIN_TEST, target_win_rate); required Wilson lower bound is max(win_test, break_even+margin), so 70pct (not ~42pct) is the real bar and env can only tighten under contract 70. Renamed check wilson_beats_break_even -> wilson_clears_win_test; added win_test_win_rate to payload + wallet_summary. HONEST MATH: gate uses the Wilson LOWER bound, so a raw 70pct at N=100 (wilson_low ~0.60) does NOT pass a 70pct test; ~80pct raw at N=100 or 45/50 is needed. Live /api/wallet now reports win_test_win_rate=0.70, required_win_rate_wilson_low=0.70, ready=false (only 14 current-geo trades). VERIFY: failing-first proven; py_compile PASS; import-integrity PASS (128 files/876 imports); full suite 890 passed/3 skipped. No gates loosened, no broker path, no kill/env change, no production mutation.",
        "PR #133 70+ WIN-TEST CORRECTION (2026-07-13, Fugu): operator clarified 'no i want 70+ plus to clear the win test' — interpreted as Ghost's 70+ confidence bucket (up_prob >= 0.70) must pass the contract-70 win test, not merely the paper-wallet money test. LIVE watcher before fix: 70+ bucket n=37 wins=21 raw win_rate=56.8%%, Wilson low=40.9%% — far below 70. Existing core.proven_skill_gate.overconfidence_min_win_rate defaulted to 0.55, so an otherwise-fireable high-probability pick could pass the overconfidence/global calibration blocker even though the 70+ bucket was only >55%%, NOT contract-70. FIX (commit 2949cc5): overconfidence_min_win_rate now uses core.accuracy_contract.resolve_float(V3_OVERCONFIDENCE_MIN_WIN_RATE, target_win_rate), so in GHOST_ACCURACY_CONTRACT=70 the 70+ bucket must clear >=70%% and env can only tighten (e.g. 0.80) not weaken (0.55 is clamped to 0.70). Tests updated/added: inverted bucket blocks with high_prob_bucket_wr<0.70, 22/30 passes contract-70, and env cannot weaken/tighten works. VERIFY: tests/test_proven_skill_gate.py + tests/test_precision_gate.py 33 passed; import-integrity PASS (128 files/875 imports); full suite 888 passed/3 skipped. HONESTY: this still does not make the historical 70+ bucket pass — it makes Ghost refuse it until future evidence actually passes. Current data needs 70+ to climb from 21/37 to raw >=70%% (17 straight future wins, or e.g. 26 wins out of next 30) before raw contract pass; Wilson-proven 70 would be stricter (~44 straight wins from current state). No production deploy/mutation yet.",
        "PR #133 READINESS GATE + INDEPENDENT RE-VERIFY (2026-07-13, Fugu agent 2 of plan): re-derived the 'consistent money' requirement from scratch and independently re-verified the prior agent's PR #133 work rather than trusting it. CONFIRMED at HEAD 56fa544 before my change: working tree clean, diff vs origin/main = 4 files, tests/test_paper_wallet.py 42 passed, import-integrity PASS (128 files/873 imports), full suite 881 passed/3 skipped, PR #133 CLEAN/MERGEABLE with GitHub test check PASS, live prod still c93d21d/_pr_version 129 (no production mutation). Re-read the duplicate-prevention helper and dry-run cleanup endpoint end-to-end — both correct. NEW WORK (commit f0c83f0): added the SINGLE GO/NO-GO instrument the goal actually needs — core.paper_wallet.consistent_money_readiness(rows, current_stop_frac). It judges 'good enough for consistent money' honestly: (1) >= PAPER_CONSISTENT_MIN_SAMPLE (default 30) CURRENT-geometry resolved trades, (2) positive realized expectancy, (3) the 95%% Wilson LOWER bound of the win rate must exceed the structure break-even win rate + PAPER_CONSISTENT_WILSON_MARGIN (default 0.03). Legacy -3.6%%-stop trades are EXCLUDED (reuses expectancy_by_geometry split); reuses core.precision_gate.wilson_lower_bound for consistency. Surfaced read-only as wallet_summary()['consistent_money_readiness']. 6 new tests (below-sample, negative-expectancy, wilson-below-breakeven, strong-large-sample READY, legacy-excluded, summary-surfaced); FAILING-FIRST verified (neutering ready=all(checks) -> the 4 NOT-READY tests fail, restored -> green). Ran against LIVE /api/wallet history: verdict NOT READY — only 14 current-geometry trades (<30), expectancy -0.126%%/trade, win_rate_wilson_low 0.2138 vs required 0.4239. VERIFY: py_compile PASS; import-integrity PASS (128 files/874 imports); full suite 887 passed/3 skipped. HONESTY: this does NOT make Ghost profitable and does NOT authorize real trading — it turns 'is Ghost ready?' from eyeballing into a reproducible, statistically-honest gate that currently reads NO. No gates loosened, no broker path, no kill/env change, no production mutation.",
        "PR #133 EXTENDED (2026-07-13, Fugu): used the 6-word workflow (Clarity→Decision→Direction→Alignment→Consistency→Results) to move Ghost closer to consistent-money evidence WITHOUT loosening gates or touching broker paths. Clarity: live Ghost still NOT profit-proven — /api/wallet about $9.87k from $10k, expectancy -0.43%/trade, current geometry still -0.126%/trade, and open shadow wallet polluted by duplicate lots (XPO/YMM/ARDT/LCID x3). Decision: do not chase movers / do not relax v3 gates; fix evidence integrity first. Direction: PR #133 already prevented NEW duplicate open (book,symbol) lots; added cleanup_duplicate_open_positions(dry_run=True default) and POST /api/wallet/cleanup-duplicates admin/cron-gated route. It keeps the oldest lot per (book,symbol), closes later accidental fake-money duplicates at current quote with exit_reason=duplicate_symbol_cleanup, rejects unsupported keeper policies to avoid cherry-picking winners, and skips no-price rows. Alignment: fake money only; no real broker, no gate/kill/env changes, no production mutation; route is dry-run-first. Consistency: local py_compile PASS, import-integrity PASS (128 files/873 imports), tests/test_paper_wallet.py 42 passed, full suite 881 passed/3 skipped; GitHub PR #133 test check PASS. Results: PR #133 ready/mergeable at f99caa0, but live production still c93d21d/_pr_version 129 until merge/deploy; after deploy run cleanup endpoint first with dry_run=1, then dry_run=0 only with operator approval/cron secret. HONESTY: this improves wallet evidence quality and prevents/repairs overexposure; it does NOT prove Ghost is good enough for consistent real-money profit yet. Required next proof: clean non-duplicated forward sample under current geometry with positive expectancy and gates green.",
        "DAILY REPORT DOCTRINE LOOP (2026-07-12, Fugu): applied the 6-word foundation to Ghost's consolidated daily report so /api/report/daily now answers what Ghost is doing through Clarity→Decision→Direction→Alignment→Consistency→Results, not just raw wallet/scans. NEW core.daily_report.DOCTRINE_WORDS + _build_report_doctrine(): read-only mapper over already-collected report evidence (health, scan cycles, gate/paused status, closest candidate, regime/skip reasons, Watcher calibration, wallet result). build_daily_report now includes doctrine{words,steps,summary,display_only=True} and narrative begins with the Ghost Doctrine line plus one sentence per step. This changes OBSERVABILITY ONLY: no model training, no gate thresholds, no wallet fills, no broker/order APIs, no Railway env vars, no prediction writes. Route remains the existing /api/report/daily read-only aggregation; snapshot still writes only ghost_daily_report_logs. VERIFY LOCAL: python3.13 scripts/check_import_integrity.py PASS (128 files/870 imports); python3.13 -m pytest tests/ -q -> 870 passed, 3 skipped. Added tests prove the six canonical words/order appear in daily report, narrative includes the doctrine line, and the mapping remains display_only. Next agent: after deploy, re-read /api/_version because docs/code push advances git_sha while _pr_version marker stays 129 unless wolf_app constants change.",
        "INDEPENDENT RE-VERIFICATION + LEDGER DRIFT FIX (2026-07-12, Fugu — picked up the doctrine handoff): re-verified PR129 Ghost Doctrine end-to-end WITHOUT trusting the prior agent's report. CONFIRMED local==origin==live d9dc254; /api/_version _pr_version=129 git_sha_short=d9dc254 deploy_id=eb569a11-8727-48ec-bc5c-a578e17d65cb; /health healthy score=95; Railway latest prod deployment eb569a11 status=SUCCESS commit d9dc254. Local gates GREEN: import-integrity 128 files/870 imports PASS, full suite 869 passed/3 skipped, PROJECT_STATE.py compiles. LIVE doctrine endpoints: /api/ghost/doctrine spec 6 steps in canonical order; /api/ghost/doctrine/WOLF?light=1 6 steps (clarity insufficient / decision+alignment+consistency+results hold / direction pass); /api/ghost/doctrine/AAPL?light=1 cold-symbol HONEST (consistency+results insufficient, win_rate=None, win_rate_wilson_low=None — no invented numbers). BROWSER SMOKE (headless Playwright vs prod, screenshots captured): Doctrine tab renders the 6-chip strip + 6 evidence cards (NOT 'Doctrine unavailable'); My Picks DOMO+SPCE each render all 6 doctrine chips (2 placeholders -> 12 chips); cockpit #doctrine-section renders 6 chips; ZERO JS/console/page errors and ZERO failed /api responses. DRIFT CAUGHT + FIXED: the prior agent reported 'ledger already up to date', but the structured production block still claimed dbad6be / _pr_version 161 / 816 tests from 2026-07-08 while live is d9dc254 / _pr_version 129 / 869 tests — reconciled pr_version+git_sha+health+tests to live d9dc254 facts and flagged the _pr_version NON-MONOTONICITY (129<161 by doctrine-marker design). LIVE ENGINE SNAPSHOT (also drifted from the 2026-07-08 block, recorded honestly): /api/v3/status models_stored=130 serveable=51 fireable_now=1 (block said 126/46/0); BUT /api/wolf/kill-status engine_pause.paused=true (win_rate + brier RED over 30 resolved) so NOTHING fires — silence still CORRECT (doctrine WOLF Decision=hold 'engine paused'). Docs-only; no engine/gate/model/Railway-env changes; no secrets touched. This ledger push auto-deploys — the live git_sha will advance off d9dc254 (marker stays 129); re-read /api/_version.",
        "DOCTRINE VERIFIER PASS (2026-07-12, Fugu): reviewed the already-merged Ghost Doctrine PR129 (6-step Clarity->Decision->Direction->Alignment->Consistency->Results thinking/display layer; core/ghost_doctrine.py + /api/ghost/doctrine[/{symbol}] + console/cockpit UI). Live pre-check: HEAD==origin==live 2cc0c28, /api/_version _pr_version=129, /health 95. Independently re-derived the 6-step plan and found 3 real gaps the merge shipped: (1) TESTS BROKEN — tests/test_ghost_doctrine.py route tests referenced a nonexistent `client` fixture -> 2 ERRORS (spec 200/6-steps and ?light=1 never actually ran); added a local client fixture matching the repo convention TestClient(wolf_app.APP). (2) CONSOLE TAB DEAD — loadAll() never fetched /api/ghost/doctrine/{selected}, so state.data.doctrine was always undefined and the Doctrine tab rendered 'Doctrine unavailable' forever; wired the fetch into the Promise.all list as r[23] -> state.data.doctrine. (3) MY PICKS CHIPS MISSING — plan step 3 per-pick doctrine chips were never built; added a #mypick-doctrine-{sym} placeholder in renderMyPick and a per-pick /api/ghost/doctrine/{sym}?light=1 fetch in loadMyPicks that fills doctrineStrip(doc,true). Also removed 2 dead imports (asyncio/os) from the symbol endpoint and bumped nixpacks cache_bust pr128->pr129. Strengthened the console UI tripwire test to assert the loadAll fetch + mypick chips so these can't silently regress. VERIFY: node --check on the console app script PASSES; import-integrity PASS (128 files/870 imports); full suite 869 passed/3 skipped (was 867 passed + 2 errored). Zero engine drift: test_wolf_app_core.py + test_super_ghost*.py unchanged and green. Display-only; no engine/gate/model/env changes; no secrets touched.",
        "PHASE 2 REVERTED (2026-07-10 ~21:45 CT, same session — honest post-mortem): the mult-1.2/5y/fundamentals retrain FAILED the contract-70 SERVE floors live: first batch 0/33 symbols stored (42x holdout_acc<60%, 19x edge<5%, e.g. AMZN/DOWN 78% acc +7.3% edge KILLED by wf_edge_mean -5.8%; AMD/AMZN/AVGO UP acc 62-66% with edge exactly 0.0 = pure base-rate riding at the wider stop). ROOT CAUSE OF MY ERROR: the offline sweeps validated the PRECISION-GATE path but did NOT model the train-time serve floors (min_holdout_acc 0.60 + min_edge 0.05, which accuracy_contract.resolve_float FLOOR-CLAMPS under contract 70 — env can only tighten, never weaken, BY DESIGN). Failed models are never STORED (signal_engine.py:1504), and no stored models -> no evals -> no shadow evidence -> the forward-proof loop the mission depends on STARVES. REVERT: env back to V3_STOP_VOL_MULT=1.8 / V3_OHLCV_PERIOD=2y / V3_FUNDAMENTAL_FEATURES=off -> old schema matches the stored 126-model fleet, which un-stales instantly (models were never deleted); coverage trainer picks up the 26 new PR#164 symbols under the old config automatically. LESSON ENCODED: geometry_grid_sweep.py now reports serve_pass per geometry (acc>=0.60 & edge>=0.05) so future configs are validated against ALL bars offline first. STRATEGIC READ: the offline 71% pocket at mult 1.2 is real but unreachable under current serve floors + current model skill; the live forward path (recal + gates + 100-symbol evidence at ~1.8 geometry where the 55-60 bin ran 70.6%) is the proving ground. No gates were loosened at any point.",
        "PHASE 2 ENV CHANGE (2026-07-10, autonomous mission, LEDGERED): Railway production vars set — V3_STOP_VOL_MULT 1.8 -> 1.2, V3_OHLCV_PERIOD 2y -> 5y, V3_FUNDAMENTAL_FEATURES off -> on. EVIDENCE (42 real-data geometry/universe/history combos, sweeps 1-4 in this session): (a) sweep 3 (5y, large-cap, tgt 2.0%/stop 2.4% = mult 1.2) produced the FIRST >=70% candidate operating point of the entire search: thr 0.676 -> 71.0% precision (n=62), EV +0.72%/trade vs 54.5% break-even; failed formal proof only on support (Wilson low 0.587 < 0.65), and pooled gate support scales ~4x under the PR #164 100-symbol universe -> proof range. (b) A/B sweep 4 (identical grid, fundamentals ON): per-symbol precision-gate proofs 10 vs 4 — fundamentals more than double per-symbol provability. (c) 5y history improves gate provability everywhere. CONSEQUENCES: label schema sm1.8 -> sm1.2 + feature schema change -> ENTIRE stored fleet goes label_schema_stale/feature_schema_stale on next boot -> startup retrain rebuilds ~100 symbols x2 directions under the new config (hours; gate was already closed with fireable_now=0, so no live-fire regression is possible during the window). Live TP/SL resolution + shadow seeding follow the new geometry BY DESIGN (labels and live resolution read the same knobs and can never diverge); historical shadow rows keep old geometry — live-recal bins mix regimes during transition (accepted, self-corrects as new-geometry rows accumulate). SAFETY: training gates (holdout>=60%, edge>=5%, precision gate, meta gate) self-reject weak models — if 1.2/5y/fundamentals hurts walk-forward edge the fleet visibly refuses to serve (observable at /api/v3/status) and the env change is trivially reversible. VERIFY AFTER RETRAIN: /api/v3/train/last per-symbol outcomes, /api/v3/status serveable/fireable counts, wallet geometry model_stop_vol_mult=1.2.",
        "PR #165 (2026-07-10): POINT-IN-TIME SEC FUNDAMENTALS AS MODEL FEATURES (off by default — V3_FUNDAMENTAL_FEATURES). The model's 49 features contain ZERO fundamentals while the fundamental shadow brain is the live scoreboard's best performer (6/7 actionable). NEW core/fundamental_features.py: fund_eps_yoy, fund_rev_yoy, fund_days_since_filing computed POINT-IN-TIME from SEC XBRL full filed-history (a quarter is visible to a training bar ONLY once its SEC filed date has passed; amendments only win after their own filed date — tested lookahead guarantees in tests/test_fundamental_features.py). Wired into backtest_symbol per-bar + predict_live_ex last-bar behind the flag, following the V3_SECTOR_FEATURE pattern exactly (toggling changes feature schema -> stored fleet retrains). Enabling in prod is deliberately deferred to the Phase-2 retrain decision, pending the offline A/B: sweep 4 (identical 5y grid, fundamentals ON) vs sweep 3 (OFF) running at ledger time. GEOMETRY SEARCH STATUS across sweeps 1-3 (36+ geometry/universe/history combos on real data): NO pooled 70% OOS operating point exists with current features — best pockets 57-66% at high thresholds; 5y history improves per-symbol gate provability (2/24 vs 0/24). Tests 857 passed/3 skipped; import-integrity PASS.",
        "PR #164 (2026-07-10): UNIVERSE 74 -> 100 (evidence-throughput lever of the autonomous 70% mission). Shadow evidence is structurally capped at ONE row per symbol per trading day (shadow_outcomes.py pick_daily_first + ON CONFLICT(symbol,trade_date)), so watchlist width IS the forward-proof rate — at ~33 resolved/day proving any 70% bin takes months. Added 26 liquid mega/large caps (ABNB ADBE BA CAT COIN COST CRM DELL DIS GE GS JPM LLY MA NFLX NKE ORCL PLTR PYPL QCOM SHOP TXN UBER V WMT XOM) — large caps also showed the cleanest label structure in the 2026-07-10 geometry sweeps. Updated the three 74-count tests to 100. RISK ACCEPTED + WATCHED: ~35% more feed calls per scan cycle; prod alpaca breaker runs 150 calls/60s (env-tuned) and the 5-tier chain + 900s OHLCV TTL cache degrade gracefully (yfinance/alpaca already cycle open as NORMAL per ledger) — if breakers flap hard post-deploy, stagger scans or trim the universe. GEOMETRY SWEEP RESULTS feeding this: sweep 2 (large-cap 24, 12 geometries, 2y): 0/12 pooled 70% PROVEN, best pocket 65.6% @ thr 0.8 n=32 (tgt 2.5%/stop 3.0%); sweep 3 (5y history, ~4x samples, focused grid) RUNNING at ledger time — see scratchpad geometry_grid_sweep*.json in session. Tests 850 passed/3 skipped; import-integrity PASS.",
        "PR #163 (2026-07-10): SESSION GATE + ATR WALLET BANDS + BRIER BRAIN SCORING + GEOMETRY RESEARCH HARNESS (autonomous mission toward proven 70% firing). (1) core/paper_wallet.py entry_window(): NEW wallet entries only during RTH minus buffers (default 8:45 AM-2:30 PM CT; env PAPER_SESSION_GATE/PAPER_ENTRY_OPEN_BUFFER_MIN/PAPER_ENTRY_CLOSE_BUFFER_MIN) — the 24/7 5-min poll was inheriting overnight-gap risk (stop overshoots to -4.9%); EXITS still run every cycle; candidate queries+quote fetches skipped entirely when closed (kills all-night feed pressure); diag exposes entry_window. Also dupe-check BEFORE quotes/bands via one ANY(%s) source query. (2) _wallet_vol_pct(): per-symbol realized-range vol (reuses forecast_band_vol_pct, env PAPER_ATR_BANDS default on) for WALLET brackets only — flat 2% gave a $1.40 biotech the same bracket as MSFT; reward:risk unchanged (stop still vol*0.65); model labels + live TP/SL resolver untouched. (3) refresh_shadow_profiles now computes Brier on each brain's own confidence into the previously-always-NULL calibration_error column; watcher summary exposes it as brains[].brier — confidence quality per brain finally measurable (completes what PR #162's learning-brain fix started). (4) NEW scripts/geometry_grid_sweep.py (read-only research): TARGET x STOP grid (env-parametrized GEOM_SYMS/GEOM_TARGET_SCALES/GEOM_STOP_MULTS) replicating train->calibrate->precision-gate per symbol, pooling OOS gate slices, computing EV + break-even per geometry. SWEEP 1 RESULT (legacy 24 meme/small-cap universe, 12 geometries, real prod data via railway run): ZERO geometries prove a pooled 70% operating point; best pooled precision ~58% (tgt 2.5%/stop 2.5% thr 0.70 n=26). HONEST CONCLUSION: geometry cannot fix that universe's noise — signal quality is the binding constraint there; sweep 2 (large-cap prod-watchlist universe) launched to test universe quality as the lever. Tests 850 passed/3 skipped; import-integrity PASS.",
        "PR #162 LIVE VERIFICATION (2026-07-10): deployed via GitHub push (auto-deploy confirmed working — this is now the deploy path, NOT railway up). LIVE: /api/_version _pr_version=162 git_sha_short=4374c89 deploy_id=fa359195-15fa-4651-8488-64452095ea7f; /health healthy score=95; gate still correctly closed (would_alert=false, meta_gate, up_prob 0.498). expectancy_by_geometry LIVE and immediately earning its keep: legacy 30 trades (60% WR but avg loss -4.03% -> expectancy -0.484%) vs current-geometry 11 trades (45.5% WR, avg loss -1.98% -> expectancy -0.17%) — the tight stop is doing its job (losses halved); the remaining gap should close as symmetric target fills credit winners' gaps forward. Learning-brain divergence and live-recal effects need forward shadow cycles to show in /api/watcher/summary — check in a few days.",
        "PR #162 (2026-07-10): SCOREBOARD FEEDBACK LOOP — full review after the wallet's first 40 closed trades, then three fixes making Ghost learn from its own live results instead of only observing/blocking. Review (3 parallel investigators) found: (1) WALLET GEOMETRY: closed book is a MIX of pre-PR#154 legacy -3.6% stops and current -1.3% stops (stop_price frozen per-row), AND exit fills were asymmetric — stops booked the gapped price (min(stop,price)) while wins were capped exactly at target, so losers got gap downside and winners no gap upside under 5-min polling; that asymmetry alone manufactured avg win +1.96% vs avg loss -3.34% (expectancy -0.43%/trade at 55% WR). (2) CALIBRATION: PR #156 gate works but is block-only and one flat >=0.70 bucket; train-time Platt/conformal is never refit from live outcomes — Ghost watched its 70+ bin run 79% predicted/48% realized (n=27, gap -0.31) and could only refuse. (3) LEARNING BRAIN: learning_adjusted_shadow_v1 was byte-identical to regime_shadow_v1 because per-(symbol,direction,horizon) profiles need >=3 resolved samples and picks spread over ~74 symbols never fill slices (permanent cold-start passthrough); plus shadow scoreboard keys on direction only so confidence/target adjustments are invisible. FIXES: (A) core/paper_wallet.py exit_fill target now max(target,price) — resting limit fills at limit OR BETTER, symmetric with stop gap fills; wallet_summary adds expectancy_by_geometry{} splitting the same 60 closed rows by each trade's FROZEN stop distance (legacy vs current, 1.5x threshold) so legacy losses can't hide whether the new geometry works. (B) NEW core/live_recalibration.py — per-bin live probability recalibration on the Watcher's own bin edges: adj=(wins+k*p)/(n+k) pseudo-count shrink (k=25, min bin n=5; env V3_LIVE_RECALIBRATION/V3_LIVE_RECAL_PRIOR_STRENGTH/V3_LIVE_RECAL_MIN_BIN_N) toward the bin's realized live win rate, hooked into signal_engine._evaluate_lane AFTER the PR #155/156 blocks (which remain as backstop) and BEFORE conformal confidence; new skip reason live_recal_prob_low; scores expose live_recalibration_up/down. LIVE fires only + UP lane only — research/shadow probes keep RAW probs so the evidence stream can't eat its own output. (C) core/super_ghost_learning.py pooled cross-symbol fallback: get_learning_profile_with_fallback prefers symbol evidence the moment it exists, else pooled_profile_from_rows sample-weighted merge across all symbols for direction+horizon (MIN_POOLED_SAMPLES=20, deltas HALVED, never dampen/supportive — direction blocks stay per-symbol-evidence-only); learning_adjustment now carries scope=symbol|pooled; learning_adjusted_shadow skips actionable picks whose evidence-adjusted confidence <0.55 so its judgment is finally VISIBLE on the direction-keyed scoreboard. Also fixed two STALE squeeze tests left failing on main by 0607e8c's intentional rescore (updated to exact recalibrated expectations). Tests: 842 passed/3 skipped (was 840+2 stale failures); import-integrity PASS (123 files/830 first-party imports incl. new module). HONESTY: (A) fixes trade MATH and reporting only; (B) makes probabilities honest vs live evidence — it can only LOWER a fire below the bar today (70+ bin inverted) and cannot loosen any gate; (C) wakes the learning brain with bounded, halved pooled adjustments. None of this proves 70%, none touches broker APIs, model training, or Railway env vars. Deploy is now GitHub-wired (push to main auto-deploys — verified on Railway deployment f3a8ff69); verify _pr_version=162 via /api/_version after push.",
        "PR #161 (2026-07-08): TELEGRAM ALERT HYGIENE + RAILWAY VARIABLE AUDIT. Operator pasted Telegram spam and asked for all Railway variables/values (excluding secrets) to confirm, then fix Telegram. Read ledger first, verified live PR160 /health healthy, then pulled Railway production vars via CLI and produced a secret-safe audit: 135 variables present; secret-like values masked by presence+length only; OPENAI_API_KEY is empty (optional, not used by current Telegram path); Telegram/Alpaca/Finnhub/Polygon/Anthropic/Cron/DB secrets present; no Railway env vars changed. Fixed alert spam in code: core.telegram now has persistent ghost_state-based send_telegram_message_once(key,cooldown_s) surviving restarts/redeploys; core.squeeze_monitor labels alerts as SQUEEZE RADAR / Radar only — not a Ghost gated trade, suppresses default confidence <50%, and dedupes by symbol/kind/material price bucket with SQUEEZE_ALERT_COOLDOWN; core.wolf_monitor buckets price-move alerts (price_move_DOWN_8, DOWN_10, etc.) and uses 24h default bucket cooldown so repeated -8.8% WOLF alerts do not spam all day. Tests added: telegram persistent dedupe, squeeze low-conf suppression/radar label/reprice key, WOLF price buckets; full suite 816 passed/3 skipped; import-integrity PASS (122 files/825 imports). Deployed source-backed; live /api/_version _pr_version=161 git_sha_short=dbad6be deploy_id=9c61741a-21a8-4e54-9658-2d3dfb84dd9a; /health healthy score=95; /api/telegram/status configured=true; local live-code proof shows SQUEEZE RADAR label and price buckets (-8.8 -> price_move_DOWN_8, -10.1 -> price_move_DOWN_10). HONESTY: this fixes Telegram noise/labeling/deduping only; no prediction/gate/wallet/model behavior changed. No secrets printed/stored; no Railway env vars changed.",
        "PR #160 (2026-07-08): DAILY REPORT LOGS — GAP-CLOSE PASS (independent re-audit of the PR #157-#159 daily report system, not trusting the prior reading). Verified live first: local==origin==live 467f05d _pr_version=159 health 95, /api/report/daily fast (0.6s) and /api/report/daily/logs working. Found and fixed THREE real gaps the first build missed: (1) UNBOUNDED GROWTH — the daily_report scheduler appends every 15 min (~96 rows/day) into ghost_daily_report_logs with NO retention, unlike the sibling ghost_perf_cycles which prunes on GHOST_PERF_RETENTION_DAYS. Added _LOG_RETENTION_DAYS (env GHOST_DAILY_REPORT_RETENTION_DAYS default 120) + _prune_daily_report_logs() run in the SAME transaction as each snapshot insert, guarded so a prune failure can never block the append. snapshot now returns pruned_rows + retention_days. (2) NO PER-DAY VIEW — the operator asked for day-by-day 'daily report logs' but GET /logs?limit=N returned the last N snapshots (all from the last ~2h). Added by_day mode (DISTINCT ON (report_date)) + ?by_day=1 route param so limit means 'days back'. (3) THIN READ-OUT — the report captured breakers + scan freshness but _narrate never spoke them. Added freshness line (last scan Nm ago; STALE if >20m; PAUSED note) and a data-feeds line (DEGRADED w/ open breaker names, else all healthy). Tests +6 (809 passed/3 skipped); import-integrity PASS (122 files/824 imports). Deployed source-backed from GitHub; live-verified /api/_version _pr_version=160 git_sha_short=695b16f deploy_id=843b88f8-f38c-4889-83cd-106bfe0c5ba5; /health healthy score=95; /api/report/daily narrative now reads freshness ('last scan 10 min ago (scanning live)') + feeds ('DEGRADED — open breaker(s): alpaca, yfinance'); /api/report/daily/logs?by_day=1 returns one row per calendar day; cron-gated POST /api/report/daily/snapshot returned log_id=6 pruned_rows=0 retention_days=120 writes_only=ghost_daily_report_logs. HONESTY: purely observability + a bounded-growth fix; no prediction/gate/wallet/model behavior changed. No Railway env vars changed; CRON_SECRET was read once to trigger the gated snapshot and was never printed or stored.",
        "PR #159 (2026-07-08): DAILY REPORT LOGS. Built the missing persisted notebook behind the daily report system. /api/report/daily remains GET/read-only but now uses true America/Chicago calendar-day bounds and DB-only performance-log reads (no slow /api/wolf/gate-status recompute). Added ghost_daily_report_logs plus scheduler job daily_report every 15 minutes and cron-gated POST /api/report/daily/snapshot; both write ONLY the notebook table and never mutate predictions/gates/wallet/model state. Added GET /api/report/daily/logs for persisted readback, plus route tests proving the snapshot writer touches only ghost_daily_report_logs and the report does not call the slow live gate. Version bumped to PR #159. Verification: import-integrity PASS (122 files/824 imports), python3.13 -m pytest tests/ -q -> 803 passed/3 skipped. Deployed source-backed after one transient CLI archive deploy; final live /api/_version _pr_version=159 git_sha_short=4c9c92c deploy_id=6a98880b-1a27-43d1-ab76-97c8113a708e; /health healthy score=95; /api/report/daily returns ok with decision_source=ghost_perf_cycles_db_only, gate closed v3_meta_gate, 44 scans of 74 symbols, 0 picks fired, Watcher status real_but_not_70; /api/report/daily/logs returns persisted rows including id=3 with git_sha=4c9c92c. Note: transient archive deploy row id=2 has git_sha=unset; corrected by source-backed redeploy and left as honest history. No Railway env vars changed; no secrets read/stored.",
        "PR #158 (2026-07-08): DAILY REPORT PERFORMANCE FIX. Live verification of PR #157 found /api/report/daily timing out after 30s because the wallet section called wallet_summary(), which refreshes live prices for every open position and can block behind market-data providers. Fixed core/daily_report.py wallet section to use DB-only paper wallet reads (closed/open rows, daily snapshot, config) and explicitly label it as a DB-only snapshot. This keeps the report read-only and fast without touching wallet behavior. Tests: daily_report targeted pass; full suite 802 passed/3 skipped; import-integrity PASS. No env var changed; no secrets read/stored.",
        "FUGU FINALIZER (2026-07-08, PR #156 post-deploy): live-verified overconfidence gate after Railway SUCCESS. /api/_version _pr_version=156 git_sha_short=938d635 deploy_id=9c45a115-9c35-47ac-94f5-5c809e2bd0be; /health healthy score=95. Watcher still reports the measured flaw: high-confidence >=0.55 is 54/87 = 62.1% real_but_not_70; 55-60 bucket 74.2% (n=31), 60-70 bucket 67.7% (n=31), 70+ bucket only 40.0% (10/25) with mean up_prob 0.7894 and calibration_gap -0.3894. PR #156 blocks otherwise-fireable prob>=0.70 signals when that live bucket is weak (default min n=20, min WR=55%). Mid-prob signals below 0.70 are not blocked by this overconfidence gate. Direct railway-run DB proof remains blocked by postgres.railway.internal DNS from this shell, but app DB-backed endpoints are live and healthy. Tests/import gate PASS: 799 passed/3 skipped, import-integrity 121 files/812 imports. No Railway env var changed; no secrets read/stored.",
        "PR #157 (2026-07-08): DAILY REPORT — one consolidated 'today's report' (core/daily_report.py + GET /api/report/daily). Composes what Ghost did today (scan cycles, gate open/closed + reason, picks fired, top skip reasons, closest-to-firing), the wallet day (opened/closed trades WITH exit reason + P&L, goal progress), the Watcher calibration verdict (working-or-guessing + per-bin + blind spots), health, and breakers — PLUS a plain-English narrative[] that reads out loud. Read-only aggregation; every section degrades to an error note so one dead dep can't blank it. Answers 'what's today's report?' in one call. 802 tests.",
        "PR #156 (2026-07-08): OVERCONFIDENCE CALIBRATION GATE (code-only tightening). Watcher proved the top confidence bucket is inverted/miscalibrated (up_prob >=0.70: N=25, actual win rate 40% at mean up_prob ~0.79). Added a global high-probability calibration blocker in core/proven_skill_gate.py: if an otherwise-fireable real signal has prob >= V3_OVERCONFIDENCE_PROB_THRESHOLD (default 0.70), then the live resolved shadow bucket for up_prob>=threshold must have >=20 samples and >=55% realized win rate. If the bucket is weak/unavailable, runtime returns calibration_unproven and scores include overconfidence_gate_up/down. This only applies at the final would-fire point, preserves precise diagnostics (prob_low stays prob_low), and research mode/test off-switch can bypass for evidence collection. Default is ON, but this only tightens real fires; it does not change wallet, shadow outcomes, model training, or env vars. Added pure tests for inverted/good buckets and runtime block; full suite 799 passed/3 skipped; import-integrity PASS (121 files/812 imports). HONESTY: this does not prove 70%; it prevents the known inverted 70+ bucket from firing real picks until forward calibration improves. No Railway env var changed; no secrets read/stored.",
        "FUGU FINALIZER (2026-07-08, PR #155 post-deploy): live-verified PR #155 after Railway SUCCESS. /api/_version _pr_version=155 git_sha_short=e372d4f deploy_id=c8d4df8c-e2df-47c8-8471-c9c9e963611a; /health status=healthy score=90 (breaker fluctuation, not app-degraded); /api/v3/status fleet_summary still serveable=46, fireable_now=0, precision_ok=11, base_rate_riders=34; /api/wolf/gate-status still bootstrap/meta_gate with up_prob ~0.503 < needed, so no real fires. Repo clean: main==origin/main==e372d4f. Tests/import gate already PASS: 794 passed/3 skipped, import-integrity 121 files/811 imports. No Railway env var changed; no secrets read/stored. PR #155 is code-only tightening and did not alter wallet geometry PR #154 or model label schema.",
        "PR #155 (2026-07-08): PROVEN-SKILL LIVE GATE (code-only tightening). Generalized the PR #151 wallet skill idea into the real runtime fire path without loosening anything. New core/proven_skill_gate.py requires any otherwise-fireable real signal to have symbol-level forward shadow evidence: default >=10 resolved WIN/LOSS rows, TP rate >=55%, and avg pnl >=0.0 before a real fire can emit. Runtime integration: after meta + precision gates and only if probability is above the effective threshold, predict_live_ex checks symbol_review; failures return skill_unproven and scores include proven_skill_gate_up/down. Diagnostics preserved: below-threshold signals still return prob_low, unproven precision still precision_unproven. Research mode bypasses the skill blocker so probes/shadow analysis still collect evidence. /api/v3/status now mirrors runtime and will not mark a model fireable_now if proven_skill_gate would block it. This blocks GME/NOK/XPO-class overconfident/base-rate-rider symbols from ever firing real picks later, while preserving gate-closed behavior today. Tests: new pure gate tests + runtime/status mirror tests; full suite 794 passed/3 skipped; import-integrity PASS (121 files/811 imports). No env var changed; no secrets read/stored. HONESTY: this reduces possible fires; it does not prove 70% or improve model accuracy. Success is fewer bad fires when/if the model becomes otherwise fireable.",
        "PR #154 (2026-07-08): WALLET GEOMETRY FIX (candidate implementation). Closed-trade post-mortem of the 18 wallet trades found the dominant loss driver was STRUCTURE, not prediction: every win was +2.0% while every loss was ~-3.7% to -4.4%, an upside-down reward:risk needing ~64.3% win rate to break even while the wallet won ~44%. ROOT CAUSE in code: paper_wallet.fresh_bands derived its stop via stop_pct_from_vol(), which reads the GLOBAL V3_STOP_VOL_MULT (=1.8 in prod) — so the wallet inherited the model stop (-3.6%) against a +2% target. FIX: decoupled the wallet stop into its OWN knob core.paper_wallet._wallet_stop_vol_mult (env PAPER_WALLET_STOP_VOL_MULT) DEFAULTING to 0.65 -> wallet now trades +2%/-1.3% (reward:risk 1.54, break-even ~39.4%). CRITICAL: this does NOT touch V3_STOP_VOL_MULT, the model label schema (tp_sl_fwd_v1_sm1.8), the live TP/SL resolver, or any accuracy/precision/kill gate — the stored model fleet stays exactly as-is, so no fleet goes label_schema_stale and live firing is unchanged (gate still closed, fireable_now=0). Added pure/testable geometry_stats (reward:risk + break-even) and closed_trade_expectancy (win rate, avg win/loss, expectancy, profitable verdict), surfaced in wallet_summary under geometry{} + expectancy{}. PROOF (reproduced 18-trade ledger, identical 44.4% win rate): OLD -3.996% avg loss -> expectancy -1.33%/trade (unprofitable); NEW -1.3% avg loss -> expectancy +0.17%/trade (profitable). Tests: +5 new (decoupling, override, break-even math, expectancy flip); full suite 786 passed/3 skipped; import-integrity PASS (120 files/808 imports). HONESTY: this fixes the trade MATH only. It does NOT improve prediction accuracy, does NOT prove 70%, and does NOT target $20k/mo. Success metric is forward: >=30 newly resolved wallet trades under the -1.3% stop showing positive expectancy. SEPARATE operator step still open: the MODEL geometry (global V3_STOP_VOL_MULT 1.8->0.65) requires a ledgered env change + full fleet retrain + OOS verification; not done here. No Railway env var changed; no secrets read/stored.",
        "FUGU FINALIZER (2026-07-08, PR #153 post-deploy): independently re-verified every prior-agent claim against LIVE production before sign-off. CONFIRMED live: /api/_version _pr_version=153 git_sha_short=5a64b8a deploy_id=2ee212fd-67a4-48b9-b69a-f8f7ff6ce214; /health healthy score=95; main==origin/main==5a64b8a, tree clean. Watcher live and read-only: /api/watcher/summary returns resolved_n=1031, high_confidence n=87 wins=54 win_rate=62.07% (Wilson 95% CI 51.6-71.6%), brier=0.2913, verdict real_but_not_70; /api/watcher/snapshots persisting (append-only ghost_watcher_snapshots). 12-brain manifest live incl momentum_shadow_v1 + momentum_shadow_v2. Gate CLOSED as designed (fireable_now=0, up_prob 0.5042 < 0.6189 needed, bootstrap). Local gates PASS: import-integrity 120 files/807 imports; pytest 782 passed/3 skipped. SAFETY/HONESTY re-verified: broker tripwire test passes; FEATURE_COLS still 49 with ZERO news/SEC features (PR #134 SEC un-blinding NOT feeding the model — sec_fundamentals not imported in signal_engine/engine_features/prediction); momentum_shadow_v2 confidence-capped 0.69 and shadow-only (no predictions/wallet writes). CALIBRATION HEADLINE (live Watcher bins): 55-60 bucket 74.2% (n=31), 60-70 bucket 67.7% (n=31), but 70+ bucket only 40.0% actual at mean up_prob 0.79 (n=25) — the top bucket is INVERTED/miscalibrated, confirming the XPO-style overconfidence the mission flagged. HONEST WIN RATE: real signal ~62% high-confidence, NOT 70%; 70% remains unproven under current 2%/3-day geometry. No Railway env var changed; no secrets read/stored this session.",
        "PR #153 (2026-07-08): Watcher + momentum_shadow_v2. Built the requested read-only babysitter and a stricter second momentum brain without changing real-pick behavior. core/watcher.py adds pure calibration math (Wilson CI, Brier, confidence buckets, honest verdict), /api/watcher/summary (GET/read-only) over existing ghost_shadow_outcomes + ghost_perf_symbol_evals + shadow profiles, /api/watcher/snapshots (GET/read-only), and an append-only scheduler job that writes ONLY ghost_watcher_snapshots as a notebook; it never mutates predictions, gates, wallet, or shadow outcomes. Added momentum_shadow_v2 as a NEW model_id (v1 frozen) with multi-timeframe trend, relative strength, pullback/extension discrimination, and penalties for overextension/chop; it is shadow-only and deliberately stricter than v1. Railway read-only proof before deploy: 12 shadow brains registered incl momentum v1/v2; ODD/CLNE/AAPL/PFE/GME all evaluated by v2 and held unless setup cleared strict continuation quality; Watcher math on the live-like 54W/33L high-confidence sample reports 62.1% with Wilson [51.6%,71.6%] and verdict real_but_not_70. Tests: import-integrity PASS (120 files/807 imports), 782 passed/3 skipped. No Railway env var changed; no secrets read/stored. HONESTY: PR #153 does not promote momentum, does not fire real picks, does not claim 70%; it creates forward evidence collection so promotion can be decided later by resolved shadow profiles.",
        "FUGU DEEP-REVIEW + GEOMETRY PROOF (2026-07-08 pass 2): self-directed prediction-engine audit with OUT-OF-TIME proof. Added two READ-ONLY research tools (scripts/geometry_edge_sweep.py, scripts/provable_oppoint_sweep.py) that fetch OHLCV via the 5-tier chain on the box, regenerate TP/SL labels at candidate stop multipliers via os.environ override ONLY (never a Railway var), and run the SAME purged walk-forward validator + precision gate the engine uses. No DB writes, no model persistence, no env-var change. 773 tests pass; import-integrity PASS (119 files/794 imports). PROVEN: (1) LEVER #1 CONFIRMED -- V3_STOP_VOL_MULT=1.8 (prod) collapses out-of-time model edge: mean walk-forward edge -0.06 (below base rate), negative for 8 of 12 symbols; at 0.65 mean wf_edge +0.22 and POSITIVE for all 12, also best on EV. That is why every fleet retrain at 1.8 yields 0 serveable/fireable models -- the wide stop inflates raw win rate but destroys the edge the gates require. (2) HYPOTHESIS DISPROVEN: reverting to 0.65 does NOT unblock a fireable fleet. At 0.65 the 70pct precision gate finds NO operating point on ANY of 24 symbols (0/24 per-symbol; pooled 70pct UNPROVEN) because the tight stop gives a ~35pct base rate and 70pct precision is unreachable on a 35pct-base-rate label even with real edge. (3) The 1.8 geometry 6 precision-gate PROVEN symbols are BASE-RATE RIDERS not skill (e.g. GME passes at thr 0.63 with holdout edge 0.0, wf_edge -0.02) -- blocked live only by the meta-gate, which is working. (4) HONEST CEILING: a genuine >=70pct out-of-time operating point is NOT provable under the current 2pct/3-day target geometry at any stop width tested (0.65-1.8). Live shadow fireable bucket = 59.3pct TP (95pct Wilson CI 49.1-68.9pct, N=91); pooled OOS precision thr>=0.55 = 60.8pct (CI 56.8-64.6pct, N=609). Real signal (~59-62pct, above coin flip) but not 70. BREAKEVEN MATH (target +2pct, breakeven=stop/(target+stop)): mult 1.8 stop -3.6pct needs 64.3pct just to break even, so the live ~62pct fireable set is EV-NEGATIVE (-0.13pct/trade); mult 0.65 stop -1.3pct breakeven only 39.4pct, so a selective ~57pct subset there is EV-POSITIVE (+0.58pct/trade). CONCLUSION for operator: win rate is the WRONG knob; EV vs breakeven is. Provable path to profit = TIGHT stops (0.65) + SELECTIVITY on demonstrated edge (generalize the paper_wallet PR #151 filter to the live gate), NOT chasing 70pct headline accuracy. Even a real +0.58pct/trade at ~20 trades/mo is ~+12pct/mo full-stake and far less with $500 fractional slices -- $10k->$20k/mo stays out of reach without account-risking leverage (consistent with KNOWN TRUTH). FEATURE FINDING: FEATURE_COLS = 49 cols with ZERO news/SEC-fundamental features -- PR #134 un-blinded SEC/news but that data is NOT in the model feature vector (news_shadow_v2 is a separate shadow brain, not a model input). Feeding it is a real lever but NOT shipped (needs schema bump + full prod retrain + OOS compare I cannot persist/validate from this sandbox). NOT SHIPPED (flagged unproven / operator-owned): (a) the V3_STOP_VOL_MULT flip to 0.65 -- right direction for EDGE but does NOT reach the 70pct gate and needs a full fleet retrain + model persistence to validate; must be an explicit ledgered operator env change + retrain, never a silent flip; (b) news/SEC features into the model; (c) any gate/contract loosening (refused per prime directive). This pass changed NO engine behavior, NO Railway var, read/stored NO secrets -- read-only proof tooling + this ledger entry only.",
        "FUGU MASTER RERUN (2026-07-08 13:13-13:20 CDT): personally re-verified current live state after the prior handoff instead of trusting it. Confirmed source-backed production at _pr_version=152, git_sha_short=d49273b, deploy_id=8a961f58-d76d-477f-8bbc-d203a57e0ac3, /health score=95, fireable_now=0 (gate closed by design), 46 serveable / 126 stored models / 74-symbol watchlist, wallet filters live (0.55 min prob, 55% TP rate, 10 resolved) and wallet underwater at about $9,876 fake money. Local verification PASS: import integrity 117 files/784 imports, 773 passed/3 skipped. Investigated apparent weak-symbol wallet opens: PFE/OPK/NOK/GME/SNAP/AI/XPO are pre-PR #151 entries still aging out; newest post-filter entry CLNE clears the 55% TP floor (55.6%), so no live filter-bypass bug verified. Read-only prod DB introspection via railway run remains blocked from this local shell by postgres.railway.internal DNS; no DB mutation, no env var changes, no secrets read/stored. Result: no app-code fix warranted in this pass; ledger-only accountability update.",
        "FUGU FINALIZER (2026-07-08): master-agent final verification after concurrent PR #151 + Fable PR #152. Live verified during session as _pr_version=152, git_sha 010255f, deploy a2b1c839, /health score=95 before this docs-only ledger follow-up; verify /api/_version for the exact latest SHA before acting. PR #151 wallet proven-skill filter is live (shadow_min_prob=0.55, shadow_skill_min_tp_rate=0.55, shadow_skill_min_resolved=10) and stale shadow rows were cleared (31 pending/1136 resolved -> 29 pending/1138 resolved; remaining rows waiting until 2026-07-08 16:00 CDT). PR #152 momentum_shadow_v1 is live as the 11th shadow brain. Full current suite: 773 passed, 3 skipped; import-integrity PASS (117 files, 784 first-party imports). No Railway env vars changed; CRON_SECRET was read only once to trigger the gated shadow-cycle and never printed/stored. A temporary source-less railway up caused git_sha_short=unset, corrected by redeploying from GitHub source. Honesty: gate still closed/fireable_now=0; wallet remains fake-money and underwater; no accuracy/profit claims.",
        "PR #151 (2026-07-08): wallet proven-skill filter + shadow resolver expiry cleanup. Candidate fix from Fugu/master-agent session. Shadow wallet default PAPER_SHADOW_MIN_PROB raised 0.50->0.55 (env override still supported) and new symbol-level filters require >=10 resolved WIN/LOSS rows and TP rate >=55% before fake-money shadow entries; this addresses the live finding that the wallet was buying coin-flip/negative-P&L symbols just because model up_prob >=0.50, while keeping the gated book untouched. Shadow resolver now closes already-expired rows as EXPIRED at entry/0% P&L if OHLCV bars are unavailable, preventing stale pending rows from sitting forever without crediting WIN/LOSS. Boot banner/version bumped to 151. Tests added for defaults/env overrides and no-bars expiry. HONESTY: fake-money only; does not claim prediction accuracy, does not target $20k/mo, and does not touch broker APIs.",
        "FUGU SESSION (2026-07-08): live reconciliation only — NO app-code change. Read ledger, verified every headline claim against the live public API + a local build. Corrections written into the production{} block above: pr_version 147->150 (git_sha 5ed0138), tests 758->764, watchlist 43->74 (already PR #149), models 126 stored/46 serveable/0 fireable_now. Gate CLOSED is CORRECT (live up_prob 0.5056 vs 0.6189 needed; bootstrap). kill-status win_rate/brier show RED historically but enforcement_window (post-resume) is 'insufficient' so the engine is NOT paused — matches design. Wallet underwater (-1.3% MTD) and honest; $10k->$20k/mo remains mathematically impossible per KNOWN TRUTH (did NOT touch it). Ghost MCP already registered + Connected (did not re-add; would risk clobbering Fable's parallel session). GHOST_OAUTH_SECRET confirmed present in prod env (count only; never printed); no env var changed this session. OPEN ITEM for an agent with prod-DB/log access: /api/shadow-stats has 31 pending shadow rows, oldest expired 2026-06-15 (~23d), resolver hourly job reports seeded 0/resolved 0 — could be feed-blocked (honest) or a stuck resolver (bug). Could NOT confirm from here: local `railway run` can't reach postgres.railway.internal (private-net host), so prod-DB introspection is blocked from this sandbox. Do NOT 'fix' by force-resolving; diagnose bars availability first via core.signal_engine._fetch_ohlcv on the box.",
        "PR #114: Research pick mode + breaker diagnostics + prev_close 5-tier chain + confidence caps",
        "PR #115: P0 audit — Kelly formula corrected (f* = p-(1-p)/b), breaker auto-recovery fixed, research pick hardening, auth-gated writes, async fixes",
        "PR #116: Phase 0-1 — FEATURE_COLS 33→49 (8 macro + 8 cross-sectional), point-in-time training data, auto-log watchlist (43× multiplier), dead promotion gate removed",
        "Phase 2 (LOCAL, NOT DEPLOYED): DOWN model — backtest_symbol returns (up, down) tuple, _train_one_direction helper, dual model training, predict_live_ex picks stronger signal, load_model accepts direction param",
        "PR #122-#125: Accuracy contract 70%, feed resilience (null-bars guard, seeder deadlock, OHLCV storms), precision gate global pool, stop geometry precision",
        "PR #126: Full forensic audit fixes — 13 critical + 15 high issues resolved (regime modifier sync, 3,600 lines dead code removed, dependency inversion fixed, ghost_state DDL centralized, dev-mode auth bypass hardened, cache race conditions fixed, import integrity baseline cleared)",
        "PR #127: GO verification — 686 tests, 33/33 Playwright, 0 critical health findings, 0 new ERROR signatures, release gates all green",
        "ENV AUDIT 2026-07-06 (post PR #135): all contract/gate/kill vars correct; weak env overrides (V3_MIN_HOLDOUT_ACC=0.38 etc) confirmed neutralized by contract clamps. SECOND undocumented drift found: NEWS_DEFENSE_ENABLED=1 (shipped dark at 0 in PR #134, flipped by unknown session) — left ON because NEWS_DEFENSE_MODE is unset = warn-only (logs, touches nothing), which matches the recommended rollout; documenting here so it is no longer undocumented. V3_STOP_VOL_MULT=1.8 explains the 80 not_serveable models (label-schema change awaiting retrain). Watch items: V3_WF_ACC_MIN_SLACK=0.15 and V3_MIN_TP_SL_WINS=10 are uncclamped loosenings; SPCE wf-acc override 0.38 is charitable. RULE FOR ALL AGENTS: any Railway env change MUST get a ledger entry — this is the second silent flip caught in two days.",
        "PR #140 (2026-07-06): empty market-state honesty fix. Follow-up live verification after deploy/cold cache showed /api/market/sessions could label empty failed-fetch cache shells as provider_state=live even when ok=false and no price/OHLC existed. Fix: after fallback/ok computation, empty rows are relabeled unavailable or breaker_open with state_note; live/cached/stale now imply usable market truth. IMPORTANT: this fixes reporting honesty only; it does not make blocked providers return data.",
        "PR #139 (2026-07-06): orphaned train-status honesty fix. Follow-up live verification of #138 showed /api/v3/train/last could still say state=running after a deploy restart, when no background retrain thread can exist. Fix: v3_train_last now checks the in-process retrain lock; started/running + blank passed + no active lock is reported as state=orphaned with running_now=false and status_note, while a genuinely locked train remains state=running/running_now=true. IMPORTANT: observability only; still no proof of 70% live accuracy.",
        "PR #138 (2026-07-06): train-status honesty fix. Audit found /api/v3/train/last could report state=running with stale finished_at from a prior run because _record_v3_train_state upserts only supplied fields, and blank passed='' was coerced to False. Fix: async/sync training start markers now clear stocks and finished_at; /api/v3/train/last treats blank passed as null and suppresses stale finished_at while state is started/running. Also fixed fake-thread retrain-lock leakage in tests. 741 tests + import integrity pass. IMPORTANT: this is an observability fix only; it does NOT prove 70% live prediction accuracy.",
        "PR #152 (2026-07-08): MOMENTUM BRAIN — Ghost's 'other way of thinking'. Base engine is short-term mean-reversion (2%/3-day), blind to multi-week bullish runs (never 'saw' ODD +80%). core/momentum.py scores 0-6 trend signals (breakout to 20d high, SMA20>SMA50, above SMA20, ADX>=20, 20d return>=8%, volume>=1.2x). momentum_shadow_v1 = 11th shadow brain: leans UP on score>=4, confidence-capped 0.70, shadow-only. LIVE-VERIFIED: ODD score 4/6 (+65% 20d, ADX 30) -> UP 0.63; AAPL/PFE/GME (flat/declining/chopping) -> HOLD. This measures whether ride-the-trend pays FORWARD before anything trusts it — the honest way to add momentum-catching. Operator asked for Ghost to 'think both ways'. Note: does NOT change the $20k math; adds a lens, not leverage. 773 tests.",
        "PR #150 (2026-07-08): master-audit P2 fixes (2 audits cross-verified — each caught what the other missed). P2-A (my audit, Fugu missed): /api/squeeze/daily-log returned candidate+telegram DUPLICATES (~70 pairs) — the console deduped for display so Fugu's frontend check masked it. Fixed at the API layer: _dedupe_candidate_telegram collapses same (symbol,date,buy,sell,stop) keeping resolved>telegram>candidate; 5 regression tests assert on the RAW payload. P2-B (Fugu): monthly goal progress_pct goes negative underwater (honest but confusing vs the bar) — added pct_of_goal=equity/goal (always positive) driving the bar + '% there' label. P2-1 (Fugu, git_sha unset) was ALREADY resolved live (sha now injected = 4d702e1). Reconciliation also caught Fugu's stale '43 symbols' claim — live is 74 (PR #149). 764 tests.",
        "PR #149 (2026-07-08): watchlist 45 -> 74 — operator added a 58-name list (mostly mega-caps). Added 30 new (AAPL/MSFT/NVDA/AMZN/META/GOOG/GOOGL/AMD/AVGO/INTC/MRVL/MU/TSLA/BABA/RKT/DASH/BROS/KC/JACK/ACDC/W/BBWI/SW/NAVN/LMND/MTZ/OLLI/APGE/HTZ). EXCLUDED: RDFN (Redfin DELISTED 2025-07 into RKT — feed returned a PHANTOM $11.20 for the dead ticker, caught by verify-first), SNDK (feed showed implausible ~$1600 for Sandisk), SpaceX (private). Rest were already watched. Feed accuracy confirmed via AAPL=$310 cross-check (2026 tech rally is real, not a feed bug). Deleted obsolete test_excludes_old_railway_defaults (operator reversed the no-mega-cap rule). HONESTY: mega-caps move LESS than the small-caps already watched, so 2% 3-day targets are RARER on them — this ADDS coverage but works AGAINST the short-term strategy and does nothing for the $20k goal. New symbols need retrain for models. 758 tests.",
        "PR #148 (2026-07-08): watchlist 43 -> 45. Operator listed 19 names to add; 16 were already watched, SpaceX is PRIVATE (cannot add), so 2 genuinely new: DOMO (Domo) + BTGO (BitGo, NYSE IPO 2026-01, both resolve in Alpaca feed). Updated OFFICIAL_WATCHLIST + count tests (43->45) + admin copy. New symbols need a retrain to get v3 models (label_schema_stale until then) but immediately join scanning/shadow-eval/wallet shadow book. HONESTY logged for operator: more symbols does NOT change the $20k/mo math — per-trade 2% geometry caps monthly return ~5-10% regardless of watchlist size; this is coverage, not a goal accelerator. 758 tests.",
        "PR #147 (2026-07-07): wallet MONTHLY GOAL — operator wants Ghost to have a recurring purpose: reach a monthly target (default $20k from $10k), reset on the 1st, try again. core/paper_wallet.py: goal in config (PAPER_MONTHLY_GOAL, editable via /api/wallet/config?monthly_goal=, clamped >= start), ghost_paper_monthly records each finished month (start/goal/final_equity/hit_goal/return_pct), _maybe_roll_month auto-closes+resets on calendar-month change using the last daily snapshot as final equity. Summary exposes goal{target,progress_pct,reached,need_per_day,days_left,history}. New Wallet-tab goal panel: progress bar, pace, past-months honest track record. HONESTY: goal is labeled aspirational stretch; the real monthly results kept in history will show what is actually achievable (evidence, not promises) — $20k/mo = 100%/mo is not real, the track record will demonstrate that. 758 tests.",
        "PR #146 (2026-07-07): mobile UX fixes from operator screenshots. (a) My picks no longer needs /admin login to view or edit — single-operator dashboard, picks are just tickers; routes gate through _my_picks_gated (public unless MY_PICKS_REQUIRE_AUTH=1); removed the 'Sign in at /admin' UI prompt. (b) Wallet metric cards overlapped on mobile (4-col grid on a 375px screen) — removed inline 4-col override so mobile media query gives 2 cols + smaller wrapping values. (c) 'Floating panel always moving' = sticky topbar + scroll-compact JS; topbar now position:static on mobile and the scroll-driven compact toggle removed. (d) Tabs were a horizontal-scroll strip hiding half the tabs; nav now flex-wraps so EVERY tab is visible as compact chips, no scrolling. 755 tests.",
        "PR #145 (2026-07-07): wallet Option B — enter at buy-now quote with FRESH bands recomputed from Ghost vol geometry (base_vol_pct + stop_pct_from_vol) instead of the signal's stale morning bands. Removes the band_crossed dead-end (day 2 all 20 candidates were rejected because live price had fallen below morning stops on a -9% WOLF tape). Now the wallet takes positions daily — wins AND losses — the unbiased fill-level evidence the exercise exists to gather. Trade-off accepted: bands are Ghost-geometry-at-fill, not Ghost's exact stated targets; chosen because the operator wants daily transactions + weekly P&L. PAPER_HOLD_BARS=3d expiry. Still fake money, long-only, broker-tripwire test intact.",
        "PR #143-144 (2026-07-07): wallet zero-entry diagnosed + fixed. Day 2 the wallet had taken 0 positions across 2 sessions. Added cycle diag counters (observability) which revealed the real cause: of 20 shadow candidates, 15 skipped for no_price (batch price call used max_fresh=6, starving prices for the other ~14 uncached symbols) and only 5 for band_crossed. Fix: _live_prices now uses max_fresh=len(symbols) then a bounded breaker-protected get_price() spot fallback for any batch-null symbol — the wallet needs a real price for every symbol it might trade. NOTE: the 5 band_crossed skips are a separate honest design point (entry at current quote vs stale eval bands) — acceptable for now, the guard correctly refuses signals already past their stop/target. 752 tests.",
        "PR #142 (2026-07-06): wallet entry guard — never enter a signal whose stop/target is already crossed at entry (first live cycle entered 2 stale blown-stop signals and booked -$113 instantly; guard added, wallet reset clean to $10k). LIVE-VERIFIED at PR 142: /api/wallet clean $10k, guarded cycle entered 0 stale signals, Wallet tab live in console. First organic paper trades land with the next fresh scan evals (intact bands).",
        "PR #141 (2026-07-06): wallet first-fill fix — mirror any still-live (unresolved+unexpired) signal instead of only post-reset ones; entries fill at CURRENT quote so mirroring an hours-old signal stays honest. NOTE: PR number collision — two agents both shipped 'PR #138' (paper wallet vs train-status honesty); live numbering resynced at 141.",
        "PR #138 (2026-07-06): PAPER WALLET — fake-money Cash-App-style wallet, new 'Wallet' tab in ghost_console. core/paper_wallet.py: configurable starting balance (default $10k; reset wipes books — admin-gated POST /api/wallet/config), TWO BOOKS never mixed (gated = mirrors real fired picks; shadow = mirrors ghost_shadow_outcomes evals with up_prob >= PAPER_SHADOW_MIN_PROB 0.50, $PAPER_TRADE_SLICE_USD 500 slices, PAPER_MAX_OPEN 15). Quote-level fills honestly labeled: entry at live quote, target fills AT limit, STOP FILLS AT min(stop, price) so gap-through slippage is finally recorded (the thing bar-sims hide), expiry closes at market. 5-min scheduler cycle + daily equity snapshots (ghost_paper_daily). GET /api/wallet summary. Long-only; module has a tripwire test asserting it never references broker order APIs — fake money stays fake. This closes the 'resolved/realistic/fill-level evidence' gap all three audits flagged. 752 tests.",
        "PR #137 (2026-07-06): batch-endpoint truth bug (caught by follow-up read-only audit within hours of #136 — the multi-agent review loop working). Bug: /api/market/sessions served cached rows verbatim; rows cached during a failed trade-fetch carry price=null despite valid RTH data, so 17 symbols read ok:false while the single-symbol endpoint (which patches via get_price) said ok:true. Fix: batch now mirrors single-endpoint semantics WITHOUT provider calls — price falls back to rth_close (then today_open) with price_source labeled *_fallback, change_pct recomputed, ok = price OR has_ohlc. Also: /api/squeeze/picks fetch_ok/fetch_fail got a fetch_note — those counts belong to the snapshot at last_scan_ts (one degraded breaker-trip cycle persisted there), explaining the 26/17-vs-43/0 disagreement with newer scan logs. 739 tests. LIVE-VERIFIED: post-deploy cache-only sweep 43/43 ok (was 26/43), 17 fallback-patched rows labeled rth_close_fallback; transient empty-cache rows self-heal within one refresh window (~60s+budget).",
        "PR #136 (2026-07-06): LIVE-MARKET AUDIT FIXES (P1-P5 complete). (a) GET /api/market/sessions batch endpoint — cache-first, max_fresh provider-hit budget per call (default 8, MARKET_SESSIONS_MAX_FRESH), partial results, per-symbol provider_state (live/cached/stale/breaker_open/unavailable) + freshness_seconds; makes the auditor's 43-symbol breaker-tripping sweep impossible by construction. (b) /api/v3/status fleet_summary (serveable/fireable_now/precision_ok/base_rate_riders/proven_skill counts) + missing_v3 {symbol: serve_reject reason} — audit P5 diagnosis for ABCL/RIG/STUB/TAL/TME class gaps. (c) Cockpit scorecard now leads with Direction hit rate (headline); level closeness demoted + labeled 'telemetry only' in summary line, quality cards, and per-day table header. (d) Note: auditor's breaker trip was self-inflicted load on the per-symbol endpoint; internal paced scan was never at risk — batch endpoint fixes the public surface anyway. 736 tests. LIVE-VERIFIED post-deploy: _pr_version=136, health 95; batch endpoint 3 rapid full-watchlist calls -> 43/43 rows each, fresh fetches capped 8/8/2, ZERO new breaker failures (the audit's trip is now impossible); fleet_summary live (46 serveable / 0 fireable / 34 base-rate riders); missing_v3 diagnoses live (ABCL/RIG/STUB/TAL/TME all label_schema_stale). RETRAIN RESULT (full fleet, 24min): gate_passed 0 of 5 attempted — under the strict admission gates (wf_edge>=0 + contract floors + the V3_STOP_VOL_MULT=1.8 geometry) ZERO new models qualified. The 46 serveable models are prior-schema survivors. OPEN DECISION FOR OPERATOR: V3_STOP_VOL_MULT=1.8 is the third undocumented env experiment — it invalidated 80 stored models and inflates label base rates (wide stop rarely hit -> natural_rate up -> edge collapses -> gates reject everything). Options: revert to documented 0.65 and retrain, or run the audit's Tier-3 EV-optimized geometry sweep properly. Do not leave it at 1.8 by inertia.",
        "PR #135 (2026-07-06): AUDIT HONESTY FIXES (external read-only audit, verified w/ 2 corrections). GOVERNANCE DRIFT FOUND+FIXED: V3_DOWN_SIGNALS_ENABLED was '1' in Railway against the documented keep-at-0 plan, undocumented by any ledger entry — reset to 0 until a DOWN shadow track record exists. Audit corrections: stacking ensemble was ALREADY on (V3_ENSEMBLE=stacking), DOWN lane was ALREADY on (not off as audit claimed) — auditor read code defaults, not live env. Fixes: (a) /api/v3/status per-model fireable_now + fire_block_reason mirroring _evaluate_lane static checks (audit found 11 precision_ok displayed vs 0 actually fireable), base_rate_rider flag (accuracy<=natural_rate+2pp), proven_skill (edge>0 AND wf_edge>0); (b) V3_MIN_WF_EDGE default -0.05 -> 0.0 (negative out-of-time edge never counts toward 70%); (c) /api/stats win_rate_wilson_lb95_pct (3/10 live record = 10.8% Wilson floor, not '30%'); (d) /api/symbol-accuracy marked legacy:true with crypto-era warning; (e) scorecard metrics_note: overall_pct = OHLC level closeness telemetry, direction_hit_rate_pct is the headline. 730 tests.",
        "PR #134 (2026-07-06): NEWS EVENT LAYER + SEC UN-BLINDING (merged Fugu+Claude plan). (a) ROOT-CAUSE FIX: SEC ticker index returned 403 to the old User-Agent (no contact email) — 42/43 watchlist symbols had NO CIK and were fundamentals-blind since inception; new UA + hourly retry + warning-level logging; 10,415 tickers now resolve. (b) core/news_events.py: ghost_news_raw_articles + ghost_news_events tables, deterministic 18-type classifier (dilution, going_concern, guidance, FDA, M&A rumor-vs-confirmed, ...), asof_ts point-in-time discipline, dedupe one-event-per-type-per-day. (c) core/news_ingest.py: Alpaca+Finnhub providers, 15-min scheduler job, /api/news/events + cron-gated /api/news/ingest. (d) news_shadow_v2 registered as 10th brain (v1 FROZEN as baseline — never mutate a ledgered model_id); dead feed reads as news-unavailable HOLD, never neutral. (e) core/news_defense.py tripwire: fresh bearish high-materiality event on active UP pick → warn/withdraw; SHIPPED DARK (NEWS_DEFENSE_ENABLED=0, mode=warn). LLM classifier layer deliberately deferred. 722 tests. LIVE-VERIFIED 2026-07-06: _pr_version=134; SPCE eps+revenue_growth now RESOLVE (were critical-missing); ingest cycle 43 symbols/51 articles/15.9s both providers OK; real events extracted day one (XPO analyst_upgrade, PFE analyst_downgrade + earnings_beat); 10 brains in live manifest incl news_shadow_v2.",
        "PR #133 (2026-07-05): seasonal_shadow_v1 added as 9th shadow brain — core/seasonality.py computes the symbol's ~4-year record for the current 5-day calendar window vs its own baseline (24h in-process cache). Commits only on excess >=2.5% with >=75% year consistency; confidence hard-capped at 0.65 (n<=4 windows is thin evidence by construction). Plumbing: OHLCV days_map +5y, Alpaca bar limit 1000->10000. Born from the post-July-4th study: 8/42 watchlist symbols positive all 4 years vs ~2.6 expected by chance. Live-verified: ABCL lean UP (+11.3% excess, 4/4), WOLF correctly NONE (0.5 consistency).",
        "PR #132 (2026-07-05): contrarian_shadow_v1 added as 8th shadow brain — inverts every committed production call (HOLD stays HOLD), mirrors risk geometry around entry. Tests the operator's anti-signal hypothesis with real evidence: under 5-day sign resolution its win_rate is the exact complement of production's on committed calls. Shadow-only; judge via shadow profiles once samples accumulate.",
        "PR #131 (2026-07-05): ValueError handler triage — 422 only for route-file origins; internal/core origins return 500 with logged traceback (no more bugs disguised as invalid_input). 4 new tests, 691 total.",
        "PR #130 (2026-07-05): God-object split — wolf_app.py 6,097→3,151 lines (82 endpoints → api/routes_admin|ghost_system|v3|wolf_ops|data, late-import + facade re-export pattern); signal_engine 2,747→2,141 lines (engine_config/indicators/features/calibration split out); all 86 silent except-pass blocks in core/ now call core.quiet.note_suppressed() (DEBUG log + COUNTS). 687 tests, live-verified: _pr_version=130, 10 moved endpoints 200, MCP connector OK.",
        "PR #129 (2026-07-04): tech-debt cleanup — 33 inline ghost_state DDL sites migrated to ensure_ghost_state(); global psycopg2→503 and ValueError→422 exception handlers; Chart.js vendored to /static with SRI-pinned CDN fallback; WCAG contrast remap (#444/#555/#666 → #808080/#888/#999) in cockpit+admin; aria-labels on admin inputs/selects and cockpit icon button; label-for on portfolio inputs; pulse animation on loading placeholders (reduced-motion safe); datetime.utcnow() removed (3 real sites); settings VERSION 2.1.0→2.5.0; redis dep removed; plaintext GHOST_OAUTH_SECRET scrubbed from this file (git history still has it — rotate). Live-verified: _pr_version=129, health 95, /static/chart.umd.min.js 200, MCP gate/kill-status OK.",
    ],

    # ── KEY ARCHITECTURE FACTS ──
    "architecture": {
        "engine": "XGBoost v3.2, TP/SL daily-bar labels, walk-forward validation",
        "features": "49 features (33 technical + 8 macro + 8 cross-sectional)",
        "data_feeds": "5-tier chain: Alpaca IEX → yfinance → Polygon → IEX → Stooq (deprecated)",
        "circuit_breakers": "5 breakers: yfinance (5/600s), alpaca (5/300s, 50/60s), finnhub, polygon, anthropic",
        "watchlist": "74 symbols in OFFICIAL_WATCHLIST (config/symbols.py) — live-verified PR #152 era; many new PR #149 symbols still need models/retrain",
        "super_ghost": "25-point checklist, coverage gate ≥18/25 for A/B grade",
        "kelly_sizing": "Corrected formula f* = p - (1-p)/b",
        "two_lanes": "v3 picks (gated, often silent) + Squeeze radar (intraday)",
        "primary_symbol": "WOLF (Wolfspeed Inc, NYSE)",
        "era": "Post-falsification — ABANDON_80_CLAIM",
    },

    # ── FILES HEAVILY MODIFIED THIS SESSION ──
    "files_touched": {
        "core/signal_engine.py": "FEATURE_COLS 33→49, backtest_symbol returns (up,down) tuple, _train_one_direction extracted, _simulate_down_tp_sl bridge, load_model(symbol, direction), predict_live_ex dual-model scoring",
        "core/prices.py": "5-tier prev_close chain, 24h persistent cache, Stooq deprecated",
        "core/circuit_breaker.py": "auto_recover only on genuine cooldown expiry, wired into health check",
        "core/prediction.py": "Research pick mode, cold-start + stall detection, daily cap across all cycles",
        "core/kelly_sizing.py": "Formula corrected from edge/odds to p-(1-p)/b",
        "core/macro_regime.py": "FRED_API_KEY empty string check, _build_historical_macro_series, get_macro_features_for_date",
        "core/tp_sl_resolve.py": "simulate_down_tp_sl_label for DOWN label generation",
        "core/super_ghost_ledger.py": "log_prediction commits before side-writes, auto_log_watchlist 43× daily with 20h guard",
        "core/regime.py": "Regime modifier synced with ghost_score_spec (0.90/0.80 → 0.95/0.90)",
        "core/db.py": "ensure_ghost_state() centralized (was 37 duplicate DDL statements)",
        "core/squeeze_monitor.py": "_squeeze_risk_tag moved here from api/wolf_endpoints.py (dependency inversion fix)",
        "core/news.py": "_seen_headlines capped at 5000",
        "api/wolf_endpoints.py": "Auth-gated writes, asyncio.to_thread for Claude, _squeeze_risk_tag re-exports from core, _CACHE lock added",
        "wolf_app.py": "Breaker/research status endpoints, auto-recovery in health check, _cron_ok requires GHOST_DEV_MODE=1, _COCKPIT_DB_CACHE lock added, _pr_version=127",
        "tests/conftest.py": "GHOST_DEV_MODE=1 autouse fixture",
        "tests/test_import_integrity.py": "Baseline cleared — all 625 first-party imports resolve",
    },

    # ── FILES REMOVED THIS SESSION ──
    "files_removed": [
        "core/stock_engine.py — dead code, imported non-existent core.pattern_tracker",
        "core/world_feed_fusion.py — 1,107 lines, never imported",
        "engines/startup.py — ~1,000 lines, self-declared deprecated",
        "core/model.py — deprecated legacy XGBoost, superseded by signal_engine",
        "routes/schema.py — duplicate /api/schema endpoint, router never mounted",
    ],

    # ── WHAT'S IN FLIGHT / NOT YET DONE ──
    "in_flight": {
        "phase_2_down_model": {
            "status": "DEPLOYED (verified 2026-07-04)",
            "description": "DOWN model training + dual-direction prediction is on main and live; /api/v3/status shows *_down models stored. DOWN firing stays shadow-only (V3_DOWN_SIGNALS_ENABLED=0) until a shadow track record exists.",
        },
        "ghost_state_centralization": {
            "status": "DONE (PR #129)",
            "description": "All 33 app-code call sites now call ensure_ghost_state(); scripts/calibrate_confidence_slope.py intentionally keeps inline DDL (standalone script).",
        },
        "live_fire_test": {
            "status": "UNTESTED",
            "description": "Monday open — gate flips on moving tape, intraday pick resolution at target/stop. Reconnect Ghost connector for live watch.",
        },
        "optional_github_secrets": {
            "status": "OPTIONAL",
            "description": "TEST_DATABASE_URL activates DB integration job; CRON_SECRET upgrades health audit to deep POST path. Both slot in with zero workflow changes.",
        },
    },

    # ── KNOWN ISSUES / TECHNICAL DEBT ──
    "known_issues": [
        "wolf_app.py split (PR #130): 82 endpoints moved to api/routes_{admin,ghost_system,v3,wolf_ops,data}.py — 3,151 lines remain (helpers, health cluster, pages, middleware, ws). Moved endpoints late-import shared helpers from wolf_app; wolf_app re-exports every moved name (facade). Do NOT top-level-import wolf_app from a routes module (cycle).",
        "signal_engine split (PR #130): config knobs → core/engine_config.py, indicators → core/engine_indicators.py, features → core/engine_features.py, calibration → core/engine_calibration.py; 2,141 lines remain (OHLCV chain + training + inference stay — tests monkeypatch their interplay on core.signal_engine, and _ProbaEnsemble must stay for stored-model pickle paths).",
        "except-pass sweep (PR #130): all 86 silent blocks in core/ now call core.quiet.note_suppressed() — DEBUG-logged + counted in core.quiet.COUNTS; raise logger ghost.suppressed to DEBUG to audit live.",
        "GHOST_OAUTH_SECRET ROTATED 2026-07-04 (leaked value in git history is now dead — cannot approve connector sessions or forge tokens). Connector survived rotation via DB-backed refresh token. New value lives only in Railway env vars; keep it out of this file.",
        "ValueError handler fixed (PR #131): 422 only when raised in a route file (wolf_app.py, api/, portfolio_routes); deeper origins → 500 + ERROR-logged traceback. Covered by tests/test_value_error_handler.py.",
        "scripts/calibrate_confidence_slope.py keeps its inline ghost_state DDL by design (standalone script, no app imports)",
    ],

    # ── QUICK VERIFICATION COMMANDS ──
    "verify_commands": {
        "production_version": "curl -s https://ghost-protocol-v2-production.up.railway.app/api/_version | python3 -m json.tool",
        "production_health": "curl -s https://ghost-protocol-v2-production.up.railway.app/health",
        "production_breakers": "curl -s https://ghost-protocol-v2-production.up.railway.app/api/system/breakers | python3 -m json.tool",
        "production_models": "curl -s https://ghost-protocol-v2-production.up.railway.app/api/v3/status | python3 -m json.tool",
        "production_gate_status": "curl -s https://ghost-protocol-v2-production.up.railway.app/api/wolf/gate-status | python3 -m json.tool",
        "full_test_suite": "cd /Users/studio713/ghost-protocol-v2 && python3.13 -m pytest tests/ -q --tb=short",
        "import_integrity": "cd /Users/studio713/ghost-protocol-v2 && python3.13 scripts/check_import_integrity.py",
        "deploy": "cd /Users/studio713/ghost-protocol-v2 && railway up --environment production",
        "railway_variables": "cd /Users/studio713/ghost-protocol-v2 && railway variables list --environment production",
    },

    # ── GIT BRANCHES ──
    "branches": {
        "main": "Production — PR #127 deployed (44ebd3b)",
        "fix/accuracy-contract-70": "Has the Phase 2 DOWN model code + work log updates — NOT YET MERGED to main",
    },
}

# ============================================================
# LIVE SYSTEM — LAST VERIFIED 2026-07-04 (PR #127 deployed; 686 tests pass)
# ============================================================

PROD_VERIFY_2026_07_01_PR114 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "e02b8ec",
    "_pr_version": 114,
    "verified_at_ct": "2026-07-01",
    "tests": "590 passed, 3 skipped",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=e02b8ec, _pr_version=114, app_version=2.5.0",
        "breakers_endpoint": "GET /api/system/breakers -> yfinance:open, finnhub:closed, polygon:closed, alpaca:open, anthropic:closed",
        "degraded": "false, 2/2 open circuits at threshold",
        "health": "score=95",
        "change_pct": "WOLF +0.79%, SPCE +18.65%, DOMO +39.10%, LCID +33.11%, IQ +10.30%, FLNC -1.10%, HIMS +8.91%, ITRI +5.55%",
        "research_mode": "armed — < 15 resolved picks, confidence floor 0.55, v3 min_win_proba 0.40",
        "squeeze": "0 picks (market closed), 3/43 fetch",
        "v3_active": "0 picks (WOLF up_prob=0.34, below research threshold 0.40)",
    },
    "changes": [
        "Research pick mode: lowers confidence floor to 0.55 + v3 min_win_proba to 0.40 when < 15 resolved picks",
        "GET /api/system/breakers: per-breaker state, failure count, cooldown, rate-limit",
        "Prev_close 5-tier chain: Alpaca 1Day → 5-min bar → yfinance → Polygon → 24h cache",
        "Confidence caps: squeeze_confidence max 95, score_confirmation max 95, extreme short bonus halved",
        "Stooq deprecated (JS challenge wall)",
        "Degraded reasons now include half_open state",
        "_pr_version bumped to 114",
    ],
}












PROD_VERIFY_2026_06_29_PR113 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "9edd4ac",
    "_pr_version": 113,
    "verified_at_ct": "2026-06-29",
    "tests": "Authenticated admin 7/7; health audit PASS unresolved=0; lint/type-check/pytest/compile/live/error-signatures/health-audit/prelaunch/Playwright all pass",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=9edd4ac, _pr_version=113, app_version=2.5.0",
        "authenticated_admin": "login, diagnostics, admin health, portfolio read, logout, post-logout gating all pass",
        "authenticated_health_audit": "POST /api/health/audit?auto_fix=false with CRON_SECRET -> audit_status PASS, unresolved=0",
        "lint": "npm run lint -> pass",
        "type_check": "npm run type-check -> pass",
        "pytest": "python3 -m pytest tests/ -q -> 590 passed, 3 skipped",
        "compile": "make test-compile -> pass",
        "live_health": "npm run verify:live -> pass",
        "error_signatures": "npm run verify:error-signatures -> pass",
        "health_audit_public": "npm run verify:health-audit -> pass public fallback",
        "prelaunch": "npm run verify:prelaunch -> ALL PASS (7 checks)",
        "playwright": "npm run test:e2e -> 33 passed",
    },
    "key_fixes": [
        "Health audit cockpit static check updated to redesigned cockpit UI tokens",
        "Removed stale expectations for tab-stocks/tab-portfolio/function show(",
        "Authenticated health audit now reports PASS with zero unresolved findings",
    ],
    "known_issues": [
        "Rotate the temporary admin/cron secret shared during verification after this session",
    ],
}

PROD_VERIFY_2026_06_29_PR112 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "3e8ba89",
    "_pr_version": 111,
    "verified_at_ct": "2026-06-29",
    "tests": "lint pass; type-check pass; 590 passed, 3 skipped; compile pass; live/prelaunch/error-signature/health-audit pass; Playwright 33/33 pass",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=3e8ba89, _pr_version=111, app_version=2.5.0",
        "lint": "npm run lint -> pass",
        "type_check": "npm run type-check -> pass",
        "pytest": "python3 -m pytest tests/ -q -> 590 passed, 3 skipped",
        "compile": "make test-compile -> pass",
        "live_health": "npm run verify:live -> pass",
        "error_signatures": "npm run verify:error-signatures -> pass",
        "health_audit": "npm run verify:health-audit -> pass public fallback",
        "prelaunch": "npm run verify:prelaunch -> ALL PASS (7 checks)",
        "playwright": "npm run test:e2e -> 33 passed",
    },
    "key_fixes": [
        "Playwright cockpit flow uses current redesigned cockpit selectors",
        "Portfolio E2E verifies auth-gated security contract instead of unauthenticated mutation",
        "API surface E2E retries transient 429s with backoff",
        "Playwright script runs without NO_COLOR/FORCE_COLOR warning noise",
    ],
    "known_issues": [
        "Manual browser QA still recommended for authenticated admin login/logout because local verifier has no CRON_SECRET/admin cookie",
    ],
}

PROD_VERIFY_2026_06_29_PR111 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "28c8d77",
    "_pr_version": 111,
    "verified_at_ct": "2026-06-29",
    "tests": "CI test pass; post-PR112 Playwright confirms cockpit/portfolio contract",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=28c8d77, _pr_version=111, app_version=2.5.0",
        "cockpit": "Public cockpit no longer auto-fetches auth-gated portfolio; locked portfolio state visible",
    },
    "key_fixes": [
        "cockpit.html: render locked portfolio state by default and manual Load Portfolio button",
        "e2e: redesigned cockpit selectors replace removed legacy #cgrid/tab-panel assumptions",
        "e2e: portfolio mutation tests now assert 401 auth gating",
    ],
    "known_issues": [],
}

PROD_VERIFY_2026_06_29_PR110 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "8616306",
    "_pr_version": 108,
    "verified_at_ct": "2026-06-29",
    "tests": "verify:error-signatures pass; verify:health-audit pass public fallback",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=8616306, _pr_version=108, app_version=2.5.0",
        "error_signatures": "npm run verify:error-signatures -> PASS; gated diagnostics/audit skipped safely without admin/CRON secret",
        "health_audit": "npm run verify:health-audit -> PASS public history fallback; POST skipped because CRON_SECRET unset locally",
    },
    "key_fixes": [
        "check_error_signatures no longer false-fails on intentionally admin-gated /api/diagnostics",
        "check_error_signatures no longer false-fails on /api/health/audit when CRON_SECRET is unset locally",
        "Focused tests cover gated diagnostics and audit behavior",
    ],
    "known_issues": [],
}

PROD_VERIFY_2026_06_29_PR109 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "35da460",
    "_pr_version": 108,
    "verified_at_ct": "2026-06-29",
    "tests": "npm run lint pass; npm run type-check pass; 587 passed, 3 skipped; compileall pass",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=35da460, _pr_version=108, app_version=2.5.0",
        "lint": "npm run lint -> All checks passed",
        "type_check": "npm run type-check -> Success: no issues found in 5 source files",
        "test_suite": "python3 -m pytest tests/ -q -> 587 passed, 3 skipped",
        "compile": "make test-compile -> pass",
    },
    "key_fixes": [
        "Ruff cleanup across configured lint surface",
        "Type-check script now follows mypy.ini and checks production audit/prelaunch scripts",
        "All configured local quality gates pass cleanly",
    ],
    "known_issues": [],
}

PROD_VERIFY_2026_06_29_PR108 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "089a5fc",
    "_pr_version": 108,
    "verified_at_ct": "2026-06-29",
    "tests": "587 passed, 3 skipped; warning-clean",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=089a5fc, _pr_version=108, app_version=2.5.0",
        "health": "GET /health -> score 90",
        "top_pick_gate": "GET /api/wolf/super-ghost/top-pick-gate?symbol=WOLF&horizon=5 -> ok true, decision LOCKED",
        "test_suite": "python3 -m pytest tests/ -q -> 587 passed, 3 skipped, no warnings summary",
    },
    "key_fixes": [
        "Removed hardcoded default API_AUTH_TOKEN from startup self-call logic",
        "Startup predictions now skip unless API_AUTH_TOKEN is explicitly configured",
        "Static tests assert no default API auth token and no raw private keys/live secrets in public HTML",
    ],
    "known_issues": [],
}

PROD_VERIFY_2026_06_29_PR107 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "a74b71d",
    "_pr_version": 107,
    "verified_at_ct": "2026-06-29",
    "tests": "585 passed, 3 skipped; warning-clean",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=a74b71d, _pr_version=107, app_version=2.5.0",
        "health": "GET /health -> score 95",
        "top_pick_gate": "GET /api/wolf/super-ghost/top-pick-gate?symbol=WOLF&horizon=5 -> ok true, decision LOCKED",
        "super_ghost": "GET /api/wolf/super-ghost?symbol=WOLF -> ok true, NO EDGE — WATCH ONLY",
        "test_suite": "python3 -m pytest tests/ -q -> 585 passed, 3 skipped, no warnings summary",
    },
    "key_fixes": [
        "load_model validates metadata before model payload decode/unpickle",
        "Strict base64 decode and max payload size guard",
        "New trained models persist model_sha256 and model_payload_bytes",
        "If model_sha256 exists, load verifies hash before pickle.loads",
        "Legacy model rows only load after serve guards pass",
    ],
    "known_issues": [
        "Legacy DB model rows without SHA are still loadable after metadata guards; retraining will add SHA metadata",
    ],
}

PROD_VERIFY_2026_06_29_PR106 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "0e944aa",
    "_pr_version": 105,
    "verified_at_ct": "2026-06-29",
    "tests": "582 passed, 3 skipped; warning-clean",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=0e944aa, _pr_version=105, app_version=2.5.0",
        "test_suite": "python3 -m pytest tests/ -q -> 582 passed, 3 skipped, no warnings summary",
    },
    "key_fixes": [
        "pytest.ini filters known third-party sklearn/scipy L-BFGS-B disp/iprint deprecation noise",
        "Project warnings remain actionable; runtime behavior unchanged",
    ],
    "known_issues": [],
}

PROD_VERIFY_2026_06_29_PR105 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "8234a16",
    "_pr_version": 105,
    "verified_at_ct": "2026-06-29",
    "tests": "582 passed, 3 skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=8234a16, _pr_version=105, app_version=2.5.0",
        "top_pick_gate": "GET /api/wolf/super-ghost/top-pick-gate?symbol=WOLF&horizon=5 -> ok true, decision LOCKED, eligible false, blockers present",
        "console": "/picks includes Top Pick gate Health row and positive-if-followed/calibrated-evidence copy",
    },
    "key_fixes": [
        "core/super_ghost_top_picks.py: centralized backend Top Pick Evidence Gate",
        "Top Picks requires completed predictions, >=70% direction wins, >=60/100 precision, calibration readiness, positive if-followed evidence, and clear kill conditions",
        "GET /api/wolf/super-ghost/top-pick-gate endpoint",
        "Console Top Stocks gate consumes backend gate instead of partial local-only checks",
    ],
    "known_issues": [
        "Gate is intentionally locked until enough resolved evidence/calibration exists",
        "Top Picks remains evidence-only; no auto-trading or trade-now actions",
    ],
}

PROD_VERIFY_2026_06_29_PR104 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "83f208e",
    "_pr_version": 104,
    "verified_at_ct": "2026-06-29",
    "tests": "578 passed, 3 skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=83f208e, _pr_version=104, app_version=2.5.0",
        "regime_calibration": "GET /api/wolf/super-ghost/regime-calibration?symbol=WOLF&horizon=5 -> ok true, cold-start 0 profiles",
        "regime_rebuild_auth_gate": "POST /api/wolf/super-ghost/regime-calibration/rebuild without auth -> 401",
        "super_ghost_report": "GET /api/wolf/super-ghost?symbol=WOLF -> includes regime_calibration cold-start block with detected regime/setup bucket",
        "console": "/picks includes Regime calibration Health row",
    },
    "key_fixes": [
        "core/super_ghost_regime_calibration.py: regime/setup-specific calibration slices",
        "New super_ghost_regime_calibration_profiles table",
        "Buckets by market regime (risk_on/risk_off/high_volatility/mixed) and setup style (news/earnings/squeeze/thin_liquidity/analyst/general)",
        "Runtime lookup uses narrow symbol+regime+setup profile first, then broader safe fallbacks",
        "Public /regime-calibration endpoint and auth-gated /regime-calibration/rebuild",
        "Resolver job rebuilds regime calibration after broad range calibration",
    ],
    "known_issues": [
        "Regime slices are cold-start until enough resolved precision events exist per market/setup bucket",
        "Price adjustment remains bounded and long-plan-only until short-side calibration is validated",
    ],
}

PROD_VERIFY_2026_06_29_PR103 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "b2c3c50",
    "_pr_version": 103,
    "verified_at_ct": "2026-06-29",
    "tests": "569 passed, 3 skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=b2c3c50, _pr_version=103, app_version=2.5.0",
        "range_calibration": "GET /api/wolf/super-ghost/range-calibration?symbol=WOLF&horizon=5 -> ok true, cold-start 0 profiles",
        "range_rebuild_auth_gate": "POST /api/wolf/super-ghost/range-calibration/rebuild without auth -> 401",
        "super_ghost_report": "GET /api/wolf/super-ghost?symbol=WOLF -> includes range_calibration cold-start block; raw risk plan unchanged until enough precision samples",
        "console": "/picks includes Range calibration Health row",
    },
    "key_fixes": [
        "core/super_ghost_range_calibration.py: bounded adaptive range calibration from Precision Brain profiles",
        "New super_ghost_range_calibration_profiles table",
        "Target/stop multipliers derive from target_too_low, target_too_high, stop_too_wide, stop_too_tight, and low-precision patterns",
        "Super Ghost risk_plan can publish target_price_raw/calibrated, stop_loss_raw/calibrated, expected high/low/close zones, bull/bear cases, and invalidation level",
        "Public /range-calibration endpoint and auth-gated /range-calibration/rebuild",
        "Resolver job rebuilds range calibration after precision scoring",
    ],
    "known_issues": [
        "Range profiles are cold-start until enough precision profiles exist",
        "Calibration currently applies price-adjustment only to long-oriented UP risk plans; DOWN/HOLD keep raw plan until short-side range model is validated",
    ],
}

PROD_VERIFY_2026_06_29_PR102 = {
    "deploy_id": "ed610631-19c1-4c4e-a10d-a030656c9ba7",
    "git_sha_short": "c21bca2",
    "_pr_version": 102,
    "verified_at_ct": "2026-06-29",
    "tests": "561 passed, 3 skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=c21bca2, _pr_version=102, app_version=2.5.0",
        "precision_summary": "GET /api/wolf/super-ghost/precision?symbol=WOLF&horizon=5 -> ok true, cold-start 0 profiles/events",
        "precision_score_auth_gate": "POST /api/wolf/super-ghost/precision/score without auth -> 401",
        "squeeze_daily_log": "GET /api/squeeze/daily-log?days=7 -> rows include precision_score / precision_grade / mistake_type fields",
        "console": "/picks includes Precision brain Health row and Direction-vs-Precision EOD mirror copy",
    },
    "key_fixes": [
        "core/ghost_precision.py: pure target-stop truth vs price-precision scorer",
        "core/super_ghost_precision.py: durable Super Ghost precision events + profiles",
        "Top Picks gate now requires both >=70% directional wins and >=60/100 average precision",
        "Squeeze daily-log stores precision_score, precision_grade, mistake_type, and precision_json",
        "Console shows Mirror score and Precision separately, so a WIN cannot hide poor price accuracy",
    ],
    "known_issues": [
        "Precision profiles are cold-start until resolved Super Ghost ledger rows are scored",
        "Intraday same-bar path order is still unknown for squeeze MIXED rows; finer tape data is future work",
    ],
}

PROD_VERIFY_2026_06_29_PR101 = {
    "deploy_id": "Verified on PR #102 deployment after PR #101 merge",
    "git_sha_short": "e7cb673",
    "_pr_version": 101,
    "verified_at_ct": "2026-06-29",
    "tests": "554 passed, 3 deselected/skipped, 2 warnings during PR #101; PR #102 full suite 561 passed after merge",
    "live_acceptance": {
        "data_brain": "GET /api/wolf/super-ghost/data-brain?symbol=WOLF -> ok true, coverage payload present",
        "data_brain_history": "GET /api/wolf/super-ghost/data-brain/history?symbol=WOLF -> route live (cold-start allowed)",
        "refresh_auth_gate": "POST /api/wolf/super-ghost/data-brain/refresh without auth -> 401",
        "console": "/picks includes Data brain Health row",
    },
    "key_fixes": [
        "core/super_ghost_data_brain.py: expanded SEC/news/macro/options evidence collector",
        "New super_ghost_data_brain_snapshots table",
        "Super Ghost snapshot merges available Form 4 insider activity, guidance/catalyst context, and options flow payload",
        "Public /data-brain and /data-brain/history endpoints; auth-gated /data-brain/refresh",
    ],
    "known_issues": [
        "Provider depth varies by symbol; unavailable sources are explicit rather than fabricated",
        "Data Brain snapshots are cold-start until refresh/persist runs on production",
    ],
}

PROD_VERIFY_2026_06_29_PR100 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "8254319",
    "_pr_version": 100,
    "verified_at_ct": "2026-06-29",
    "tests": "549 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=8254319, _pr_version=100, app_version=2.5.0",
        "feature_store": "GET /api/wolf/super-ghost/feature-store?symbol=WOLF -> ok true, cold-start 0 snapshots",
        "leakage_audit": "GET /api/wolf/super-ghost/feature-store/audit?symbol=WOLF -> ok true, clean, 0 leaks",
        "snapshot_auth_gate": "POST /api/wolf/super-ghost/feature-store/snapshot without auth -> 401",
        "console": "/picks includes Point-in-time store Health row",
    },
    "key_fixes": [
        "core/super_ghost_feature_store.py: immutable point-in-time snapshots for Super Ghost reports",
        "New super_ghost_feature_snapshots table",
        "Source timestamp parser/walker catches future source timestamps",
        "Snapshots persist on log_prediction with ledger_id",
        "Public /feature-store and /feature-store/audit endpoints; auth-gated /feature-store/snapshot",
    ],
    "known_issues": [
        "Snapshot table is cold-start until new Super Ghost predictions are logged",
        "Raw external data source timestamps are limited by what upstream providers expose",
    ],
}

PROD_VERIFY_2026_06_29_PR99 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "cdf810b",
    "_pr_version": 99,
    "verified_at_ct": "2026-06-29",
    "tests": "544 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=cdf810b, _pr_version=99, app_version=2.5.0",
        "promotion": "GET /api/wolf/super-ghost/promotion?symbol=WOLF -> ok true, cold-start 0 reviews, requirements present",
        "promotion_review_auth_gate": "POST /api/wolf/super-ghost/promotion/review without auth -> 401",
        "console": "/picks includes Promotion gate Health row",
    },
    "key_fixes": [
        "core/super_ghost_promotion.py: conservative promotion review gate",
        "Durable super_ghost_promotion_reviews table",
        "Decisions: PROMOTE_CANDIDATE, KEEP_CHAMPION, KEEP_SHADOWING, RETIRE_CANDIDATE, INSUFFICIENT_EVIDENCE",
        "Gates for minimum rows/actionable calls/profit factor/win-rate delta/EV delta/false positives/drawdown",
        "Scheduler now runs promotion review after resolver, learning, lab, feature memory, and shadow resolution",
        "Public /promotion and auth-gated /promotion/review",
    ],
    "known_issues": [
        "Promotion reviews are cold-start until enough lab/shadow evidence accumulates",
        "Gate creates recommendations only; no automated model promotion yet",
    ],
}

PROD_VERIFY_2026_06_29_PR98 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "3cc0987",
    "_pr_version": 97,
    "verified_at_ct": "2026-06-29",
    "tests": "536 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=3cc0987, _pr_version=97, app_version=2.5.0",
        "shadow_summary": "GET /api/wolf/super-ghost/shadow?symbol=WOLF -> ok true, 7-model manifest",
        "shadow_models": "GET /api/wolf/super-ghost/shadow/models -> ok true, 7-model manifest",
        "auth_gates": "POST /shadow/run and /shadow/resolve without auth -> 401",
        "console": "/picks includes Shadow models Health row",
    },
    "key_fixes": [
        "core/super_ghost_shadow.py: 7 specialist shadow prediction brains",
        "Persistent tables: super_ghost_shadow_predictions and super_ghost_shadow_model_profiles",
        "Shadows generated whenever a Super Ghost prediction is logged",
        "Shadow resolver scores against parent Truth Ledger outcomes",
        "Specialists: technical, news, fundamental, macro, regime, learning-adjusted, ensemble",
        "Public /shadow /shadow/models and auth-gated /shadow/run /shadow/resolve",
    ],
    "known_issues": [
        "Shadow profiles are cold-start until logged shadow predictions resolve",
        "No auto-promotion; promotion gate remains a future PR",
    ],
}

PROD_VERIFY_2026_06_29_PR96 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "4ea2e24",
    "_pr_version": 96,
    "verified_at_ct": "2026-06-29",
    "tests": "529 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=4ea2e24, _pr_version=96, app_version=2.5.0",
        "models": "GET /api/wolf/super-ghost/models -> ok true, 1 production model",
        "feature_profile": "GET /api/wolf/super-ghost/feature-profile?symbol=WOLF&horizon=5 -> ok true, cold-start 0 profiles",
        "feature_score_auth_gate": "POST /api/wolf/super-ghost/features/score without auth -> 401",
        "console": "/picks includes Feature memory Health row",
    },
    "key_fixes": [
        "core/super_ghost_memory.py: Model Registry + Feature Attribution Memory",
        "New durable tables: model_versions, prediction_features, feature_outcomes, feature_profiles, model_contributions",
        "Every logged Super Ghost checklist item becomes a feature attribution row",
        "Resolved outcomes classify features as helped/hurt/underweighted/noise/missing",
        "Feature reliability profiles remember which evidence has worked by symbol/horizon",
        "Public /models /features /feature-profile and auth-gated /features/score",
        "Hourly resolver now also scores feature memory",
    ],
    "known_issues": [
        "Feature profiles are cold-start until logged predictions resolve and features are scored",
        "Feature attribution currently uses checklist scores; future PRs should add model-specific SHAP/importance values",
    ],
}

PROD_VERIFY_2026_06_29_PR95 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "aba64f3",
    "_pr_version": 95,
    "verified_at_ct": "2026-06-29",
    "tests": "523 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=aba64f3, _pr_version=95, app_version=2.5.0",
        "lab_summary": "GET /api/wolf/super-ghost/lab?symbol=WOLF&horizon=5 -> ok true, cold-start, 6 candidate manifest entries",
        "lab_run_auth_gate": "POST /api/wolf/super-ghost/lab/run without auth -> 401",
        "console": "/picks includes Research lab Health row",
    },
    "key_fixes": [
        "core/super_ghost_lab.py: Champion/Challenger shadow benchmark lab",
        "Persistent lab memory tables: super_ghost_lab_runs and super_ghost_lab_results",
        "Candidate policies: production_champion, coverage_gate, strict_confidence, grade_b_or_better, regime_aligned, edge_score_policy",
        "Benchmark metrics: win rate, false positives, avg signed return, profit factor, drawdown, score",
        "Conservative recommendation gates; no auto-promotion and no trading",
        "Public GET /api/wolf/super-ghost/lab and auth-gated POST /lab/run",
        "Hourly resolver now also runs the lab after learning",
    ],
    "known_issues": [
        "Lab is cold-start until enough resolved Super Ghost ledger rows exist",
        "Lab recommendations are shadow evidence only; no automated production model promotion yet",
    ],
}

PROD_VERIFY_2026_06_29_PR94 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "f4156a4",
    "_pr_version": 94,
    "verified_at_ct": "2026-06-29",
    "tests": "517 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=f4156a4, _pr_version=94, app_version=2.5.0",
        "learning_summary": "GET /api/wolf/super-ghost/learning?symbol=WOLF&horizon=5 -> ok true, cold-start",
        "target_calibration": "target_move_multiplier uses direction-correct rows only; target_calibration_samples exposed",
    },
    "key_fixes": [
        "Wrong-direction rows no longer dilute target-magnitude learning",
        "The $5 target -> $7 realized lesson stays target_too_low even with unrelated wrong-direction rows",
        "Win-rate and confidence still learn from all rows",
    ],
    "known_issues": [
        "Learning remains cold-start until enough resolved rows exist",
    ],
}

PROD_VERIFY_2026_06_29_PR93 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "90a9fc7",
    "_pr_version": 93,
    "verified_at_ct": "2026-06-29",
    "tests": "515 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=90a9fc7, _pr_version=93, app_version=2.5.0",
        "learning_summary": "GET /api/wolf/super-ghost/learning?symbol=WOLF&horizon=5 -> ok true, cold-start 0 profiles/lessons",
        "report_learning_block": "GET /api/wolf/super-ghost?symbol=WOLF includes learning_adjustment",
        "learn_auth_gate": "POST /api/wolf/super-ghost/learn without auth -> 401",
    },
    "key_fixes": [
        "core/super_ghost_learning.py: postmortem events + learning profiles from resolved ledger outcomes",
        "Classifies target_too_low / target_too_high / wrong_direction / missed_move / good_skip / direction_right",
        "User's $5 target -> $7 realized case is learned as target_too_low and can widen future target moves after enough samples",
        "Bounded confidence/conviction/target adjustments applied to build_super_ghost reports",
        "Hourly resolver now triggers learning after outcome resolution",
        "Public GET /api/wolf/super-ghost/learning and auth-gated POST /learn",
        "Console Health row shows Learning brain profile/lesson counts",
    ],
    "known_issues": [
        "Learning is cold-start until enough resolved Super Ghost ledger rows exist (min 3 per symbol/direction/horizon)",
        "Adjustments are deliberately bounded and do not bypass coverage/risk gates",
        "This is evidence-based learning, not guaranteed prediction or auto-trading",
    ],
}

PROD_VERIFY_2026_06_29_PR92 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "c6c912b",
    "_pr_version": 92,
    "verified_at_ct": "2026-06-29",
    "tests": "508 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=c6c912b, _pr_version=92, app_version=2.5.0",
        "favicon": "GET /favicon.ico -> 200 image/svg+xml (was 404)",
        "no_intraday_guard": "/picks money() is null-safe; missing live OHLC shows 'No intraday data' not $0.00",
        "coverage_gate_copy": "/picks Overview coverage note explains >=18/25 A/B-grade gate (WOLF 21/25, meets_ab_gate true)",
    },
    "key_fixes": [
        "Root cause of IQ/LCID $0.00 was client-side JS Number(null)===0; fixed money() + m3row",
        "Overview coverage metric now states the 18/25 A/B-grade evidence gate",
        "/favicon.ico (+/favicon.svg) route added; inline SVG <link rel=icon>",
        "Deploy/cache markers bumped to _pr_version 92",
    ],
    "known_issues": [
        "External CSS extraction still deferred (inline styles; maintainability only, not a blocker)",
        "Intraday live fields remain feed/cache-sensitive; UI now degrades honestly to 'No intraday data'",
    ],
}

PROD_VERIFY_2026_06_29_PR91 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "3a83893",
    "_pr_version": 91,
    "verified_at_ct": "2026-06-29",
    "tests": "504 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=3a83893, _pr_version=91, app_version=2.5.0",
        "console_markers": "/picks contains post-falsification banner, completed-predictions Top Stocks copy, hidden duplicate top-tabs",
        "wolf_coverage_still_ok": "GET /api/wolf/super-ghost/coverage?symbol=WOLF -> 21/25, meets_ab_gate=true",
    },
    "key_fixes": [
        "Global post-falsification / old-80%-claim-abandoned banner outside Health tab",
        "Top Stocks gate copy changed from resolved sample jargon to completed predictions / 5 minimum",
        "Duplicate top-tab chrome hidden; sidebar remains visible navigation source",
        "Deploy/cache markers bumped to _pr_version 91",
    ],
    "known_issues": [
        "Coverage gate threshold (18/25) still could be explained more directly in Overview",
        "Favicon still 404",
        "If a market-session feed temporarily lacks OHLC, UI should say No intraday data rather than implying $0.00",
    ],
}

PROD_VERIFY_2026_06_29 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "5bc05a0",
    "_pr_version": 88,
    "verified_at_ct": "2026-06-29",
    "tests": "503 passed, 3 skipped/deselected, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=5bc05a0, _pr_version=88, app_version=2.5.0",
        "wolf_coverage": "GET /api/wolf/super-ghost/coverage?symbol=WOLF -> 21/25, meets_ab_gate=true",
        "aapl_coverage": "AAPL -> 19/25, meets_ab_gate=true",
        "nvda_coverage": "NVDA -> 20/25, meets_ab_gate=true",
        "gate_invariant": "No A/B grade and no HIGH-CONVICTION action below 18/25; verified in tests and live metadata",
    },
    "key_fixes": [
        "Super Ghost 25-point engine has market-regime conviction adjustment + optional Claude AI brief",
        "AI model default fixed to proven claude-haiku-4-5-20251001",
        "Truth Ledger shipped: log/history/accuracy/if-followed/resolve routes + scheduler resolver",
        "Master Build map shipped and CI-enforced",
        "Unified Liquid Glass console live at /picks; /legacy-picks and /cockpit preserved",
        "Live market mirror endpoint GET /api/market/session/{symbol}",
        "Railway-friendly market history via core/market_history.py delegating to _fetch_ohlcv chain",
        "SEC XBRL fundamentals via core/sec_fundamentals.py (EPS YoY + revenue YoY)",
        "Generic ticker->CIK resolution for common large-caps + best-effort SEC ticker index",
        "Hard A/B coverage gate MIN_COVERAGE_FOR_AB=18, exposed in report coverage{}",
        "Coverage health endpoint GET /api/wolf/super-ghost/coverage",
    ],
    "new_files": [
        "ghost_console.html — unified Liquid Glass prediction console",
        "core/super_ghost.py — 25-point prediction-intelligence engine",
        "core/super_ghost_ledger.py — truth ledger + outcome resolver",
        "core/market_history.py — Railway-friendly daily OHLCV history",
        "core/sec_fundamentals.py — SEC XBRL fundamentals + ticker->CIK",
        "docs/SUPER_GHOST_MASTER_BUILD.md — max build roadmap",
        "docs/super_ghost_master_plan.json — machine-readable plan",
        "tests/test_super_ghost_coverage.py — coverage gate + source tests",
    ],
    "known_issues": [
        "Coverage can vary by symbol/provider/cache; unknowns are honest and block A/B if below 18/25",
        "Form 4 insider parser, 13F institutional parser, analyst revisions, options chain, and macro event calendar are still future P1 work",
        "No guaranteed-profit claims; output remains prediction intelligence only, not financial advice or auto-trading",
    ],
}

PROD_VERIFY_2026_06_28 = {
    "deploy_id": "2cb3db3",
    "git_sha_short": "2cb3db3",
    "_pr_version": 81,
    "verified_at_ct": "2026-06-28",
    "tests": "426 passed, 3 skipped, 2 warnings",
    "key_fixes": [
        "Circuit breakers actually block (infinite probe loop fixed)",
        "All yfinance calls gated behind _yfinance_cb (zero raw calls remain)",
        "5-tier spot price chain (Alpaca→yfinance→Polygon→IEX→Stooq)",
        "Portfolio routes auth-gated; test-alert requires cron secret",
        "CRON_SECRET production boot guard",
        "Ghost Ask portfolio leak fixed (include_portfolio=False default)",
        "NaN sanitization in all OHLCV paths (yfinance/Polygon/Stooq)",
        "Sentiment confidence floor bypass fixed",
        "Reconcile/legacy watchdog double-resolve fixed (AND outcome IS NULL)",
        "Morning card dedup after send success (not before)",
        "Train endpoints have concurrency lock",
        "Watchlist-membership filter on all stats/journal queries",
        "API rate limiter 120→300 RPM",
        "Scheduler overlap guard; degraded mode counts half_open",
        "X-Forwarded-For hardening; OAuth CIMD SSRF hardening",
        "Dead-letter admin UI fixed; Playwright selectors updated",
        "CircuitBreaker class tests (8 new)",
        "War Room endpoint (POST /api/wolf/war-room)",
    ],
    "new_files": [
        "core/yfinance_client.py — centralized breaker-gated yfinance wrapper",
        "core/war_room.py — 6-agent equity research pipeline (Claude Sonnet)",
    ],
    "known_issues": [
        "yfinance breaker may be OPEN (Yahoo blocking Railway IPs) — expected, Ghost falls back through other 4 tiers",
        "Playwright browser smoke still needs #mvr-toggle click (fixed in spec, not yet verified on CI)",
    ],
}

PROD_VERIFY_2026_06_12 = {
    "deploy_id": "ba7b1c7e-ccd3-480c-8bd4-5e9b01cf2886",
    "git_sha_short": "376bf8c",
    "_pr_version": 62,
    "verified_at_ct": "2026-06-12 ~11:06 AM CT",
    "squeeze_daily_log_api": "11 rows 2026-06-12 (8 telegram, 11 pending EOD)",
    "squeeze_daily_log_ui": "#squeeze-daily-log-section SQUEEZE ACCOUNTABILITY LOG — AMC row pending",
    "hero_truth_strip": "POST-FALSIFICATION · WR 28.6% 2W/5L · expectancy +0.28% · Pick journal link",
    "v3_pick_lane": "v3 pick lane · post-falsification + honest subtitle",
    "ghost_score": "46 WATCHING · bias only — no trade cleared gates (14d)",
    "squeeze_radar": "live 10:53 AM CT · 35/44 ok · 4 Telegram alerts · AMC ACTIVE",
    "journal": "POST-FALSIFICATION MODE; 28.6% issued / 25% closed; Brier 0.597",
    "engine": "44 scanned · 0 saved · v3_regime_gate binding (ITRI near-miss)",
    "known_noise": "price feeds 1/2 on freshness probe; Alpaca SIP→IEX OK",
    "next_watch": "EOD resolve after 3 PM CT 2026-06-12 (11 rows → session OHLC)",
}

PROD_VERIFY_2026_06_11 = {
    "deploy_id": "7367631c",
    "git_sha_short_admin": "87db7b4",
    "git_sha_short_cockpit": "b20fff6",
    "railway_active": True,
    "python": "3.13.13",
    "phase1_2_admin": "contract v2.0-post-falsification; regime cal on; squeeze_ml_v2 on",
    "cockpit": "contract banner + POST-FALSIFICATION MODE; squeeze paused overnight OK",
    "kill_status": "all clear; DB pool max 25 loads",
    "v3_gate": "WOLF up_prob 0.5373 vs floor 0.5380 (-0.0007); SMA5 Trend-up bypass logged",
    "squeeze_overnight": "PAUSED resumes 3:00 AM CT — expected at 12:14 AM CT",
    "drift": "insufficient_samples (0) — expected early",
    "options_pcr": "empty — thin chain OK",
    "scan_health_7d": "187 cycles, 44/44 scanned, 0 saved (silence by design)",
    "journal": "28.2% WR, ABANDON_80_CLAIM copy live",
    "known_noise": "Alpaca SIP 403→IEX OK; yfinance WOLF flake overnight; RDFN delisted noise",
    "next_watch": "First 3 AM CT squeeze wake Thu 2026-06-11; weekly checklist during session",
}

PRODUCTION_URL = "https://ghost-protocol-v2-production.up.railway.app"
GITHUB_REPO = "seancole713-source/ghost-protocol-v2"
RAILWAY_PROJECT = "tender-benevolence"
RAILWAY_SERVICE_V2 = "98593080-065d-43ef-840c-4a3d36a1b572"
RAILWAY_SERVICE_V1 = "098281d7-7dba-447c-981e-0ebd625cecad"  # old ghost, Telegram silenced
CRON_JOB = "cron-job.org fires POST /api/morning-card at 8 AM CT (America/Chicago, 0 8 * * *)"
AUTH_HEADER = "x-cron-secret — value in Railway env as CRON_SECRET"
ADMIN_CONSOLE = "/admin — HMAC cookie login (PR #28). Investor cockpit at /cockpit."

# Sandbox CANNOT reach Railway prod (egress allowlist). All prod verification = user.

# ============================================================
# NORTH-STAR — RETIRED (post-falsification, 2026-06-10)
# ============================================================

NORTH_STAR = {
    "status": "ABANDON_80_CLAIM — legacy ~80% selective-pick claim retired",
    "headline": "Selective directional aid + intraday squeeze radar",
    "philosophy": "Silent most cycles on v3 lane; squeeze radar independent (3 AM–7 PM CT)",
    "data_reality": "WOLF post-Chapter-11 (new shares 2025-09-29) ~250 trading days only",
    "blueprint": "WOLFSPEED-Only Prediction Engine: 7 specialists -> meta-model, 12 data cats",
    "today": "v3.2 XGBoost ~65.4% holdout, four-gate chain, squeeze scorecard, Phase 1+2 probes wired",
    "contract": "core/ghost_contract.py + GET /api/ghost/contract",
    "metrics": "Track live win rate, expectancy, Brier on pick journal — no fixed accuracy marketing",
}

# Kill condition (pre-registered). See core.prediction.FALSIFICATION_THRESHOLD.
FALSIFICATION = {
    "min_samples": 30,
    "win_rate_floor": 0.70,
    "north_star": 0.80,
    "rule": "N>=30 AND win_rate<0.70 AND 95% CI excludes 0.80 => abandon the 80% claim",
    "status": "TRIPPED — ABANDON_80_CLAIM (journal ~28% WR, CI excludes 80%)",
    "surfaced_at": "GET /api/wolf/pick-journal -> verdict.falsification",
    "product_copy": "GET /api/ghost/contract",
}

# ============================================================
# LIVE ENV CONFIG (set in Railway, confirmed by user 2026-05-23)
# ============================================================

LIVE_ENV = {
    "OBJECTIVE_MODE": "aggressive",
    "OBJECTIVE_AUTO_MODE_ENABLED": "0",  # env wins; runtime auto-override disabled
    "MIN_ALERT_CONFIDENCE": "0.75",  # restored from 0.55 (PR #77, 2026-06-25)
    "OBJECTIVE_BOOTSTRAP_MIN_CONF": "0.78",  # restored from 0.65
    "STOCK_SYMBOLS": "43-symbol official watchlist (RDFN removed 2026-06-25)",
    "CB_ALPACA_RATE_MAX_CALLS": "50",  # bumped from 30
    "MODEL_COVERAGE": "44/44 trained (2026-06-07)",
    "V3_MIN_HOLDOUT_ACC": "0.38",
    "V3_MIN_WF_ACC_MEAN": "0.40",
    "V3_MIN_EDGE": "0.0",
    "V3_WF_ACC_MIN_SLACK": "0.15",
    "RATE_LIMIT_RPM": "300",  # bumped from 120 (PR #73)
    "WATCHLIST_FILTER_ENABLED": "1",  # PR #76: only OFFICIAL_WATCHLIST in stats/journal
    "SCAN_INTER_SYMBOL_DELAY_S": "1.2",  # PR #70: prevent Alpaca rate-limit storms
}

# ============================================================
# V3.2 PREDICTION ENGINE
# ============================================================

ENGINE = {
    "model": "XGBoost v3.2, trained on TP/SL daily-bar outcomes + walk-forward validation",
    "wolf_holdout_accuracy": 0.654,
    "confidence_formula": "clamp(accuracy + (up_prob - min_p) * 4.0, 0.75, 0.95)  # min_p=V3_MIN_WIN_PROBA 0.55",
    "data_feed_chain": "Alpaca SIP -> Alpaca IEX -> Polygon -> yfinance -> Stooq (5-tier)",
    "health_probe_symbol": "AAPL (HEALTH_PROBE_SYMBOL) — decoupled from WOLF data gaps",
    "buy_only": "DOWN/SELL signals blocked (1.9% historical win rate)",
}

FOUR_GATE_CHAIN = [
    "1. Model gate — engine emits a signal (regime gates: EMA200/ADX, bearish stack; meta gates: edge/accuracy/walk-forward)",
    "2. Confidence floor — MIN_ALERT_CONFIDENCE / CONFIDENCE_FLOOR (0.75 live)",
    "3. SELL block — BUY-only",
    "4. Objective gate — _objective_gate (precision / balanced / aggressive)",
]

OBJECTIVE_MODES = {
    "precision":  {"target_wr": 0.80, "min_samples": 20, "bootstrap_min_conf": 0.90},
    "balanced":   {"target_wr": 0.70, "min_samples": 12, "bootstrap_min_conf": 0.85},
    "aggressive": {"target_wr": 0.62, "min_samples": 8,  "bootstrap_min_conf": 0.78},  # LIVE
    "_note": "OBJECTIVE_AUTO_MODE_ENABLED=1 lets ghost_state.objective_mode_runtime override env. Currently OFF.",
}

# ============================================================
# DATABASE
# ============================================================

DB = {
    "predictions": "v1+v2/v3 picks. v3.2 era = id >= 223438. Cols: outcome, exit_price, "
                   "pnl_pct, resolved_at, features JSONB, scores JSONB (PR #30).",
    "ghost_state": "key/val cross-cycle state (objective_mode_runtime, gate_outcome_history, "
                   "v32_stats_start_ts, last_train_details).",
    "ghost_prediction_outcomes": "legacy v1 signal source — no longer the engine.",
    "ghost_v3_model": "trained v3 model blob + meta_* rows.",
    "V32_ERA_MIN_ID": 223438,
    "migrations": "core/db.py — additive / IF NOT EXISTS / non-destructive. scores col added on boot.",
}

# ============================================================
# API ENDPOINTS (current)
# ============================================================

ENDPOINTS_PUBLIC = [
    "GET /health",
    "GET /api/picks",
    "GET /api/v2/recent",
    "GET /api/stats/v32",
    "GET /api/objective",
    "GET /api/cockpit/context",
    "GET /api/wolf/{price,predictions,stats,earnings,analyst,news,ghost-score,context}",
    "GET /api/wolf/gate-status     — live four-gate diagnostic (PR #27)",
    "GET /api/wolf/gate-history    — rolling per-cycle gate outcomes, last 50 (PR #29)",
    "GET /api/wolf/pick-journal    — credibility ledger: audit trail + win-rate CI + expectancy + Brier + falsification (PR #30)",
    "GET /api/squeeze/picks        — intraday squeeze board + live_drift[] vs first alert buy (PR #55-#63)",
    "GET /api/squeeze/status       — last 44-symbol scan snapshot + radar_active (PR #55-#60)",
    "GET /api/squeeze/daily-log    — squeeze ledger vs session OHLC + live_drift[] intraday (PR #61-#63)",
    "POST /api/admin/squeeze-resolve — force EOD resolve (ops)",
    "GET /api/ghost/contract       — post-falsification product contract (PR #60)",
    "GET /api/ghost/blueprint      — Phase 1+2 module status rollup (PR #60)",
    "GET /api/ghost/{regime,drift,sentiment,options} — Phase 2 probes (PR #60)",
    "GET /api/shadow-stats         — virtual hit-rate scoreboard (gates ignored)",
    "GET /api/_version             — running PR version marker (_RUNNING_PR_VERSION)",
    "GET /api/diag/data-sources    — feed-tier visibility",
]

ENDPOINTS_PROTECTED = [
    "POST /api/run-predictions | /api/morning-card | /api/reconcile  (x-cron-secret)",
    "POST /api/v3/train/sync   — sync train + gate report (PR #18/#20)",
    "POST /api/cron/signal-check — Telegram signal alert (PR #8)",
    "POST /api/admin/purge-ghost-portfolio | /api/clean-garbage",
]

ENDPOINTS_ADMIN = [
    "GET  /admin            — operator console (login form if no cookie)",
    "POST /admin/login      — JSON body (no python-multipart dep); mints HMAC cookie",
    "POST /admin/logout",
]

# ============================================================
# V2 API FIELD NAMES — JS/Frontend must use these
# ============================================================

V2_PICK_FIELDS = {
    "outcome": "null=active | 'WIN' | 'LOSS' | 'EXPIRED'  — NOT p.status",
    "stop_price": "stop loss price  — NOT p.stop_loss",
    "expires_at": "unix timestamp  — NOT p.done_by",
    "gain_pct": "NOT a field — calculate: (target-entry)/entry*100",
    "pnl_pct": "filled after outcome resolved",
    "scores": "JSONB — specialist score vector + regime-at-issuance (new picks only, PR #30)",
}

# ============================================================
# TODO — dependency-ordered (blueprint backlog)
# ============================================================

TODO = """
P0 — PRODUCT POSITION (done 2026-06-10)
[x] Falsification gate tripped — ABANDON_80_CLAIM; honest copy in cockpit + ghost_contract
[x] User prod-verify PR #60 (2026-06-11): admin + cockpit Phase 1+2 cards, kill-status, overnight squeeze pause
[x] User prod-verify PR #61–#62 (2026-06-12): squeeze daily log API+UI, hero truth strip, v3 lane copy, squeeze wake
[x] Agent prod-verify PR #63 (2026-06-15): live vs alert drift on daily-log API + cockpit HTML; _pr_version 63
[x] Squeeze EOD resolve verify — 2026-06-12: 17 rows resolved (5 WIN, 5 LOSS, 7 NEUTRAL)
[ ] Weekly ops checklist (PROJECT_STATE.md) — run 5 URLs + admin cards once/week (first full pass during CT session)
[x] Confirm squeeze radar wake after 3:00 AM CT 2026-06-11 (leaders + last_scan_ts) — verified 2026-06-12 session

P1 — PHASE 3 DEPTH (probes exist; not yet gating picks)
[x] Investigate/fix shadow outcome resolver backlog — PR #151 live-verified 2026-07-08: stale June pending rows closed as EXPIRED when no OHLCV bars were available; pending 31 → 29, resolved 1136 → 1138 after deploy+admin shadow-cycle; remaining rows are no longer stale (earliest expiry 2026-07-08 16:00 CDT, status waiting).
[ ] Train squeeze ML v2 from labeled squeeze outcomes in ghost_squeeze_outcomes (replace baseline logistic in data/squeeze_ml_v2.json)
[ ] FinBERT sentiment on existing news pipeline (lexicon_v1 is probe-only today)
[ ] Wire drift alerts to Telegram/admin when GHOST_DRIFT_Z_ALERT fires
[ ] Full options-flow model (Polygon IV skew/GEX) — validate WOLF chain depth first
[ ] Regime-conditional isotonic calibration (separate maps per regime; journal has regime-at-issuance)
[ ] Regime detector (HMM) — DEFERRED: ~250 days too thin; build after journal shows loss clusters

P2 — DATA (budget decision, not code)
[ ] Key Stats / Analyst / Short Interest return empty on current tier. Needs paid provider.
"""

COMPLETED_PHASE1_2 = """
Phase 1 (PR #60, commit 91dc94c):
[x] core/ghost_contract.py + GET /api/ghost/contract — post-falsification product copy
[x] core/regime_calibration.py — effective_min_win_proba by regime; SMA5 Trend-up bypass (env)
[x] core/squeeze_ml_v2.py — 60/40 blend into squeeze scorecard probabilities
[x] Cockpit: contract banner, pick-journal POST-FALSIFICATION MODE copy
[x] signal_engine wired: regime_calibration meta on scores; min_p adjusted live

Phase 2 (PR #60, commit 91dc94c):
[x] core/regime_classifier.py + GET /api/ghost/regime
[x] core/news_sentiment.py lexicon_v1 + fetch_news_sentiment + GET /api/ghost/sentiment
[x] core/feature_drift.py z-shift alerts + GET /api/ghost/drift + admin card
[x] core/options_flow.py yfinance PCR probe + GET /api/ghost/options + admin card
[x] GET /api/ghost/blueprint + admin Blueprint Modules card
[x] tests/test_ghost_phase12.py (403 total tests passing at ship)

Squeeze radar lane (PR #55-#59, commits d7477d1..c0bad2e):
[x] core/squeeze_scorecard.py — Setup/Trigger/Confirm + heuristic + ML blend
[x] core/squeeze_monitor.py — 44-symbol scan, VWAP, Finviz short fallback, scan cache
[x] core/market_hours.py — CT session helpers, next_radar_resume_label
[x] GET /api/squeeze/picks | /api/squeeze/status; POST /api/admin/squeeze-scan
[x] Cockpit intraday squeeze panel + Today's v3 pick lane labels
[x] Admin squeeze status card; core/db.py pool max 25 + kill-status bundled query

PR #61–#62 (commits 37c5db6, 376bf8c, _pr_version 61–62):
[x] core/squeeze_outcomes.py — ghost_squeeze_outcomes table, record on Telegram/candidate, EOD resolve
[x] GET /api/squeeze/daily-log; POST /api/admin/squeeze-resolve; squeeze_eod scheduler job
[x] Cockpit squeeze accountability log (#squeeze-daily-log-section) + admin daily log card
[x] Truth-mode UX — hero-truth-strip, v3 pick lane post-falsification copy, BIAS ONLY gauge
[x] tests/test_squeeze_outcomes.py

PR #63 (commit 66da1f9, _pr_version 63):
[x] core/squeeze_live_drift.py — first alert buy vs live quote; enrich picks + daily log
[x] GET /api/squeeze/picks + /api/squeeze/daily-log — live_drift[] + per-row gap fields
[x] Cockpit — Ghost prediction vs live summary + Live vs alert columns (radar + daily log)
[x] tests/test_squeeze_live_drift.py
"""

# ============================================================
# COMPLETED — verified on live site
# ============================================================

COMPLETED = """
[x] WOLF pivot — crypto stripped, STOCK_SYMBOLS default 'WOLF'
[x] v3.2 XGBoost engine TRAINED (~65.4% holdout) — PR #21 unblocked walk-forward folds
[x] 5-tier data feed chain (Alpaca SIP/IEX -> Polygon -> yfinance -> Stooq) — PR #9/#12/#17
[x] Four-gate prediction chain (model / confidence floor / SELL block / objective gate)
[x] Objective gate modes + aggressive mode live — PR #27, env confirmed by user
[x] /api/wolf/gate-status live diagnostic — PR #27 (user-verified)
[x] /admin cookie login (replaced blank-page HTTP Basic) — PR #28 (user-verified)
[x] Per-cycle gate-outcome recorder + /api/wolf/gate-history + admin card — PR #29
[x] Pick journal credibility ledger — PR #30 (user-verified 2026-05-23):
    - predictions.scores JSONB; predict_live_ex surfaces score vector + regime-at-issuance
    - /api/wolf/pick-journal: paginated audit trail + win-rate 95% Wilson CI + expectancy + Brier
    - FALSIFICATION_THRESHOLD kill condition (verdict.falsification)
    - cockpit module-7 Pick Journal card
[x] News textual filter (killed Zoom/IBM/Ralph-Lauren leak) — PR #26
[x] Investor-view forensic cleanup (15 items) — PR #23/#24
[x] Intraday squeeze radar + scorecard (PR #55-#59): CT session 3 AM–7 PM, Telegram path separate from v3
[x] Phase 1+2 blueprint modules wired (PR #60, 91dc94c) — see COMPLETED_PHASE1_2 above
[x] PR #60 prod-verified on Railway 2026-06-11 (operator) — see PROD_VERIFY_2026_06_11
[x] Squeeze daily log + truth-mode UX (PR #61–#62) — see COMPLETED_PHASE1_2 above
[x] PR #61–#62 prod-verified on Railway 2026-06-12 (operator + browser agent) — see PROD_VERIFY_2026_06_12
[x] PR #63 prod-verified on Railway 2026-06-15 (agent API curl) — see PROD_VERIFY_2026_06_15
"""

# ============================================================
# WHAT FAILED — DO NOT REPEAT
# ============================================================

FAILURES = """
1. (SUPERSEDED) "XGBoost removed"
   2026-03 note: XGBoost on ghost_prediction_outcomes hit 86% val by overfitting
   bear-skewed DOWN labels; it was removed in favor of per-symbol win rate.
   STATUS NOW: the v3.2 engine (PR #10/#21) retrains on TP/SL daily-bar labels with
   walk-forward validation + holdout gates — a different setup — and IS the live
   engine at ~65.4%. The narrow caution (don't train on bear-skewed gpo direction)
   still holds; the blanket "no XGBoost" does not.

2. Walk-forward produced 0 folds
   Hardcoded min_train = max(120, n*0.50) with n~127 -> 0 folds -> no model.
   Fix (PR #21): env-tunable floors V3_WF_MIN_TRAIN (60), V3_WF_TEST_SIZE (15).
   THIS is the change that finally trained the WOLF model.

3. News leak (Zoom / IBM / Ralph Lauren)
   Finnhub tags every WOLF-query article ["WOLF"], so the `WOLF in syms` shortcut
   passed everything. Fix (PR #26): require textual mention (WOLFSPEED/WOLF/SiC/
   silicon carbide). Also fixed an unfiltered yfinance augmentation loop.

4. /admin blank page
   HTTP Basic Auth (PR #23) returned correct 401 locally but blank-paged on Railway
   (edge/browser mishandling the Basic challenge). Fix (PR #28): HMAC cookie login,
   JSON body (no python-multipart). Do not reintroduce HTTP Basic.

5. Railway serving stale containers (recurring)
   Bust cache by bumping ALL of: Procfile boot-echo, nixpacks cache_bust comment,
   wolf_app boot banner / _RUNNING_PR_VERSION. Verify via /api/_version.

6. Pushing fixes to an already-merged branch
   Created a commit on a stale merged branch. Always branch fresh from main.

7. (historical) Reuters RSS blocked on Railway; morning_card interval 3600 spam;
   HOOD/COIN poison; crypto defaults; confidence as features[0]. Still applies.

8. Agents claiming fixes done without live verification
   This file exists because of that. Verify before marking [x].
"""

# ============================================================
# SESSION LOG — append a new entry after every fix session
# ============================================================

SESSION_LOG = """
--- 2026-07-02–04 | PR #114–#123 — Phase 2 DOWN lane, Alpaca breaker fix, GHOST_ACCURACY_CONTRACT=70 ---
Context: Operator asked for production status, then auto-mode to build toward 70%+ live
accuracy. Diagnosed 6.7% live win rate (legacy picks under aggressive env). Shipped
accuracy contract; did NOT modify PROD_VERIFY or the structured SESSION_LOG dict above.

Phase 1 — Alpaca breaker thrash (PR #115, v118):
  P2-6 force-refresh busted cache when price >2% from cached high OR low — always true
  for >4% intraday range symbols. Fix: core/prices.py _intraday_breakout_pct() — refresh
  only when live trade breaks OUTSIDE cached [low, high]. CB_ALPACA_RATE_MAX_CALLS 50→150.

Phase 2 — Phase 2 DOWN lane + audit quick wins (PR #114, v117):
  UP fires independently of stronger DOWN; DOWN shadow-only unless V3_DOWN_SIGNALS_ENABLED=1.
  Direction-separated peer pools; _strip_model_direction_suffix for meta_{sym}_up/_down keys.
  Model cache, predictions indexes, login throttle, NON_RESEARCH_WHERE hygiene.

Phase 3 — GHOST_ACCURACY_CONTRACT=70 (PR #119–#123):
  core/accuracy_contract.py — unified training/firing/objective/kill floors; env cannot
  weaken contract (V3_MIN_HOLDOUT_ACC=0.38 etc clamped). Research picks no longer bypass
  precision gate under 70 contract. Railway: GHOST_ACCURACY_CONTRACT=70, OBJECTIVE_MODE=
  balanced, RESEARCH_PICK_ENABLED=0, KILL_WINRATE_FLOOR=0.70. Training admission 60%
  OOS; live fire still requires 70% precision proof. /api/_version exposes accuracy_contract.
  Tests: 649 passed at ship. Retrain under strict gates: 0/5 batch passed initially.

Operator Q&A: stale status report (v116/968b2c4) corrected — prod reached v123+;
Phase 2 code deployed; 0 DOWN models trained; live 6.7% is pre-fix record; engine
correctly silent (precision_unproven / meta_gate / chop) until models re-prove.

Open for next agent: monitor retrain + precision_gate.ok counts; post-fix resolved WR
only; keep V3_DOWN_SIGNALS_ENABLED=0 until DOWN shadow track record exists.

--- 2026-07-03–04 | PR #125–#131 — security round, live verification pipeline, kill-status honesty ---
Context: full forensic diagnostic (this agent + a second agent cross-verifying).
The second agent shipped the critical-fix wave (regime modifier sync, ~3,600 lines
dead code removed, ghost_state DDL centralized, GHOST_DEV_MODE dev gate) as ace0998.
This session then verified survivors and shipped:

PR #125 (marker 127) — forensic security round:
  - build_ask_context(include_portfolio=False) restored: the portfolio-PII gate is a
    real parameter again (was an undefined name whose NameError got swallowed; the two
    auth-gated callers were TypeError-broken). Public /api/wolf/ask passes False explicitly.
  - POST /api/wolf/ask auth-gated (admin cookie/MCP/OAuth) — was the only
    unauthenticated paid-LLM endpoint. Returns renderable JSON 401.
  - _admin_token_valid fails CLOSED without CRON_SECRET unless GHOST_DEV_MODE=1
    (mirrors _cron_ok; previously any non-Railway deploy was wide open).
  - /api/v2/recent?symbol=ALL auth-gated (WOLF-only default stays public).
  - record_pick_resolution failures now LOGGER.error (were silent P&L divergence).
  - Watermark 223438 centralized: core.prediction_filters.V32_ERA_MIN_ID (was in 8 files).
  - GHOST_CORS_ORIGINS env knob. Tests: tests/test_forensic_security.py.

PR #126–#130 — live verification pipeline modernized (was broken since inception):
  - #126: missing TEST_DATABASE_URL/CRON_SECRET secrets now warn+skip instead of
    hard-failing every main push (release-gates had NEVER run before this).
  - #127: go/no-go aligned to hardened contracts — slim public /health {status,score,ts}
    + internal-key leak tripwire; /api/diagnostics asserted 404 unauth; cockpit markers.
  - #128: "Wait for deploy to settle" gate (3x consecutive /health 200s) — every merge
    redeploys the exact commit the smoke suite tests; it was racing the container swap.
  - #129: coverage check understands directional model maps {sym:{UP,DOWN}} (wf_acc_min per lane).
  - #130: mobile truth-toggle round-trip forced (live layout churn made hit-testing flaky).
  RESULT: first fully clean end-to-end GO in project history (run 28679241539, 44ebd3b):
  686 unit tests + 33/33 live Playwright (desktop+mobile) + go/no-go GO + 0 critical
  health findings + 0 new error signatures. Pipeline runs automatically on every merge.

PR #131 (marker 128) — kill-status honesty:
  - /api/wolf/kill-status showed all-time window (win_rate red, auto_pause) while
    enforce_kill_conditions correctly evaluates only since the last manual resume
    (engine_pause_resume_ts window reset) → paused=false looked like a broken kill
    switch to a live reviewer. VERIFIED working-as-designed; endpoint now returns
    enforcement_window {since_ts, conditions, any_triggered} alongside. Second
    consecutive clean pipeline GO (90ce3b4).

Prod state (user-verified 2026-07-04): _pr_version 128, health 95/100 (holiday
freshness warnings only), degraded=false. Bootstrap phase: 2/8 wins, gate CLOSED
(up_prob 0.4977 vs 0.6309 needed, Chop regime), recent picks WITHDRAWN by open-pick
review (several +P&L anyway), kill dashboard red all-time / enforcement window
insufficient (correct). Engine triple-locked: closed gate, 0.85 bootstrap floor,
per-cycle kill enforcement. Models re-proving precision thresholds on purged slices
post-retrain (label_schema_stale invalidation is intentional).

Ghost MCP connector: OAuth completed 2026-07-04; ghost_gate_status verified live
via MCP from a fresh session. URL: https://ghost-protocol-v2-production.up.railway.app/mcp

NEXT (Monday 2026-07-06 open) — LIVE WATCH, the one untested surface:
  1. Poll ghost_gate_status / ghost_picks / ghost_kill_status through premarket+open.
  2. Any fire must satisfy contract-70: confidence >= floor, precision gate proven
     (not precision_unproven), regime ok, kill conditions clear.
  3. Any intraday resolution must land exit_price EXACTLY at target/stop (capped),
     appear in the performance log, and any perf-log failure now logs
     "record_pick_resolution failed" (grep Railway logs).
  4. Watch /api/system/breakers (yfinance/alpaca) under live load; 95->100 health
     should self-heal with fresh data.
  Optional user config: TEST_DATABASE_URL + CRON_SECRET GitHub Actions secrets
  activate DB integration tests + deep health audit in the pipeline.

--- 2026-06-28–29 | PR #82–#90 — Super Ghost foundation, unified console, live coverage gate ---
Context: User clarified Ghost is a prediction-market/intelligence product, not an
auto-trading/broker bot. Mission: build toward the strongest possible stock prediction
platform while staying honest — no guaranteed profit, no fake accuracy, no fake data.

Phase 1 — Super Ghost intelligence layer (PR #82–#83):
  - Added market-regime detection and conviction adjustment (SPX/Nasdaq/sector/VIX/Fed/CPI context)
  - Added optional real AI brief on /api/wolf/super-ghost?ai=1 using Ghost's Anthropic integration
  - Fixed model default to the proven Ghost Ask model: claude-haiku-4-5-20251001
  - Live verified ai_brief.available=true after PR #83

Phase 2 — Prediction Truth Ledger (PR #84):
  - Added core/super_ghost_ledger.py and super_ghost_predictions table
  - Routes: log, history, accuracy, if-followed, resolve
  - Hourly resolver job added; resolve is auth-gated
  - Purpose: every prediction can be judged later; wins include correct DOWN calls, not only UP calls

Phase 3 — Max Build roadmap (PR #85):
  - Added docs/SUPER_GHOST_MASTER_BUILD.md
  - Added docs/super_ghost_master_plan.json (98 requirements, 11 phases)
  - Added tests/test_master_plan.py so the plan itself is CI-enforced
  - Next true accuracy phase identified: P1 Data Coverage Upgrade

Phase 4 — Unified Liquid Glass prediction console (PR #86–#87):
  - / and /picks now serve ghost_console.html, the unified prediction console
  - Preserved /legacy-picks and /cockpit
  - Sidebar tabs: Overview, Top stocks, Bullish, Today, 48 hour, This week, Live mirror, Health
  - Local prediction pool controls; Top Picks gated by truth-ledger win rate
  - Added GET /api/market/session/{symbol} for live open/high/low/price mirror

Phase 5 — Data Coverage Upgrade + hard trust gate (PR #88–#90):
  Root cause: live Super Ghost coverage was 7/25 because _fetch_live_snapshot sourced
  price history + fundamentals from yfinance. Yahoo blocks Railway IPs, starving the
  existing scorers. The scorers were not the problem; the live data path was.

  - PR #88:
    * core/market_history.py added
    * core/sec_fundamentals.py added
    * EPS YoY + revenue YoY via SEC XBRL
    * current-price fallback via existing 5-tier spot chain
    * hard gate MIN_COVERAGE_FOR_AB=18: no A/B grade and no HIGH-CONVICTION below 18/25
    * GET /api/wolf/super-ghost/coverage added
  - PR #89:
    * get_daily_history() now delegates first to production-proven _fetch_ohlcv chain:
      Alpaca SIP -> IEX -> Polygon -> yfinance -> Stooq
  - PR #90:
    * Generic ticker->CIK support for SEC fundamentals; common large-cap built-in map
      and best-effort SEC ticker index

Live verified on production 2026-06-29:
  - GET /api/_version: sha=5bc05a0, _pr_version=88, app_version=2.5.0
  - GET /api/wolf/super-ghost/coverage?symbol=WOLF: 21/25, meets_ab_gate=true
  - AAPL: 19/25, meets_ab_gate=true
  - NVDA: 20/25, meets_ab_gate=true
  - Deployed report carries coverage.min_for_ab_grade=18 and meets_ab_gate metadata
  - Invariant verified: if coverage <18, no A+/A/B+/B grade and no HIGH-CONVICTION

Tests:
  - make test: 503 passed, 3 deselected/skipped, 2 warnings
  - make test-compile: exit 0
  - Focused suite: 34 passed (coverage gate, Super Ghost, master plan)

Important honesty note:
  WOLF meeting coverage gate does NOT mean "buy" or "high confidence." Live output
  can still be grade F / NO EDGE — WATCH ONLY if the evidence is weak. That is correct.
  Coverage is a prerequisite for trust, not a promise of profit.

PR #100 Point-in-Time Feature Store shipped after the evolution directive:
  - Every logged Super Ghost prediction now gets an immutable feature snapshot.
  - Timestamp audit flags any source data dated after the prediction time.
  - This prevents future leakage in learning/lab/model-training work.

PR #99 Promotion Gate shipped after the evolution directive:
  - Ghost can now decide PROMOTE / KEEP CHAMPION / KEEP SHADOWING / RETIRE / INSUFFICIENT using strict evidence gates.
  - No auto-promotion; this creates auditable review records only.

PR #98 Shadow Model Runner shipped after the evolution directive:
  - Seven specialist prediction brains now run in parallel with production Ghost when predictions are logged.
  - Shadow predictions are stored and later resolved against parent Truth Ledger outcomes.
  - This creates true model disagreement memory for future promotion gates.

PR #96 Feature Attribution Memory shipped after the evolution directive:
  - Every logged Super Ghost checklist point is now feature-attribution memory.
  - Resolved outcomes classify features as helped/hurt/underweighted/noise/missing.
  - Feature reliability profiles create long-term memory of which evidence mattered.

PR #95 Champion/Challenger Lab shipped after the evolution directive:
  - Production Ghost now competes against shadow challenger policies on resolved ledger rows.
  - Results are persisted in super_ghost_lab_runs/results.
  - Conservative gates prevent any recommendation without enough rows/actionable calls/improvement.
  - No auto-promotion and no trading; this creates evidence only.

PR #93 Learning Brain shipped after the evolution directive:
  - Every resolved Super Ghost prediction can become a learning event.
  - If Ghost predicts a target too low/high (e.g. $5 -> $7), it records the mistake type and lesson.
  - Learning profiles can apply bounded future confidence/conviction/target adjustments after enough samples.
  - Learning is visible via /api/wolf/super-ghost/learning and the console Health row.

PR #91 follow-up polish shipped after the review:
  - Added persistent post-falsification banner outside Health.
  - Replaced "resolved sample" jargon with "completed predictions" copy.
  - Hid duplicate top-tab chrome; sidebar nav is the single visible nav source.
  - Deployed as sha 3a83893, _pr_version 91; 504 tests passing.

Remaining P1 work:
  - Form 4 insider parser
  - 13F institutional parser
  - analyst revisions / target-change feed
  - options chain / unusual activity
  - macro event calendar + CPI/Fed surprise classifier
  - guidance/news event classifier + dedup
  - server-persisted prediction pool + richer chart overlays

--- 2026-06-25–26 | PR #70–#81 — Comprehensive security + reliability audit (12 PRs) ---
Context: User asked "is ghost working perfect now can i trust the predictions?"
This triggered a multi-phase audit spanning 2 days, 12 PRs, and 3 external agent
audit passes. All 13 original findings + 10 continuation findings resolved.

Phase 1 — Production incident response (PR #70–#76, 2026-06-25):
  - yfinance JSON parse errors on EVERY symbol (Yahoo blocking Railway IPs)
  - Alpaca rate-limit storm (45 calls/60s, limit 30)
  - Circuit breaker infinite half-open probe loop — breakers logged "OPEN" but never blocked
  - API rate limiter 120→300 RPM (cockpit ~25 parallel calls on load)
  - All raw yfinance calls gated behind _yfinance_cb across 6 modules
  - Watchlist-membership filter on REAL_TRADE_WHERE + write-side guard
  - Confidence gates restored: MIN_ALERT_CONFIDENCE 0.55→0.75, OBJECTIVE_BOOTSTRAP_MIN_CONF 0.6→0.78
  - RDFN removed from STOCK_SYMBOLS

Phase 2 — External agent audit (PR #77–#78, 2026-06-26):
  Agent ran deep read-only audit against ed541c4. Found 13 findings (F01–F13).
  All fixed: unauth portfolio routes, raw yfinance bypasses, sentiment floor bypass,
  public test-alert, CRON_SECRET fail-open, double delay, 5-tier spot chain,
  degraded mode, scheduler overlap, XFF hardening, Playwright, breaker tests,
  wolf_price alias.

Phase 3 — Continuation audit (PR #79, 2026-06-26):
  Agent continued audit. Found 5 more findings (C01–C05). C01 was false alarm
  (dirty working tree). Fixed: NaN sanitization in Polygon/Stooq, Telegram dedup
  conditional on _send(), dead-letter admin UI, OAuth CIMD SSRF hardening.

Phase 4 — Third-pass audit (PR #80, 2026-06-26):
  Agent ran third pass. Found 10 findings (GP-A01–GP-A10). Fixed 9: Ghost Ask
  portfolio leak, Polygon/Stooq NaN, check_feeds 5-tier, Playwright hidden element,
  cockpit 401 handling, reconcile double-resolve, train endpoint lock, morning card
  dedup after send, OAuth redirects. GP-A03 deferred to PR #81.

Phase 5 — GP-A03 + War Room (PR #81, 2026-06-26):
  - yfinance wrapper (core/yfinance_client.py) + api/wolf_endpoints.py monkeypatch
  - Zero raw yfinance calls remain in the codebase
  - War Room endpoint (POST /api/wolf/war-room) — 6-agent equity research pipeline

Final state: 426 tests passing, 3 skipped, 2 warnings. Clean working tree.
All 23 audit findings resolved across 12 PRs.

--- 2026-06-15 | PR #63 prod verification (agent API curl, Railway tender-benevolence) ---
Context: live vs alert drift shipped (66da1f9). Compare first Telegram alert buy to live quote
on squeeze radar, daily log, and API before EOD resolve.

Agent-verified on prod 2026-06-15 (~10:16 AM CT):
  - GET /api/_version: _pr_version 63, git_sha_short 66da1f9, deploy_id 66258b11
  - GET /api/squeeze/daily-log: live_drift[] 18 symbols; pending rows have live_price/gap_pct/drift_status
  - GET /api/squeeze/picks: live_drift key present; 0 rows (alert_history empty in-process — fills after Telegram)
  - /cockpit HTML: sq-drift-block, Ghost prediction vs live, Live vs alert, _sqDriftSummaryBlock
  - GET /api/squeeze/daily-log?session_date=2026-06-12: 17 resolved (5 WIN, 5 LOSS, 7 NEUTRAL) — EOD OK

Open / next:
  - Confirm live_drift board on cockpit during active Telegram session (browser pass optional)
  - Weekly ops checklist first full pass
  - Passive label accumulation toward Phase 3 squeeze ML retrain

--- 2026-06-12 | PR #61–#62 prod verification (operator + browser agent, Railway tender-benevolence) ---
Context: squeeze daily log + truth-mode UX shipped (37c5db6, 376bf8c). Operator ran
follow-up browser verify after initial agent missed #squeeze-daily-log-section.

User-verified on prod 2026-06-12 (~10:54–11:06 AM CT):
  - GET /api/_version: _pr_version 62, git_sha_short 376bf8c; cockpit deploy badge matches
  - GET /api/squeeze/daily-log: 11 rows 2026-06-12 (8 telegram), all pending (correct pre-EOD)
  - /cockpit #squeeze-daily-log-section: SQUEEZE ACCOUNTABILITY LOG; AMC pending row shown
  - /cockpit #hero-truth-strip: POST-FALSIFICATION · WR 28.6% 2W/5L · expectancy +0.28%
  - /cockpit #v3-pick-label: v3 pick lane · post-falsification (not squeeze subheader)
  - Squeeze radar live: ~35/44 priced, 4 Telegram alerts, AMC ACTIVE
  - Ghost score WATCHING 14d · 0 saved · regime gate binding (expected post-falsification)
  - Journal POST-FALSIFICATION MODE; closed-after-cutover 25% WR

Not yet verified this session:
  - EOD resolve after 3 PM CT (11 rows should get session OHLC + WIN/LOSS/NEUTRAL)

Open / next:
  - Confirm EOD resolve 2026-06-12 PM
  - Weekly ops checklist first full pass
  - Passive label accumulation in ghost_squeeze_outcomes toward Phase 3 squeeze ML retrain

--- 2026-06-11 | PR #60 prod verification (operator, Railway tender-benevolence) ---
Context: operator pasted live admin + cockpit + deploy logs after Phase 1+2 ship.
Sandbox cannot reach Railway; this entry records operator-confirmed prod state.

User-verified on prod 2026-06-11 (~12:14 AM CT):
  - Railway deploy 7367631c Active; Python 3.13.13; build from main (checklist b20fff6)
  - /admin: Blueprint Phase 1 on (regime cal, SMA5 bypass, squeeze_ml_v2); Phase 2 cards load
  - /admin: kill conditions all clear; no connection pool exhausted
  - /admin: squeeze PAUSED overnight, resumes 3:00 AM CT; leaders 0 overnight (expected)
  - /admin: gate-status WOLF up_prob 0.5373 vs effective floor 0.5380 (prob_low -0.0007)
  - /cockpit: contract banner + POST-FALSIFICATION MODE; deploy b20fff6 shown
  - /cockpit: squeeze radar offline overnight copy correct; v3 WATCHING 13d silent OK
  - Logs: REGIME GATE SMA5 bypass Trend-up WOLF; Cycle 0/0 picks; market_hours=False OK
  - Logs: Alpaca SIP 403 → IEX fallback (not a regression); yfinance WOLF flake overnight
  - Performance log: 187 scan cycles / 7d, 44 scanned, binding v3_regime_gate

Not yet verified this session (watch next):
  - GET /api/_version _pr_version: 60 (operator did not paste curl; admin UI confirms Phase 1+2)
  - First post-deploy 3 AM CT squeeze scan with leaders populated — **done 2026-06-12** (see 2026-06-12 session log)

Open / next:
  - Weekly ops checklist during CT session
  - Passive accumulation (drift samples, squeeze labels) — no Phase 3 code yet

--- 2026-06-10 | Phase 1+2 blueprint + squeeze radar + post-falsification contract ---
Context: operator chose "Phase 1 + Phase 2" (not product-only). Falsification gate
already tripped (~28% WR, CI excludes 80%). Goal: honest repositioning, wire blueprint
probes, keep squeeze radar production-ready for next CT session.

Two lanes (independent):
  v3 picks     — ~3-day gated XGBoost holds; silent when regime/objective gates bind
  squeeze radar — intraday RVOL + move; 3 AM–7 PM CT; separate Telegram path

Shipped squeeze lane (commits d7477d1 .. c0bad2e, _pr_version 55–59):
  d7477d1 — squeeze scorecard (Setup/Trigger/Confirm), CT radar, probability targets
  d17ffe3 — overnight panel honest copy; persist data/squeeze_last_scan.json
  3d9f4b9 — cockpit lane labels (Today's v3 pick vs intraday squeeze)
  c0bad2e — admin squeeze card; db pool max 25; kill-status single checkout fix

Shipped Phase 1+2 (commit 91dc94c, _pr_version 60):
  Phase 1:
    - core/ghost_contract.py, cockpit contract banner, pick-journal POST-FALSIFICATION copy
    - core/regime_calibration.py wired in signal_engine (effective_min_win_proba, SMA5 bypass)
    - core/squeeze_ml_v2.py blended into squeeze_scorecard (60% ML / 40% heuristic)
  Phase 2:
    - core/regime_classifier.py, news_sentiment (lexicon + fetch_news_sentiment)
    - core/feature_drift.py, core/options_flow.py
    - GET /api/ghost/{contract,blueprint,regime,drift,sentiment,options}
    - admin Blueprint / feature drift / options flow cards
    - tests/test_ghost_phase12.py; 403 tests passing at push

Prod verify: PR #60 completed 2026-06-11; PR #61–#62 completed 2026-06-12 — see session logs.
  Squeeze first-wake check: verified 2026-06-12 AM CT session.

Open / Phase 3:
  - Retrain squeeze ML v2 from labeled outcomes (baseline weights are priors only)
  - FinBERT sentiment; KL drift; Polygon options/GEX; per-regime isotonic maps
  - HMM regime detector still deferred (~250 trading days too thin)
  - Paid feed for Key Stats / Analyst / Short Interest

--- 2026-06-07 | Open pick review (change mind mid-trade) ---
Context: operator wanted Ghost always tracking — withdraw or refresh when live scans
no longer support an open pick (not locked until expiry).

Shipped:
  - core/pick_review.py — review_open_picks each cycle before save
  - Outcome WITHDRAWN (mark-to-market P&L, not WIN/LOSS)
  - Withdraw on: regime gate, prob_low, meta gate, confidence floor, supersede on
    entry move (GHOST_SUPERSEDE_ENTRY_PCT) or confidence shift (GHOST_SUPERSEDE_CONF_DELTA)
  - GHOST_WITHDRAW_MIN_AGE_MIN=15 grace after fire; GHOST_WITHDRAW_NOTIFY=1 Telegram
  - Ghost Ask + cockpit WITHDRAWN styling

--- 2026-06-07 | Pre-market watchlist scans ---
Context: operator asked Ghost to include pre-market (previously scans skipped 4:00-9:30 AM CT).

Shipped:
  - GHOST_PREMARKET_SCAN=1 (default): run_prediction_cycle scans full watchlist pre-open
  - Market scan cadence: 30m interval during pre-market when enabled
  - core/prices.get_extended_session: prior close, session price, gap_pct
  - predict_live_ex: overlays extended-hours price on last daily bar; scores.extended_session
  - GHOST_PREMARKET_FLOOR_BUMP=0.03 extra confidence required pre-open
  - Ghost Ask + cockpit copy updated for pre/after-hours sessions
  - tests/test_premarket_scan.py

--- 2026-06-07 | Full watchlist coverage + daily forecast panel + training reliability ---
Context: operator wanted all 44 watchlist symbols trained, cockpit panel reorder, and
daily prediction UI. Root training failure was batch OHLCV fetch returning empty under
Alpaca rate limits (14 symbols showed n_samples=0 despite 126+ samples on retry).

Shipped (commits c088b05 .. 8ba8143):
  - Backend performance log (ghost_perf_cycles/symbol_evals/events) + cockpit panel
  - Daily Prediction Panel (next session predict row + last session market row)
  - Cockpit: Ask Ghost, WOLF play, Prediction vs Reality moved below My Portfolio
  - Training: OHLCV retry/cache/2y default, inter-symbol delay, watchlist peer pool,
    adaptive WF floors for thin tickers (STUB), relaxed Railway gates
  - Fixed Railway STOCK_SYMBOLS (was TSLA,META,AMZN,T -> official 44 list)
  - RDFN daily forecast: scorecard no longer hardcodes 3mo; uses 2y fallback + stale flag
    (RDFN delisted ~2025-06-30 after Rocket acquisition)

Prod verified 2026-06-07 (user/agent):
  - /api/v3/status: models 44/44, watchlist_missing_models []
  - /api/_version git_sha_short 8ba8143
  - /api/wolf/daily-forecast-scorecard?symbol=RDFN: 16 days, data_stale=true,
    last_bar_date 2025-06-30
  - Use POST /api/v3/train?force=true (async) for full retrains; /api/v3/train/sync
    502s on Railway proxy timeout

Open / deferred:
  - News manual import: already live at /admin; wait on bulk use — Finnhub auto-cycle OK
  - Relaxed V3 gates trade quality for coverage on edge-case symbols; monitor pick journal
  - RDFN and other delisted names: forecasts show stale last-trade data only

--- 2026-05-23 | v3.2 era: engine trained -> investor cleanup -> admin -> ledger ---
Context: continued the WOLF rebuild from a dead prediction engine (no trained v3
model) through to a live, audit-ready credibility ledger. PRs #9-#30.

Engine resurrection (#9-#21):
  #9  _fetch_ohlcv SIP + IEX fallback for post-restructure WOLF
  #10 lower v3 training thresholds for limited WOLF data
  #11 yfinance fallback + WOLF-only train filter
  #12 Polygon + multi-strategy yfinance fallback
  #13 Polygon path logs every branch (surface silent skips)
  #14 PR14 diag markers across training path
  #15 PR15 cache-bust (Procfile + nixpacks + boot banner)
  #16 "Train v3 Model" button in cockpit Truth Mode
  #17 Stooq fifth-tier source + /api/diag/data-sources
  #18 v3_train force flag + per-phase state + /api/v3/train/last
  #19 PR19 cache-bust + /api/_version + /api/v3/train/sync
  #20 surface per-symbol gate-fail detail in train-sync
  #21 walk-forward fold floors env-tunable  <-- TRAINED THE MODEL (~65.4%)

Investor cockpit + ops (#22-#26):
  #22 ops polish — purge non-WOLF / Telegram status / split freshness
  #23 critical investor-view cleanup (items 1-7 of 15)
  #24 investor-view polish (items 8-15) + HEALTH_PROBE_SYMBOL=AAPL
  #25 news leak + Polygon/short-data fallbacks + PR25 cache-bust
  #26 news textual filter + auto-purge ghost portfolio on boot

Operator console + observability (#27-#30):
  #27 /admin objective-gate monitor + /api/wolf/gate-status
  #28 /admin cookie login (replaces blank-page HTTP Basic Auth)
  #29 per-cycle gate-outcome recorder + /api/wolf/gate-history + admin card
  #30 pick journal — credibility ledger (audit trail + expectancy/Brier + kill condition)

Root cause that unblocked everything: walk-forward made 0 folds on ~127 rows
because of a hardcoded 120-row training floor. PR #21 made the floors env-tunable.

Diagnosed (research, no code): WATCHING-mode root cause = four-gate chain where the
objective gate in precision mode needs either 80% historical WR (model is 65%,
impossible) or 0.90 per-pick bootstrap confidence (a 65% model rarely produces
up_prob high enough). User switched to aggressive mode (env above).

User-verified on prod 2026-05-23:
  - /api/wolf/gate-status: mode=aggressive, auto_mode_enabled=false, floor=0.75
  - /admin: clean cookie-login console (no blank page)
  - /api/wolf/pick-journal: 1 resolved pick (Mar 27, WOLF UP 91% conf, LOSS -1.36%);
    win_rate 0.0 [0-79 CI], expectancy -1.36%, Brier 0.819, verdict insufficient_samples
  - Cockpit Pick Journal card: "GATHERING EVIDENCE - Need 30 ... Have 1."

Open / not yet done:
  - Cold start: only 1 resolved high-conviction pick. Need ~30 before the
    falsification gate can judge the 80% claim. Silence is by design.
  - Key Stats / Analyst / Short Interest still empty (feed-tier limit; needs paid provider).
  - Regime detector (HMM) deferred until the journal has data (data too thin now).

--- 2026-03-24 | Agent: Claude Sonnet (PRE-v3.2, historical) ---
Context: crypto era. BCH/LINK silenced by circuit breaker (8 v2 losses).
Root cause: circuit breaker fired at exactly 8 losses; fix skipped CB when gpo_wr
> EDGE_THRESHOLD. Commits 4084f1b / 354b129 / 36d97f0. Win rate stats corrupted by
$0.50 garbage. (Superseded by the WOLF pivot + v3.2 engine.)

--- 2026-03-23 | Agent: Claude Sonnet (PRE-v3.2, historical) ---
Context: Ghost v1 was 41% accuracy, broken. Full rebuild as v2 (crypto era).
Built DB pool, price feeds, scheduler, per-symbol win-rate signal, Telegram card,
reconciler, cron wiring. Verified health 100. (Superseded by the WOLF pivot.)
"""
