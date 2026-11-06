"""Operational truth separate from uptime, strategy eligibility, and accuracy."""

from __future__ import annotations

import os
from datetime import datetime

from edge.contracts import ET

AUTH_TTL_S = 3600


def report(store, *, now):
    day = datetime.fromtimestamp(now, ET).date().isoformat()
    issues = []
    auth = store.get("edge_provider_health", "openai") or {}
    enabled = os.getenv("EDGE_RESEARCH_ENABLED", "0").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if enabled:
        age = now - int(auth.get("checked_at") or 0)
        if not auth or age < 0 or age > AUTH_TTL_S * 2:
            issues.append(
                {
                    "code": "reviewer_auth_unverified",
                    "remedy": "Run the bounded authentication check",
                }
            )
        elif auth.get("status") != "OK":
            issues.append(
                {
                    "code": "reviewer_auth_failed",
                    "http_status": auth.get("http_status"),
                    "remedy": "Validate the OpenAI credential and deploy it; fallback is not independent review",
                }
            )
    card = store.get("edge_cards", day) or {}
    candidates, priced = card.get("candidates"), card.get("priced")
    coverage = priced / candidates if candidates and priced is not None else None
    if coverage is not None and coverage < 0.95:
        issues.append(
            {
                "code": "low_candidate_price_coverage",
                "coverage": coverage,
                "priced": priced,
                "candidates": candidates,
                "remedy": "Inspect feed entitlement and timestamps; never relabel delayed data as live",
            }
        )
    budget = store.get("edge_research_budget", day) or {}
    cap = budget.get("admission_cap_usd")
    if cap is not None and float(budget.get("spent_usd") or 0) > cap:
        issues.append(
            {
                "code": "research_budget_overrun",
                "remedy": "Review provider usage; admission estimates are not billing limits",
            }
        )
    failures = [
        r
        for r in store.scan("edge_research", day=day)
        if (r.get("independent_review") or {}).get("status")
        in {
            "authentication_failed",
            "http_error",
            "transport_error",
            "invalid_response",
        }
    ]
    if failures:
        issues.append(
            {
                "code": "independent_review_fallback",
                "count": len(failures),
                "remedy": "Inspect independent_review diagnostics; same-family fallback is correlated",
            }
        )
    public_auth = {k: v for k, v in auth.items() if k != "credential_hash"}
    return {
        "day": day,
        "checked_at": now,
        "status": "DEGRADED" if issues else "NO_OBSERVED_FAULTS",
        "issues": issues,
        "reviewer_auth": public_auth or None,
        "candidate_price_coverage": coverage,
        "coverage_target": 0.95,
        "coverage_target_is_release_gate": False,
        "accuracy_proven": False,
        "note": "Operational monitoring only. No observed faults does not establish complete coverage or predictive edge.",
    }


def check(http, store, *, now):
    """One credential read per hour; no model request, source text, or secrets stored."""
    import hashlib

    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    prev = store.get("edge_provider_health", "openai") or {}
    age = now - int(prev.get("checked_at") or 0)
    if http is not None and (
        prev.get("credential_hash") != fingerprint or not 0 <= age < AUTH_TTL_S
    ):
        state = {"checked_at": now, "status": "NO_KEY", "credential_hash": fingerprint}
        if key:
            try:
                r = http.get(
                    "https://api.openai.com/v1/models",
                    timeout=8,
                    headers={"Authorization": "Bearer " + key},
                )
                state.update(
                    status="OK" if r.status_code == 200 else "ERROR",
                    http_status=r.status_code,
                )
            except Exception as exc:  # noqa: BLE001
                state.update(status="ERROR", error_type=type(exc).__name__)
        store.put("edge_provider_health", "openai", state)
    return report(store, now=now)
