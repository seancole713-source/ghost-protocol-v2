#!/usr/bin/env python3
"""
scripts/check_error_signatures.py — Release gate: validate error response signatures.

Compares live error payloads from the health audit endpoint against the
baseline signatures stored in .github/error-signatures-baseline.json.
Any deviation in required keys, status codes, or error_code values is
reported as a failure.

Exit codes:
  0  All signatures match baseline
  1  One or more signatures deviate from baseline
  2  Baseline file missing or endpoint unreachable

Usage:
    CRON_SECRET=<secret> python scripts/check_error_signatures.py
    BASE_URL=https://... CRON_SECRET=<secret> python scripts/check_error_signatures.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package not installed. Run: pip install requests")
    sys.exit(2)

BASE_URL = os.getenv("BASE_URL", "https://ghost-protocol-v2-production.up.railway.app").rstrip("/")
CRON_SECRET = os.getenv("CRON_SECRET", "")
TIMEOUT = int(os.getenv("AUDIT_TIMEOUT", "30"))

BASELINE_PATH = Path(__file__).parent.parent / ".github" / "error-signatures-baseline.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pass(msg: str) -> None:
    print(f"  PASS  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}")


def _warn(msg: str) -> None:
    print(f"  WARN  {msg}")


def _load_baseline() -> Optional[Dict[str, Any]]:
    if not BASELINE_PATH.exists():
        print(f"ERROR: baseline file not found at {BASELINE_PATH}")
        return None
    try:
        with open(BASELINE_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"ERROR: failed to parse baseline: {e}")
        return None


# ---------------------------------------------------------------------------
# Signature probes
# ---------------------------------------------------------------------------

def _probe_403(bad_secret: str = "wrong-secret-for-gate-check") -> Dict[str, Any]:
    """Hit the audit endpoint with a bad secret and capture the error payload."""
    url = f"{BASE_URL}/api/health/audit"
    try:
        resp = requests.post(
            url,
            headers={"x-cron-secret": bad_secret},
            timeout=TIMEOUT,
        )
        return {
            "http_status": resp.status_code,
            "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {},
        }
    except Exception as e:
        return {"http_status": -1, "error": str(e)}


def _probe_200() -> Dict[str, Any]:
    """Hit the audit endpoint with the correct secret and capture the success payload."""
    url = f"{BASE_URL}/api/health/audit"
    headers = {}
    if CRON_SECRET:
        headers["x-cron-secret"] = CRON_SECRET
    try:
        resp = requests.post(url, headers=headers, timeout=TIMEOUT)
        return {
            "http_status": resp.status_code,
            "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {},
        }
    except Exception as e:
        return {"http_status": -1, "error": str(e)}


# ---------------------------------------------------------------------------
# Signature comparison
# ---------------------------------------------------------------------------

def _compare_signature(
    probe_name: str,
    live: Dict[str, Any],
    baseline_sig: Dict[str, Any],
) -> List[str]:
    """Return list of deviations (empty = match)."""
    deviations: List[str] = []

    # HTTP status code
    expected_status = baseline_sig.get("http_status")
    live_status = live.get("http_status")
    if expected_status is not None and live_status != expected_status:
        deviations.append(
            f"{probe_name}: http_status={live_status} expected={expected_status}"
        )

    body = live.get("body", {})
    baseline_body = baseline_sig.get("body", {})

    # Required keys
    required_keys = baseline_sig.get("required_keys", list(baseline_body.keys()))
    missing = [k for k in required_keys if k not in body]
    if missing:
        deviations.append(f"{probe_name}: body missing keys {missing}")

    # Exact-match fields
    for field, expected_val in baseline_sig.get("exact_match", {}).items():
        live_val = body.get(field)
        if live_val != expected_val:
            deviations.append(
                f"{probe_name}: {field}={live_val!r} expected={expected_val!r}"
            )

    # Type checks
    for field, expected_type in baseline_sig.get("type_checks", {}).items():
        live_val = body.get(field)
        type_map = {"str": str, "int": int, "bool": bool, "list": list, "dict": dict, "float": float}
        t = type_map.get(expected_type)
        if t and not isinstance(live_val, t):
            deviations.append(
                f"{probe_name}: {field} type={type(live_val).__name__} expected={expected_type}"
            )

    # Allowed values
    for field, allowed in baseline_sig.get("allowed_values", {}).items():
        live_val = body.get(field)
        if live_val not in allowed:
            deviations.append(
                f"{probe_name}: {field}={live_val!r} not in allowed={allowed}"
            )

    return deviations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("Ghost Protocol v2 — Error Signature Gate")
    print(f"BASE_URL  : {BASE_URL}")
    print(f"BASELINE  : {BASELINE_PATH}")
    print(f"SECRET    : {'set' if CRON_SECRET else 'NOT SET'}")
    print("=" * 60)

    baseline = _load_baseline()
    if baseline is None:
        return 2

    all_passed = True
    signatures = baseline.get("signatures", {})

    # ── 403 auth rejection ────────────────────────────────────────────────
    print("\n[1] 403 auth-rejection signature")
    if "auth_rejection_403" in signatures:
        live_403 = _probe_403()
        if live_403.get("http_status") == -1:
            _fail(f"endpoint unreachable: {live_403.get('error')}")
            all_passed = False
        else:
            deviations = _compare_signature("auth_rejection_403", live_403, signatures["auth_rejection_403"])
            if deviations:
                for d in deviations:
                    _fail(d)
                all_passed = False
            else:
                _pass("403 signature matches baseline")
    else:
        _warn("no auth_rejection_403 signature in baseline — skipping")

    # ── 200 success payload ───────────────────────────────────────────────
    print("\n[2] 200 success-payload signature")
    if "success_200" in signatures:
        live_200 = _probe_200()
        if live_200.get("http_status") == -1:
            _fail(f"endpoint unreachable: {live_200.get('error')}")
            all_passed = False
        else:
            deviations = _compare_signature("success_200", live_200, signatures["success_200"])
            if deviations:
                for d in deviations:
                    _fail(d)
                all_passed = False
            else:
                _pass("200 signature matches baseline")
    else:
        _warn("no success_200 signature in baseline — skipping")

    # ── Baseline metadata ─────────────────────────────────────────────────
    print("\n[3] Baseline metadata")
    meta = baseline.get("meta", {})
    version = meta.get("version", "unknown")
    updated = meta.get("updated", "unknown")
    _pass(f"baseline version={version!r} updated={updated!r}")

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if all_passed:
        print("GO: all error signatures match baseline")
        return 0
    else:
        print("NO-GO: error signature deviation(s) detected")
        return 1


if __name__ == "__main__":
    sys.exit(main())
