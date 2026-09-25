#!/usr/bin/env python3
"""Release gate: zero critical unresolved health-audit findings.

Outcomes (exactly one is printed as the final line):

- ``PASS: ...``    the audit ran and has zero critical unresolved findings.
- ``FAIL: ...``    the audit ran and failed, or the endpoint misbehaved (exit 1).
- ``SKIPPED: ...`` the gate could NOT be checked (no CRON_SECRET in this
                   environment). Exit 0 so CI stays green, but PASS is never
                   printed for a check that did not happen.

The audit call is always read-only: ``auto_fix=false&persist=false`` means no
self-heal writes and no history row on the target deployment.
"""
import json
import os
import sys
from typing import Any, Dict, List

import requests

# Never let a release gate write to the deployment it is judging.
AUDIT_QUERY = "auto_fix=false&persist=false"


def _fetch_json(url: str, *, method: str = "GET", headers: Dict[str, str] | None = None) -> Dict[str, Any]:
    resp = requests.request(method=method, url=url, headers=headers or {}, timeout=30)
    resp.raise_for_status()
    return json.loads(resp.text)


def _critical_unresolved(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in findings:
        if item.get("status") == "FAIL" and str(item.get("impact", "")).lower() == "critical":
            out.append(item)
    return out


def main() -> int:
    base_url = os.getenv("BASE_URL", "https://ghost-protocol-v2-production.up.railway.app").rstrip("/")
    cron_secret = os.getenv("CRON_SECRET", "").strip()
    if not cron_secret:
        print(
            "SKIPPED: CRON_SECRET is not set, so POST /api/health/audit cannot be called; "
            "the zero-critical-findings gate was NOT checked."
        )
        return 0

    url = f"{base_url}/api/health/audit?{AUDIT_QUERY}"
    headers: Dict[str, str] = {"X-Cron-Secret": cron_secret}

    try:
        payload = _fetch_json(url, method="POST", headers=headers)
    except requests.HTTPError as exc:
        body = (exc.response.text or "").strip()
        preview = body[:300] if body else "<empty>"
        print(f"FAIL: /api/health/audit returned HTTP {exc.response.status_code} body={preview}")
        return 1
    except Exception as exc:  # pragma: no cover - defensive live gate
        print(f"FAIL: unable to run /api/health/audit: {type(exc).__name__}")
        return 1

    if payload.get("ok") is not True:
        print(f"FAIL: /api/health/audit returned error payload: {payload}")
        return 1

    audit = payload.get("audit")
    if not isinstance(audit, dict):
        print("FAIL: /api/health/audit returned no audit report")
        return 1
    findings = audit.get("findings")
    if not isinstance(findings, list) or not findings:
        # A real audit always reports its PASS records too; an empty list means
        # nothing was checked, which must not read as "zero critical".
        print("FAIL: /api/health/audit returned no findings; cannot verify zero critical")
        return 1
    critical = _critical_unresolved(findings)

    print(f"audit_status={audit.get('status')}")
    print(f"unresolved_count={audit.get('unresolved_count')}")
    print(f"critical_unresolved={len(critical)}")

    if critical:
        print("FAIL: critical unresolved health-audit findings present")
        for item in critical:
            loc = item.get("location", "unknown")
            ev = item.get("evidence", "")
            print(f"- {loc}: {ev}")
        return 1

    print("PASS: health audit has zero critical unresolved findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
