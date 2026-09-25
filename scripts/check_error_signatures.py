#!/usr/bin/env python3
"""Release gate: alert on ERROR signatures not in the baseline.

Sources: ``/api/diagnostics`` (admin-cookie gated, normally not collectible
from CI) and the read-only health audit (``auto_fix=false&persist=false``,
needs CRON_SECRET), whose findings already include the diagnostics errors.

Outcomes (the final line is exactly one of):

- ``PASS: ...``    the audit source was collected and has no unknown signature.
- ``FAIL: ...``    an unknown signature was found, or an endpoint misbehaved.
- ``SKIPPED: ...`` no source that covers the application could be collected
                   (e.g. CRON_SECRET unset). Exit 0 so CI stays green, but a
                   check that did not happen is never reported as PASS.
"""
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import requests

# Never let a release gate write to the deployment it is judging.
AUDIT_QUERY = "auto_fix=false&persist=false"

def _fetch_json(url: str, *, method: str = "GET", headers: Dict[str, str] | None = None) -> Dict[str, Any]:
    resp = requests.request(method=method, url=url, headers=headers or {}, timeout=30)
    resp.raise_for_status()
    return json.loads(resp.text)


def _normalize(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"\b\d+\b", "<num>", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered[:180]


def _load_baseline(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    patterns = payload.get("allowed_patterns", [])
    return [p for p in patterns if isinstance(p, dict) and "check" in p and "detail_regex" in p]


def _is_known(sig: Dict[str, str], baseline: List[Dict[str, str]]) -> bool:
    for rule in baseline:
        if sig["check"] != rule["check"]:
            continue
        if re.search(rule["detail_regex"], sig["detail"]):
            return True
    return False


def _diagnostics_errors(base_url: str) -> Optional[List[Dict[str, str]]]:
    """Diagnostics error signatures, or None when the source is not collectible."""
    try:
        payload = _fetch_json(f"{base_url}/api/diagnostics")
    except requests.HTTPError as exc:
        # /api/diagnostics intentionally returns 404 without the admin cookie so
        # public CI/live gates do not leak internals. Treat auth-gated absence as
        # "not collectible" (None) — NOT as "no errors".
        if exc.response is not None and exc.response.status_code in (401, 403, 404):
            print("WARN: /api/diagnostics gated; diagnostics error signatures not collected")
            return None
        raise
    details = payload.get("details", {})
    errors = details.get("errors", []) if isinstance(details, dict) else []
    out: List[Dict[str, str]] = []
    for item in errors:
        check = str(item.get("check", "unknown"))
        detail = _normalize(str(item.get("detail", "")))
        out.append({"source": "diagnostics", "check": check, "detail": detail})
    return out


def _audit_failures(base_url: str, cron_secret: str) -> Optional[List[Dict[str, str]]]:
    """Audit FAIL signatures (read-only call), or None when CRON_SECRET is unset."""
    if not cron_secret:
        print("WARN: CRON_SECRET unset; /api/health/audit error signatures not collected")
        return None
    headers: Dict[str, str] = {"X-Cron-Secret": cron_secret}
    payload = _fetch_json(f"{base_url}/api/health/audit?{AUDIT_QUERY}", method="POST", headers=headers)
    if payload.get("ok") is not True:
        return [{"source": "audit", "check": "audit.endpoint", "detail": _normalize(str(payload))}]
    audit = payload.get("audit", {})
    findings = audit.get("findings", []) if isinstance(audit, dict) else []
    if not isinstance(findings, list) or not findings:
        # A real audit always lists its PASS records; empty means nothing ran.
        return [{"source": "audit", "check": "audit.no_findings",
                 "detail": "health audit returned no findings"}]
    out: List[Dict[str, str]] = []
    for item in findings:
        if item.get("status") != "FAIL":
            continue
        check = f"audit:{item.get('location', 'unknown')}"
        detail = _normalize(str(item.get("evidence", "")))
        out.append({"source": "audit", "check": check, "detail": detail})
    return out


def _print_annotation(level: str, message: str) -> None:
    # GitHub annotation format, still readable in plain logs.
    print(f"::{level}::{message}")


def main() -> int:
    base_url = os.getenv("BASE_URL", "https://ghost-protocol-v2-production.up.railway.app").rstrip("/")
    cron_secret = os.getenv("CRON_SECRET", "").strip()
    baseline_path = os.getenv(
        "ERROR_SIGNATURE_BASELINE",
        ".github/error-signatures-baseline.json",
    )

    if not os.path.exists(baseline_path):
        print(f"FAIL: baseline file not found: {baseline_path}")
        return 1

    try:
        baseline = _load_baseline(baseline_path)
    except Exception as exc:
        print(f"FAIL: unable to load baseline: {exc}")
        return 1

    try:
        diag = _diagnostics_errors(base_url)
        audit = _audit_failures(base_url, cron_secret)
    except requests.HTTPError as exc:
        body = (exc.response.text or "").strip()
        preview = body[:300] if body else "<empty>"
        print(f"FAIL: endpoint returned HTTP {exc.response.status_code} body={preview}")
        return 1
    except Exception as exc:  # pragma: no cover - defensive live gate
        print(f"FAIL: unable to collect error signatures: {type(exc).__name__}")
        return 1

    signatures: List[Dict[str, str]] = (diag or []) + (audit or [])
    # The audit embeds the diagnostics errors, so it alone covers the app.
    # Without it the gate cannot vouch for anything beyond what it did see.
    covered = audit is not None
    skipped = [name for name, src in (("diagnostics", diag), ("health-audit", audit)) if src is None]

    unknown: List[Tuple[str, str, str]] = []
    for sig in signatures:
        if not _is_known(sig, baseline):
            unknown.append((sig["source"], sig["check"], sig["detail"]))

    if unknown:
        _print_annotation("error", f"Detected {len(unknown)} new error signatures")
        print("FAIL: new error signatures detected")
        for source, check, detail in unknown:
            print(f"- [{source}] {check}: {detail}")
        return 1

    if not covered:
        _print_annotation("warning", "error-signature gate SKIPPED: " + ", ".join(skipped) + " not collected")
        print(
            "SKIPPED: error signatures were NOT checked ("
            + ", ".join(skipped)
            + " not collectible; set CRON_SECRET for the read-only health audit)."
        )
        return 0

    if not signatures:
        print("PASS: no active error signatures detected")
        return 0

    _print_annotation("warning", f"{len(signatures)} known error signatures detected")
    print("PASS: only known error signatures detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
