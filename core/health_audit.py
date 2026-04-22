"""
core/health_audit.py — Deep reliability audit for Ghost Protocol v2.

Runs a structured battery of checks against live health, diagnostics,
stats, and cockpit payloads.  Each check emits a deterministic record:

    {
        "check":      str,   # stable dotted identifier
        "status":     str,   # "PASS" | "WARN" | "FAIL"
        "location":   str,   # endpoint or subsystem
        "evidence":   str,   # human-readable finding
        "impact":     str,   # consequence if left unresolved
        "auto_fix":   bool,  # whether a fix was attempted
        "fix_result": str,   # outcome of fix attempt (or "n/a")
    }

The function is intentionally side-effect-free except for the optional
auto-fix hooks and the persistent audit-run record written to
health_audit_runs.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.health_audit")

# ---------------------------------------------------------------------------
# Stage detection
# ---------------------------------------------------------------------------

def _is_production() -> bool:
    """Return True when running in the production Railway environment."""
    env = os.getenv("RAILWAY_ENVIRONMENT", os.getenv("APP_ENV", "production")).lower()
    return env in ("production", "prod")


def _stage_label() -> str:
    return "production" if _is_production() else os.getenv(
        "RAILWAY_ENVIRONMENT", os.getenv("APP_ENV", "development")
    ).lower()


# ---------------------------------------------------------------------------
# Deterministic error payload builder
# ---------------------------------------------------------------------------

def _error_payload(
    check: str,
    location: str,
    evidence: str,
    impact: str,
    status: str = "FAIL",
    auto_fix: bool = False,
    fix_result: str = "n/a",
) -> Dict[str, Any]:
    """Return a fully-populated, schema-stable error record."""
    return {
        "check": check,
        "status": status,
        "location": location,
        "evidence": str(evidence)[:400],
        "impact": impact,
        "auto_fix": auto_fix,
        "fix_result": fix_result,
        "stage": _stage_label(),
        "ts": int(time.time()),
    }


def _pass_payload(check: str, location: str, evidence: str = "ok") -> Dict[str, Any]:
    return {
        "check": check,
        "status": "PASS",
        "location": location,
        "evidence": str(evidence)[:400],
        "impact": "",
        "auto_fix": False,
        "fix_result": "n/a",
        "stage": _stage_label(),
        "ts": int(time.time()),
    }


# ---------------------------------------------------------------------------
# Individual check helpers
# ---------------------------------------------------------------------------

def _check_health_payload(h: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    required_keys = {"status", "score", "db", "issues", "warnings"}
    missing = required_keys - set(h.keys())
    if missing:
        results.append(_error_payload(
            "health.schema",
            "/health",
            f"Missing keys: {sorted(missing)}",
            "Health endpoint schema drift — monitors may misread status",
        ))
    else:
        results.append(_pass_payload("health.schema", "/health", f"all required keys present"))

    status = h.get("status", "")
    if status not in ("healthy", "degraded", "critical"):
        results.append(_error_payload(
            "health.status_value",
            "/health",
            f"status={status!r} is not a recognised value",
            "Monitors cannot classify service state",
        ))
    else:
        results.append(_pass_payload("health.status_value", "/health", f"status={status!r}"))

    if not h.get("db"):
        results.append(_error_payload(
            "health.db",
            "/health",
            "db=False — database connection failed",
            "All DB-backed endpoints will fail; picks cannot be stored or resolved",
            status="FAIL",
        ))
    else:
        results.append(_pass_payload("health.db", "/health", "db=True"))

    issues = h.get("issues", [])
    if issues:
        results.append(_error_payload(
            "health.issues",
            "/health",
            f"{len(issues)} active issue(s): {issues[:3]}",
            "Service is degraded; investigate before routing production traffic",
            status="WARN" if h.get("status") == "degraded" else "FAIL",
        ))
    else:
        results.append(_pass_payload("health.issues", "/health", "no active issues"))

    return results


def _check_diagnostics_payload(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    required_keys = {"score", "status", "details", "checks_passed"}
    missing = required_keys - set(d.keys())
    if missing:
        results.append(_error_payload(
            "diagnostics.schema",
            "/api/diagnostics",
            f"Missing keys: {sorted(missing)}",
            "Diagnostics endpoint schema drift",
        ))
    else:
        results.append(_pass_payload("diagnostics.schema", "/api/diagnostics"))

    score = d.get("score", 0)
    if score < 50:
        results.append(_error_payload(
            "diagnostics.score",
            "/api/diagnostics",
            f"score={score} (critical threshold <50)",
            "Multiple subsystem failures detected; deployment should be blocked",
            status="FAIL",
        ))
    elif score < 80:
        results.append(_error_payload(
            "diagnostics.score",
            "/api/diagnostics",
            f"score={score} (degraded threshold <80)",
            "Service is degraded; monitor closely",
            status="WARN",
        ))
    else:
        results.append(_pass_payload("diagnostics.score", "/api/diagnostics", f"score={score}"))

    errors = d.get("details", {}).get("errors", [])
    if errors:
        names = [e.get("check", "?") for e in errors[:5]]
        results.append(_error_payload(
            "diagnostics.errors",
            "/api/diagnostics",
            f"{len(errors)} error check(s): {names}",
            "Logic errors detected that /health does not surface",
            status="FAIL" if score < 50 else "WARN",
        ))
    else:
        results.append(_pass_payload("diagnostics.errors", "/api/diagnostics", "no error checks"))

    return results


def _check_stats_payload(s: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if not s.get("ok"):
        results.append(_error_payload(
            "stats.ok",
            "/api/stats",
            s.get("error", "ok=False"),
            "Stats endpoint unavailable; cockpit and monitors will show stale data",
        ))
        return results

    results.append(_pass_payload("stats.ok", "/api/stats"))

    total = s.get("total", 0)
    wins = s.get("wins", 0)
    if total > 0:
        wr = round(wins / total * 100, 1)
        if wr < 40:
            results.append(_error_payload(
                "stats.win_rate",
                "/api/stats",
                f"all-time win rate {wr}% ({wins}W/{total - wins}L) — below 40%",
                "Model performance is critically low; retrain required",
                status="WARN",
            ))
        else:
            results.append(_pass_payload("stats.win_rate", "/api/stats", f"{wr}% ({wins}W/{total - wins}L)"))
    else:
        results.append(_pass_payload("stats.win_rate", "/api/stats", "no resolved picks yet"))

    return results


def _check_cockpit_stats_consistency(
    s: Dict[str, Any], c: Dict[str, Any]
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if not (s.get("ok") and c.get("ok")):
        results.append(_pass_payload(
            "consistency.stats_cockpit",
            "/api/stats vs /api/cockpit/context",
            "skipped — one or both payloads unavailable",
        ))
        return results

    cs = c.get("stats", {})
    mismatches = []
    for key in ("wins", "losses"):
        sv = s.get(key)
        cv = cs.get(key)
        if sv != cv:
            mismatches.append(f"{key}: stats={sv} cockpit={cv}")

    if mismatches:
        results.append(_error_payload(
            "consistency.stats_cockpit",
            "/api/stats vs /api/cockpit/context",
            "; ".join(mismatches),
            "Cockpit displays inconsistent data vs stats endpoint — cache or query bug",
            status="WARN",
        ))
    else:
        results.append(_pass_payload(
            "consistency.stats_cockpit",
            "/api/stats vs /api/cockpit/context",
            "wins/losses consistent",
        ))

    return results


def _check_open_positions(s: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    open_pos = s.get("open_positions", 0)
    if open_pos == 0:
        results.append(_pass_payload("picks.open_positions", "/api/stats", "no open positions"))
    elif open_pos > 20:
        results.append(_error_payload(
            "picks.open_positions",
            "/api/stats",
            f"{open_pos} open positions — unusually high",
            "Dedup may be broken; picks accumulating without resolution",
            status="WARN",
        ))
    else:
        results.append(_pass_payload("picks.open_positions", "/api/stats", f"{open_pos} open"))
    return results


def _check_stage_safety(h: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Production-specific safety checks that are relaxed in staging/dev."""
    results: List[Dict[str, Any]] = []
    prod = _is_production()

    tg_ok = h.get("telegram_configured", False)
    if prod and not tg_ok:
        results.append(_error_payload(
            "stage.telegram_configured",
            "env",
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in production",
            "Morning cards and watchdog alerts will not be delivered",
            status="FAIL",
        ))
    else:
        results.append(_pass_payload(
            "stage.telegram_configured",
            "env",
            f"telegram_configured={tg_ok} (stage={_stage_label()})",
        ))

    conf_floor = h.get("confidence_floor", 0.0)
    if prod and conf_floor < 0.70:
        results.append(_error_payload(
            "stage.confidence_floor",
            "env/MIN_ALERT_CONFIDENCE",
            f"confidence_floor={conf_floor} is below production minimum 0.70",
            "Low-quality picks may reach users in production",
            status="WARN",
        ))
    else:
        results.append(_pass_payload(
            "stage.confidence_floor",
            "env/MIN_ALERT_CONFIDENCE",
            f"confidence_floor={conf_floor}",
        ))

    return results


# ---------------------------------------------------------------------------
# Auto-fix hooks
# ---------------------------------------------------------------------------

def _attempt_dedup_fix(db_conn_factory: Any) -> str:
    """Expire duplicate open picks (keep highest confidence per symbol)."""
    try:
        now = int(time.time())
        with db_conn_factory() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, symbol, confidence FROM predictions "
                "WHERE outcome IS NULL AND expires_at > %s "
                "ORDER BY symbol, confidence DESC",
                (now,),
            )
            rows = cur.fetchall()
            seen: Dict[str, int] = {}
            to_expire: List[int] = []
            for pid, sym, conf in rows:
                if sym not in seen:
                    seen[sym] = pid
                else:
                    to_expire.append(pid)
            if to_expire:
                cur.execute(
                    "UPDATE predictions SET outcome='EXPIRED', resolved_at=%s "
                    "WHERE id = ANY(%s)",
                    (now, to_expire),
                )
                return f"expired {len(to_expire)} duplicate picks"
        return "no duplicates found"
    except Exception as e:
        return f"fix failed: {str(e)[:120]}"


def _attempt_stale_pick_expiry(db_conn_factory: Any) -> str:
    """Expire picks open longer than 72 hours with no resolution."""
    try:
        cutoff = int(time.time()) - 72 * 3600
        now = int(time.time())
        with db_conn_factory() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE predictions SET outcome='EXPIRED', resolved_at=%s "
                "WHERE outcome IS NULL AND predicted_at < %s AND predicted_at IS NOT NULL",
                (now, cutoff),
            )
            expired = cur.rowcount
        return f"expired {expired} stale picks (>72h)"
    except Exception as e:
        return f"fix failed: {str(e)[:120]}"


# ---------------------------------------------------------------------------
# Persistent audit run record
# ---------------------------------------------------------------------------

def _persist_audit_run(
    db_conn_factory: Any,
    status: str,
    coverage_pct: float,
    unresolved_count: int,
    resolved_count: int,
    payload: Dict[str, Any],
) -> None:
    try:
        with db_conn_factory() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS health_audit_runs (
                    id SERIAL PRIMARY KEY,
                    run_ts BIGINT NOT NULL,
                    status TEXT NOT NULL,
                    coverage_pct FLOAT NOT NULL,
                    unresolved_count INT NOT NULL,
                    resolved_count INT NOT NULL,
                    payload JSONB NOT NULL
                )
                """
            )
            cur.execute(
                """
                INSERT INTO health_audit_runs
                    (run_ts, status, coverage_pct, unresolved_count, resolved_count, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    int(time.time()),
                    status,
                    float(coverage_pct),
                    int(unresolved_count),
                    int(resolved_count),
                    json.dumps(payload),
                ),
            )
    except Exception as e:
        LOGGER.warning("health_audit: failed to persist run record: %s", str(e)[:120])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_health_audit(
    app: Any,
    db_conn: Any,
    health_payload: Dict[str, Any],
    diagnostics_payload: Dict[str, Any],
    stats_payload: Dict[str, Any],
    cockpit_payload: Dict[str, Any],
    auto_fix: bool = True,
) -> Dict[str, Any]:
    """
    Run the full audit battery and return a structured report.

    All exceptions inside individual checks are caught and converted to
    deterministic FAIL records so the endpoint always returns a usable
    payload — never a 500 from an audit sub-check crashing.
    """
    run_ts = int(time.time())
    findings: List[Dict[str, Any]] = []

    # ── 1. Health payload checks ──────────────────────────────────────────
    try:
        findings.extend(_check_health_payload(health_payload))
    except Exception as e:
        findings.append(_error_payload(
            "health.check_crashed", "/health", str(e)[:200],
            "Audit sub-check crashed — investigate health_audit.py",
        ))

    # ── 2. Diagnostics payload checks ────────────────────────────────────
    try:
        findings.extend(_check_diagnostics_payload(diagnostics_payload))
    except Exception as e:
        findings.append(_error_payload(
            "diagnostics.check_crashed", "/api/diagnostics", str(e)[:200],
            "Audit sub-check crashed",
        ))

    # ── 3. Stats payload checks ──────────────────────────────────────────
    try:
        findings.extend(_check_stats_payload(stats_payload))
    except Exception as e:
        findings.append(_error_payload(
            "stats.check_crashed", "/api/stats", str(e)[:200],
            "Audit sub-check crashed",
        ))

    # ── 4. Open positions ────────────────────────────────────────────────
    try:
        findings.extend(_check_open_positions(stats_payload))
    except Exception as e:
        findings.append(_error_payload(
            "picks.check_crashed", "/api/stats", str(e)[:200],
            "Audit sub-check crashed",
        ))

    # ── 5. Cross-payload consistency ─────────────────────────────────────
    try:
        findings.extend(_check_cockpit_stats_consistency(stats_payload, cockpit_payload))
    except Exception as e:
        findings.append(_error_payload(
            "consistency.check_crashed",
            "/api/stats vs /api/cockpit/context",
            str(e)[:200],
            "Audit sub-check crashed",
        ))

    # ── 6. Stage-safety checks ───────────────────────────────────────────
    try:
        findings.extend(_check_stage_safety(health_payload))
    except Exception as e:
        findings.append(_error_payload(
            "stage.check_crashed", "env", str(e)[:200],
            "Audit sub-check crashed",
        ))

    # ── 7. Auto-fix hooks (production-safe, idempotent) ──────────────────
    fix_log: List[str] = []
    if auto_fix:
        open_pos = stats_payload.get("open_positions", 0)
        if open_pos > 20:
            result = _attempt_dedup_fix(db_conn)
            fix_log.append(f"dedup_fix: {result}")
            LOGGER.info("health_audit auto-fix dedup: %s", result)

        # Expire stale picks in production only (staging may have intentional old data)
        if _is_production():
            result = _attempt_stale_pick_expiry(db_conn)
            fix_log.append(f"stale_expiry: {result}")
            LOGGER.info("health_audit auto-fix stale_expiry: %s", result)

    # ── 8. Summarise ─────────────────────────────────────────────────────
    fails = [f for f in findings if f["status"] == "FAIL"]
    warns = [f for f in findings if f["status"] == "WARN"]
    passes = [f for f in findings if f["status"] == "PASS"]

    total = len(findings)
    resolved_count = len(passes)
    unresolved_count = len(fails) + len(warns)
    coverage_pct = round(resolved_count / total * 100, 1) if total else 0.0

    overall_status: str
    if fails:
        overall_status = "FAIL"
    elif warns:
        overall_status = "WARN"
    else:
        overall_status = "PASS"

    report: Dict[str, Any] = {
        "run_ts": run_ts,
        "stage": _stage_label(),
        "overall_status": overall_status,
        "summary": {
            "total_checks": total,
            "passed": len(passes),
            "warned": len(warns),
            "failed": len(fails),
            "coverage_pct": coverage_pct,
        },
        "findings": findings,
        "auto_fix_log": fix_log,
    }

    # ── 9. Persist run record ─────────────────────────────────────────────
    try:
        _persist_audit_run(
            db_conn,
            overall_status,
            coverage_pct,
            unresolved_count,
            resolved_count,
            report,
        )
    except Exception as e:
        LOGGER.warning("health_audit: persist failed: %s", str(e)[:120])

    return report
