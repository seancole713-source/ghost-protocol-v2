#!/usr/bin/env python3
"""HTTP smoke checks to run before manual browser QA.

Automates what curl can verify: static routes, auth gating, WOLF-only picks,
and ghost-score payload sanity. Does not replace DevTools/responsive testing.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Callable, Dict, List, Tuple

import requests

Check = Tuple[str, Callable[[], None]]


def _base_url() -> str:
    return os.getenv("BASE_URL", "https://ghost-protocol-v2-production.up.railway.app").rstrip("/")


def _get(path: str, **kwargs: Any) -> requests.Response:
    return requests.get(f"{_base_url()}{path}", timeout=20, **kwargs)


def _fail(name: str, detail: str) -> None:
    raise AssertionError(f"{name}: {detail}")


def check_static_routes() -> None:
    for path in ("/version", "/robots.txt", "/sitemap.xml"):
        r = _get(path)
        if r.status_code != 200:
            _fail(path, f"HTTP {r.status_code}")


def check_health_parity() -> None:
    h1 = _get("/health")
    h2 = _get("/api/health")
    if h1.status_code != 200 or h2.status_code != 200:
        _fail("health", f"/health={h1.status_code} /api/health={h2.status_code}")
    j1, j2 = h1.json(), h2.json()
    if sorted(j1.keys()) != sorted(j2.keys()):
        _fail("health", "payload key mismatch")
    if j1.get("status") != j2.get("status"):
        _fail("health", f"status mismatch {j1.get('status')} != {j2.get('status')}")


def check_public_pages() -> None:
    for path in ("/cockpit", "/admin"):
        r = _get(path)
        if r.status_code != 200:
            _fail(path, f"HTTP {r.status_code}")
        text = r.text.lower()
        if "ghost" not in text:
            _fail(path, "missing GHOST branding in HTML")


def check_admin_gating() -> None:
    for path in ("/admin/health", "/api/diagnostics"):
        r = _get(path)
        if r.status_code != 404:
            _fail(path, f"expected 404 without cookie, got {r.status_code}")


def check_picks_wolf_only() -> None:
    r = _get("/api/picks")
    if r.status_code != 200:
        _fail("/api/picks", f"HTTP {r.status_code}")
    body = r.json()
    if body.get("ok") is not True:
        _fail("/api/picks", "ok=false")
    if body.get("symbol") != "WOLF":
        _fail("/api/picks", f"symbol={body.get('symbol')!r}, expected WOLF")
    for key in ("active", "recent"):
        items = body.get(key) or []
        for pick in items:
            if pick.get("symbol") not in (None, "WOLF"):
                _fail("/api/picks", f"non-WOLF pick in {key}: {pick.get('symbol')}")


def check_ghost_score() -> None:
    r = _get("/api/wolf/ghost-score")
    if r.status_code != 200:
        _fail("/api/wolf/ghost-score", f"HTTP {r.status_code}")
    body = r.json()
    if body.get("ok") is not True:
        _fail("/api/wolf/ghost-score", "ok=false")
    score = body.get("score")
    if not isinstance(score, (int, float)):
        _fail("/api/wolf/ghost-score", f"score={score!r}")
    if body.get("signal") not in ("STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"):
        _fail("/api/wolf/ghost-score", f"unexpected signal={body.get('signal')!r}")
    floor = body.get("confidence_floor")
    if floor is not None and not (0.0 < float(floor) <= 1.0):
        _fail("/api/wolf/ghost-score", f"confidence_floor={floor!r}")


def check_chartjs_cdn_reachable() -> None:
    """Cockpit loads Chart.js from jsdelivr — verify CDN responds (not a CSP check)."""
    r = requests.get(
        "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js",
        timeout=20,
    )
    if r.status_code != 200:
        _fail("chart.js CDN", f"HTTP {r.status_code}")
    if "Chart" not in r.text:
        _fail("chart.js CDN", "unexpected payload")


CHECKS: List[Check] = [
    ("static routes (/version, /robots.txt, /sitemap.xml)", check_static_routes),
    ("health parity (/health vs /api/health)", check_health_parity),
    ("public pages (/cockpit, /admin)", check_public_pages),
    ("admin gating (404 without cookie)", check_admin_gating),
    ("picks WOLF-only", check_picks_wolf_only),
    ("ghost score payload", check_ghost_score),
    ("Chart.js CDN reachable", check_chartjs_cdn_reachable),
]


def main() -> int:
    base = _base_url()
    print(f"Prelaunch smoke — BASE_URL={base}")
    print()
    failed: List[str] = []
    for name, fn in CHECKS:
        try:
            fn()
            print(f"PASS: {name}")
        except AssertionError as exc:
            print(f"FAIL: {name} — {exc}")
            failed.append(name)
        except requests.RequestException as exc:
            print(f"FAIL: {name} — network error: {exc}")
            failed.append(name)

    print()
    if failed:
        print(f"FAILED ({len(failed)}/{len(CHECKS)}): {', '.join(failed)}")
        print("Manual browser QA still required (responsive, DevTools, auth flow).")
        return 1

    print(f"ALL PASS ({len(CHECKS)} checks)")
    print("Manual browser QA still required: responsive layout, tab/modal clicks,")
    print("DevTools console/page/network/CSP, admin login/logout, auto-refresh pill.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
