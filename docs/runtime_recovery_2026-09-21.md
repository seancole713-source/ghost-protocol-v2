# Production runtime recovery - September 21, 2026

## Incident evidence

At 13:13 UTC, production version, health, picks, squeeze picks and v3 status all
returned HTTP 502. Unavailability is not evidence of zero predictions.

PR #203's deployed commit `c459895` (deployment
`86973ac5-cf50-439c-a196-7c87e955799b`) crashed during lifespan imports. Its pinned
Python 3.13.13 / NumPy 1.26.4 stack loaded a NumPy extension requiring GLIBC_2.38,
which the final runtime image did not provide. Unit/integration CI success on a
different OS image did not establish production ABI compatibility. This was an
existing unsupported version pairing exposed by the rebuild, not a successful
production verification of #203.

Recovery attempted the exact previous image, commit `6572468`, deployment
`b7666ab9-8cd1-4b15-a2aa-75b57b8e4502`. Native imports worked, but startup spent
about 118 seconds in schema initialization. Uvicorn bound around 124 seconds
after startup, beyond the configured 120-second health deadline. Railway marked
that recovery FAILED; it must not be described as a successful rollback.

## Repair

- Python 3.12.14 in both runtime declarations and CI reading `.python-version`.
  NumPy 1.26.4 officially supports Python 3.9-3.12. Numerical/model package pins
  are deliberately unchanged; no speculative NumPy/sklearn major upgrade.
- Critical native dependencies require binary wheels rather than locally built,
  potentially incompatible ABI artifacts.
- `scripts/runtime_preflight.py` validates the exact interpreter and pinned native
  versions, imports the numerical/database extensions, and exercises NumPy,
  SciPy, pandas, sklearn and XGBoost using tiny synthetic arrays. It imports no
  Ghost app, connects to no DB/provider and creates no forecast or model artifact.
- Run the check in CI, Railway's **final-image pre-deploy container**, and on every
  web boot before Uvicorn. An import or arithmetic failure exits nonzero rather
  than letting an HTTP-only check hide a broken prediction runtime.
- A bounded 300-second startup allowance accommodates the measured cold schema
  work; the `/health` check is retained. The release wait has a 15-minute wall
  clock deadline for building and starting, still requiring the **exact commit
  three consecutive times**. No HTTP or statistical gate is bypassed.

## Validation record

A fresh isolated Python 3.12.14 environment passed native preflight, pip check,
2,112 unit tests, 47 isolated localhost PostgreSQL integration tests, Ruff and
compile checks. The old Python 3.13 environment correctly failed preflight.
A pre-existing scanner test mocked RTH but used the real premarket elapsed
fraction; fixing the fixture removes that clock-dependent failure without
changing a production gate. Linux CI results, deployment ID and repeat
production observations belong in the PR release evidence; this source
document is not itself deployment proof.

## Unchanged constraints and follow-up

No frozen experiment, model weights, feature contract, acceptance threshold,
kill switch, two-agent quorum, trading permission or historical outcome is
changed. Synthetic preflight data never contributes to accuracy counts.

The 118-second schema startup remains a performance issue, not solved by a
longer deadline. Legacy migration code repeatedly updates artifact fields and
rebuilds an index; measuring and making migrations truly one-time is follow-up,
not an excuse for an unreviewed production data rewrite during recovery.

Production must demonstrate repeated completed scans, quote clocks, current
forecast timestamps and explicit rejection reasons. Research observations are
not approved trading picks. The recovery does not establish 70% accuracy, nor
can it recreate forecasts missed during the outage. Pre-market and regular
sessions need separate observed validation; regular-session success alone must
not be called pre-market proof.

**Deploy freeze (F29):** Railway's pre-deploy (`python scripts/runtime_preflight.py --pre-deploy`) fails any deploy on an NYSE trading day (edge/calendar.py) between 08:00 and 16:30 America/New_York so the scheduler is never restarted mid-session; the previous deployment keeps serving. For an emergency fix set `DEPLOY_FREEZE_OVERRIDE=1` on the service, redeploy, then remove it. Boot and CI run the preflight without the flag, and any error in the freeze check itself allows the deploy with a warning.

## Primary references

- [NumPy 1.26.4 supported Python versions](https://numpy.org/devdocs/release/1.26.4-notes.html)
- [Python 3.12.14 release](https://www.python.org/downloads/release/python-31214/)
- [Railpack Python version selection](https://railpack.com/languages/python)
- [Railway pre-deploy commands](https://docs.railway.com/guides/pre-deploy-command)
