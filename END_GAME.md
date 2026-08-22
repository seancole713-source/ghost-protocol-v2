# END GAME — Completion Ledger

**Generated:** 2026-08-22
**Baseline:** 1557 passed / 34 skipped · prod `/health` 95 · mypy clean · 1 ruff error

This ledger is the authoritative record of what prevents Ghost Protocol v2 from
being considered *finished*. Each item is root-caused, not symptom-patched.

---

## Classification note (why the ledger is short, not 500 items)

A full-repository audit (TODO/FIXME/HACK/placeholder/stub/mock sweep, frontend↔backend
contract diff, swallowed-exception and dead-code review, skipped-test enumeration,
Git history) found the codebase is **structurally complete and internally
consistent**, with a large volume of *intentional, documented* deferrals that are
**not defects**:

- **Phase-3 ML depth** (`PROJECT_STATE.py` `TODO`, `docs/`): FinBERT sentiment,
  HMM regime detector, options-flow GEX model, KL-divergence drift, regime-conditional
  isotonic calibration. These are explicitly deferred pending more labeled data or a
  paid-provider budget decision — building them now against ~250 trading days would
  be the wrong call (overfit), not "finishing" the app.
- **Paid data-provider gaps** (Key Stats / Analyst / Short Interest empty on the
  free tier) — a budget decision documented in `PROJECT_STATE.md`, not code.
- **Graceful-degradation `except` + `note_suppressed()`** paths — the product's
  designed behavior (return `ok:false`/empty fallback, never crash the console).
- **34 skipped tests** — all are `@pytest.mark.integration` gated on
  `TEST_DATABASE_URL` + `GHOST_INTEGRATION_TESTS=1` (intentional; needs a live DB).

The `placeholder`/`stub`/`TODO` matches in source are **documented** markers
(`config/symbols.py` V3 strategy seed values "retrain in Phase 3", `signal_engine.py`
"placeholder until the HMM specialist", `_stooq_spot` deprecation stub, HTML input
`placeholder=` attributes). None are mock data shipped to production.

---

## Open items (root-caused defects found in audit)

| ID | Category | Severity | Root cause | Fix | Verify | Status |
|----|----------|----------|-----------|-----|--------|--------|
| EG-001 | Frontend | Medium | `cockpit.html` has two short-interest renderers (`loadShortInterest` + `renderShortInterest`) writing the same `#short-tile`; `renderShortInterest` reads `ctx.short_data.short_float` but backend field is `short_float_pct` (always `undefined`). Both fire, last-writer-wins clobbers. | Delete dead `renderShortInterest`; keep `loadShortInterest` as single source. | grep shows one renderer; `/cockpit` renders short tile | VERIFIED |
| EG-002 | Frontend | Medium | `ghost_console.html` `renderMyPick` renders ledger confidence as `Math.round(lg.confidence)` (0..1 fraction) → always `0%`/`1%`, while the adjacent active-pick line correctly uses `*100`. | Multiply by 100 to match unit. | grep; UI shows correct % | VERIFIED |
| EG-003 | Data/backend | Low | `wolf_app._auto_purge_bad_models` swallows `except Exception: pass`, so corrupt/`null`-accuracy metadata silently never purges and never surfaces. | Log the skip with model id. | grep; no silent pass | VERIFIED |
| EG-004 | Data/backend | Medium | `routes_admin` `morning_card.today` swallows DB read failure (`except: pass`), leaving `_mc_last=None` → falsely deduces "No card today" and −10 health even when card *was* sent. | On read failure, treat as unknown (warning, no deduction). | grep; deduction gated on successful read | VERIFIED |
| EG-005 | Data/backend | Low | `_stooq_spot` is a permanent `return None` stub still wired into the live prev_close chain and `check_feeds`; makes the "5 feeds" health summary misleading (can never exceed 4). | Remove stub + its call sites; report 4 live feeds honestly. | grep: `_stooq_spot` gone; feed summary `/4` | VERIFIED |
| EG-006 | Dead code | Low | `config/symbols.py` `DIRECTION_FLIP='flip'` is unreferenced anywhere in core/api/routes. | Remove constant (and its type-comment mention). | grep: zero refs | VERIFIED |
| EG-007 | Tooling | Low | `tests/test_checklist_ledger.py:67` assigns `orig` never used (ruff F841), the only lint failure. | Remove the dead assignment. | `npm run lint` clean | VERIFIED |

---

## Verified-clean (audited, no defect)

- All `fetch('/api/...')` endpoints in the five HTML pages resolve to real routes.
- `picks.html`, `cockpit.html` pick-journal / attribution / kill-status / degraded /
  pnl / stats field reads all match backend output (no mismatches).
- No duplicate route definitions; `/api/squeeze/hunter/{board,scan}` share one
  handler by design.
- Every `except Exception as e:` binding in `core/prediction.py`, `core/prices.py`,
  `core/signal_engine.py` uses `e` (logging or `str(e)`) — no unused bindings.
- `core/pnl.py` env parsing uses sane fallbacks (intentional degradation).

---

## Status key

- **DISCOVERED** → root-caused, fix queued
- **IN_PROGRESS** → fix in flight
- **FIXED** → code changed
- **VERIFIED** → change proven by test/grep/runtime
- **BLOCKED** → external dependency (paid provider, live DB, operator action)
