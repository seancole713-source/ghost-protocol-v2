#!/usr/bin/env python3
"""
scripts/verify_health_audit.py — Release gate: verify POST /api/health/audit.

Exit codes:
  0  All checks passed
  1  One or more checks failed
  2  Endpoint unreachable or returned unexpected HTTP status

Usage:
    CRON_SECRET=<secret> python scripts/verify_health_audit.py
    BASE_URL=https://... CRON_SECRET=<secret> python scripts/verify_health_audit.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package not installed. Run: pip install requests")
    sys.exit(2)

BASE_URL = os.getenv("BASE_URL", "https://ghost-protocol-v2-production.up.railway.app").rstrip("/")
CRON_SECRET = os.getenv("CRON_SECRET", "")
TIMEOUT = int(os.getenv("AUDIT_TIMEOUT", "60"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pass(msg: str) -> None:
    print(f"  PASS  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}")


def _warn(msg: str) -> None:
    print(f"  WARN  {msg}")


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

REQUIRED_TOP_KEYS = {"ok", "audit"}
REQUIRED_AUDIT_KEYS = {"run_ts", "stage", "overall_status", "summary", "findings"}
REQUIRED_SUMMARY_KEYS = {"total_checks", "passed", "warned", "failed", "coverage_pct"}
VALID_STATUSES = {"PASS", "WARN", "FAIL"}
REQUIRED_FINDING_KEYS = {"check", "status", "location", "evidence", "impact", "auto_fix", "fix_result"}


def _validate_schema(payload: Dict[str, Any]) -> List[str]:
    """Return list of schema violations (empty = valid)."""
    errors: List[str] = []

    missing_top = REQUIRED_TOP_KEYS - set(payload.keys())
    if missing_top:
        errors.append(f"top-level missing keys: {sorted(missing_top)}")
        return errors  # can't go deeper

    if payload.get("ok") is not True:
        errors.append(f"ok={payload.get('ok')!r} — expected True")

    audit = payload.get("audit", {})
    missing_audit = REQUIRED_AUDIT_KEYS - set(audit.keys())
    if missing_audit:
        errors.append(f"audit missing keys: {sorted(missing_audit)}")

    summary = audit.get("summary", {})
    missing_summary = REQUIRED_SUMMARY_KEYS - set(summary.keys())
    if missing_summary:
        errors.append(f"audit.summary missing keys: {sorted(missing_summary)}")

    overall = audit.get("overall_status", "")
    if overall not in VALID_STATUSES:
        errors.append(f"audit.overall_status={overall!r} not in {VALID_STATUSES}")

    findings = audit.get("findings", [])
    if not isinstance(findings, list):
        errors.append("audit.findings is not a list")
    else:
        for i, f in enumerate(findings[:5]):  # spot-check first 5
            missing_f = REQUIRED_FINDING_KEYS - set(f.keys())
            if missing_f:
                errors.append(f"findings[{i}] missing keys: {sorted(missing_f)}")
            if f.get("status") not in VALID_STATUSES:
                errors.append(f"findings[{i}].status={f.get('status')!r} invalid")

    return errors


# ---------------------------------------------------------------------------
# Error payload validation
# ---------------------------------------------------------------------------

def _validate_error_payload(payload: Dict[str, Any], http_status: int) -> List[str]:
    """Validate that error responses have the deterministic shape."""
    errors: List[str] = []
    required = {"ok", "error", "error_code", "stage", "ts"}
    missing = required - set(payload.keys())
    if missing:
        errors.append(f"error payload missing keys: {sorted(missing)}")
    if payload.get("ok") is not False:
        errors.append(f"error payload ok={payload.get('ok')!r} — expected False")
    if not isinstance(payload.get("ts"), int):
        errors.append("error payload ts is not an int")
    return errors


# ---------------------------------------------------------------------------
# Gate checks
# ---------------------------------------------------------------------------

def check_auth_rejection() -> bool:
    """Endpoint must return 403 with deterministic payload for bad secret."""
    print("\n[1] Auth rejection (bad secret)")
    url = f"{BASE_URL}/api/health/audit"
    try:
        resp = requests.post(
            url,
            headers={"x-cron-secret": "definitely-wrong-secret-xyz"},
            timeout=TIMEOUT,
        )
    except Exception as e:
        _fail(f"request failed: {e}")
        return False

    if resp.status_code != 403:
        _fail(f"expected HTTP 403, got {resp.status_code}")
        return False
    _pass(f"HTTP 403 returned")

    try:
        body = resp.json()
    except Exception:
        _fail("403 response body is not valid JSON")
        return False

    errs = _validate_error_payload(body, 403)
    if errs:
        for e in errs:
            _fail(e)
        return False

    _pass(f"deterministic error payload: error_code={body.get('error_code')!r}")
    return True


def check_successful_audit() -> bool:
    """Endpoint must return 200 with valid audit schema."""
    print("\n[2] Successful audit (valid secret)")
    url = f"{BASE_URL}/api/health/audit"
    headers = {}
    if CRON_SECRET:
        headers["x-cron-secret"] = CRON_SECRET

    try:
        resp = requests.post(url, headers=headers, timeout=TIMEOUT)
    except Exception as e:
        _fail(f"request failed: {e}")
        return False

    if resp.status_code != 200:
        _fail(f"expected HTTP 200, got {resp.status_code}")
        try:
            body = resp.json()
            _fail(f"body: {json.dumps(body)[:300]}")
        except Exception:
            pass
        return False
    _pass("HTTP 200 returned")

    try:
        body = resp.json()
    except Exception:
        _fail("response body is not valid JSON")
        return False

    errs = _validate_schema(body)
    if errs:
        for e in errs:
            _fail(e)
        return False
    _pass("response schema valid")

    audit = body["audit"]
    summary = audit["summary"]
    overall = audit["overall_status"]
    stage = audit.get("stage", "unknown")
    total = summary.get("total_checks", 0)
    passed = summary.get("passed", 0)
    warned = summary.get("warned", 0)
    failed = summary.get("failed", 0)
    cov = summary.get("coverage_pct", 0.0)

    _pass(f"stage={stage!r}  overall={overall}  checks={total}  passed={passed}  warned={warned}  failed={failed}  coverage={cov}%")

    if overall == "FAIL":
        _fail("overall_status=FAIL — audit found critical issues")
        # Print failing findings for CI visibility
        for f in audit.get("findings", []):
            if f.get("status") == "FAIL":
                _fail(f"  [{f['check']}] {f['evidence']}")
        return False

    if overall == "WARN":
        _warn("overall_status=WARN — audit found warnings (non-blocking)")
        for f in audit.get("findings", []):
            if f.get("status") == "WARN":
                _warn(f"  [{f['check']}] {f['evidence']}")

    return True


def check_history_endpoint() -> bool:
    """GET /api/health/audit/history must return valid schema."""
    print("\n[3] Audit history endpoint")
    url = f"{BASE_URL}/api/health/audit/history"
    try:
        resp = requests.get(url, timeout=TIMEOUT)
    except Exception as e:
        _fail(f"request failed: {e}")
        return False

    if resp.status_code != 200:
        _fail(f"expected HTTP 200, got {resp.status_code}")
        return False
    _pass("HTTP 200 returned")

    try:
        body = resp.json()
    except Exception:
        _fail("response body is not valid JSON")
        return False

    if body.get("ok") is not True:
        _fail(f"ok={body.get('ok')!r}")
        return False

    runs = body.get("runs", [])
    if not isinstance(runs, list):
        _fail("runs is not a list")
        return False

    _pass(f"history returned {len(runs)} run(s)")
    return True


def check_response_time() -> bool:
    """Audit endpoint must respond within AUDIT_TIMEOUT seconds."""
    print("\n[4] Response time")
    url = f"{BASE_URL}/api/health/audit"
    headers = {}
    if CRON_SECRET:
        headers["x-cron-secret"] = CRON_SECRET

    start = time.monotonic()
    try:
        resp = requests.post(url, headers=headers, timeout=TIMEOUT)
    except Exception as e:
        _fail(f"request failed: {e}")
        return False
    elapsed = time.monotonic() - start

    if elapsed > TIMEOUT * 0.8:
        _warn(f"response took {elapsed:.1f}s (>{TIMEOUT * 0.8:.0f}s threshold)")
    else:
        _pass(f"response in {elapsed:.2f}s")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("Ghost Protocol v2 — Health Audit Gate")
    print(f"BASE_URL : {BASE_URL}")
    print(f"SECRET   : {'set' if CRON_SECRET else 'NOT SET (open endpoint)'}")
    print("=" * 60)

    results = [
        check_auth_rejection(),
        check_successful_audit(),
        check_history_endpoint(),
        check_response_time(),
    ]

    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    if all(results):
        print(f"GO: all {total} gate checks passed")
        return 0
    else:
        print(f"NO-GO: {total - passed}/{total} gate checks failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
