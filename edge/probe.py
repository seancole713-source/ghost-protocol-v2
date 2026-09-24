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
        {"need": "full-market premarket movers", "ok": [B.SNAPSHOT_ALL, "movers.premarket_scan", B.MOVERS]},
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
    "short_interest_ignition": [
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
    # Operations, not strategies: each is what a whole stage of the day depends on.
    "paper_execution": [
        {"need": "Alpaca PAPER account accepting these keys", "ok": ["broker.paper"]},
    ],
    "phone_messages": [
        {"need": "Telegram bot that can reach the operator's chat", "ok": ["notify.telegram"]},
    ],
    "ai_research": [
        {"need": "Claude research author", "ok": ["llm.research.author"]},
        {"need": "independent reviewer", "ok": ["llm.reviewer.independent"], "fallback": ["llm.research.author"]},
    ],
}

UNLOCK: Dict[str, str] = {
    B.QUOTE_SIP: "real-time consolidated (SIP) quotes: Alpaca's paid market-data plan "
                 "(its docs name it Algo Trader Plus) or a Polygon/Massive real-time stocks plan",
    B.QUOTE_IEX: "Alpaca's free plan provides IEX -- check ALPACA_KEY_ID/SECRET",
    B.SNAPSHOT_ALL: "Polygon/Massive stocks plan that includes the all-tickers snapshot",
    B.MOVERS: "Alpaca screener (documented as available on the free data plan) -- check the key",
    B.DAILY_ALL: "Polygon/Massive plan that includes grouped daily bars (OK on 2026-09-22)",
    B.MINUTE_BARS: "minute aggregates: Polygon/Massive stocks plan, or Alpaca SIP bars (>15 min delayed on free)",
    B.UNIVERSE: "Polygon/Massive reference tickers (reference endpoints are on most plans)",
    B.SPLITS: "Polygon/Massive reference splits",
    B.DIVIDENDS: "Polygon/Massive reference dividends",
    B.NEWS: "Alpaca news (free with a data key) or Polygon/Massive news",
    B.FILINGS: "SEC EDGAR is free -- set EDGAR_USER_AGENT to a name and a real contact email",
    B.BORROW: "IBKR borrow data: the FTP file (blocked from Railway) or iBorrowDesk over HTTPS (free, "
              "unofficial). Commercial alternatives (Ortex, Fintel, S3 Partners) need quotes and licence terms",
    B.SHORT_INTEREST: "FINRA consolidated short interest via the FINRA Query API (free; may need free "
                      "API credentials -- the probe says which)",
    B.SHORT_VOLUME: "FINRA daily short-sale volume is free (it is NOT short interest)",
    "movers.premarket_scan": "IEX snapshots of the prior session's liquid stocks (free); a consolidated view needs SIP",
    "broker.paper": "Alpaca PAPER keys in ALPACA_KEY_ID/ALPACA_SECRET_KEY (live keys are refused by the paper host)",
    "notify.telegram": "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID for a bot that is in the operator's chat",
    "llm.research.author": "ANTHROPIC_API_KEY with access to claude-opus-5-5 (and credit)",
    "llm.reviewer.independent": "OPENAI_API_KEY with credit, and EDGE_OPENAI_MODEL set to a model the probe lists",
}


# The probe makes dozens of data calls (the premarket scan alone is ~33 IEX
# batches). Run during the session it starves the live radar: on 2026-09-24 a
# 9:48 CT redeploy re-ran it at once and the 9:57 CT intraday tick got a 429.
# So it runs once per ET day, never between 08:00 and 16:30 ET on a weekday.
QUIET_START, QUIET_END = (8, 0), (16, 30)


def due(now: int, last_checked_at: Optional[int]) -> bool:
    """True when the daily probe should run now."""
    from datetime import datetime
    from edge.contracts import ET
    t = datetime.fromtimestamp(now, tz=ET)
    if t.weekday() < 5 and QUIET_START <= (t.hour, t.minute) < QUIET_END:
        return False
    if last_checked_at is None:
        return True
    return datetime.fromtimestamp(int(last_checked_at), tz=ET).date() != t.date()


def collect(get: Optional[B.HttpGet] = None, *, ibkr_fetch: Optional[Callable[[], str]] = None,
            http=None, llm_client=None) -> List[B.Probe]:
    """`http` (requests-like, with post) enables the FINRA API, iBorrowDesk and LLM probes."""
    from edge.providers import shortdata
    probes: List[B.Probe] = []
    probes += polygon.probe(get)
    probes += alpaca.probe(get)
    from edge import premarket as PM
    probes.append(PM.probe(get))
    probes.append(public.probe_finra(get))
    probes.append(public.probe_edgar(get))
    probes.append(public.probe_ibkr(ibkr_fetch) if ibkr_fetch else public.probe_ibkr())
    if http is not None:
        from edge import research_openai
        probes.append(shortdata.probe_finra_si(http))
        probes.append(shortdata.probe_borrow(http))
        probes.append(research_openai.probe(http))
        from edge import research_worker
        probes.append(research_worker.probe(llm_client))
        from edge import notify, paper
        probes.append(paper.probe(http))
        probes.append(notify.probe(http))
    else:
        probes.append(B.Probe(B.SHORT_INTEREST, "finra_api", B.UNVERIFIED, note="probe not run (no http client)"))
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
