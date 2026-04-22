#!/usr/bin/env bash
# scripts/go-no-go.sh — Ghost Protocol v2 release gate orchestrator.
#
# Chains all gate checks together and returns a clear pass/fail status.
# All checks must pass for a GO decision.
#
# Usage:
#   BASE_URL=https://... CRON_SECRET=<secret> bash scripts/go-no-go.sh
#
# Exit codes:
#   0  GO  — all checks passed
#   1  NO-GO — one or more checks failed
set -euo pipefail

BASE_URL="${BASE_URL:-https://ghost-protocol-v2-production.up.railway.app}"
CRON_SECRET="${CRON_SECRET:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║       Ghost Protocol v2 — Go / No-Go Gate               ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo "BASE_URL=$BASE_URL"
echo "SCRIPT_DIR=$SCRIPT_DIR"
echo

GATE_FAILURES=0

pass() { echo "  ✓ PASS: $1"; }
fail() { echo "  ✗ FAIL: $1"; GATE_FAILURES=$((GATE_FAILURES + 1)); }
warn() { echo "  ⚠ WARN: $1"; }
section() { echo; echo "── $1 ──────────────────────────────────────────────────"; }

# ── Gate 1: Core health endpoints ────────────────────────────────────────────
section "Gate 1 — Core health endpoints"

h1_code="$(curl -sS -o /tmp/gp_health.json -w "%{http_code}" "$BASE_URL/health")"
if [ "$h1_code" != "200" ]; then
  fail "/health returned HTTP $h1_code"
else
  pass "/health HTTP 200"
fi

h2_code="$(curl -sS -o /tmp/gp_api_health.json -w "%{http_code}" "$BASE_URL/api/health")"
if [ "$h2_code" != "200" ]; then
  fail "/api/health returned HTTP $h2_code"
else
  pass "/api/health HTTP 200"
fi

if [ -f /tmp/gp_health.json ] && [ -f /tmp/gp_api_health.json ]; then
  python3 - <<'PY' "$(cat /tmp/gp_health.json)" "$(cat /tmp/gp_api_health.json)" || { fail "health payload validation failed"; }
import json, sys
a = json.loads(sys.argv[1]); b = json.loads(sys.argv[2])
req = {"status", "score", "db", "issues", "warnings"}
assert req.issubset(a.keys()), f"/health missing keys: {req - set(a.keys())}"
assert req.issubset(b.keys()), f"/api/health missing keys: {req - set(b.keys())}"
assert a["status"] in ("healthy", "degraded", "critical"), f"bad status: {a['status']}"
assert b["status"] in ("healthy", "degraded", "critical"), f"bad status: {b['status']}"
print("    health payloads valid")
PY
  pass "health payload schema"
fi

# ── Gate 2: Stats / cockpit consistency ──────────────────────────────────────
section "Gate 2 — Stats / cockpit consistency"

stats_code="$(curl -sS -o /tmp/gp_stats.json -w "%{http_code}" "$BASE_URL/api/stats")"
ctx_code="$(curl -sS -o /tmp/gp_ctx.json -w "%{http_code}" "$BASE_URL/api/cockpit/context")"

if [ "$stats_code" != "200" ]; then
  fail "/api/stats returned HTTP $stats_code"
elif [ "$ctx_code" != "200" ]; then
  fail "/api/cockpit/context returned HTTP $ctx_code"
else
  python3 - <<'PY' "$(cat /tmp/gp_stats.json)" "$(cat /tmp/gp_ctx.json)" || { fail "stats/context consistency check failed"; }
import json, sys
s = json.loads(sys.argv[1]); c = json.loads(sys.argv[2])
assert s.get("ok") is True, f"stats ok=false"
assert c.get("ok") is True, f"cockpit ok=false"
cs = c.get("stats", {})
assert cs.get("wins") == s.get("wins"), f"wins mismatch {cs.get('wins')} != {s.get('wins')}"
assert cs.get("losses") == s.get("losses"), f"losses mismatch {cs.get('losses')} != {s.get('losses')}"
assert cs.get("post_v32") == s.get("post_v32"), "post_v32 mismatch"
print("    stats/cockpit consistent")
PY
  pass "/api/stats vs /api/cockpit/context consistent"
fi

# ── Gate 3: Diagnostics ───────────────────────────────────────────────────────
section "Gate 3 — Diagnostics"

diag_code="$(curl -sS -o /tmp/gp_diag.json -w "%{http_code}" "$BASE_URL/api/diagnostics")"
if [ "$diag_code" != "200" ]; then
  fail "/api/diagnostics returned HTTP $diag_code"
else
  python3 - <<'PY' "$(cat /tmp/gp_diag.json)" || { fail "diagnostics payload invalid"; }
import json, sys
d = json.loads(sys.argv[1])
assert "score" in d and "status" in d and "details" in d, "missing required keys"
assert isinstance(d.get("checks_passed"), int), "checks_passed not int"
score = d.get("score", 0)
if score < 50:
    print(f"    WARNING: diagnostics score={score} (critical)")
else:
    print(f"    diagnostics score={score} status={d.get('status')}")
PY
  pass "/api/diagnostics payload valid"
fi

# ── Gate 4: Coverage / model visibility ──────────────────────────────────────
section "Gate 4 — Coverage / model visibility"

cov_code="$(curl -sS -o /tmp/gp_cov.json -w "%{http_code}" "$BASE_URL/api/coverage")"
if [ "$cov_code" != "200" ]; then
  fail "/api/coverage returned HTTP $cov_code"
else
  python3 - <<'PY' "$(cat /tmp/gp_cov.json)" || { fail "coverage payload invalid"; }
import json, sys
c = json.loads(sys.argv[1])
assert c.get("ok") is True, "coverage ok=false"
ms = c.get("model_status", {})
assert "trained" in ms, "model_status missing 'trained'"
if ms.get("trained"):
    syms = ms.get("symbols", {})
    for name, meta in syms.items():
        assert "wf_acc_min" in meta, f"{name} missing wf_acc_min"
print(f"    coverage ok, trained={ms.get('trained')}")
PY
  pass "/api/coverage payload valid"
fi

# ── Gate 5: Health audit endpoint ────────────────────────────────────────────
section "Gate 5 — Health audit endpoint (POST /api/health/audit)"

AUDIT_HEADERS=()
if [ -n "$CRON_SECRET" ]; then
  AUDIT_HEADERS=(-H "x-cron-secret: $CRON_SECRET")
fi

audit_code="$(curl -sS -o /tmp/gp_audit.json -w "%{http_code}" \
  -X POST "${AUDIT_HEADERS[@]}" "$BASE_URL/api/health/audit")"

if [ "$audit_code" != "200" ]; then
  fail "/api/health/audit returned HTTP $audit_code"
else
  python3 - <<'PY' "$(cat /tmp/gp_audit.json)" || { fail "audit payload invalid"; }
import json, sys
body = json.loads(sys.argv[1])
assert body.get("ok") is True, f"ok={body.get('ok')}"
audit = body.get("audit", {})
required = {"run_ts", "stage", "overall_status", "summary", "findings"}
missing = required - set(audit.keys())
assert not missing, f"audit missing keys: {missing}"
summary = audit.get("summary", {})
overall = audit.get("overall_status", "")
assert overall in ("PASS", "WARN", "FAIL"), f"invalid overall_status={overall!r}"
print(f"    audit ok: overall={overall} checks={summary.get('total_checks')} passed={summary.get('passed')} failed={summary.get('failed')}")
if overall == "FAIL":
    for f in audit.get("findings", []):
        if f.get("status") == "FAIL":
            print(f"    FAIL finding: [{f['check']}] {f['evidence']}")
    raise SystemExit("audit overall_status=FAIL")
PY
  pass "/api/health/audit schema and status"
fi

# ── Gate 6: Auth rejection signature ─────────────────────────────────────────
section "Gate 6 — Auth rejection deterministic payload"

reject_code="$(curl -sS -o /tmp/gp_reject.json -w "%{http_code}" \
  -X POST -H "x-cron-secret: wrong-secret-gate-check" "$BASE_URL/api/health/audit")"

if [ "$reject_code" != "403" ]; then
  fail "/api/health/audit with bad secret returned HTTP $reject_code (expected 403)"
else
  python3 - <<'PY' "$(cat /tmp/gp_reject.json)" || { fail "403 error payload not deterministic"; }
import json, sys
body = json.loads(sys.argv[1])
required = {"ok", "error", "error_code", "stage", "ts"}
missing = required - set(body.keys())
assert not missing, f"error payload missing keys: {missing}"
assert body.get("ok") is False, f"ok={body.get('ok')!r} expected False"
assert isinstance(body.get("ts"), int), "ts is not int"
print(f"    deterministic 403: error_code={body.get('error_code')!r} stage={body.get('stage')!r}")
PY
  pass "403 error payload is deterministic"
fi

# ── Gate 7: Python release gate scripts ──────────────────────────────────────
section "Gate 7 — Python release gate scripts"

if command -v python3 &>/dev/null; then
  echo "  Running verify_health_audit.py..."
  if BASE_URL="$BASE_URL" CRON_SECRET="$CRON_SECRET" \
      python3 "$SCRIPT_DIR/verify_health_audit.py"; then
    pass "verify_health_audit.py"
  else
    fail "verify_health_audit.py"
  fi

  echo "  Running check_error_signatures.py..."
  if BASE_URL="$BASE_URL" CRON_SECRET="$CRON_SECRET" \
      python3 "$SCRIPT_DIR/check_error_signatures.py"; then
    pass "check_error_signatures.py"
  else
    fail "check_error_signatures.py"
  fi
else
  warn "python3 not found — skipping Python gate scripts"
fi

# ── Gate 8: Cockpit page ──────────────────────────────────────────────────────
section "Gate 8 — Cockpit page"

cockpit_code="$(curl -sS -o /tmp/gp_cockpit.html -w "%{http_code}" "$BASE_URL/cockpit")"
if [ "$cockpit_code" != "200" ]; then
  fail "/cockpit returned HTTP $cockpit_code"
else
  grep -Eq "Ghost Protocol|GHOST PROTOCOL" /tmp/gp_cockpit.html \
    || { fail "/cockpit missing expected title text"; }
  grep -q "tab-crypto" /tmp/gp_cockpit.html \
    || { fail "/cockpit missing expected tab marker (tab-crypto)"; }
  pass "/cockpit page reachable and contains expected markers"
fi

# ── Final verdict ─────────────────────────────────────────────────────────────
echo
echo "╔══════════════════════════════════════════════════════════╗"
if [ "$GATE_FAILURES" -eq 0 ]; then
  echo "║  GO: all release gates passed                           ║"
  echo "╚══════════════════════════════════════════════════════════╝"
  exit 0
else
  echo "║  NO-GO: $GATE_FAILURES gate check(s) failed                        ║"
  echo "╚══════════════════════════════════════════════════════════╝"
  exit 1
fi
