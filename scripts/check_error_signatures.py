#!/usr/bin/env python3
import json
import os
import re
import sys
from typing import Any, Dict, List, Tuple

import requests

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


def _diagnostics_errors(base_url: str) -> List[Dict[str, str]]:
    payload = _fetch_json(f"{base_url}/api/diagnostics")
    details = payload.get("details", {})
    errors = details.get("errors", []) if isinstance(details, dict) else []
    out: List[Dict[str, str]] = []
    for item in errors:
        check = str(item.get("check", "unknown"))
        detail = _normalize(str(item.get("detail", "")))
        out.append({"source": "diagnostics", "check": check, "detail": detail})
    return out


def _audit_failures(base_url: str, cron_secret: str) -> List[Dict[str, str]]:
    headers: Dict[str, str] = {}
    if cron_secret:
        headers["X-Cron-Secret"] = cron_secret
    payload = _fetch_json(f"{base_url}/api/health/audit?auto_fix=true", method="POST", headers=headers)
    if payload.get("ok") is not True:
        return [{"source": "audit", "check": "audit.endpoint", "detail": _normalize(str(payload))}]
    audit = payload.get("audit", {})
    findings = audit.get("findings", []) if isinstance(audit, dict) else []
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
        signatures = _diagnostics_errors(base_url)
        signatures.extend(_audit_failures(base_url, cron_secret))
    except requests.HTTPError as exc:
        body = (exc.response.text or "").strip()
        preview = body[:300] if body else "<empty>"
        print(f"FAIL: endpoint returned HTTP {exc.response.status_code} body={preview}")
        return 1
    except Exception as exc:  # pragma: no cover - defensive live gate
        print(f"FAIL: unable to collect error signatures: {exc}")
        return 1

    if not signatures:
        print("PASS: no active error signatures detected")
        return 0

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

    _print_annotation("warning", f"{len(signatures)} known error signatures detected")
    print("PASS: only known error signatures detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
