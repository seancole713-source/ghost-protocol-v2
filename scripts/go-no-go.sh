#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-https://ghost-protocol-v2-production.up.railway.app}"

echo "== Ghost Deploy Go/No-Go =="
echo "BASE_URL=$BASE_URL"
echo

python3 - <<'PY' "$BASE_URL"
import sys

import requests

base_url = sys.argv[1].rstrip("/")


def pass_check(name: str) -> None:
    print(f"PASS: {name}")


def fail_check(name: str, detail: str) -> None:
    print(f"FAIL: {name} - {detail}")
    raise SystemExit(1)


def get_json(path: str) -> dict:
    url = f"{base_url}{path}"
    resp = requests.get(url, timeout=20)
    if resp.status_code != 200:
        fail_check(path, f"HTTP {resp.status_code}")
    return resp.json()


h1 = get_json("/health")
h2 = get_json("/api/health")
required = {"status", "score", "db", "issues", "warnings"}
if not required.issubset(h1):
    fail_check("/health", f"missing keys {required - set(h1)}")
if not required.issubset(h2):
    fail_check("/api/health", f"missing keys {required - set(h2)}")
if h1.get("status") not in ("healthy", "degraded", "critical"):
    fail_check("/health", f"invalid status {h1.get('status')}")
if h2.get("status") not in ("healthy", "degraded", "critical"):
    fail_check("/api/health", f"invalid status {h2.get('status')}")
pass_check("/health and /api/health")

stats = get_json("/api/stats")
ctx = get_json("/api/cockpit/context")
if stats.get("ok") is not True:
    fail_check("/api/stats", "ok=false")
if ctx.get("ok") is not True:
    fail_check("/api/cockpit/context", "ok=false")
cs = ctx.get("stats", {})
if cs.get("wins") != stats.get("wins"):
    fail_check("stats consistency", f"wins mismatch {cs.get('wins')} != {stats.get('wins')}")
if cs.get("losses") != stats.get("losses"):
    fail_check("stats consistency", f"losses mismatch {cs.get('losses')} != {stats.get('losses')}")
if cs.get("post_v32") != stats.get("post_v32"):
    fail_check("stats consistency", "post_v32 mismatch")
pass_check("/api/stats vs /api/cockpit/context")

diag = get_json("/api/diagnostics")
if not all(k in diag for k in ("score", "status", "details")):
    fail_check("/api/diagnostics", "missing keys")
if not isinstance(diag.get("checks_passed"), int):
    fail_check("/api/diagnostics", "checks_passed not int")
pass_check("/api/diagnostics")

cov = get_json("/api/coverage")
if cov.get("ok") is not True:
    fail_check("/api/coverage", "ok=false")
ms = cov.get("model_status", {})
if "trained" not in ms:
    fail_check("/api/coverage", "model_status missing trained")
if ms.get("trained"):
    for name, meta in ms.get("symbols", {}).items():
        if "wf_acc_min" not in meta:
            fail_check("/api/coverage", f"{name} missing wf_acc_min")
pass_check("/api/coverage")

cockpit = requests.get(f"{base_url}/cockpit", timeout=20)
if cockpit.status_code != 200:
    fail_check("/cockpit", f"HTTP {cockpit.status_code}")
html = cockpit.text
if "Ghost Protocol" not in html and "GHOST PROTOCOL" not in html:
    fail_check("/cockpit", "missing expected title text")
if "tab-crypto" not in html:
    fail_check("/cockpit", "missing expected tab marker")
pass_check("/cockpit page")

print()
print("GO: all checks passed")
PY
