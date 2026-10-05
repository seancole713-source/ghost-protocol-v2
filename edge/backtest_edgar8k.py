"""Backtest of a PREREGISTERED hypothesis: edgar_8k_breakout@v1 (registered 2026-10-03).

History replay only: no live order, no forward ledger, nothing here is ever a forecast of record.

The rule, implemented as registered (never loosened):
  eligibility  decided by 09:05 ET on session D. A Form 8-K (NOT 8-K/A) ACCEPTED by SEC EDGAR in
               the window [D-1 16:00:00 ET, D 09:00:00 ET] -- both ends INCLUSIVE, D-1 being the
               previous trading SESSION -- that lists Item 1.01, or Item 8.01 with an EX-99*
               exhibit. EXCLUDED when the same filing lists Item 3.02, 2.03, 1.03 or 3.01, or has an
               exhibit described as underwriting / securities purchase / registered direct /
               at-the-market / placement agency (or agent) / registration rights agreement.
               The issuer CIK must map to exactly one common-stock ticker (SEC company_tickers.json
               intersected with Polygon reference type=CS on D; see MAPPING below). D-1 close (as
               traded, restated onto D's basis for splits executed after D-1 and by D) $2-$500.
               20-session average daily dollar volume >= $5M (Polygon grouped daily, the 20 sessions
               before D, the ticker present in all 20). No reverse split executed in sessions
               D-5..D (Polygon reference splits). Up to 4 per day, ranked by 20-day dollar volume.
  levels       ($1,000, frozen) buy stop-limit placed 09:25 ET; trigger = close(D-1) x 1.04;
               limit = trigger x 1.015; shares = floor(1000 / limit); target = trigger x 1.05;
               stop = trigger x 0.97; entry window 09:30-10:30 ET; time exit 15:30 ET. Every level
               is rounded to the cent (edge.contracts' rounding).
  fill         the first minute bar in the window whose high >= trigger fills at
               max(trigger, that bar's open), which must be <= limit, else NO_FILL (opening above
               the limit = NO_FILL). The order is then graded by edge.resolver.resolve_execution
               (resolver_v2: one bar touching target and stop = LOSS). Where the resolver is
               STRICTER than the registered fill (a bar that triggers inside the minute and closes
               above the limit: the resolver rests the order at the limit, as the paper broker
               did), the stricter answer stands and the trade is flagged `resolver_stricter`.
               Never the other way: a bar that opened above the limit is NO_FILL even though the
               resolver alone would fill a dip back to the limit.
  control      "no-8-K twins", simulated only: same-day common stocks passing the same price /
               liquidity / reverse-split filters whose CIK has NO 8-K or 8-K/A accepted in the
               window, that hit the same +4% trigger inside 09:30-10:30; identical levels and
               grading. CAPPED at the top TWIN_CAP (20) per day by 20-day dollar volume. A ticker
               whose CIK cannot be identified (no unique mapping) is never a twin: its 8-Ks are
               unknowable. Twins are chosen GIVEN that they triggered (the 8-K arm is not), so the
               comparison is per filled trade, not per signal.
  costs        headline 10 bps a side; 25 bps a side; a 1-minute entry delay (window opens
               09:31) at both costs.
  gate         step-1 PASS only if the Wilson 95% LOWER bound of the win rate >= 30% AND the
               expectancy after 25 bps a side > $0 AND it beats the twins (higher expectancy at the
               headline cost; the difference is reported with a session-clustered 95% interval).
               Break-even reference 37.5%.

MAPPING. company_tickers.json is TODAY's list: it no longer carries issuers acquired or delisted
since. Polygon's point-in-time reference rows carry their own `cik`, so a CIK with no CS ticker in
company_tickers.json falls back to the Polygon CS rows with that CIK on D. Each mapping records its
source; a ticker claimed by two CIKs is ambiguous and dropped. CS membership on D is the union of
the Polygon type=CS snapshots (quarterly, `date=`) bracketing D.

DATA AND RESUMABILITY. The run spans many overnight ticks and is resumable at every step:
  phase 0  meta: Polygon splits, quarterly CS snapshots, SEC company_tickers.json      (stored once)
  phase 1  Polygon grouped daily (adjusted=false), one paced call per session, cached per
           session with that session's plan (passing tickers, restated prior close, 20-day
           dollar volume, the day's high). A stored session is never fetched again. A 403 /
           plan error or an empty answer is stored as UNAVAILABLE and reported, not retried.
           EDGAR daily form indexes are fetched in the Polygon pacing gaps.
  phase 2  the rest of the EDGAR daily indexes (form.YYYYMMDD.idx), 8-K and 8-K/A rows cached.
  phase 3  the number of filing headers the run needs is counted once and reported.
  phase 4  one session at a time: the filing headers it needs (-index-headers.html, cached per
           accession, paced <= 5 requests/s), Alpaca raw SIP minute bars, the grades. The
           session's result is stored and never recomputed.
EDGAR filing headers are fetched only for 8-K / 8-K/A rows whose CIK maps to a ticker that passed
the price / liquidity / split filters that day.

Limits: see LIMITS (printed on every report).
"""
from __future__ import annotations

import html
import json
import logging
import math
import os
import random
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from edge import stats
from edge.backtest import MINUTE_ADJUSTMENT, PRICE_BASIS, PRICE_BASIS_DETAIL, _bars, share_factor
from edge.contracts import (COUNTED, ET, LOSS, NO_FILL, TIME_EXIT, UNRESOLVED, WIN, ExperimentSpec, Forecast,
                            forecast_id_for)
from edge.providers import alpaca as A, polygon as PG
from edge.providers.public import _sec_user_agent
from edge.resolver import RESOLVER_VERSION, resolve_execution
from shared.redaction import redact_exc

LOG = logging.getLogger("edge.backtest_edgar8k")

VERSION = "edgar_8k_breakout_backtest_v1"
TABLE = "edge_backtest_edgar8k"                 # the final report, one row per VERSION
T_META = "edge_bt_edgar8k_meta"                 # splits, CS snapshots, company tickers; progress
T_DAILY = "edge_bt_edgar8k_daily"               # Polygon grouped daily (CS tickers), per session
T_PLAN = "edge_bt_edgar8k_plan"                 # per session: tickers passing the filters
T_DAY = "edge_bt_edgar8k_day"                   # per session: the graded result
T_INDEX = "edge_edgar_index"                    # EDGAR daily form index, 8-K / 8-K/A rows, per date
T_HEADER = "edge_edgar_header"                  # EDGAR filing header, parsed, per accession
PARSER_VERSION = 1

PREREG_START, PREREG_END = date(2026, 7, 1), date(2026, 9, 30)
END_DAY = PREREG_END
DEFAULT_START = date(2024, 10, 7)
START_ENV = "EDGE_BT_EDGAR_START"

PRICE_MIN, PRICE_MAX, MIN_ADV, ADV_DAYS, SPLIT_SESSIONS = 2.0, 500.0, 5_000_000.0, 20, 5
TRIGGER_MULT, LIMIT_MULT, TARGET_MULT, STOP_MULT, SIZE_USD = 1.04, 1.015, 1.05, 0.97, 1000.0
MAX_PER_DAY, TWIN_CAP, TWIN_CHUNK = 4, 20, 40
GATE_WILSON_LOW = 0.30
VARIANTS = {"cost_10bps": (10.0, 0), "cost_25bps": (25.0, 0),
            "delay_1min_10bps": (10.0, 60), "delay_1min_25bps": (25.0, 60)}
HEADLINE = "cost_10bps"

SPEC = ExperimentSpec(
    name="edgar_8k_breakout", version=1, setup="edgar_8k_breakout",
    description="backtest-only, preregistered 2026-10-03: overnight 8-K (Item 1.01, or 8.01 + EX-99), "
                "no dilution items/exhibits; buy stop-limit at prior close +4%, +5% / -3%, flat 15:30 ET",
    trigger_mult=TRIGGER_MULT, limit_mult=round(TRIGGER_MULT * LIMIT_MULT, 6), target_mult=TARGET_MULT,
    stop_mult=STOP_MULT, entry_expiry_et="10:30", time_exit_et="15:30", size_usd=SIZE_USD,
    max_per_day=MAX_PER_DAY,
    eligibility={"filing": "8-K (not 8-K/A) accepted D-1 16:00 .. D 09:00 ET",
                 "items": "1.01, or 8.01 with an EX-99 exhibit",
                 "exclude_items": ["3.02", "2.03", "1.03", "3.01"],
                 "exclude_exhibits": ["underwriting", "securities purchase", "registered direct",
                                      "at-the-market", "placement agency", "registration rights agreement"],
                 "price": [PRICE_MIN, PRICE_MAX], "min_avg_dollars_20d": MIN_ADV,
                 "no_reverse_split_sessions": SPLIT_SESSIONS, "rank": "avg_dollar_volume_20d desc",
                 "limit": "trigger x 1.015", "shares": "floor(1000 / limit)"})

LIMITS = [
    "research evidence on past sessions, NOT the forward record",
    "CIK->ticker uses SEC company_tickers.json as of the run (not point-in-time); a CIK it no longer "
    "lists falls back to Polygon's point-in-time reference cik (each mapping records its source)",
    "common-stock membership from quarterly Polygon type=CS snapshots bracketing each session",
    "twins are capped at the top 20 by 20-day dollar volume per day and pre-screened by the "
    "day's Polygon high >= trigger; they are chosen given they triggered",
    "costs 10 / 25 bps a side are assumed, not measured; SIP minute bars, raw as traded",
    "prices and volume as traded (raw), restated only for splits executed by the session; "
    "an unlisted split is a miss",
    "EDGAR acceptance time is the SEC's ACCEPTANCE-DATETIME read as America/New_York",
]

# NYSE closures / 13:00 early closes before 2026 (edge.calendar's built-in table starts in 2026).
EXTRA_HOLIDAYS = {
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04",
    "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
    "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
}
EXTRA_EARLY_CLOSE = {"2024-07-03", "2024-11-29", "2024-12-24", "2025-07-03", "2025-11-28", "2025-12-24"}

SEC_BASE = "https://www.sec.gov"
SEC_TICKERS_URL = SEC_BASE + "/files/company_tickers.json"
SEC_MIN_INTERVAL_S = 0.2           # <= 5 requests a second (the SEC allows 10)
SEC_BACKOFF_S = (2.0, 10.0)
DEFAULT_BUDGET_S = 2700            # 45 of the job's 55 minutes per tick
SNAPSHOT_EVERY_DAYS = 91


class _Transient(RuntimeError):
    """A source failed in a way worth retrying next tick (rate limit, 5xx, timeout)."""


class _OutOfTime(RuntimeError):
    """This tick's budget is spent; progress is stored and the next tick continues."""


class _MinuteUnavailable(RuntimeError):
    """Alpaca refused (401/403) or returned nothing: the session's minute data is not available."""


# ------------------------------------------------------------------------------ calendar --
def is_session(d: date) -> bool:
    from edge.pipeline import trading_day
    return d.weekday() < 5 and d.isoformat() not in EXTRA_HOLIDAYS and trading_day(d)


def early_close(d: date) -> bool:
    from edge import calendar as CAL
    return d.isoformat() in EXTRA_EARLY_CLOSE or bool(CAL.session(d)["early_close"])


def sessions(start: date, end: date, warm: int = ADV_DAYS) -> List[date]:
    """`warm` sessions before `start`, then every session from `start` through `end`."""
    out, d = [], start
    while d <= end:
        if is_session(d):
            out.append(d)
        d += timedelta(days=1)
    pre, d = [], start - timedelta(days=1)
    while len(pre) < warm:
        if is_session(d):
            pre.append(d)
        d -= timedelta(days=1)
    return list(reversed(pre)) + out


def _at(day: date, hh: int, mm: int, ss: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=ET).timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=ET).isoformat()


def _weekdays(lo: date, hi: date) -> List[date]:
    out, d = [], lo
    while d <= hi:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def window_bounds(day: date, prev_day: date) -> Tuple[datetime, datetime]:
    """The 8-K acceptance window for session `day`: [prev_day 16:00:00, day 09:00:00] ET, inclusive."""
    return (datetime(prev_day.year, prev_day.month, prev_day.day, 16, 0, 0, tzinfo=ET),
            datetime(day.year, day.month, day.day, 9, 0, 0, tzinfo=ET))


def in_window(accepted: Optional[str], day: date, prev_day: date) -> bool:
    if not accepted:
        return False
    t = datetime.fromisoformat(accepted).replace(tzinfo=ET)
    lo, hi = window_bounds(day, prev_day)
    return lo <= t <= hi


# --------------------------------------------------------------------------------- EDGAR --
_IDX_RE = re.compile(r"^(8-K(?:/A)?)\s+(.+?)\s+(\d{1,10})\s+(\d{8}|\d{4}-\d{2}-\d{2})\s+(edgar/data/\S+)\s*$")


def daily_index_url(d: date) -> str:
    return f"{SEC_BASE}/Archives/edgar/daily-index/{d.year}/QTR{(d.month - 1) // 3 + 1}/form.{d:%Y%m%d}.idx"


def daily_index_listing_url(d: date) -> str:
    """The quarter's directory listing: which form.YYYYMMDD.idx files EDGAR actually published."""
    return f"{SEC_BASE}/Archives/edgar/daily-index/{d.year}/QTR{(d.month - 1) // 3 + 1}/index.json"


def parse_listing(text: str) -> Optional[Set[str]]:
    """form.*.idx names in an EDGAR directory listing (index.json), or None when it cannot be read."""
    try:
        items = json.loads(text)["directory"]["item"]
    except (ValueError, KeyError, TypeError):
        return None
    names = {str(i.get("name")) for i in items if isinstance(i, dict) and str(i.get("name", "")).startswith("form.")}
    return names or None


def parse_daily_index(text: str) -> List[List[Any]]:
    """[[form, cik, accession]] for every 8-K and 8-K/A row of an EDGAR form.YYYYMMDD.idx."""
    out = []
    for line in (text or "").splitlines():
        m = _IDX_RE.match(line.rstrip())
        if not m:
            continue
        acc = m.group(5).rsplit("/", 1)[-1]
        acc = acc[:-4] if acc.endswith(".txt") else acc
        out.append([m.group(1), int(m.group(3)), acc])
    return out


def header_url(cik: int, accession: str) -> str:
    return f"{SEC_BASE}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index-headers.html"


def submission_url(cik: int, accession: str) -> str:
    return f"{SEC_BASE}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}.txt"


def _norm_text(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


# EDGAR's ITEM INFORMATION values are the item TITLES; matched by normalized prefix.
ITEM_TITLES = [
    ("1.01", "entry into a material definitive agreement"),
    ("1.02", "termination of a material definitive agreement"),
    ("1.03", "bankruptcy or receivership"),
    ("1.04", "mine safety"),
    ("1.05", "material cybersecurity incident"),
    ("2.01", "completion of acquisition or disposition of assets"),
    ("2.02", "results of operations and financial condition"),
    ("2.03", "creation of a direct financial obligation"),
    ("2.04", "triggering events that accelerate or increase a direct financial obligation"),
    ("2.05", "costs associated with exit or disposal activities"),
    ("2.06", "material impairments"),
    ("3.01", "notice of delisting or failure to satisfy a continued listing rule"),
    ("3.02", "unregistered sales of equity securities"),
    ("3.03", "material modification to rights of security holders"),
    ("4.01", "changes in registrant s certifying accountant"),
    ("4.02", "non reliance on previously issued financial statements"),
    ("5.01", "changes in control of registrant"),
    ("5.02", "departure of directors or"),
    ("5.03", "amendments to articles of incorporation or bylaws"),
    ("5.04", "temporary suspension of trading under registrant s employee benefit plans"),
    ("5.05", "amendments to the registrant s code of ethics"),
    ("5.06", "change in shell company status"),
    ("5.07", "submission of matters to a vote of security holders"),
    ("5.08", "shareholder director nominations"),
    ("7.01", "regulation fd disclosure"),
    ("8.01", "other events"),
    ("9.01", "financial statements and exhibits"),
]
_ITEM_NUM_RE = re.compile(r"\b([1-9]\.\d{2})\b")


def item_number(value: str) -> Optional[str]:
    """'Entry into a Material Definitive Agreement' / 'Item 1.01' / '1.01' -> '1.01'."""
    m = _ITEM_NUM_RE.search(value or "")
    if m:
        return m.group(1)
    v = _norm_text(value)
    if not v:
        return None
    for num, title in ITEM_TITLES:
        if v.startswith(title) or (len(v) >= 10 and title.startswith(v)):
            return num
    return None


def parse_header(text: str) -> Dict[str, Any]:
    """Parse an EDGAR -index-headers.html (or the .txt submission's header). JSON-safe:
    {"form", "accepted" ("YYYY-MM-DDTHH:MM:SS", America/New_York), "items", "items_unknown",
     "exhibits" [[type, description]], "ok"}."""
    t = html.unescape(text or "")
    out: Dict[str, Any] = {"form": None, "accepted": None, "items": [], "items_unknown": [], "exhibits": [],
                           "parser": PARSER_VERSION}
    m = re.search(r"<ACCEPTANCE-DATETIME>\s*(\d{14})", t)
    if m:
        s = m.group(1)
        out["accepted"] = f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:{s[12:14]}"
    m = re.search(r"CONFORMED SUBMISSION TYPE:\s*([^\s<]+)", t)
    if m:
        out["form"] = m.group(1).strip().upper()
    head = t.split("</SEC-HEADER>", 1)[0]
    raw_items = [x.strip() for x in re.findall(r"ITEM INFORMATION:\s*([^\n<]+)", head)]
    raw_items += [x.strip() for x in re.findall(r"<ITEMS>\s*([^\n<]+)", head)]
    items: List[str] = []
    for v in raw_items:
        n = item_number(v)
        if n:
            if n not in items:
                items.append(n)
        elif v and v not in out["items_unknown"]:
            out["items_unknown"].append(v)
    out["items"] = items
    for block in t.split("<DOCUMENT>")[1:]:
        mt = re.search(r"<TYPE>\s*([^\s<]+)", block)
        if not mt:
            continue
        md = re.search(r"<DESCRIPTION>\s*([^\n<]*)", block)
        out["exhibits"].append([mt.group(1).strip().upper(), (md.group(1).strip() if md else "")])
    if out["form"] is None and out["exhibits"]:
        out["form"] = out["exhibits"][0][0]
    out["ok"] = bool(out["accepted"] and out["form"])
    return out


# ---- independent cross-check of parse_header (operator decision 2026-10-03) ------------------
# parse_header was written from EDGAR's documented format without ever seeing a live SEC answer
# (the build sandbox cannot reach sec.gov). Every header the run fetches is ALSO parsed by EdgarTools
# (MIT, widely used) from the same text -- no extra SEC request -- and the two must agree on form,
# acceptance time and item numbers. Too much disagreement means the replay's 8-K selection cannot be
# trusted, and both windows' gates are forced to FAIL (crosscheck_verdict).
CROSSCHECK_MAX_DISAGREE = 0.01        # above 1% of checked headers disagreeing: untrusted
CROSSCHECK_MIN_CHECKED = 50           # fewer checked than this: too few to judge (reported, not forced)
CROSSCHECK_EXAMPLES = 25


def crosscheck_header(text: str, ours: Dict[str, Any]) -> Dict[str, Any]:
    """{"status": "agree" | "disagree" | "error" | "unavailable", "diffs": [...]} for one header."""
    try:
        from edgar.sgml import FilingHeader
    except Exception:  # noqa: BLE001 - the checker is optional where it is not installed
        return {"status": "unavailable"}
    try:
        t = html.unescape(text or "")
        a, b = t.find("<SEC-HEADER>"), t.find("</SEC-HEADER>")
        if a < 0:
            return {"status": "error", "diffs": ["no <SEC-HEADER> block"]}
        hdr = FilingHeader.parse_from_sgml_text(t[a:(b + len("</SEC-HEADER>")) if b > a else len(t)])
        theirs_form = str(hdr.form or "").strip().upper() or None
        acc = hdr.acceptance_datetime
        theirs_acc = acc.strftime("%Y-%m-%dT%H:%M:%S") if hasattr(acc, "strftime") else None
        raw = str(hdr.filing_metadata.get("ITEM INFORMATION") or "") if hdr.filing_metadata else ""
        theirs_items = sorted({n for n in (item_number(x) for x in raw.split(", ")) if n})
    except Exception as exc:  # noqa: BLE001 - a checker crash is counted, never fatal
        return {"status": "error", "diffs": [f"edgartools: {type(exc).__name__}: {str(exc)[:80]}"]}
    diffs = []
    if theirs_form != ours.get("form"):
        diffs.append(f"form ours={ours.get('form')} edgartools={theirs_form}")
    if theirs_acc != ours.get("accepted"):
        diffs.append(f"accepted ours={ours.get('accepted')} edgartools={theirs_acc}")
    if theirs_items != sorted(ours.get("items") or []):
        diffs.append(f"items ours={sorted(ours.get('items') or [])} edgartools={theirs_items}")
    return {"status": "disagree" if diffs else "agree", "diffs": diffs}


def crosscheck_verdict(cc: Dict[str, Any]) -> Dict[str, Any]:
    """Summary of the run's cross-check counters, and whether the 8-K selection can be trusted."""
    checked = int(cc.get("agree") or 0) + int(cc.get("disagree") or 0)
    rate = (int(cc.get("disagree") or 0) / checked) if checked else None
    if int(cc.get("unavailable") or 0) and not checked:
        trusted, why = None, "EdgarTools not installed: parser not cross-checked"
    elif checked < CROSSCHECK_MIN_CHECKED:
        trusted, why = None, f"only {checked} headers cross-checked (< {CROSSCHECK_MIN_CHECKED}): too few to judge"
    else:
        trusted = rate <= CROSSCHECK_MAX_DISAGREE
        why = (f"{rate:.1%} of {checked} headers disagree with EdgarTools"
               + ("" if trusted else f" (> {CROSSCHECK_MAX_DISAGREE:.0%}): 8-K selection untrusted"))
    return {**{k: int(cc.get(k) or 0) for k in ("agree", "disagree", "error", "unavailable")},
            "checked": checked, "disagree_rate": rate, "trusted": trusted, "note": why,
            "examples": list(cc.get("examples") or [])[:CROSSCHECK_EXAMPLES]}


EXCLUDED_ITEMS = ("3.02", "2.03", "1.03", "3.01")
DILUTION_EXHIBIT_RE = re.compile(r"underwrit|securities\s+purchase|registered\s+direct|at[\s-]*the[\s-]*market|"
                                 r"placement\s+agen|registration\s+rights", re.I)


def classify(h: Dict[str, Any]) -> Dict[str, Any]:
    """The registered filing rule, for one parsed header (the acceptance window is checked apart)."""
    items = set(h.get("items") or [])
    exhibits = [(t, d) for t, d in (h.get("exhibits") or []) if str(t).upper().startswith("EX-")]
    has_ex99 = any(str(t).upper().startswith("EX-99") for t, _ in exhibits)
    amendment = str(h.get("form") or "").upper() != "8-K"
    item_rule = "1.01" in items or ("8.01" in items and has_ex99)
    reasons = [f"item {i}" for i in EXCLUDED_ITEMS if i in items]
    reasons += [f"exhibit {t}: {d}"[:120] for t, d in exhibits if DILUTION_EXHIBIT_RE.search(d or "")]
    return {"amendment": amendment, "item_rule": item_rule, "dilution": reasons,
            "eligible": bool(h.get("ok")) and not amendment and item_rule and not reasons}


# ----------------------------------------------------------------------------- the order --
def _cents(x: float) -> float:
    return round(x + 1e-9, 2)


def levels(prev_close: float) -> Dict[str, Any]:
    trigger = _cents(prev_close * TRIGGER_MULT)
    limit = _cents(trigger * LIMIT_MULT)
    return {"prev_close": round(prev_close, 4), "trigger": trigger, "limit": limit,
            "target": _cents(trigger * TARGET_MULT), "stop": _cents(trigger * STOP_MULT),
            "shares": int(math.floor(SIZE_USD / limit)) if limit > 0 else 0}


def forecast(symbol: str, day: date, lv: Dict[str, Any], *, delay_s: int = 0) -> Forecast:
    return Forecast(
        forecast_id=forecast_id_for(SPEC.experiment_id, symbol, day.isoformat()),
        experiment_id=SPEC.experiment_id, spec_hash=SPEC.spec_hash(), symbol=symbol.upper(),
        session_date=day.isoformat(), issued_at=_at(day, 9, 25), window_start=_at(day, 9, 30) + delay_s,
        entry_expiry=_at(day, 10, 30), time_exit=_at(day, 15, 30), entry_ref=lv["prev_close"],
        entry_trigger=lv["trigger"], entry_limit=lv["limit"], target=lv["target"], stop=lv["stop"],
        shares=lv["shares"])


def simulate(f: Forecast, bars: List[tuple], *, cost_bps: float, complete: Optional[bool] = None) -> Dict[str, Any]:
    """The registered fill rule, then resolver_v2 for the fill and the exit (stricter answer wins)."""
    first = next((b for b in sorted(bars) if f.window_start <= b[0] < f.entry_expiry and b[2] >= f.entry_trigger),
                 None)
    if first is not None and max(f.entry_trigger, first[1]) > f.entry_limit:
        return {"simulated": NO_FILL, "pnl_usd": None, "note": "opened above the limit"}
    x = resolve_execution(f, bars, cost_bps_per_side=cost_bps, complete=complete)
    out = {"simulated": x.outcome, "pnl_usd": x.pnl_usd, "entry": x.entry_fill, "exit": x.exit_price,
           "ambiguous": x.ambiguous}
    if first is not None:
        want = max(f.entry_trigger, first[1])
        if x.entry_fill is None and x.outcome == NO_FILL or (x.entry_fill is not None
                                                              and abs(x.entry_fill - want) > 1e-6):
            out["resolver_stricter"] = True
    return out


def grade(symbol: str, day: date, lv: Dict[str, Any], bars: List[tuple], complete: Optional[bool]) -> Dict[str, Any]:
    out = {}
    for name, (bps, delay) in VARIANTS.items():
        out[name] = simulate(forecast(symbol, day, lv, delay_s=delay), bars, cost_bps=bps, complete=complete)
    return out


def triggered_in_window(bars: List[tuple], day: date, trigger: float) -> bool:
    return any(_at(day, 9, 30) <= b[0] < _at(day, 10, 30) and b[2] >= trigger for b in bars)


# ------------------------------------------------------------------------------- summary --
def _variant(rows: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    xs = [r[key] for r in rows if key in r]
    filled = [x for x in xs if x["simulated"] in COUNTED]
    n, wins = len(filled), sum(1 for x in filled if x["simulated"] == WIN)
    lo, hi = stats.wilson(wins, n)
    e = stats.expectancy([x["pnl_usd"] for x in filled if x["pnl_usd"] is not None])
    return {"trades": len(xs), "fills": n, "wins": wins,
            "losses": sum(1 for x in filled if x["simulated"] == LOSS),
            "time_exits": sum(1 for x in filled if x["simulated"] == TIME_EXIT),
            "no_fills": sum(1 for x in xs if x["simulated"] == NO_FILL),
            "unresolved": sum(1 for x in xs if x["simulated"] == UNRESOLVED),
            "resolver_stricter": sum(1 for x in xs if x.get("resolver_stricter")),
            "win_rate": wins / n if n else None, "wilson_ci": [lo, hi] if n else None,
            "expectancy_usd": e["mean"], "total_usd": e["total"], "profit_factor": e["profit_factor"]}


def arm_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    v = {k: _variant(rows, k) for k in VARIANTS}
    h, c25 = v[HEADLINE], v["cost_25bps"]
    be = SPEC.break_even_win_rate()
    return {**{k: h[k] for k in ("trades", "fills", "wins", "losses", "time_exits", "no_fills", "unresolved",
                                "resolver_stricter", "win_rate", "wilson_ci")},
            "expectancy_usd_10bps": h["expectancy_usd"], "expectancy_usd_25bps": c25["expectancy_usd"],
            "total_usd_10bps": h["total_usd"], "total_usd_25bps": c25["total_usd"],
            "break_even_win_rate": be,
            "vs_break_even": stats.break_even_verdict(h["wins"], h["fills"], be),
            "variants": v}


def mean_diff_ci(by_day: Dict[str, Tuple[float, int, float, int]], *, iters: int = 4000, seed: int = 7,
                 alpha: float = 0.05) -> Optional[List[float]]:
    """Session-clustered bootstrap of mean(A) - mean(B) $/trade: whole sessions resampled, the same
    draw for both arms (they traded the same market). None when too few draws hold both arms."""
    days = [v for v in by_day.values() if v[1] or v[3]]
    if not days or not sum(d[1] for d in days) or not sum(d[3] for d in days):
        return None
    rng, diffs, m = random.Random(seed), [], len(days)
    for _ in range(iters):
        sa = na = sb = nb = 0
        for _ in range(m):
            d = days[rng.randrange(m)]
            sa += d[0]; na += d[1]; sb += d[2]; nb += d[3]
        if na and nb:
            diffs.append(sa / na - sb / nb)
    if len(diffs) < iters / 2:
        return None
    diffs.sort()
    n = len(diffs)
    return [diffs[int(alpha / 2 * n)], diffs[min(n - 1, int((1 - alpha / 2) * n))]]


def gate(arm: List[Dict[str, Any]], twins: List[Dict[str, Any]]) -> Dict[str, Any]:
    a, t = arm_summary(arm), arm_summary(twins)
    reasons = []
    if not a["fills"]:
        reasons.append("no filled trades")
    elif a["wilson_ci"][0] < GATE_WILSON_LOW:
        reasons.append(f"Wilson lower bound {a['wilson_ci'][0]:.1%} < {GATE_WILSON_LOW:.0%}")
    e25 = a["expectancy_usd_25bps"]
    if e25 is None or e25 <= 0:
        reasons.append("expectancy after 25 bps a side is not above $0"
                       + (f" (${e25:.2f}/trade)" if e25 is not None else ""))
    ea, et = a["expectancy_usd_10bps"], t["expectancy_usd_10bps"]
    if et is None:
        reasons.append("no twin fills to compare against")
    elif ea is None or ea <= et:
        reasons.append("does not beat the no-8-K twins"
                       + (f" (${ea:.2f} vs ${et:.2f}/trade)" if ea is not None else ""))
    by_day: Dict[str, List[float]] = {}
    for rows, side in ((arm, 0), (twins, 2)):
        for r in rows:
            x = r[HEADLINE]
            if x["simulated"] in COUNTED and x["pnl_usd"] is not None:
                d = by_day.setdefault(r["day"], [0.0, 0, 0.0, 0])
                d[side] += x["pnl_usd"]; d[side + 1] += 1
    wr = stats.diff_ci(a["wins"], a["fills"], t["wins"], t["fills"])
    return {"verdict": "FAIL" if reasons else "PASS", "reasons": reasons,
            "rule": "PASS only if Wilson lower >= 30% AND expectancy after 25 bps > $0 AND expectancy "
                    "(10 bps) above the no-8-K twins'",
            "expectancy_diff_usd_10bps": {
                "diff": (ea - et) if ea is not None and et is not None else None,
                "ci95_session_clustered": mean_diff_ci({k: tuple(v) for k, v in by_day.items()})},
            "win_rate_diff": {"diff": wr[0], "ci95_newcombe": [wr[1], wr[2]]} if wr else None,
            "sample_note": (f"only {a['fills']} filled trades: below {stats.MIN_FILLED}, no interval is evidence"
                            if a["fills"] < stats.MIN_FILLED else "")}


# ------------------------------------------------------------------------------ the run --
def _status_of(exc: BaseException) -> Optional[int]:
    return getattr(getattr(exc, "response", None), "status_code", None)


def _norm_ticker(t: Any) -> str:
    return str(t or "").strip().upper().replace("-", ".")


def _cik(x: Any) -> int:
    try:
        return int(str(x).strip() or 0)
    except (TypeError, ValueError):
        return 0


def _env_start() -> date:
    raw = (os.getenv(START_ENV) or "").strip()
    try:
        return date.fromisoformat(raw) if raw else DEFAULT_START
    except ValueError:
        return DEFAULT_START


def cs_snapshot(get, as_of: date, *, sleep=None, pause=None, max_pages: int = 40) -> Dict[str, int]:
    """{ticker: cik or 0} for Polygon reference type=CS stocks active on `as_of`."""
    url = PG._base_url() + "/v3/reference/tickers"
    params: Dict[str, Any] = {"market": "stocks", "type": "CS", "date": as_of.isoformat(), "active": "true",
                              "limit": 1000, "apiKey": PG._key()}
    out: Dict[str, int] = {}
    for page in range(max_pages):
        if page and pause:
            pause()
        r = PG._get_patiently(get, url, params, sleep=sleep)
        r.raise_for_status()
        p = r.json() or {}
        for row in p.get("results") or []:
            t = _norm_ticker(row.get("ticker"))
            if t:
                out[t] = _cik(row.get("cik"))
        nxt = p.get("next_url")
        if not nxt:
            return out
        url, params = nxt, {"apiKey": PG._key()}
    raise RuntimeError(f"polygon reference tickers: page cap ({max_pages}) hit; the CS list is incomplete")


class _Run:
    def __init__(self, get, store, *, start: date, end: date, pace: float, sec_interval: float, budget_s: float,
                 sleep, clock) -> None:
        self.get, self.store, self.start, self.end = get, store, start, end
        self.pace, self.sec_interval, self.sleep, self.clock = pace, sec_interval, sleep, clock
        self.deadline = clock() + budget_s
        self.S = sessions(start, end)
        self.first = ADV_DAYS                       # index of the first decision session in self.S
        self.pos = {d: i for i, d in enumerate(self.S)}
        self.prog = store.get(T_META, VERSION + ":progress") or {}
        if self.prog.get("start") != start.isoformat() or self.prog.get("end") != end.isoformat():
            calls = self.prog.get("calls") or {}
            self.prog = {"start": start.isoformat(), "end": end.isoformat(), "calls": calls}
        self.prog.setdefault("calls", {})
        for k in ("polygon", "sec", "alpaca"):
            self.prog["calls"].setdefault(k, 0)
        self.meta: Dict[str, Any] = store.get(T_META, VERSION) or {}
        self._sec_last: Optional[float] = None
        self._poly_n = 0
        self._map_cache: Dict[Tuple[str, str], Any] = {}
        self.index_dates = _weekdays(self.S[self.first - 1], end)
        self.ix = int(self.prog.get("index_next") or 0)
        self._listings: Dict[str, Optional[Set[str]]] = {}

    # ---- plumbing
    def check_time(self) -> None:
        if self.clock() >= self.deadline:
            raise _OutOfTime()

    def save(self) -> None:
        self.prog["index_next"] = self.ix
        self.prog["updated_at"] = int(time.time())
        self.store.put(T_META, VERSION + ":progress", self.prog)

    def sec(self, url: str) -> Tuple[int, str]:
        err = ""
        for attempt in range(len(SEC_BACKOFF_S) + 1):
            if self._sec_last is not None:
                wait = self._sec_last + self.sec_interval - self.clock()
                if wait > 0:
                    self.sleep(wait)
            self._sec_last = self.clock()
            self.prog["calls"]["sec"] += 1
            try:
                r = self.get(url, headers={"User-Agent": _sec_user_agent(), "Accept-Encoding": "gzip, deflate"},
                             timeout=30)
                code = getattr(r, "status_code", None)
            except Exception as exc:  # noqa: BLE001
                r, code, err = None, None, type(exc).__name__
            if code == 200:
                return 200, r.text
            if code == 404:
                return 404, ""
            if code is not None:
                err = f"HTTP {code}" + (" (the SEC answers 403 for a file it never published, or for a "
                                        "request without a descriptive User-Agent: EDGAR_USER_AGENT)"
                                        if code == 403 else "")
            if attempt < len(SEC_BACKOFF_S):
                self.sleep(SEC_BACKOFF_S[attempt])
        raise _Transient(f"sec {url.rsplit('/', 1)[-1]}: {err}")

    def poly(self, fn):
        """One paced Polygon call; the pacing gap is spent fetching EDGAR daily indexes."""
        if self._poly_n:
            self.idle(self.pace)
        self._poly_n += 1
        self.prog["calls"]["polygon"] += 1
        return fn()

    def page_pause(self) -> None:
        """Between pages of one paged Polygon answer: each page is a call against the key's budget."""
        self.idle(self.pace)
        self.prog["calls"]["polygon"] += 1

    def idle(self, seconds: float) -> None:
        until = self.clock() + seconds
        while self.clock() < until - 1.0 and self.clock() < self.deadline and self.index_step():
            pass
        rest = until - self.clock()
        if rest > 0:
            self.sleep(rest)

    # ---- phase 0: meta
    def phase_meta(self) -> None:
        m, changed = self.meta, False
        lo, hi = self.S[0].isoformat(), self.end.isoformat()
        rng = m.get("splits_range") or ["9999", "0000"]
        if not (rng[0] <= lo and rng[1] >= hi):
            self.check_time()
            try:
                rows = self.poly(lambda: PG.splits(self.get, self.S[0], self.end, sleep=self.sleep))
            except Exception as exc:  # noqa: BLE001 - no split list, no run: nothing would be on one basis
                raise _Transient(f"split list unavailable: {redact_exc(exc, 160)}")
            m["splits"] = [[s["ticker"], s["execution_date"].isoformat(), s["split_to"] / s["split_from"]]
                           for s in rows]
            m["splits_range"] = [lo, hi]
            changed = True
        if not m.get("sec_tickers"):
            self.check_time()
            code, text = self.sec(SEC_TICKERS_URL)
            if code != 200:
                raise _Transient(f"company_tickers.json unavailable (HTTP {code})")
            import json
            by: Dict[str, List[str]] = {}
            for v in (json.loads(text) or {}).values():
                t, c = _norm_ticker(v.get("ticker")), _cik(v.get("cik_str"))
                if t and c:
                    by.setdefault(str(c), []).append(t)
            m["sec_tickers"] = by
            changed = True
        if changed:
            self.store.put(T_META, VERSION, m)
        snaps = m.setdefault("snapshots", {})
        bad = m.setdefault("snapshot_unavailable", {})
        for d in self.snapshot_dates():
            k = d.isoformat()
            if k in snaps or k in bad:
                continue
            self.check_time()
            try:
                snaps[k] = self.poly(lambda: cs_snapshot(self.get, d, sleep=self.sleep, pause=self.page_pause))
            except Exception as exc:  # noqa: BLE001
                if _status_of(exc) in (401, 403):
                    bad[k] = redact_exc(exc, 120)
                else:
                    raise _Transient(f"polygon CS list {k}: {redact_exc(exc, 120)}")
            self.store.put(T_META, VERSION, m)
        if not snaps:
            raise _Transient("no Polygon CS snapshot is available")
        self.splits: Dict[str, List[Tuple[date, float]]] = {}
        for t, ex, mult in m["splits"]:
            self.splits.setdefault(_norm_ticker(t), []).append((date.fromisoformat(ex), float(mult)))
        self.sec_tickers = {int(c): ts for c, ts in m["sec_tickers"].items()}
        self.snap_days = sorted(snaps)
        self.cs_union: Set[str] = set().union(*[set(v) for v in snaps.values()])

    def snapshot_dates(self) -> List[date]:
        out, d = [], self.S[self.first]
        while d < self.end:
            out.append(d)
            d += timedelta(days=SNAPSHOT_EVERY_DAYS)
        return out + [self.end]

    def cs_on(self, d: date) -> Dict[str, int]:
        k = d.isoformat()
        lo = max((x for x in self.snap_days if x <= k), default=None)
        hi = min((x for x in self.snap_days if x >= k), default=None)
        out: Dict[str, int] = {}
        for x in (lo, hi):
            if x:
                for t, c in self.meta["snapshots"][x].items():
                    out[t] = c or out.get(t, 0)
        return out

    def mapping(self, d: date):
        """(by_cik {cik: [CS tickers]}, source {cik: "sec"|"polygon"}, owner {ticker: cik}) on `d`."""
        k = d.isoformat()
        key = (max((x for x in self.snap_days if x <= k), default=""),
               min((x for x in self.snap_days if x >= k), default=""))
        if key in self._map_cache:
            return self._map_cache[key]
        cs = self.cs_on(d)
        by_cik: Dict[int, List[str]] = {}
        source: Dict[int, str] = {}
        for c, ts in self.sec_tickers.items():
            hit = sorted({t for t in ts if t in cs})
            if hit:
                by_cik[c], source[c] = hit, "sec"
        poly: Dict[int, Set[str]] = {}
        for t, c in cs.items():
            if c and c not in by_cik:
                poly.setdefault(c, set()).add(t)
        for c, ts in poly.items():
            by_cik[c], source[c] = sorted(ts), "polygon"
        claims: Dict[str, List[int]] = {}
        for c, ts in by_cik.items():
            for t in ts:
                claims.setdefault(t, []).append(c)
        owner = {t: cs_[0] for t, cs_ in claims.items() if len(cs_) == 1 and len(by_cik[cs_[0]]) == 1}
        self._map_cache = {key: (by_cik, source, owner)}
        return by_cik, source, owner

    # ---- phase 1: Polygon daily + plans (EDGAR indexes in the gaps)
    def phase_daily(self) -> None:
        if self.prog.get("daily_done"):
            return
        i0 = int(self.prog.get("daily_next") or 0)
        window: List[Dict[str, Any]] = []
        for j in range(max(0, i0 - ADV_DAYS), i0):
            window.append(self.store.get(T_DAILY, self.S[j].isoformat()) or {"status": "missing"})
        for i in range(i0, len(self.S)):
            d = self.S[i]
            row = self.store.get(T_DAILY, d.isoformat())
            if row is None:
                self.check_time()
                row = self.fetch_daily(d)
                if i >= self.first:
                    self.store.put(T_PLAN, d.isoformat(), self.plan(i, window[-ADV_DAYS:], row))
                self.store.put(T_DAILY, d.isoformat(), row)
            elif i >= self.first and self.store.get(T_PLAN, d.isoformat()) is None:
                self.store.put(T_PLAN, d.isoformat(), self.plan(i, window[-ADV_DAYS:], row))
            if row.get("status") == "ok":
                first = self.prog.get("daily_first_ok")
                self.prog["daily_first_ok"] = min(first, d.isoformat()) if first else d.isoformat()
            else:
                bad = self.prog.setdefault("daily_unavailable", {})
                bad[d.isoformat()] = row.get("why") or row.get("status")
            window = (window + [row])[-ADV_DAYS:]
            self.prog["daily_next"] = i + 1
            if i % 10 == 0:
                self.save()
        self.prog["daily_done"] = True
        self.save()

    def fetch_daily(self, d: date) -> Dict[str, Any]:
        try:
            rows = self.poly(lambda: PG.grouped_daily(self.get, d, adjusted=False, sleep=self.sleep))
        except Exception as exc:  # noqa: BLE001
            if _status_of(exc) in (401, 403):
                return {"day": d.isoformat(), "status": "unavailable", "why": redact_exc(exc, 160)}
            raise _Transient(f"polygon grouped daily {d}: {redact_exc(exc, 120)}")
        if not rows:
            return {"day": d.isoformat(), "status": "unavailable", "why": "empty answer"}
        bars = {}
        for r in rows:
            t = _norm_ticker(r.get("T"))
            if t not in self.cs_union or not r.get("c"):
                continue
            v, c = float(r.get("v") or 0), float(r["c"])
            bars[t] = [c, float(r.get("h") or c), round(v * float(r.get("vw") or c))]
        return {"day": d.isoformat(), "status": "ok", "bars": bars}

    def plan(self, i: int, prior: List[Dict[str, Any]], today: Dict[str, Any]) -> Dict[str, Any]:
        d, prev = self.S[i], self.S[i - 1]
        base = {"day": d.isoformat(), "prev_day": prev.isoformat()}
        if len(prior) < ADV_DAYS or any(r.get("status") != "ok" for r in prior) or today.get("status") != "ok":
            return {**base, "status": "no_data",
                    "why": "Polygon daily missing for this session or one of the 20 before it"}
        cs = self.cs_on(d)
        lo_split = self.S[i - SPLIT_SESSIONS]
        passing, rs_excluded = {}, []
        for t, (c, _h, _dv) in prior[-1]["bars"].items():
            if t not in cs:
                continue
            f = share_factor(self.splits, t, prev, d)
            pc = c / f
            if not (PRICE_MIN <= pc <= PRICE_MAX):
                continue
            dvs = [r["bars"].get(t) for r in prior]
            if any(x is None for x in dvs):
                continue
            adv = sum(x[2] for x in dvs) / len(dvs)
            if adv < MIN_ADV:
                continue
            if any(lo_split <= ex <= d and mult < 1.0 for ex, mult in self.splits.get(t, [])):
                rs_excluded.append(t)
                continue
            hd = today["bars"].get(t)
            passing[t] = [round(pc, 4), round(adv), hd[1] if hd else None, f]
        return {**base, "status": "ok", "passing": passing, "rs_excluded": sorted(rs_excluded)}

    # ---- phase 2: EDGAR daily indexes
    def sec_closed(self, d: date) -> bool:
        """True when EDGAR published no index for weekday `d`: the SEC was closed (a federal holiday the
        market trades through, e.g. Columbus Day or Veterans Day, or a market holiday). EDGAR answers 403,
        not 404, for that missing file, which read as a transient refusal and stalled the run on
        2024-10-14. Decided from the quarter's directory listing, and only for a day earlier than the
        listing's latest file, so a listing fetched before the quarter ended never hides a later day.
        When the listing cannot be read, nothing is skipped: the index itself is fetched as before."""
        url = daily_index_listing_url(d)
        if url not in self._listings:
            try:
                code, text = self.sec(url)
            except _Transient:
                code, text = None, ""
            self._listings[url] = parse_listing(text) if code == 200 else None
        names = self._listings[url]
        name = f"form.{d:%Y%m%d}.idx"
        return bool(names) and name not in names and name < max(names)

    def index_step(self) -> bool:
        while self.ix < len(self.index_dates):
            d = self.index_dates[self.ix]
            if self.store.get(T_INDEX, d.isoformat()) is None:
                self.check_time()
                if self.sec_closed(d):
                    self.store.put(T_INDEX, d.isoformat(), {"date": d.isoformat(), "status": "not_published",
                                                           "rows": [], "parser": PARSER_VERSION})
                    self.prog["sec_closed_days"] = sorted(set(self.prog.get("sec_closed_days") or [])
                                                          | {d.isoformat()})
                    self.ix += 1
                    return True
                code, text = self.sec(daily_index_url(d))
                self.store.put(T_INDEX, d.isoformat(), {"date": d.isoformat(), "status": code,
                                                       "rows": parse_daily_index(text) if code == 200 else [],
                                                       "parser": PARSER_VERSION})
                self.ix += 1
                if self.ix % 20 == 0:
                    self.save()
                return True
            self.ix += 1
        return False

    def phase_index(self) -> None:
        while self.index_step():
            pass
        self.save()

    def index_rows(self, lo: date, hi: date) -> List[Tuple[date, List[Any]]]:
        out = []
        for d in _weekdays(lo, hi):
            row = self.store.get(T_INDEX, d.isoformat()) or {}
            out.extend((d, r) for r in row.get("rows") or [])
        return out

    def candidates(self, i: int, plan: Dict[str, Any]):
        """Index rows for session S[i] split into (to-fetch [(form, cik, acc, ticker)], unmapped, multi,
        not passing)."""
        d, prev = self.S[i], self.S[i - 1]
        by_cik, source, owner = self.mapping(d)
        passing = plan.get("passing") or {}
        cands, unmapped, multi, not_passing = [], set(), set(), 0
        for _f, (form, cik, acc) in self.index_rows(prev, d):
            ts = by_cik.get(int(cik))
            if not ts:
                if form == "8-K":
                    unmapped.add(int(cik))
                continue
            if len(ts) > 1 or owner.get(ts[0]) != int(cik):
                if form == "8-K":
                    multi.add(int(cik))
                continue
            if ts[0] not in passing:
                not_passing += 1
                continue
            cands.append((form, int(cik), acc, ts[0], source.get(int(cik))))
        return cands, unmapped, multi, not_passing

    # ---- phase 3: count the headers the run needs
    def phase_count(self) -> None:
        if self.prog.get("headers_needed") is not None:
            return
        need: Set[str] = set()
        for i in range(self.first, len(self.S)):
            plan = self.store.get(T_PLAN, self.S[i].isoformat()) or {}
            if plan.get("status") != "ok" or early_close(self.S[i]):
                continue
            need.update(acc for _f, _c, acc, _t, _s in self.candidates(i, plan)[0])
        self.prog["headers_needed"] = len(need)
        self.prog["headers_eta_min"] = round(len(need) * max(self.sec_interval, 0.35) / 60, 1)
        LOG.warning("EDGE_BACKTEST_EDGAR8K headers needed: %d (~%.0f min at the SEC pace)",
                    len(need), self.prog["headers_eta_min"])
        self.save()

    def header(self, cik: int, acc: str) -> Dict[str, Any]:
        h = self.store.get(T_HEADER, acc)
        if h is not None and h.get("parser") == PARSER_VERSION:
            return h
        self.check_time()
        code, text = self.sec(header_url(cik, acc))
        if code == 404:
            code, text = self.sec(submission_url(cik, acc))
        h = parse_header(text) if code == 200 else {"ok": False, "status": code, "parser": PARSER_VERSION}
        if code == 200:
            cc = crosscheck_header(text, h)
            h["crosscheck"] = cc["status"]
            tally = self.prog.setdefault("crosscheck", {})
            tally[cc["status"]] = int(tally.get(cc["status"]) or 0) + 1
            if cc["status"] in ("disagree", "error") and len(tally.setdefault("examples", [])) < CROSSCHECK_EXAMPLES:
                tally["examples"].append({"accession": acc, "diffs": cc.get("diffs", [])})
        h["accession"] = acc
        self.store.put(T_HEADER, acc, h)
        self.prog["headers_fetched"] = int(self.prog.get("headers_fetched") or 0) + 1
        return h

    # ---- phase 4: one session at a time
    def phase_decide(self) -> None:
        for i in range(max(self.first, int(self.prog.get("decide_next") or 0)), len(self.S)):
            d = self.S[i]
            if self.store.get(T_DAY, f"{VERSION}:{d.isoformat()}") is None:
                self.check_time()
                self.store.put(T_DAY, f"{VERSION}:{d.isoformat()}", self.decide(i))
            self.prog["decide_next"] = i + 1
            self.save()

    def minute(self, syms: List[str], d: date):
        self.minute_called = True
        try:
            got, complete = A.bars_pages(self.get, syms, timeframe="1Min", start=_iso(_at(d, 9, 30)),
                                         end=_iso(_at(d, 16, 0)), adjustment=MINUTE_ADJUSTMENT)
        except Exception as exc:  # noqa: BLE001
            if _status_of(exc) in (401, 403):
                raise _MinuteUnavailable(redact_exc(exc, 160))
            raise _Transient(f"alpaca minute bars {d}: {redact_exc(exc, 120)}")
        finally:
            self.prog["calls"]["alpaca"] += 1
        bars = {s: _bars(got.get(s) or []) for s in syms}
        if syms and not any(bars.values()):
            raise _MinuteUnavailable("empty answer for every symbol")
        return bars, complete

    def decide(self, i: int) -> Dict[str, Any]:
        d, prev = self.S[i], self.S[i - 1]
        self.minute_called = False
        out: Dict[str, Any] = {"day": d.isoformat(), "prev_day": prev.isoformat(), "picks": [], "twins": []}
        plan = self.store.get(T_PLAN, d.isoformat()) or {"status": "no_data", "why": "no plan"}
        if plan.get("status") != "ok":
            return {**out, "status": "no_data", "why": plan.get("why")}
        if early_close(d):
            return {**out, "status": "early_close",
                    "why": "13:00 close: the 15:30 time exit is after the close"}
        passing = plan["passing"]
        cands, unmapped, multi, not_passing = self.candidates(i, plan)
        # index 8-Ks counted once per filing date: the dates after the previous session, through D
        own = [r for f, r in self.index_rows(prev + timedelta(days=1), d)]
        counts = {"index_8k": len({r[2] for r in own if r[0] == "8-K"}),
                  "index_8ka": len({r[2] for r in own if r[0] == "8-K/A"}),
                  "passing_tickers": len(passing), "reverse_split_excluded": len(plan.get("rs_excluded") or []),
                  "not_passing_rows": not_passing, "headers_needed": len({c[2] for c in cands}),
                  "in_window_8k": 0, "in_window_8ka": 0, "header_unavailable": 0, "item_rule_met": 0,
                  "excluded_dilution": 0, "dilution_flagged": 0, "eligible_filings": 0}
        touched: Set[str] = set()          # tickers whose CIK had an 8-K / 8-K/A in the window (or unknown)
        eligible: Dict[str, List[Dict[str, Any]]] = {}
        seen: Set[Tuple[str, str]] = set()
        for form, cik, acc, t, src in cands:
            if (acc, t) in seen:
                continue
            seen.add((acc, t))
            h = self.header(cik, acc)
            if not h.get("ok"):
                counts["header_unavailable"] += 1
                touched.add(t)
                continue
            if not in_window(h.get("accepted"), d, prev):
                continue
            touched.add(t)
            c = classify(h)
            if c["amendment"] or form != "8-K":
                counts["in_window_8ka"] += 1
                continue
            counts["in_window_8k"] += 1
            counts["dilution_flagged"] += bool(c["dilution"])
            if c["item_rule"]:
                counts["item_rule_met"] += 1
                if c["dilution"]:
                    counts["excluded_dilution"] += 1
            if c["eligible"]:
                counts["eligible_filings"] += 1
                eligible.setdefault(t, []).append({"accession": acc, "cik": cik, "accepted": h["accepted"],
                                                   "items": h["items"], "mapping": src})
        ranked = sorted(eligible, key=lambda t: (-passing[t][1], t))
        picks, over = ranked[:MAX_PER_DAY], ranked[MAX_PER_DAY:]
        counts.update({"eligible_tickers": len(ranked), "picked": len(picks), "over_cap": len(over)})
        _by_cik, source, owner = self.mapping(d)
        pool = []
        for t, (pc, adv, hi, _f) in passing.items():
            if t in touched or t not in owner or hi is None:
                continue
            if hi >= levels(pc)["trigger"]:
                pool.append((adv, t))
        pool = [t for _adv, t in sorted(pool, key=lambda x: (-x[0], x[1]))]
        counts["twin_prescreen"] = len(pool)
        try:
            if picks:
                bars, complete = self.minute(picks, d)
                for t in picks:
                    out["picks"].append(self.trade(t, "edgar_8k", d, passing[t], bars[t], complete,
                                                   filings=eligible[t]))
            twins: List[Dict[str, Any]] = []
            k = 0
            while len(twins) < TWIN_CAP and k < len(pool):
                chunk, k = pool[k:k + TWIN_CHUNK], k + TWIN_CHUNK
                bars, complete = self.minute(chunk, d)
                for t in chunk:
                    lv = levels(passing[t][0])
                    if len(twins) < TWIN_CAP and triggered_in_window(bars[t], d, lv["trigger"]):
                        twins.append(self.trade(t, "no_8k_twin", d, passing[t], bars[t], complete,
                                                mapping=source.get(owner[t])))
            out["twins"] = twins
            counts["twins_cap_hit"] = len(twins) >= TWIN_CAP
        except _MinuteUnavailable as exc:
            return {**out, "picks": [], "twins": [], "status": "minute_unavailable", "why": str(exc),
                    "counts": counts, "unmapped_ciks": sorted(unmapped), "multi_ciks": sorted(multi)}
        return {**out, "status": "ok", "minute_data": self.minute_called, "counts": counts, "over_cap": over,
                "unmapped_ciks": sorted(unmapped), "multi_ciks": sorted(multi)}

    def trade(self, t: str, arm: str, d: date, p: List[Any], bars: List[tuple], complete, **extra) -> Dict[str, Any]:
        pc, adv, _hi, f = p
        lv = levels(pc)
        row = {"day": d.isoformat(), "symbol": t, "arm": arm, "avg_dollars_20d": adv, **lv,
               **{k: v for k, v in extra.items() if v is not None}, **grade(t, d, lv, bars, complete)}
        if f != 1.0:
            row["split_restated"] = {"raw_prev_close": round(pc * f, 4), "share_factor": f}
        return row

    # ---- finalize
    def finalize(self) -> Dict[str, Any]:
        days = []
        for d in self.S[self.first:]:
            r = self.store.get(T_DAY, f"{VERSION}:{d.isoformat()}")
            if r is None:
                raise _Transient(f"session {d} has no stored result yet")
            days.append(r)
        windows = {"preregistered": (max(PREREG_START, self.start), min(PREREG_END, self.end)),
                   "full": (self.start, self.end)}
        out_w = {}
        for name, (lo, hi) in windows.items():
            sel = [r for r in days if lo.isoformat() <= r["day"] <= hi.isoformat()]
            out_w[name] = self.window_report(sel, lo, hi)
        decided = [r["day"] for r in days if r["status"] == "ok"]
        minute_ok = [r["day"] for r in days if r["status"] == "ok" and r.get("minute_data")]
        avail = {"polygon_daily_first_session": self.prog.get("daily_first_ok"),
                 "polygon_daily_unavailable_sessions": len(self.prog.get("daily_unavailable") or {}),
                 "polygon_daily_unavailable_sample": dict(sorted((self.prog.get("daily_unavailable") or {})
                                                                 .items())[:5]),
                 "alpaca_minute_first_session": min(minute_ok) if minute_ok else None,
                 "alpaca_minute_unavailable_sessions": sum(1 for r in days if r["status"] == "minute_unavailable"),
                 "first_decided_session": min(decided) if decided else None,
                 "no_data_sessions": sum(1 for r in days if r["status"] == "no_data"),
                 "early_close_sessions": sum(1 for r in days if r["status"] == "early_close"),
                 "cs_snapshots": self.snap_days,
                 "cs_snapshot_unavailable": sorted(self.meta.get("snapshot_unavailable") or {})}
        xcheck = crosscheck_verdict(self.prog.get("crosscheck") or {})
        if xcheck["trusted"] is False:
            for v in out_w.values():
                v["gate"]["verdict"] = "FAIL"
                v["gate"]["reasons"].append(f"parser cross-check failed: {xcheck['note']}")
        prereg = [t for r in days if PREREG_START.isoformat() <= r["day"] <= PREREG_END.isoformat()
                  for t in r.get("picks", []) + r.get("twins", [])]
        res = {"version": VERSION, "hypothesis": SPEC.experiment_id, "spec_hash": SPEC.spec_hash(),
               "eligibility": SPEC.eligibility, "resolver_version": RESOLVER_VERSION,
               "price_basis": PRICE_BASIS, "price_basis_detail": PRICE_BASIS_DETAIL, "limits": LIMITS,
               "break_even": SPEC.break_even_win_rate(), "twin_cap": TWIN_CAP, "variants": list(VARIANTS),
               "window": [self.start.isoformat(), self.end.isoformat()], "windows": out_w,
               "data_availability": avail, "calls": self.prog.get("calls"),
               "headers_needed": self.prog.get("headers_needed"),
               "parser_crosscheck": xcheck,
               "completed_at": int(time.time()),
               "trades_prereg": prereg[:3000],
               "picks_full": [t for r in days for t in r.get("picks", [])][:3000]}
        self.store.put(TABLE, VERSION, res)
        return {"status": "complete", "version": VERSION, "window": res["window"],
                "windows": {k: {"gate": v["gate"]["verdict"], "reasons": v["gate"]["reasons"],
                                "edgar_8k": {x: v["arms"]["edgar_8k"][x] for x in
                                             ("fills", "wins", "win_rate", "wilson_ci", "expectancy_usd_10bps",
                                              "expectancy_usd_25bps")},
                                "twins_fills": v["arms"]["no_8k_twin"]["fills"]} for k, v in out_w.items()},
                "data_availability": avail, "calls": res["calls"],
                "parser_crosscheck": {k: xcheck[k] for k in ("checked", "disagree", "trusted", "note")}}

    @staticmethod
    def window_report(days: List[Dict[str, Any]], lo: date, hi: date) -> Dict[str, Any]:
        picks = [t for r in days for t in r.get("picks", [])]
        twins = [t for r in days for t in r.get("twins", [])]
        keys = ("index_8k", "index_8ka", "in_window_8k", "in_window_8ka", "header_unavailable", "item_rule_met",
                "excluded_dilution", "dilution_flagged", "eligible_filings", "eligible_tickers", "picked",
                "over_cap", "reverse_split_excluded", "twin_prescreen")
        counts = {k: sum(int((r.get("counts") or {}).get(k) or 0) for r in days) for k in keys}
        counts["unmapped_ciks"] = len(set().union(*[set(r.get("unmapped_ciks") or []) for r in days]))
        counts["multi_ticker_ciks"] = len(set().union(*[set(r.get("multi_ciks") or []) for r in days]))
        counts["twins_cap_hit_days"] = sum(1 for r in days if (r.get("counts") or {}).get("twins_cap_hit"))
        counts["picks_mapped_via_polygon_cik"] = sum(
            1 for t in picks if any(f.get("mapping") == "polygon" for f in t.get("filings") or []))
        return {"from": lo.isoformat(), "to": hi.isoformat(), "sessions": len(days),
                "decided_sessions": sum(1 for r in days if r.get("status") == "ok"),
                "status_counts": {s: sum(1 for r in days if r.get("status") == s)
                                  for s in sorted({r.get("status") for r in days})},
                "counts": counts,
                "arms": {"edgar_8k": arm_summary(picks), "no_8k_twin": arm_summary(twins)},
                "gate": gate(picks, twins)}

    def progress(self) -> Dict[str, Any]:
        decided = max(0, int(self.prog.get("decide_next") or self.first) - self.first)
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "sessions": len(self.S) - self.first,
                "daily_fetched_through": (self.S[int(self.prog.get("daily_next") or 1) - 1].isoformat()
                                          if self.prog.get("daily_next") else None),
                "daily_done": bool(self.prog.get("daily_done")),
                "edgar_indexes": f"{self.ix}/{len(self.index_dates)}",
                "sec_closed_days": len(self.prog.get("sec_closed_days") or []),
                "headers_needed": self.prog.get("headers_needed"),
                "headers_fetched": self.prog.get("headers_fetched", 0),
                "parser_crosscheck": crosscheck_verdict(self.prog.get("crosscheck") or {})["note"],
                "sessions_decided": decided, "calls": self.prog.get("calls")}


def run(get, store, *, end_day: date = END_DAY, start_day: Optional[date] = None, pace_s: Optional[float] = None,
        sec_interval_s: float = SEC_MIN_INTERVAL_S, budget_s: Optional[float] = None, sleep=time.sleep,
        clock=time.monotonic) -> Dict[str, Any]:
    """Advance the backtest as far as this tick's budget allows; store the report when done.

    Once per VERSION: a stored report means "already_run". Otherwise each call resumes where the last
    stopped (every fetched session, index and header is stored) and returns "in_progress" with how far
    it got, or "complete" with the report's headline."""
    if store.get(TABLE, VERSION):
        return {"status": "already_run"}
    start = start_day or _env_start()
    pace = float(os.getenv("EDGE_PROBE_POLYGON_PACE_S", "13")) if pace_s is None else pace_s
    budget = float(os.getenv("EDGE_BT_EDGAR_BUDGET_S", DEFAULT_BUDGET_S)) if budget_s is None else budget_s
    r = _Run(get, store, start=start, end=end_day, pace=pace, sec_interval=sec_interval_s, budget_s=budget,
             sleep=sleep, clock=clock)
    try:
        r.phase_meta()
        r.phase_daily()
        r.phase_index()
        r.phase_count()
        r.phase_decide()
        out = r.finalize()
    except _OutOfTime:
        r.save()
        return {"status": "in_progress", "why": "tick budget used; the next tick continues", **r.progress()}
    except _Transient as exc:
        r.save()
        return {"status": "in_progress", "why": str(exc)[:200], **r.progress()}
    except Exception as exc:  # noqa: BLE001
        r.save()
        return {"status": "error", "why": redact_exc(exc, 200), **r.progress()}
    r.save()
    return out
