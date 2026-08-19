"""Evidence-gated YMM earnings bull-case checklist.

The checklist is a scenario monitor, not a probability model. It keeps the
operator's twelve visible boxes while preventing correlated observations from
being counted as independent evidence. Production inputs carry explicit units,
event identity, period, source, and availability timestamps.

Read-only intelligence. It never fires a pick or changes a trading gate.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from core.evidence_integrity import (
    CONFIRMED,
    MISSING,
    PENDING,
    SCENARIO as EVIDENCE_SCENARIO,
    UNVERIFIED,
    VERIFIED_CONFLICT,
    chain_gaps,
    conflict_records,
    integrity_status,
    is_confirmed,
    merge_evidence_sets,
)


STATE_RED = "red"
STATE_NEUTRAL = "neutral"
STATE_GREEN = "green"
STATE_VERY_GREEN = "very_green"
STATE_EXTREME = "extreme"
STATE_CHASE_RISK = "chase_risk"
STATE_PENDING = "pending_confirmation"
STATE_UNKNOWN = "unknown"
STATE_UNVERIFIED = "unverified"
STATE_CONFLICT = "verified_conflict"

_PASS_STATES = {STATE_GREEN, STATE_VERY_GREEN, STATE_EXTREME}

SCENARIO: Dict[str, Any] = {
    "scenario_id": "YMM_2026_Q2_12_5D",
    "scoring_version": "ymm_earnings_bull_case_v2",
    "symbol": "YMM",
    "period": "2026-Q2",
    "event_date": "2026-08-19",
    "target_price": 12.0,
    "reference_price": 8.80,
    "reference_price_date": "2026-08-18",
    "target_horizon_trading_days": 5,
    "threshold_source": "operator_spec_2026-08-18",
    "calibrated": False,
    "event_source": (
        "https://ir.fulltruckalliance.com/2026-08-05-Full-Truck-Alliance-Co-Ltd-"
        "to-Announce-Second-Quarter-2026-Financial-Results-on-Wednesday%2C-August-19%2C-2026"
    ),
}

_EVENT_EVIDENCE_NOT_BEFORE_TS = int(
    # Freeze an earliest plausible premarket release window. This blocks prior-
    # quarter values submitted after midnight but before the scheduled event.
    datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc).timestamp()
)
_RVOL_MAX_AGE_S = 20 * 60
_OPERATOR_EVIDENCE_MAX_AGE_S = 24 * 60 * 60
OPERATOR_PROVENANCE = "operator_submitted_unverified_official_url"
_OFFICIAL_EVIDENCE_HOSTS = {
    "ir.fulltruckalliance.com",
    "sec.gov",
    "www.sec.gov",
}

# Explicit production input names prevent raw statement values from being
# mistaken for USD millions or GAAP EPS from being mixed with adjusted ADS EPS.
_OPERATOR_INPUTS: Dict[str, Dict[str, Any]] = {
    "revenue_actual_usd_m": {"kind": "number", "min": 0.0, "max": 5_000.0},
    "eps_adjusted_ads_usd": {"kind": "number", "min": -10.0, "max": 10.0},
    "transaction_growth_pct": {"kind": "number", "min": -100.0, "max": 1_000.0},
    "order_growth_pct": {"kind": "number", "min": -100.0, "max": 1_000.0},
    "shipper_growth_pct": {"kind": "number", "min": -100.0, "max": 1_000.0},
    "profitability_improved": {"kind": "bool"},
    "guidance_outcome": {
        "kind": "enum",
        "values": {"withdrawn", "cut", "maintained", "raised", "raised_accelerating"},
    },
}

_VALUE_ALIASES = {
    "revenue_beat": "revenue_actual_usd_m",
    "eps_beat": "eps_adjusted_ads_usd",
    "transaction_growth": "transaction_growth_pct",
    "order_growth": "order_growth_pct",
    "shipper_growth": "shipper_growth_pct",
    "profitability": "profitability_improved",
    "guidance": "guidance_outcome",
    "premarket_gap": "premarket_gap_pct",
    "relative_volume": "relative_volume",
    "breakout_950": "live_price",
    "breakout_1000": "live_price",
    "breakout_1100": "live_price",
}


class ChecklistInputError(ValueError):
    """Raised when supplied scenario evidence is invalid or ambiguous."""


class UnsupportedScenarioError(ValueError):
    """Raised when a symbol has no registered bull-case scenario."""


def _now() -> int:
    return int(time.time())


def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _evidence(
    value: Any,
    *,
    source: str,
    as_of_ts: Optional[int],
    unit: Optional[str] = None,
    provenance: str = "operator",
    status: str = UNVERIFIED,
    source_timestamp: Optional[int] = None,
    observation_timestamp: Optional[int] = None,
    reporting_period: Optional[str] = None,
    currency: Optional[str] = None,
    basis: Optional[str] = None,
    expected_value: Any = None,
    methodology: Optional[str] = None,
    comparable_prior_period_value: Any = None,
) -> Dict[str, Any]:
    timestamp = int(as_of_ts) if as_of_ts is not None else None
    return {
        "value": value,
        "actual_value": value,
        "expected_value": expected_value,
        "source": source,
        "as_of_ts": timestamp,
        "source_timestamp": int(source_timestamp) if source_timestamp is not None else timestamp,
        "observation_timestamp": (
            int(observation_timestamp) if observation_timestamp is not None else timestamp
        ),
        "reporting_period": reporting_period,
        "currency": currency,
        "unit": unit,
        "basis": basis,
        "calculation_methodology": methodology,
        "comparable_prior_period_value": comparable_prior_period_value,
        "status": status,
        "confidence_status": status,
        "provenance": provenance,
    }


def _evidence_summary(record: Optional[Dict[str, Any]], *, growth: bool = False) -> Dict[str, Any]:
    status = integrity_status(record, growth=growth)
    actual = (record or {}).get("actual_value", (record or {}).get("value"))
    expected = (record or {}).get("expected_value")
    delta = None
    try:
        if actual is not None and expected is not None:
            delta = round(float(actual) - float(expected), 6)
    except (TypeError, ValueError):
        pass
    return {
        "actual": actual,
        "consensus": expected,
        "delta": delta,
        "source": (record or {}).get("source"),
        "source_timestamp": (record or {}).get("source_timestamp"),
        "observation_timestamp": (record or {}).get("observation_timestamp"),
        "reporting_period": (record or {}).get("reporting_period"),
        "currency": (record or {}).get("currency"),
        "unit": (record or {}).get("unit"),
        "basis": (record or {}).get("basis"),
        "calculation_methodology": (record or {}).get("calculation_methodology"),
        "comparable_prior_period_value": (record or {}).get("comparable_prior_period_value"),
        "status": status,
        "missing_chain_fields": chain_gaps(record, growth=growth),
    }


def _integrity_gated_state(
    record: Optional[Dict[str, Any]],
    calculated_state: str,
    *,
    growth: bool = False,
) -> str:
    status = integrity_status(record, growth=growth)
    if status == VERIFIED_CONFLICT:
        return STATE_CONFLICT
    if status in {UNVERIFIED, EVIDENCE_SCENARIO}:
        return STATE_UNVERIFIED
    if status == PENDING:
        return STATE_PENDING
    if status == MISSING or record is None:
        return STATE_UNKNOWN
    return calculated_state if is_confirmed(record, growth=growth) else STATE_UNVERIFIED


def _check(
    *,
    key: str,
    label: str,
    category: str,
    group: str,
    state: str,
    evidence: Optional[Dict[str, Any]],
    note: str,
    critical: bool = False,
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "category": category,
        "group": group,
        "critical": critical,
        "value": evidence.get("value") if evidence else None,
        "state": state,
        "passed": state in _PASS_STATES,
        "note": note,
        "evidence": evidence,
        "evidence_summary": _evidence_summary(
            evidence,
            growth=key in {"transaction_growth", "order_growth", "shipper_growth", "profitability"},
        ),
    }


def _numeric_state(
    value: Optional[float],
    *,
    green: float,
    very_green: Optional[float] = None,
    extreme: Optional[float] = None,
    red_below: Optional[float] = None,
    strict: bool = False,
) -> str:
    """Grade a numeric value while preserving neutral bands.

    ``strict=True`` implements language such as ``>30%``. It intentionally does
    not turn every value below the green threshold red unless a red boundary was
    explicitly specified.
    """
    if value is None:
        return STATE_UNKNOWN
    if red_below is not None and value < red_below:
        return STATE_RED

    passes = (lambda x, threshold: x > threshold) if strict else (lambda x, threshold: x >= threshold)
    if extreme is not None and passes(value, extreme):
        return STATE_EXTREME
    if very_green is not None and passes(value, very_green):
        return STATE_VERY_GREEN
    if passes(value, green):
        return STATE_GREEN
    return STATE_NEUTRAL


def _guidance_state(value: Any) -> str:
    mapping = {
        "withdrawn": STATE_RED,
        "cut": STATE_RED,
        "maintained": STATE_GREEN,
        "raised": STATE_VERY_GREEN,
        "raised_accelerating": STATE_EXTREME,
    }
    return mapping.get(str(value or "").strip().lower(), STATE_UNKNOWN)


def _premarket_state(value: Optional[float]) -> str:
    if value is None:
        return STATE_UNKNOWN
    if value >= 20.0:
        return STATE_CHASE_RISK
    if value >= 10.0:
        return STATE_EXTREME
    if value >= 5.0:
        return STATE_VERY_GREEN
    if value >= 3.0:
        return STATE_GREEN
    if value < 0.0:
        return STATE_RED
    return STATE_NEUTRAL


def _volume_state(rvol: Optional[float], price_change_pct: Optional[float]) -> Tuple[str, str]:
    if rvol is None:
        return STATE_UNKNOWN, "Relative volume unavailable."
    if price_change_pct is None:
        return STATE_PENDING, "Price direction is required before volume can confirm the move."
    if price_change_pct <= 0:
        if rvol >= 2.0:
            return STATE_RED, "Abnormal volume with non-advancing price is distribution, not confirmation."
        return STATE_NEUTRAL, "Volume is not confirming an advancing move."
    return (
        _numeric_state(rvol, green=2.0, very_green=3.0, extreme=5.0),
        "RVOL counts only while price is advancing.",
    )


def _normalize_direct_values(values: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Compatibility adapter for pure callers and tests; production uses records."""
    out: Dict[str, Dict[str, Any]] = {}
    raw = values or {}
    for key, value in raw.items():
        canonical = _VALUE_ALIASES.get(key, key)
        if canonical in ("profitability_improved",) and value in (0, 1):
            value = bool(value)
        if canonical == "guidance_outcome" and isinstance(value, (int, float)):
            value = {1: "maintained", 2: "raised", 3: "raised_accelerating"}.get(int(value))
        out[canonical] = _evidence(
            value,
            source="direct_input",
            as_of_ts=1,
            unit={
                "revenue_actual_usd_m": "USD millions",
                "eps_adjusted_ads_usd": "USD per ADS",
                "transaction_growth_pct": "percent YoY",
                "order_growth_pct": "percent YoY",
                "shipper_growth_pct": "percent YoY",
                "profitability_improved": "boolean YoY comparison",
                "guidance_outcome": "categorical outlook",
                "premarket_gap_pct": "percent",
                "relative_volume": "multiple",
                "price_change_pct": "percent",
                "live_price": "USD per share",
            }.get(canonical, "N/A"),
            provenance="test_or_internal",
            status=CONFIRMED,
            reporting_period=SCENARIO["period"],
            currency="USD" if canonical in {"revenue_actual_usd_m", "eps_adjusted_ads_usd", "live_price"} else "N/A",
            basis="adjusted" if canonical == "eps_adjusted_ads_usd" else "market_or_reported",
            expected_value=0.0,
            methodology="synthetic direct-input test fixture",
            comparable_prior_period_value=(
                0.0
                if canonical.endswith("_growth_pct") or canonical == "profitability_improved"
                else None
            ),
        )
    return out


def validate_operator_payload(
    symbol: str,
    payload: Dict[str, Any],
    *,
    now_ts: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """Validate explicitly sourced post-release operator evidence."""
    sym = str(symbol or "").strip().upper()
    if sym != SCENARIO["symbol"]:
        raise UnsupportedScenarioError(f"No registered bull-run scenario for {sym or 'blank symbol'}")
    if not isinstance(payload, dict):
        raise ChecklistInputError("JSON object required")
    if payload.get("scenario_id") != SCENARIO["scenario_id"]:
        raise ChecklistInputError(f"scenario_id must be {SCENARIO['scenario_id']}")
    if payload.get("period") != SCENARIO["period"]:
        raise ChecklistInputError(f"period must be {SCENARIO['period']}")

    records = payload.get("evidence")
    if not isinstance(records, dict) or not records:
        raise ChecklistInputError("evidence must be a non-empty object")
    unknown = sorted(set(records) - set(_OPERATOR_INPUTS))
    if unknown:
        raise ChecklistInputError(f"unsupported evidence keys: {', '.join(unknown)}")

    now = int(now_ts or _now())
    normalized: Dict[str, Dict[str, Any]] = {}
    for key, record in records.items():
        if not isinstance(record, dict):
            raise ChecklistInputError(f"{key} must include value, source, and as_of_ts")
        source = str(record.get("source") or "").strip()
        if not source.startswith(("https://", "http://")):
            raise ChecklistInputError(f"{key}.source must be an http(s) evidence URL")
        source_host = (urlparse(source).hostname or "").lower()
        if source_host not in _OFFICIAL_EVIDENCE_HOSTS:
            raise ChecklistInputError(f"{key}.source must be an official FTA IR or SEC URL")
        if source_host == "ir.fulltruckalliance.com" and "/2026-08-19-" not in source:
            raise ChecklistInputError(f"{key}.source must identify the 2026-08-19 results release")
        raw_as_of_ts = record.get("as_of_ts")
        try:
            if raw_as_of_ts is None or isinstance(raw_as_of_ts, bool):
                raise ValueError
            as_of_ts = int(str(raw_as_of_ts))
        except (TypeError, ValueError):
            raise ChecklistInputError(f"{key}.as_of_ts must be Unix seconds") from None
        if as_of_ts < _EVENT_EVIDENCE_NOT_BEFORE_TS:
            raise ChecklistInputError(f"{key} predates the {SCENARIO['period']} release window")
        if as_of_ts > now + 300:
            raise ChecklistInputError(f"{key}.as_of_ts cannot be in the future")
        if now - as_of_ts > _OPERATOR_EVIDENCE_MAX_AGE_S:
            raise ChecklistInputError(f"{key}.as_of_ts is stale")

        spec = _OPERATOR_INPUTS[key]
        value = record.get("value")
        if spec["kind"] == "number":
            numeric = _f(value)
            if numeric is None or not (spec["min"] <= numeric <= spec["max"]):
                raise ChecklistInputError(
                    f"{key}.value must be between {spec['min']} and {spec['max']} in the declared unit"
                )
            value = numeric
        elif spec["kind"] == "bool":
            if not isinstance(value, bool):
                raise ChecklistInputError(f"{key}.value must be true or false")
        elif spec["kind"] == "enum":
            value = str(value or "").strip().lower()
            if value not in spec["values"]:
                allowed = ", ".join(sorted(spec["values"]))
                raise ChecklistInputError(f"{key}.value must be one of: {allowed}")

        unit = str(record.get("unit") or "").strip()
        currency = str(record.get("currency") or "").strip().upper()
        basis = str(record.get("basis") or "").strip().lower()
        methodology = str(record.get("calculation_methodology") or "").strip()
        raw_source_ts = record.get("source_timestamp")
        raw_observation_ts = record.get("observation_timestamp")
        if not unit or not currency or not basis or not methodology:
            raise ChecklistInputError(
                f"{key} requires unit, currency, basis, and calculation_methodology"
            )
        try:
            source_ts = int(str(raw_source_ts))
            observation_ts = int(str(raw_observation_ts))
        except (TypeError, ValueError):
            raise ChecklistInputError(
                f"{key} requires source_timestamp and observation_timestamp Unix seconds"
            ) from None
        if source_ts > now + 300 or observation_ts > now + 300:
            raise ChecklistInputError(f"{key} evidence timestamps cannot be in the future")

        expected_value = record.get("expected_value")
        if key in {"revenue_actual_usd_m", "eps_adjusted_ads_usd", "guidance_outcome"} and expected_value is None:
            raise ChecklistInputError(f"{key}.expected_value is required")
        comparable = record.get("comparable_prior_period_value")
        if key in {"transaction_growth_pct", "order_growth_pct", "shipper_growth_pct", "profitability_improved"} and comparable is None:
            raise ChecklistInputError(f"{key}.comparable_prior_period_value is required")

        normalized[key] = _evidence(
            value,
            source=source,
            as_of_ts=as_of_ts,
            unit=unit,
            provenance=OPERATOR_PROVENANCE,
            status=UNVERIFIED,
            source_timestamp=source_ts,
            observation_timestamp=observation_ts,
            reporting_period=SCENARIO["period"],
            currency=currency,
            basis=basis,
            expected_value=expected_value,
            methodology=methodology,
            comparable_prior_period_value=comparable,
        )
    return normalized


def fetch_auto_evidence(symbol: str = "YMM") -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Fetch only event-safe market evidence; earnings remain operator-sourced."""
    sym = str(symbol or "").strip().upper()
    if sym != SCENARIO["symbol"]:
        raise UnsupportedScenarioError(f"No registered bull-run scenario for {sym or 'blank symbol'}")

    evidence: Dict[str, Dict[str, Any]] = {}
    sources: Dict[str, Any] = {
        "earnings": {
            "available": False,
            "reason": "event_safe_post_release_evidence_required",
            "note": "Latest-quarter free feeds are not period/currency/basis safe for this scenario.",
        }
    }

    try:
        from core.prices import get_extended_session, get_intraday_session

        ext = get_extended_session(sym) or {}
        sources["extended_session"] = {
            "available": bool(ext),
            "session": ext.get("session"),
            "as_of_ts": ext.get("price_as_of_ts"),
        }
        ext_ts = ext.get("price_as_of_ts")
        if (
            str(ext.get("session") or "").lower() == "premarket"
            and _f(ext.get("gap_pct")) is not None
            and ext_ts is not None
        ):
            evidence["premarket_gap_pct"] = _evidence(
                _f(ext.get("gap_pct")),
                source="core.prices.get_extended_session",
                as_of_ts=int(ext_ts),
                unit="percent",
                provenance="auto_market",
                status=UNVERIFIED,
                source_timestamp=int(ext_ts),
                observation_timestamp=int(ext_ts),
                reporting_period=str(ext.get("session") or "premarket"),
                currency="USD",
                basis="market_quote",
                expected_value=0.0,
                methodology="(session price - previous close) / previous close * 100; previous-close operand provenance unavailable",
            )

        intra = get_intraday_session(sym) or {}
        stale = bool(intra.get("data_stale"))
        sources["intraday_session"] = {
            "available": bool(intra) and not stale,
            "stale": stale,
            "feed": intra.get("feed"),
            "as_of_ts": intra.get("price_as_of_ts"),
        }
        price_ts = intra.get("price_as_of_ts")
        if intra and not stale and price_ts is not None:
            as_of_ts = int(price_ts)
            live_price = _f(intra.get("price"))
            price_change = _f(intra.get("change_pct"))
            if live_price is not None and live_price > 0:
                evidence["live_price"] = _evidence(
                    live_price,
                    source=str(intra.get("feed") or "alpaca_trade"),
                    as_of_ts=as_of_ts,
                    unit="USD per share",
                    provenance="auto_market",
                    status=CONFIRMED,
                    source_timestamp=as_of_ts,
                    observation_timestamp=as_of_ts,
                    reporting_period=str(intra.get("market_date") or intra.get("session") or "live"),
                    currency="USD",
                    basis="market_quote",
                    expected_value=SCENARIO["reference_price"],
                    methodology="latest synchronized provider trade",
                )
            if price_change is not None:
                evidence["price_change_pct"] = _evidence(
                    price_change,
                    source=str(intra.get("feed") or "alpaca_trade"),
                    as_of_ts=as_of_ts,
                    unit="percent",
                    provenance="auto_market",
                    status=UNVERIFIED,
                    source_timestamp=as_of_ts,
                    observation_timestamp=as_of_ts,
                    reporting_period=str(intra.get("market_date") or intra.get("session") or "live"),
                    currency="USD",
                    basis="market_quote",
                    expected_value=0.0,
                    methodology="(latest trade - previous close) / previous close * 100; previous-close operand provenance unavailable",
                )
    except Exception as exc:
        sources["prices"] = {
            "available": False,
            "reason": type(exc).__name__,
        }

    try:
        from core.squeeze_monitor import get_squeeze_picks

        board = get_squeeze_picks() or {}
        rows = list(board.get("picks") or []) + list(board.get("leaders") or [])
        row = next((item for item in rows if str(item.get("symbol") or "").upper() == sym), None)
        rvol = _f((row or {}).get("rvol"))
        raw_scan_ts = (row or {}).get("as_of_ts") or board.get("last_scan_ts")
        scan_ts = int(raw_scan_ts) if raw_scan_ts is not None else None
        fresh = bool(
            scan_ts is not None
            and 0 <= _now() - scan_ts <= _RVOL_MAX_AGE_S
            and not board.get("snapshot_stale")
        )
        if rvol is not None and fresh:
            evidence["relative_volume"] = _evidence(
                rvol,
                source="core.squeeze_monitor radar observation",
                as_of_ts=scan_ts,
                unit="multiple",
                provenance="auto_market",
                status=UNVERIFIED,
                source_timestamp=scan_ts,
                observation_timestamp=scan_ts,
                reporting_period="current_market_session",
                currency="N/A",
                basis="market_volume",
                expected_value=1.0,
                methodology="current cumulative volume / elapsed-session expected volume; numerator and denominator provenance unavailable",
            )
        sources["relative_volume"] = {
            "available": rvol is not None and fresh,
            "as_of_ts": scan_ts,
            "reason": (
                None if rvol is not None and fresh
                else "stale_radar_snapshot" if rvol is not None
                else "symbol_not_in_latest_radar"
            ),
        }
    except Exception as exc:
        sources["relative_volume"] = {
            "available": False,
            "reason": type(exc).__name__,
        }
    return evidence, sources


def _build_checks(evidence: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    def num(key: str) -> Optional[float]:
        return _f((evidence.get(key) or {}).get("value"))

    checks: List[Dict[str, Any]] = []
    specs = (
        (
            "revenue_beat", "Revenue result", "revenue_actual_usd_m", "USD millions",
            _numeric_state(num("revenue_actual_usd_m"), green=470.0, very_green=480.0, red_below=455.0),
            "RED below $455M; neutral $455M-$469.99M; GREEN $470M+; VERY GREEN $480M+.", True,
        ),
        (
            "eps_beat", "Adjusted EPS result", "eps_adjusted_ads_usd", "USD per ADS",
            _numeric_state(num("eps_adjusted_ads_usd"), green=0.20, very_green=0.22, red_below=0.18),
            "RED below $0.18; neutral $0.18-$0.19; GREEN $0.20+; VERY GREEN $0.22+.", True,
        ),
        (
            "transaction_growth", "Transaction-service growth", "transaction_growth_pct", "percent YoY",
            _numeric_state(num("transaction_growth_pct"), green=30.0, very_green=35.0, extreme=40.0, strict=True),
            "GREEN >30%; VERY GREEN >35%; EXTREME >40%.", False,
        ),
        (
            "order_growth", "Order growth", "order_growth_pct", "percent YoY",
            _numeric_state(num("order_growth_pct"), green=10.0, very_green=15.0, extreme=20.0, strict=True),
            "GREEN >10%; VERY GREEN >15%; EXTREME >20%.", False,
        ),
        (
            "shipper_growth", "Shipper growth", "shipper_growth_pct", "percent YoY",
            _numeric_state(num("shipper_growth_pct"), green=10.0, very_green=15.0, strict=True),
            "GREEN >10%; VERY GREEN >15%.", False,
        ),
    )
    for key, label, evidence_key, unit, state, note, critical in specs:
        record = evidence.get(evidence_key)
        if record and not record.get("unit"):
            record = {**record, "unit": unit}
        state = _integrity_gated_state(
            record,
            state,
            growth=evidence_key in {"transaction_growth_pct", "order_growth_pct", "shipper_growth_pct"},
        )
        checks.append(_check(
            key=key,
            label=label,
            category="fundamentals",
            group="fundamentals",
            state=state,
            evidence=record,
            note=note,
            critical=critical,
        ))

    profitability = evidence.get("profitability_improved")
    profitability_value = (profitability or {}).get("value")
    profitability_state = _integrity_gated_state(
        profitability,
        (
            STATE_UNKNOWN if profitability is None
            else STATE_GREEN if profitability_value is True
            else STATE_RED
        ),
        growth=True,
    )
    checks.append(_check(
        key="profitability",
        label="Profitability improves",
        category="fundamentals",
        group="fundamentals",
        state=profitability_state,
        evidence=profitability,
        note="Requires explicit YoY adjusted-income or operating-margin confirmation.",
    ))

    guidance = evidence.get("guidance_outcome")
    checks.append(_check(
        key="guidance",
        label="Guidance",
        category="forward_outlook",
        group="guidance",
        state=_integrity_gated_state(
            guidance,
            _guidance_state((guidance or {}).get("value")),
        ),
        evidence=guidance,
        note="Maintained=GREEN; raised=VERY GREEN; raised with acceleration=EXTREME; cut/withdrawn=RED.",
        critical=True,
    ))

    gap = evidence.get("premarket_gap_pct")
    checks.append(_check(
        key="premarket_gap",
        label="Premarket reaction",
        category="market_confirmation",
        group="premarket_reaction",
        state=_integrity_gated_state(gap, _premarket_state(num("premarket_gap_pct"))),
        evidence=gap,
        note="+3%=GREEN; +5%=VERY GREEN; +10%=EXTREME; +20% or more is chase risk and does not pass.",
    ))

    rvol = evidence.get("relative_volume")
    price_change_record = evidence.get("price_change_pct")
    volume_state, volume_note = _volume_state(num("relative_volume"), num("price_change_pct"))
    volume_state = _integrity_gated_state(rvol, volume_state)
    if is_confirmed(rvol) and not is_confirmed(price_change_record):
        volume_state = _integrity_gated_state(price_change_record, STATE_PENDING)
        volume_note = "Relative volume cannot confirm direction until synchronized price-change evidence is confirmed."
    checks.append(_check(
        key="relative_volume",
        label="Relative volume with price confirmation",
        category="market_confirmation",
        group="volume_price_confirmation",
        state=volume_state,
        evidence=rvol,
        note=volume_note,
    ))

    live = evidence.get("live_price")
    live_price = num("live_price")
    for key, label, level, min_volume_state in (
        ("breakout_950", "$9.50 breakout", 9.50, STATE_GREEN),
        ("breakout_1000", "$10 breakout", 10.0, STATE_GREEN),
        ("breakout_1100", "$11 breakout", 11.0, STATE_VERY_GREEN),
    ):
        if live_price is None:
            state = STATE_UNKNOWN
        elif not is_confirmed(live):
            state = _integrity_gated_state(live, STATE_PENDING)
        elif live_price < level:
            state = STATE_NEUTRAL
        elif volume_state not in _PASS_STATES:
            state = STATE_PENDING
        elif min_volume_state == STATE_VERY_GREEN and volume_state == STATE_GREEN:
            state = STATE_PENDING
        else:
            state = STATE_GREEN
        checks.append(_check(
            key=key,
            label=label,
            category="price_progression",
            group="price_path",
            state=state,
            evidence=live,
            note=(
                f"Price must clear ${level:.2f} with "
                + ("RVOL >=3x and advancing price." if level == 11.0 else "RVOL >=2x and advancing price.")
            ),
        ))
    return checks


def _group_summary(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key = {item["key"]: item for item in checks}
    fundamental_keys = {
        "revenue_beat", "eps_beat", "transaction_growth", "order_growth",
        "shipper_growth", "profitability",
    }
    fundamentals = [by_key[key] for key in fundamental_keys]
    fundamental_passes = sum(bool(item["passed"]) for item in fundamentals)
    fundamental_reds = sum(item["state"] == STATE_RED for item in fundamentals)
    if by_key["revenue_beat"]["passed"] and by_key["eps_beat"]["passed"] and fundamental_passes >= 4:
        fundamental_status = "confirmed"
    elif fundamental_reds:
        fundamental_status = "rejected"
    elif any(item["state"] in {STATE_UNKNOWN, STATE_UNVERIFIED, STATE_CONFLICT} for item in fundamentals):
        fundamental_status = "pending"
    else:
        fundamental_status = "not_confirmed"

    def single_group(key: str) -> str:
        item = by_key[key]
        if item["passed"]:
            return "confirmed"
        if item["state"] in {STATE_RED, STATE_CHASE_RISK}:
            return "rejected"
        if item["state"] in {STATE_UNKNOWN, STATE_PENDING, STATE_UNVERIFIED, STATE_CONFLICT}:
            return "pending"
        return "not_confirmed"

    path_rows = [by_key[key] for key in ("breakout_950", "breakout_1000", "breakout_1100")]
    path_status = "confirmed" if any(item["passed"] for item in path_rows) else (
        "pending" if any(item["state"] in {STATE_UNKNOWN, STATE_PENDING, STATE_UNVERIFIED, STATE_CONFLICT} for item in path_rows)
        else "not_confirmed"
    )
    return [
        {
            "key": "fundamentals",
            "status": fundamental_status,
            "confirmed_checks": fundamental_passes,
            "required_checks": 4,
            "note": "Revenue and EPS plus at least two additional fundamental checks are required.",
        },
        {"key": "guidance", "status": single_group("guidance"), "confirmation_credit": 1},
        {"key": "premarket_reaction", "status": single_group("premarket_gap"), "confirmation_credit": 1},
        {
            "key": "volume_price_confirmation",
            "status": single_group("relative_volume"),
            "confirmation_credit": 1,
        },
        {
            "key": "price_path",
            "status": path_status,
            "confirmation_credit": 1,
            "note": "The three nested breakout boxes contribute at most one independent confirmation.",
        },
    ]


def _decision(checks: List[Dict[str, Any]], groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_key = {item["key"]: item for item in checks}
    confirmed = sum(bool(item["passed"]) for item in checks)
    known = sum(item["state"] != STATE_UNKNOWN for item in checks)
    unknown = len(checks) - known
    pending = sum(item["state"] in {STATE_PENDING, STATE_UNVERIFIED, STATE_CONFLICT} for item in checks)
    red = sum(item["state"] == STATE_RED for item in checks)
    evidence_conflicts = sum(item["state"] == STATE_CONFLICT for item in checks)
    missing_evidence = sum(item["state"] in {STATE_UNKNOWN, STATE_UNVERIFIED} for item in checks)
    independent_confirmed = sum(item["status"] == "confirmed" for item in groups)
    market_confirmed = sum(
        item["status"] == "confirmed"
        for item in groups
        if item["key"] in {"premarket_reaction", "volume_price_confirmation", "price_path"}
    )
    critical = [by_key[key] for key in ("revenue_beat", "eps_beat", "guidance")]
    critical_rejected = [item["key"] for item in critical if item["state"] == STATE_RED]
    critical_pending = [
        item["key"] for item in critical
        if item["state"] in {STATE_UNKNOWN, STATE_PENDING, STATE_NEUTRAL, STATE_UNVERIFIED, STATE_CONFLICT}
    ]
    chase_risk = by_key["premarket_gap"]["state"] == STATE_CHASE_RISK

    raw_band = "strong" if confirmed >= 8 else "moderate" if confirmed >= 5 else "weak"
    fundamental_group = next(item for item in groups if item["key"] == "fundamentals")
    strong = (
        confirmed >= 8
        and not critical_rejected
        and not critical_pending
        and not chase_risk
        and fundamental_group["status"] == "confirmed"
        and independent_confirmed >= 4
        and market_confirmed >= 2
    )
    moderate = (
        confirmed >= 5
        and not critical_rejected
        and not critical_pending
        and not chase_risk
        and independent_confirmed >= 2
        and known >= 7
    )

    if evidence_conflicts:
        decision = "no_trade"
        label = "NO TRADE — unresolved evidence conflicts block scoring."
    elif known == 0:
        decision = "data_unavailable"
        label = "Market and event evidence unavailable; no directional conclusion."
    elif critical_rejected or chase_risk:
        decision = "weak"
        label = "Critical evidence rejects the $12 bull-case setup."
    elif strong:
        decision = "strong"
        label = "Strong heuristic confirmation; target probability remains uncalibrated."
    elif moderate:
        decision = "moderate"
        label = "Moderate evidence; the $12 target is not confirmed."
    elif critical_pending or unknown or pending:
        decision = "pending_evidence"
        label = "Required evidence is still pending or unavailable."
    else:
        decision = "weak"
        label = "Observed evidence does not confirm the $12 bull case."

    return {
        "decision": decision,
        "decision_label": label,
        "confirmed": confirmed,
        "total": len(checks),
        "known": known,
        "unknown": unknown,
        "pending": pending,
        "red": red,
        "evidence_conflicts": evidence_conflicts,
        "missing_evidence": missing_evidence,
        "trade_action": "NO_TRADE" if evidence_conflicts or critical_pending or critical_rejected or chase_risk or known == 0 else "RESEARCH_ONLY",
        "raw_box_band": raw_band,
        "independent_confirmations": independent_confirmed,
        "independent_confirmation_total": len(groups),
        "market_confirmations": market_confirmed,
        "critical_rejected": critical_rejected,
        "critical_pending": critical_pending,
        "chase_risk": chase_risk,
        "proven_probability": False,
    }


_INTERROGATION_QUESTIONS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("market_data", "Is the quote from an identified provider?", ("premarket_gap",)),
    ("market_data", "Is the provider timestamp present?", ("premarket_gap",)),
    ("market_data", "Is observation time distinct from ingestion time?", ("premarket_gap",)),
    ("market_data", "Is the previous close synchronized?", ("premarket_gap",)),
    ("market_data", "Is currency identified?", ("premarket_gap",)),
    ("market_data", "Is price unit identified?", ("premarket_gap",)),
    ("market_data", "Is the premarket gap calculation documented?", ("premarket_gap",)),
    ("market_data", "Is the live price independently confirmed?", ("breakout_950",)),
    ("market_data", "Is relative volume fresh?", ("relative_volume",)),
    ("market_data", "Does price direction confirm volume?", ("relative_volume",)),
    ("earnings", "Is official Q2 revenue actual confirmed?", ("revenue_beat",)),
    ("earnings", "Is revenue consensus timestamped and sourced?", ("revenue_beat",)),
    ("earnings", "Is the revenue beat calculation reproducible?", ("revenue_beat",)),
    ("earnings", "Is official adjusted EPS actual confirmed?", ("eps_beat",)),
    ("earnings", "Is adjusted EPS consensus sourced?", ("eps_beat",)),
    ("earnings", "Are GAAP and adjusted bases kept separate?", ("eps_beat",)),
    ("earnings", "Is the reporting period exactly 2026-Q2?", ("revenue_beat", "eps_beat")),
    ("earnings", "Is the source publication time captured?", ("revenue_beat", "eps_beat")),
    ("business_performance", "Is transaction-service growth confirmed?", ("transaction_growth",)),
    ("business_performance", "Is transaction growth calculated against a comparable period?", ("transaction_growth",)),
    ("business_performance", "Is order growth confirmed?", ("order_growth",)),
    ("business_performance", "Is order growth calculated against a comparable period?", ("order_growth",)),
    ("business_performance", "Is shipper growth confirmed?", ("shipper_growth",)),
    ("business_performance", "Is shipper growth calculated against a comparable period?", ("shipper_growth",)),
    ("business_performance", "Is profitability improvement confirmed on a consistent basis?", ("profitability",)),
    ("business_performance", "Are non-comparable business metrics excluded?", ("profitability",)),
    ("forward_outlook", "Is official Q3 guidance confirmed?", ("guidance",)),
    ("forward_outlook", "Is guidance consensus sourced and timestamped?", ("guidance",)),
    ("forward_outlook", "Is guidance currency and basis explicit?", ("guidance",)),
    ("forward_outlook", "Is guidance change versus prior outlook reproducible?", ("guidance",)),
    ("market_reaction", "Is premarket reaction confirmed after the release?", ("premarket_gap",)),
    ("market_reaction", "Is live volume at least 2x while price advances?", ("relative_volume",)),
    ("market_reaction", "Has $9.50 cleared with volume confirmation?", ("breakout_950",)),
    ("market_reaction", "Has $10.00 cleared with volume confirmation?", ("breakout_1000",)),
    ("market_reaction", "Has $11.00 cleared with at least 3x volume?", ("breakout_1100",)),
    ("historical_comparison", "Are four prior earnings events available point-in-time?", ()),
    ("historical_comparison", "Are prior-event metrics methodologically comparable?", ()),
    ("historical_comparison", "Is historical similarity explicitly uncalibrated?", ()),
    ("risk", "Are all data conflicts resolved?", ()),
    ("risk", "Is an explicit invalidation and NO TRADE path present?", ()),
)


def _pre_trade_interrogation(
    checks: List[Dict[str, Any]],
    decision: Dict[str, Any],
    conflicts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    by_key = {item["key"]: item for item in checks}
    questions: List[Dict[str, Any]] = []
    for number, (category, question, keys) in enumerate(_INTERROGATION_QUESTIONS, 1):
        rows = [by_key[key] for key in keys if key in by_key]
        if number == 39:
            status, answer = (CONFIRMED, True) if not conflicts else (VERIFIED_CONFLICT, False)
        elif number == 40:
            status, answer = CONFIRMED, True
        elif not rows:
            status, answer = MISSING, None
        elif any(item["evidence_summary"]["status"] == VERIFIED_CONFLICT for item in rows):
            status, answer = VERIFIED_CONFLICT, None
        elif all(item["evidence_summary"]["status"] == CONFIRMED for item in rows):
            status, answer = CONFIRMED, all(item["passed"] for item in rows)
        elif any(item["evidence_summary"]["status"] == PENDING for item in rows):
            status, answer = PENDING, None
        else:
            status, answer = UNVERIFIED, None
        questions.append({
            "number": number,
            "category": category,
            "question": question,
            "answer": answer,
            "status": status,
            "signal_keys": list(keys),
            "sources": sorted({
                item["evidence_summary"].get("source") for item in rows
                if item["evidence_summary"].get("source")
            }),
            "missing_chain_fields": sorted({
                field for item in rows for field in item["evidence_summary"].get("missing_chain_fields", [])
            }),
        })
    verified = sum(item["status"] == CONFIRMED for item in questions)
    missing = sum(item["status"] in {MISSING, UNVERIFIED, PENDING} for item in questions)
    market_confirmed = decision.get("market_confirmations", 0)
    return {
        "question_count": len(questions),
        "questions": questions,
        "core_conditions_verified": verified,
        "historical_similarity": {
            "status": MISSING,
            "events_required": 4,
            "events_verified": 0,
            "calibrated": False,
        },
        "market_confirmation": {
            "confirmed_groups": market_confirmed,
            "status": CONFIRMED if market_confirmed >= 2 else UNVERIFIED,
        },
        "invalidation": "Unresolved critical evidence, any DATA_CONFLICT, weak price reaction, or unconfirmed volume => NO TRADE.",
        "scenarios": {
            "base": "Target not confirmed until critical earnings and market evidence are confirmed.",
            "bull": "Requires confirmed earnings/guidance plus at least two independent market confirmations.",
            "extreme": "Requires confirmed extreme fundamentals, synchronized reaction, and advancing volume; chase risk still blocks entry.",
        },
        "confidence": {"available": False, "calibrated": False, "reason": "No validated probability calibration."},
        "conflict_count": len(conflicts),
        "missing_evidence_count": missing,
        "trade_action": decision.get("trade_action", "NO_TRADE"),
    }


def build_ymm_12_checklist(
    values: Optional[Dict[str, Any]] = None,
    *,
    evidence: Optional[Dict[str, Dict[str, Any]]] = None,
    source_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Pure scenario evaluation from direct values or normalized evidence."""
    normalized = dict(evidence or _normalize_direct_values(values))
    conflicts = conflict_records(normalized)
    checks = _build_checks(normalized)
    groups = _group_summary(checks)
    decision = _decision(checks, groups)
    required_return = (SCENARIO["target_price"] / SCENARIO["reference_price"] - 1.0) * 100.0
    data_status = (
        "DATA_UNAVAILABLE" if decision["known"] == 0
        else "PARTIAL" if decision["unknown"] or decision["pending"]
        else "AVAILABLE"
    )
    return {
        "ok": True,
        "symbol": SCENARIO["symbol"],
        "scenario": {
            **SCENARIO,
            "required_return_pct": round(required_return, 2),
        },
        "target": SCENARIO["target_price"],
        "target_label": f"${SCENARIO['target_price']:g}",
        "data_status": data_status,
        **decision,
        "groups": groups,
        "checks": checks,
        "data_conflicts": conflicts,
        "pre_trade_interrogation": _pre_trade_interrogation(checks, decision, conflicts),
        "source_status": source_status or {},
        "disclaimer": (
            "Uncalibrated scenario checklist, not a probability or trading instruction. "
            "Nested price milestones contribute only one independent confirmation."
        ),
    }


def auto_fill_ymm_12(
    symbol: str = "YMM",
    *,
    operator_evidence: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Merge safe live-market evidence with validated post-release evidence."""
    auto_evidence, sources = fetch_auto_evidence(symbol)
    merged = merge_evidence_sets(auto_evidence, operator_evidence or {})
    return build_ymm_12_checklist(evidence=merged, source_status=sources)


__all__ = [
    "ChecklistInputError",
    "OPERATOR_PROVENANCE",
    "UnsupportedScenarioError",
    "SCENARIO",
    "auto_fill_ymm_12",
    "build_ymm_12_checklist",
    "fetch_auto_evidence",
    "validate_operator_payload",
]
