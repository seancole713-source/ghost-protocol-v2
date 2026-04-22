#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-https://ghost-protocol-v2-production.up.railway.app}"

echo "== Ghost Deploy Go/No-Go =="
echo "BASE_URL=$BASE_URL"
echo

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; exit 1; }

# 1) Core health endpoints
h1="$(curl -fsS "$BASE_URL/health")" || fail "/health unreachable"
h2_code="$(curl -sS -o /tmp/gp_api_health.json -w "%{http_code}" "$BASE_URL/api/health")"
[ "$h2_code" = "200" ] || fail "/api/health returned HTTP $h2_code"
h2="$(cat /tmp/gp_api_health.json)"

python3 - <<'PY' "$h1" "$h2" || fail "health payload validation failed"
import json,sys
a=json.loads(sys.argv[1]); b=json.loads(sys.argv[2])
req={"status","score","db","issues","warnings"}
assert req.issubset(a.keys()), f"/health missing keys: {req-set(a.keys())}"
assert req.issubset(b.keys()), f"/api/health missing keys: {req-set(b.keys())}"
assert a["status"] in ("healthy","degraded","critical")
assert b["status"] in ("healthy","degraded","critical")
print("health payloads valid")
PY
pass "/health and /api/health"

# 2) Cockpit + stats consistency
stats="$(curl -fsS "$BASE_URL/api/stats")" || fail "/api/stats unreachable"
ctx="$(curl -fsS "$BASE_URL/api/cockpit/context")" || fail "/api/cockpit/context unreachable"

python3 - <<'PY' "$stats" "$ctx" || fail "stats/context consistency failed"
import json,sys
s=json.loads(sys.argv[1]); c=json.loads(sys.argv[2])
assert s.get("ok") is True, "stats ok=false"
assert c.get("ok") is True, "cockpit ok=false"
cs=c.get("stats",{})
assert cs.get("wins")==s.get("wins"), f"wins mismatch {cs.get('wins')} != {s.get('wins')}"
assert cs.get("losses")==s.get("losses"), f"losses mismatch {cs.get('losses')} != {s.get('losses')}"
assert cs.get("post_v32")==s.get("post_v32"), "post_v32 mismatch"
print("stats/cockpit consistency valid")
PY
pass "/api/stats vs /api/cockpit/context"

# 3) Diagnostics endpoint
diag="$(curl -fsS "$BASE_URL/api/diagnostics")" || fail "/api/diagnostics unreachable"
python3 - <<'PY' "$diag" || fail "diagnostics payload invalid"
import json,sys
d=json.loads(sys.argv[1])
assert "score" in d and "status" in d and "details" in d
assert isinstance(d.get("checks_passed"), int)
print("diagnostics payload valid")
PY
pass "/api/diagnostics"

# 4) Coverage/model visibility
cov="$(curl -fsS "$BASE_URL/api/coverage")" || fail "/api/coverage unreachable"
python3 - <<'PY' "$cov" || fail "coverage payload invalid"
import json,sys
c=json.loads(sys.argv[1])
assert c.get("ok") is True
ms=c.get("model_status",{})
assert "trained" in ms
if ms.get("trained"):
    syms=ms.get("symbols",{})
    for name,meta in syms.items():
        assert "wf_acc_min" in meta, f"{name} missing wf_acc_min"
print("coverage payload valid")
PY
pass "/api/coverage"

# 5) Cockpit page reachable
cockpit_code="$(curl -sS -o /tmp/gp_cockpit.html -w "%{http_code}" "$BASE_URL/cockpit")"
[ "$cockpit_code" = "200" ] || fail "/cockpit returned HTTP $cockpit_code"
grep -q "GHOST PROTOCOL" /tmp/gp_cockpit.html || fail "/cockpit missing expected title text"
pass "/cockpit page"

echo
echo "GO: all checks passed"
