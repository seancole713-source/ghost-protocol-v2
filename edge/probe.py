"""Readiness probe: what the CURRENT keys can actually see -- before money is spent.

Runs inside production (the only place with both the keys and the network),
one bounded request per capability, and answers per strategy:

    READY     every required capability answered with data
    DEGRADED  runs, but on a fallback with known coverage limits (e.g. IEX quotes)
    BLOCKED   a required capability is missing -- and names what would unblock it

The unlock hints name vendor plans as their own documentation describes them.
Prices and licensing change; get a written quote before buying. Nothing here
estimates cost.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

from edge.providers import alpaca, polygon, public
from edge.providers import base as B

# Each requirement is a group: any capability in `ok` satisfies it; a
# capability in `fallback` satisfies it DEGRADED.
STRATEGIES: Dict[str, List[Dict[str, Any]]] = {
    "premarket_continuation": [
        {"need": "full-market premarket movers", "ok": [B.SNAPSHOT_ALL, B.MOVERS]},
        {"need": "real-time quotes", "ok": [B.QUOTE_SIP], "fallback": [B.QUOTE_IEX]},
        {"need": "minute bars (outcomes)", "ok": [B.MINUTE_BARS]},
        {"need": "dated catalysts", "ok": [B.NEWS, B.FILINGS]},
    ],
    "catalyst_breakout": [
        {"need": "dated catalysts", "ok": [B.NEWS, B.FILINGS]},
        {"need": "real-time quotes", "ok": [B.QUOTE_SIP], "fallback": [B.QUOTE_IEX]},
        {"need": "minute bars", "ok": [B.MINUTE_BARS]},
    ],
    "crowded_short_ignition": [
        {"need": "borrow fee and availability", "ok": [B.BORROW]},
        {"need": "short interest (not short VOLUME)", "ok": [B.SHORT_INTEREST]},
        {"need": "real-time quotes", "ok": [B.QUOTE_SIP], "fallback": [B.QUOTE_IEX]},
        {"need": "minute bars", "ok": [B.MINUTE_BARS]},
    ],
    "intraday_continuation": [
        {"need": "full-market movers", "ok": [B.SNAPSHOT_ALL, B.MOVERS]},
        {"need": "real-time quotes", "ok": [B.QUOTE_SIP], "fallback": [B.QUOTE_IEX]},
        {"need": "minute bars", "ok": [B.MINUTE_BARS]},
    ],
    "daily_miss_audit": [
        {"need": "every stock's daily move", "ok": [B.DAILY_ALL, B.SNAPSHOT_ALL]},
        {"need": "corporate actions", "ok": [B.SPLITS]},
    ],
    "supported_universe": [
        {"need": "listed-ticker reference", "ok": [B.UNIVERSE]},
    ],
}

UNLOCK: Dict[str, str] = {
    B.QUOTE_SIP: "real-time consolidated (SIP) quotes: Alpaca's paid market-data plan "
                 "(its docs name it Algo Trader Plus) or a Polygon/Massive real-time stocks plan",
    B.QUOTE_IEX: "Alpaca's free plan provides IEX -- check ALPACA_KEY_ID/SECRET",
    B.SNAPSHOT_ALL: "Polygon/Massive stocks plan that includes the all-tickers snapshot",
    B.MOVERS: "Alpaca screener (documented as available on the free data plan) -- check the key",
    B.DAILY_ALL: "Polygon/Massive plan that includes grouped daily bars (refused 403 on 2026-09-05)",
    B.MINUTE_BARS: "minute aggregates: Polygon/Massive stocks plan, or Alpaca SIP bars (>15 min delayed on free)",
    B.UNIVERSE: "Polygon/Massive reference tickers (reference endpoints are on most plans)",
    B.SPLITS: "Polygon/Massive reference splits",
    B.DIVIDENDS: "Polygon/Massive reference dividends",
    B.NEWS: "Alpaca news (free with a data key) or Polygon/Massive news",
    B.FILINGS: "SEC EDGAR is free -- set SEC_USER_AGENT to a real contact",
    B.BORROW: "IBKR's public shortstock FTP file is free; if it errors, Railway may block outbound FTP. "
              "Commercial alternatives (Ortex, Fintel, S3 Partners) need quotes and licence terms",
    B.SHORT_INTEREST: "FINRA equity short interest (free, twice monthly) -- adapter not built yet",
    B.SHORT_VOLUME: "FINRA daily short-sale volume is free (it is NOT short interest)",
}


def collect(get: Optional[B.HttpGet] = None, *, ibkr_fetch: Optional[Callable[[], str]] = None) -> List[B.Probe]:
    probes: List[B.Probe] = []
    probes += polygon.probe(get)
    probes += alpaca.probe(get)
    probes.append(public.probe_finra(get))
    probes.append(public.probe_edgar(get))
    probes.append(public.probe_ibkr(ibkr_fetch) if ibkr_fetch else public.probe_ibkr())
    probes.append(B.Probe(B.SHORT_INTEREST, "finra", B.UNVERIFIED, note="adapter not built yet"))
    return probes


def best_by_capability(probes: List[B.Probe]) -> Dict[str, Dict[str, Any]]:
    rank = {B.OK: 0, B.EMPTY: 1, B.RATE_LIMITED: 2, B.NOT_AUTHORIZED: 3, B.ERROR: 4, B.NO_KEY: 5, B.UNVERIFIED: 6}
    out: Dict[str, Dict[str, Any]] = {}
    for p in probes:
        cur = out.get(p.capability)
        if cur is None or rank.get(p.status, 9) < rank.get(cur["status"], 9):
            out[p.capability] = {"status": p.status, "provider": p.provider,
                                 "http_status": p.http_status, "rows": p.rows, "note": p.note}
    return out


def assess(caps: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    ok = lambda c: caps.get(c, {}).get("status") == B.OK
    result = {}
    for name, groups in STRATEGIES.items():
        state, missing, degraded = "READY", [], []
        for g in groups:
            if any(ok(c) for c in g["ok"]):
                continue
            if any(ok(c) for c in g.get("fallback", [])):
                degraded.append(g["need"])
                if state == "READY":
                    state = "DEGRADED"
                continue
            state = "BLOCKED"
            missing.append({"need": g["need"],
                            "tried": {c: caps.get(c, {}).get("status", B.UNVERIFIED) for c in g["ok"]},
                            "unlock": [UNLOCK[c] for c in g["ok"] if c in UNLOCK]})
        result[name] = {"state": state, "missing": missing, "degraded": degraded}
    return result


def run(get: Optional[B.HttpGet] = None, **kw) -> Dict[str, Any]:
    probes = collect(get, **kw)
    caps = best_by_capability(probes)
    return {"probe_version": "edge_probe_v1", "checked_at": int(time.time()),
            "capabilities": caps, "strategies": assess(caps),
            "probes": [p.to_dict() for p in probes],
            "caveat": "Plan names come from vendor documentation; get written quotes "
                      "and licence terms before buying."}


def summary_lines(report: Dict[str, Any]) -> List[str]:
    lines = []
    for name, s in report["strategies"].items():
        line = f"{name}: {s['state']}"
        if s["degraded"]:
            line += f" (fallback for: {', '.join(s['degraded'])})"
        for m in s["missing"]:
            line += f" | needs {m['need']} -> {m['unlock'][0] if m['unlock'] else 'no known source'}"
        lines.append(line)
    return lines


if __name__ == "__main__":
    rep = run()
    print("EDGE_PROBE " + json.dumps(rep, separators=(",", ":"), default=str))
    for ln in summary_lines(rep):
        print("EDGE_PROBE_SUMMARY " + ln)
