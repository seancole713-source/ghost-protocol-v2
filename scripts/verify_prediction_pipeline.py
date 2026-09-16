"""Smoke-test observable prediction plumbing, not statistical accuracy."""
from __future__ import annotations

import os

import requests


def main() -> int:
    base = os.getenv("BASE_URL", "https://ghost-protocol-v2-production.up.railway.app").rstrip("/")
    paths = ("/api/_version", "/api/shadow-stats", "/api/forecasts/research?limit=1")
    results = []
    for path in paths:
        response = requests.get(base + path, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("ok") is False or payload.get("error"):
            raise RuntimeError(f"{path}: unhealthy response")
        results.append(payload)
    version, stats, forecasts = results
    expected = os.getenv("EXPECTED_SHA", "").strip()
    if expected and version.get("git_sha") != expected:
        raise RuntimeError("Expected commit is not deployed")
    if forecasts.get("trading_eligible") is not False or forecasts.get("accuracy_proven") is not False:
        raise RuntimeError("Research forecasts must not imply trading/accuracy approval")
    if not isinstance(stats.get("resolved"), int) or not isinstance(forecasts.get("forecasts"), list):
        raise RuntimeError("Prediction evidence schema missing")
    print(f"PASS: evidence APIs work; research open={forecasts['total_open']}, resolved={stats['resolved']}")
    print("This verifies plumbing only. It does not establish predictive accuracy or trading readiness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
