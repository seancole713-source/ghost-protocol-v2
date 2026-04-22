#!/usr/bin/env python3
import os
import sys

import requests


def main() -> int:
    base_url = os.getenv("BASE_URL", "https://ghost-protocol-v2-production.up.railway.app").rstrip("/")
    health = requests.get(f"{base_url}/health", timeout=20)
    api_health = requests.get(f"{base_url}/api/health", timeout=20)

    print(f"/health status={health.status_code}")
    print(f"/api/health status={api_health.status_code}")

    if health.status_code != 200:
        print("FAIL: /health is not 200")
        return 1
    if api_health.status_code != 200:
        print("FAIL: /api/health is not 200 (deploy parity missing)")
        return 1

    h1 = health.json()
    h2 = api_health.json()
    keys_match = sorted(h1.keys()) == sorted(h2.keys())
    status_match = h1.get("status") == h2.get("status")

    print(f"keys_match={keys_match}")
    print(f"status_match={status_match}")

    if not keys_match or not status_match:
        print("FAIL: /health and /api/health payload mismatch")
        return 1

    print("PASS: live health parity verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
