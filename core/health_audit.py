import json
import os
import time
from typing import Any, Dict, List, Tuple


def _finding(
    status: str,
    location: str,
    evidence: str,
    impact: str,
    auto_fix: bool,
    fix_result: str,
    category: str,
) -> Dict[str, Any]:
    item = {
        "status": status,
        "location": location,
        "evidence": evidence,
        "impact": impact,
        "auto_fix": auto_fix,
        "fix_result": fix_result,
        "category": category,
    }
    # Uppercase aliases for strict reporting contracts.
    item["STATUS"] = status
    item["LOCATION"] = location
    item["EVIDENCE"] = evidence
    item["IMPACT_LEVEL"] = impact
    item["AUTO_FIX"] = auto_fix
    item["FIX_RESULT"] = fix_result
    return item


def _safe_perf(start_ts: float) -> int:
    return int(max(0, (time.time() - start_ts) * 1000))


def _required_route_paths() -> List[str]:
    return [
        "/health",
        "/api/health",
        "/api/diagnostics",
        "/api/stats",
        "/api/cockpit/context",
        "/api/picks",
        "/api/v2/recent",
        "/api/news",
    ]


def _persist_run(db_conn, payload: Dict[str, Any]) -> None:
    with db_conn() as conn:
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
            INSERT INTO health_audit_runs(run_ts, status, coverage_pct, unresolved_count, resolved_count, payload)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                int(time.time()),
                payload.get("status", "unknown"),
                float(payload.get("coverage_pct", 0.0)),
                int(payload.get("unresolved_count", 0)),
                int(payload.get("resolved_count", 0)),
                json.dumps(payload),
            ),
        )


def run_health_audit(
    app,
    db_conn,
    health_payload: Dict[str, Any],
    diagnostics_payload: Dict[str, Any],
    stats_payload: Dict[str, Any],
    cockpit_payload: Dict[str, Any],
    auto_fix: bool = True,
) -> Dict[str, Any]:
    started = time.time()
    findings: List[Dict[str, Any]] = []
    autofix_attempted = 0
    autofix_resolved = 0

    # 1) API route availability surface.
    routes = {getattr(r, "path", "") for r in getattr(app, "routes", [])}
    for path in _required_route_paths():
        if path in routes:
            findings.append(
                _finding(
                    "PASS",
                    f"route:{path}",
                    "Route registered",
                    "low",
                    False,
                    "not needed",
                    "api_availability",
                )
            )
        else:
            findings.append(
                _finding(
                    "FAIL",
                    f"route:{path}",
                    "Route missing from FastAPI router map",
                    "critical" if path in ("/health", "/api/health", "/api/stats") else "high",
                    False,
                    "not attempted",
                    "api_availability",
                )
            )

    # 2) DB connectivity + integrity.
    db_query_start = time.time()
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            cur.execute("SELECT outcome, COUNT(*) FROM predictions WHERE outcome IN ('WIN','LOSS') GROUP BY outcome")
            rows = {r[0]: int(r[1]) for r in cur.fetchall()}
        db_latency_ms = _safe_perf(db_query_start)
        findings.append(
            _finding(
                "PASS",
                "db:connectivity",
                f"DB query path ok ({db_latency_ms}ms)",
                "critical",
                False,
                "not needed",
                "database",
            )
        )
        expected_w = rows.get("WIN", 0)
        expected_l = rows.get("LOSS", 0)
        if stats_payload.get("wins") == expected_w and stats_payload.get("losses") == expected_l:
            findings.append(
                _finding(
                    "PASS",
                    "db:data_integrity",
                    "Stats endpoint aligns with direct DB counts",
                    "high",
                    False,
                    "not needed",
                    "database",
                )
            )
        else:
            findings.append(
                _finding(
                    "FAIL",
                    "db:data_integrity",
                    f"Mismatch: stats({stats_payload.get('wins')}/{stats_payload.get('losses')}) vs db({expected_w}/{expected_l})",
                    "critical",
                    False,
                    "not attempted",
                    "database",
                )
            )
    except Exception as e:
        findings.append(
            _finding(
                "FAIL",
                "db:connectivity",
                f"DB audit failed: {str(e)[:160]}",
                "critical",
                False,
                "not attempted",
                "database",
            )
        )

    # 3) Environment variable audit.
    required_env = [
        ("DATABASE_URL", "critical"),
        ("CRON_SECRET", "high"),
        ("TELEGRAM_BOT_TOKEN", "high"),
        ("TELEGRAM_CHAT_ID", "high"),
    ]
    for env_name, impact in required_env:
        if os.getenv(env_name, "").strip():
            findings.append(
                _finding(
                    "PASS",
                    f"env:{env_name}",
                    "Present",
                    impact,
                    False,
                    "not needed",
                    "environment",
                )
            )
        else:
            findings.append(
                _finding(
                    "FAIL",
                    f"env:{env_name}",
                    f"Missing required environment variable {env_name}",
                    impact,
                    False,
                    "manual required",
                    "environment",
                )
            )

    # 4) Runtime payload checks.
    if health_payload.get("status") in ("healthy", "degraded", "critical"):
        findings.append(
            _finding(
                "PASS",
                "runtime:/health",
                f"Health endpoint status={health_payload.get('status')} score={health_payload.get('score')}",
                "high",
                False,
                "not needed",
                "runtime",
            )
        )
    else:
        findings.append(
            _finding(
                "FAIL",
                "runtime:/health",
                f"Unexpected health payload: {str(health_payload)[:160]}",
                "high",
                False,
                "not attempted",
                "runtime",
            )
        )

    if isinstance(diagnostics_payload, dict) and diagnostics_payload.get("checks_passed") is not None:
        findings.append(
            _finding(
                "PASS",
                "runtime:/api/diagnostics",
                f"checks_passed={diagnostics_payload.get('checks_passed')} warnings={diagnostics_payload.get('warnings')} errors={diagnostics_payload.get('errors')}",
                "high",
                False,
                "not needed",
                "runtime",
            )
        )
    else:
        findings.append(
            _finding(
                "FAIL",
                "runtime:/api/diagnostics",
                "Diagnostics payload missing required fields",
                "high",
                False,
                "not attempted",
                "runtime",
            )
        )

    # 5) Cockpit/API consistency check.
    cp_stats = (cockpit_payload or {}).get("stats", {}) if isinstance(cockpit_payload, dict) else {}
    if cp_stats.get("wins") == stats_payload.get("wins") and cp_stats.get("losses") == stats_payload.get("losses"):
        findings.append(
            _finding(
                "PASS",
                "api:data_consistency",
                "cockpit_context.stats aligns with /api/stats",
                "high",
                False,
                "not needed",
                "api_availability",
            )
        )
    else:
        findings.append(
            _finding(
                "FAIL",
                "api:data_consistency",
                "Mismatch between cockpit_context.stats and /api/stats",
                "critical",
                False,
                "not attempted",
                "api_availability",
            )
        )

    # 6) Basic frontend static surface check (server-side).
    cockpit_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cockpit.html")
    try:
        with open(cockpit_path, "r", encoding="utf-8") as f:
            html = f.read()
        required_tokens = ["tab-crypto", "tab-stocks", "tab-portfolio", "addPos()", "delPos("]
        missing = [tok for tok in required_tokens if tok not in html]
        if not missing:
            findings.append(
                _finding(
                    "PASS",
                    "frontend:cockpit_static",
                    "Key UI handlers/tabs present in cockpit.html",
                    "medium",
                    False,
                    "not needed",
                    "frontend",
                )
            )
        else:
            findings.append(
                _finding(
                    "FAIL",
                    "frontend:cockpit_static",
                    "Missing expected UI tokens: " + ",".join(missing),
                    "high",
                    False,
                    "not attempted",
                    "frontend",
                )
            )
    except Exception as e:
        findings.append(
            _finding(
                "FAIL",
                "frontend:cockpit_static",
                f"Unable to read cockpit.html: {str(e)[:160]}",
                "high",
                False,
                "not attempted",
                "frontend",
            )
        )

    # 7) Auto-fixable path: normalize malformed ghost_state.v32_stats_start_ts.
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='v32_stats_start_ts'")
            row = cur.fetchone()
            raw = row[0] if row else None
            needs_fix = False
            if raw is not None:
                try:
                    int(raw)
                except Exception:
                    needs_fix = True
            if needs_fix and auto_fix:
                autofix_attempted += 1
                cur.execute(
                    "INSERT INTO ghost_state(key,val) VALUES('v32_stats_start_ts',%s) "
                    "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                    ("0",),
                )
                autofix_resolved += 1
                findings.append(
                    _finding(
                        "PASS",
                        "selfheal:ghost_state.v32_stats_start_ts",
                        f"Normalized malformed value: {raw}",
                        "medium",
                        True,
                        "success",
                        "self_heal",
                    )
                )
            elif needs_fix:
                findings.append(
                    _finding(
                        "FAIL",
                        "selfheal:ghost_state.v32_stats_start_ts",
                        f"Malformed integer value: {raw}",
                        "medium",
                        True,
                        "not attempted",
                        "self_heal",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "PASS",
                        "selfheal:ghost_state.v32_stats_start_ts",
                        "Value well-formed or absent",
                        "low",
                        True,
                        "not needed",
                        "self_heal",
                    )
                )
    except Exception as e:
        findings.append(
            _finding(
                "FAIL",
                "selfheal:ghost_state.v32_stats_start_ts",
                f"Auto-fix check failed: {str(e)[:160]}",
                "medium",
                True,
                "failed",
                "self_heal",
            )
        )

    total_checks = len(findings)
    fail_count = sum(1 for f in findings if f["status"] == "FAIL")
    pass_count = total_checks - fail_count
    coverage_pct = round((total_checks / 14.0) * 100.0, 1)  # 14 baseline reliability categories
    unresolved = fail_count
    resolved = autofix_resolved
    status = "PASS" if fail_count == 0 else "FAIL"
    elapsed_ms = _safe_perf(started)

    report = {
        "status": status,
        "summary": {
            "total_checks": total_checks,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "autofix_attempted": autofix_attempted,
            "autofix_resolved": resolved,
            "elapsed_ms": elapsed_ms,
        },
        "coverage_pct": min(100.0, coverage_pct),
        "resolved_count": resolved,
        "unresolved_count": unresolved,
        "findings": findings,
        "timestamp": int(time.time()),
    }

    # Persistent run history (best effort).
    try:
        _persist_run(db_conn, report)
    except Exception:
        pass

    return report

