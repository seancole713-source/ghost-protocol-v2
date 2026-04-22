#!/usr/bin/env python3
import json
import os
import sys
from typing import Any, Dict, List

import requests


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
    url = f"{base_url}/api/health/audit?auto_fix=true"
    headers: Dict[str, str] = {}
    if cron_secret:
        headers["X-Cron-Secret"] = cron_secret

    try:
        payload = _fetch_json(url, method="POST", headers=headers)
    except requests.HTTPError as exc:
        body = (exc.response.text or "").strip()
        preview = body[:300] if body else "<empty>"
        print(f"FAIL: /api/health/audit returned HTTP {exc.response.status_code} body={preview}")
        return 1
    except Exception as exc:  # pragma: no cover - defensive live gate
        print(f"FAIL: unable to run /api/health/audit: {exc}")
        return 1

    if payload.get("ok") is not True:
        print(f"FAIL: /api/health/audit returned error payload: {payload}")
        return 1

    audit = payload.get("audit", {})
    findings = audit.get("findings", []) if isinstance(audit, dict) else []
    critical = _critical_unresolved(findings if isinstance(findings, list) else [])

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
