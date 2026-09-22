"""Short interest and borrow data -- both free, both verified only by the probe.

SHORT INTEREST: FINRA Query API, consolidated short interest (exchange-listed
and OTC, reported twice a month, published with a lag). Written to FINRA's
public API conventions; field names differ across FINRA datasets, so the parser
accepts the known variants and the probe REPORTS the field names it actually
saw -- a mismatch is diagnosed on the next probe run, not guessed at.

BORROW: iBorrowDesk's public JSON API, which republishes Interactive Brokers'
shortable-shares and fee data over HTTPS. That is the same data as IBKR's FTP
file, which Railway's network blocked (probe, 2026-09-22: TimeoutError).
Unofficial and third-party: every value carries source="iborrowdesk".

Neither is ever converted into a zero or a default. Missing means UNKNOWN.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from edge.providers import base as B

FINRA_SI = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
IBD = "https://iborrowdesk.com/api/ticker/{symbol}"

_SYM = ("symbolCode", "issueSymbolIdentifier", "symbol")
_QTY = ("currentShortPositionQuantity", "currentShortShareNumber", "shortInterestQuantity")
_DATE = ("settlementDate", "settlementDateLatest")
_ADV = ("averageDailyVolumeQuantity", "averageShortVolume", "averageDailyVolume")
_DTC = ("daysToCoverQuantity", "daysToCover")


def _pick(row: Dict[str, Any], keys) -> Any:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def parse_finra_si(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sym = _pick(r, _SYM)
        qty = _pick(r, _QTY)
        if not sym or qty is None:
            continue
        try:
            q = float(qty)
        except (TypeError, ValueError):
            continue
        adv, dtc = _pick(r, _ADV), _pick(r, _DTC)
        key, when = str(sym).upper(), str(_pick(r, _DATE) or "")
        prior = out.get(key)
        if prior is not None and str(prior.get("settlement_date") or "") >= when:
            continue            # several reports per symbol: the LATEST settlement wins, whatever the row order
        out[key] = {
            "short_shares": q, "settlement_date": _pick(r, _DATE),
            "avg_daily_volume": float(adv) if adv not in (None, "") else None,
            "days_to_cover": float(dtc) if dtc not in (None, "") else None,
            "source": "finra_consolidated_short_interest",
        }
    return out


def fetch_finra_si(http, *, settlement_date: Optional[str] = None, limit: int = 5000) -> List[Dict[str, Any]]:
    body: Dict[str, Any] = {"limit": limit}
    if settlement_date:
        body["compareFilters"] = [{"compareType": "EQUAL", "fieldName": "settlementDate",
                                   "fieldValue": settlement_date}]
    r = http.post(FINRA_SI, json=body, headers={"Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def probe_finra_si(http) -> B.Probe:
    t0 = time.monotonic()
    try:
        r = http.post(FINRA_SI, json={"limit": 5}, headers={"Accept": "application/json"}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return B.Probe(B.SHORT_INTEREST, "finra_api", B.ERROR,
                       latency_ms=int((time.monotonic() - t0) * 1000), note=type(exc).__name__)
    ms = int((time.monotonic() - t0) * 1000)
    st = B.classify(r.status_code)
    if st != B.OK:
        return B.Probe(B.SHORT_INTEREST, "finra_api", st, http_status=r.status_code, latency_ms=ms,
                       note="FINRA API may require (free) credentials" if st == B.NOT_AUTHORIZED else "")
    try:
        rows = r.json() if isinstance(r.json(), list) else []
    except Exception:  # noqa: BLE001
        rows = []
    fields = sorted(rows[0].keys())[:25] if rows else []
    parsed = parse_finra_si(rows)
    status = B.OK if parsed else (B.EMPTY if not rows else B.ERROR)
    note = f"fields seen: {', '.join(fields)}" if status != B.OK else "consolidated short interest, twice monthly"
    return B.Probe(B.SHORT_INTEREST, "finra_api", status, http_status=r.status_code, latency_ms=ms,
                   rows=len(parsed), note=note[:300])


def parse_iborrowdesk(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Latest point from iBorrowDesk: prefer real_time, else the last daily row."""
    for key in ("real_time", "daily"):
        rows = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(rows, list) and rows:
            last = rows[-1] if key == "daily" else rows[0]
            fee, avail = last.get("fee"), last.get("available")
            when = last.get("time") or last.get("date")
            return {"borrow_fee_pct": float(fee) if fee is not None else None,
                    "available_shares": float(avail) if avail is not None else None,
                    "as_of": when, "series": key, "source": "iborrowdesk (IBKR data, unofficial)"}
    return None


def fetch_borrow(http, symbol: str) -> Optional[Dict[str, Any]]:
    r = http.get(IBD.format(symbol=symbol.upper()), headers={"User-Agent": "edge-research"}, timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return parse_iborrowdesk(r.json())


def probe_borrow(http, symbol: str = "GME") -> B.Probe:
    t0 = time.monotonic()
    try:
        r = http.get(IBD.format(symbol=symbol), headers={"User-Agent": "edge-research"}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        return B.Probe(B.BORROW, "iborrowdesk", B.ERROR, latency_ms=int((time.monotonic() - t0) * 1000),
                       note=type(exc).__name__)
    ms = int((time.monotonic() - t0) * 1000)
    st = B.classify(r.status_code)
    if st != B.OK:
        return B.Probe(B.BORROW, "iborrowdesk", st, http_status=r.status_code, latency_ms=ms)
    try:
        point = parse_iborrowdesk(r.json())
    except Exception:  # noqa: BLE001
        point = None
    return B.Probe(B.BORROW, "iborrowdesk", B.OK if point and point["borrow_fee_pct"] is not None else B.EMPTY,
                   http_status=r.status_code, latency_ms=ms, rows=1 if point else 0,
                   note="IBKR borrow data via iBorrowDesk (unofficial)")


def si_age_days(settlement_date: Optional[str], today: date) -> Optional[float]:
    if not settlement_date:
        return None
    try:
        d = datetime.fromisoformat(str(settlement_date)[:10]).date()
    except ValueError:
        return None
    return float((today - d).days)


def fetch_finra_si_for(http, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Latest consolidated short interest for these symbols (one request).

    Uses FINRA's domainFilters / sortFields query conventions; if FINRA rejects
    the shape, the error is recorded by the caller and the signal stays UNKNOWN.
    """
    body = {"limit": 1000, "domainFilters": [{"fieldName": "symbolCode", "values": [s.upper() for s in symbols]}],
            "sortFields": ["-settlementDate"]}
    r = http.post(FINRA_SI, json=body, headers={"Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    rows = r.json() if isinstance(r.json(), list) else []
    return parse_finra_si(rows)
