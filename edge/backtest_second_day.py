"""Backtest of a PREREGISTERED hypothesis: second_day_open@v1 (registered 2026-10-03).

A history replay only: no live trading, no forward ledger. The rule below was written down
before this code ran and is implemented as written -- never loosened.

The rule, point-in-time, decided by 09:00 ET on day D from data available before D's open:
  universe   common stock only: Polygon reference type CS (warrants, units and rights are other
             types and never enter)
  momentum   prior-day return close(D-1) / close(D-2) - 1 >= +25%, raw as traded; close(D-2) is
             restated only for splits executed on D-1 (edge.backtest.share_factor)
  strength   closed in the upper half of its D-1 range: (C - L) / (H - L) >= 0.50
  liquidity  D-1 dollar volume (volume x VWAP, Polygon grouped daily) >= $10M
  price      D-1 close between $2 and $100
  splits     no REVERSE split executed in sessions D-5..D (Polygon reference splits)
  dilution   no 8-K accepted between D-1 00:00 ET and D 09:00 ET listing Item 3.02, or with an
             exhibit titled underwriting / securities purchase / registered direct /
             at-the-market / placement agency agreement (SEC EDGAR; `dilution_8k_filter`)
  rank       D-1 dollar volume, top 5 per day
Levels (frozen, $1,000 size): a limit-on-open buy at limit = close(D-1) x 1.15 (on D's basis);
shares = floor(1000 / limit); the simulated fill is the 09:30 ET first-minute bar's open when it is
<= the limit, else NO_FILL (never chased); then an OCO bracket, target = fill x 1.05 and
stop = fill x 0.97, and a time exit at 15:30 ET at market. Exits come from edge.resolver
resolve_execution (resolver_v2): one minute touching target and stop is a LOSS.

CONTROL arm, side by side: the identical rule for a prior-day return >= +10% and < +25% (top 5).
Costs: headline 10 bps a side; also 25 bps a side, and a one-minute entry delay (the 09:31 bar's
open, the same limit rule) at 10 bps.

Step-1 gate (per window): PASS only if the Wilson 95% LOWER bound of the win rate >= 30% AND the
expectancy after 25 bps a side > $0 AND it beats the control (a higher expectancy -- at 10 and at
25 bps -- and a higher win rate). A preregistered rule that could not be applied in full (the
8-K dilution filter unavailable or partial) is not the rule that was registered: FAIL, with that
reason named. Break-even for +5% / -3% is 37.5%.

Windows (scope set 2026-10-03): the replay runs from EDGE_BT2_START (default 2024-10-07) through
`end_day`, as far back as the data plans answer, and every result is reported twice -- for the
preregistered window 2026-07-09..2026-10-02 and for the whole window. Where the data stops (a
403 / plan refusal or an empty answer for an early date) is recorded, never a failure.

Runs resumably: one decision day at a time, its result stored per day, the grouped-daily answer
for each session cached in the store (compacted once no later decision needs it) and never
fetched twice. A run that hits its time budget stores its progress and the next tick continues.
"""
from __future__ import annotations

import html as _html
import math
import os
import re
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from edge import stats
from edge.backtest import MINUTE_ADJUSTMENT, PRICE_BASIS, PRICE_BASIS_DETAIL, Splits, share_factor, split_index
from edge.contracts import (COUNTED, ET, LOSS, NO_FILL, TIME_EXIT, UNRESOLVED, WIN, ExperimentSpec, Forecast,
                            forecast_id_for)
from edge.providers import alpaca as A, polygon as PG
from edge.resolver import RESOLVER_VERSION, resolve_execution
from shared.redaction import redact_exc

VERSION = "second_day_open_backtest_v1"
TABLE = "edge_backtest_second_day"                       # the finished record, one per VERSION
PROGRESS_TABLE = "edge_backtest_second_day_progress"     # where a resumed run picks up
SESSIONS_TABLE = "edge_backtest_second_day_sessions"     # one row per decision day
DAILY_TABLE = "edge_backtest_second_day_grouped"         # grouped-daily cache, one row per session
REFERENCE_TABLE = "edge_backtest_second_day_reference"   # monthly CS ticker snapshots

PREREG_START, PREREG_END = date(2026, 7, 9), date(2026, 10, 2)
START_ENV, DEFAULT_START = "EDGE_BT2_START", date(2024, 10, 7)
WINDOW_END = PREREG_END

MAIN, CONTROL = "second_day_open", "control_10_to_25"
MIN_RETURN, CONTROL_RETURN = 0.25, 0.10
MIN_RANGE_POS, MIN_DOLLARS, MIN_PRICE, MAX_PRICE = 0.50, 10_000_000.0, 2.0, 100.0
REVERSE_SPLIT_SESSIONS = 5
TOP_N, SIZE_USD = 5, 1000.0
LIMIT_MULT, TARGET_MULT, STOP_MULT = 1.15, 1.05, 0.97
HEADLINE_BPS, STRESS_BPS = 10.0, 25.0
GATE_WILSON_LOW = 0.30
NO_DATA = "NO_DATA"           # the bar the fill needs is missing: neither a fill nor a NO_FILL
MAX_ATTEMPTS = 3              # a session whose fetch keeps failing transiently is given up after this
DENIED = {401, 402, 403}      # the plan's word: this date is outside what the key may read

_SPEC_COMMON = dict(setup="second_day_open", trigger_mult=LIMIT_MULT, limit_mult=LIMIT_MULT,
                    target_mult=TARGET_MULT, stop_mult=STOP_MULT, entry_expiry_et="09:31",
                    time_exit_et="15:30", size_usd=SIZE_USD, max_per_day=TOP_N)
_ELIGIBILITY = {"type": "CS", "range_pos_min": MIN_RANGE_POS, "dollar_volume_min": MIN_DOLLARS,
                "price": [MIN_PRICE, MAX_PRICE], "no_reverse_split_sessions": REVERSE_SPLIT_SESSIONS,
                "no_8k": "Item 3.02, or exhibit titled underwriting / securities purchase / registered direct "
                         "/ at-the-market / placement agency agreement, accepted D-1 00:00..D 09:00 ET",
                "rank": "D-1 dollar volume desc", "entry": "limit-on-open at close(D-1) x 1.15; fill = 09:30 "
                "first-minute open if <= limit else NO_FILL; target fill x 1.05, stop fill x 0.97"}
SECOND_DAY_OPEN = ExperimentSpec(
    name=MAIN, version=1, description="preregistered 2026-10-03, backtest only: prior-day +25% common stock "
    "closing in the upper half of its range, limit-on-open next day", **_SPEC_COMMON,
    eligibility={**_ELIGIBILITY, "prior_day_return": [MIN_RETURN, None]})
SECOND_DAY_CONTROL = ExperimentSpec(
    name="second_day_open_control", version=1, description="control arm of second_day_open@v1: the identical "
    "rule for a prior-day return of +10% to +25%", **_SPEC_COMMON,
    eligibility={**_ELIGIBILITY, "prior_day_return": [CONTROL_RETURN, MIN_RETURN]})
SPECS = {MAIN: SECOND_DAY_OPEN, CONTROL: SECOND_DAY_CONTROL}

LIMITS = [
    "research evidence on past sessions, NOT the forward record",
    "the fill is the 09:30 first-minute bar's open (SIP), a stand-in for the opening auction print",
    "costs stated at 10 / 25 bps a side and a one-minute entry delay; thin names can cost more",
    "common stock = in Polygon's CS list on the month's first session at or before D-1, or on the next "
    "month's (a security's type is fixed at listing; the later list only classifies names listed in between)",
    "dollar volume needs Polygon's VWAP; a bar without one is not counted as liquid",
    "split list from Polygon reference splits; an unlisted split is a miss, not a pass",
    "8-K data from SEC EDGAR submissions (acceptance times) and filing indexes; acceptanceDateTime is read "
    "as Eastern time and, conservatively, also as UTC -- either reading inside the window counts",
    "prices and volume as traded (raw), restated only for splits executed by the session",
    "gap_baseline is not compared: edge.backtest computes it only inside its own 09:10 premarket replay",
]
GAP_BASELINE_NOTE = {"status": "skipped", "why": "edge/backtest.py exposes gap_baseline only inside its own "
                     "session() replay (a 09:10 premarket reference, news, a different universe): not cheap to "
                     "recompute for these sessions, so it is not compared"}


class _Transient(Exception):
    """A fetch failed for a reason that is not the plan's (5xx, timeout, 429): try again next tick."""

    def __init__(self, key: str, why: str) -> None:
        self.key, self.why = key, why
        super().__init__(f"{key}: {why}")


def _at(day: date, hh: int, mm: int) -> int:
    return int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET).timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=ET).isoformat()


def _http_status(exc: BaseException) -> Optional[int]:
    return getattr(getattr(exc, "response", None), "status_code", None)


def start_day_from_env() -> date:
    raw = (os.getenv(START_ENV) or "").strip()
    try:
        return date.fromisoformat(raw) if raw else DEFAULT_START
    except ValueError:
        return DEFAULT_START


# ------------------------------------------------------------------ SEC 8-K (the dilution filter) --
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_SUBMISSIONS_FILE = "https://data.sec.gov/submissions/{name}"
SEC_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{acc}-index.htm"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_ARCHIVES = "https://www.sec.gov"
DILUTIVE_ITEM = "3.02"
DILUTIVE_TITLES = {
    "underwriting agreement": re.compile(r"underwriting\s+agreement", re.I),
    "securities purchase agreement": re.compile(r"securities\s+purchase\s+agreement", re.I),
    "registered direct": re.compile(r"registered[\s\-]+direct", re.I),
    "at-the-market": re.compile(r"at[\s\-]+the[\s\-]+market", re.I),
    "placement agency agreement": re.compile(r"placement\s+agency\s+agreement", re.I),
}
EIGHT_K_FORMS = {"8-K", "8-K/A"}
MAX_DOCS_PER_FILING = 8
TITLE_CHARS = 1500


def dilutive_title(text: str) -> Optional[str]:
    """The preregistered exhibit title the text carries, if any."""
    for name, rx in DILUTIVE_TITLES.items():
        if rx.search(text or ""):
            return name
    return None


def _visible_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw or "")
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))).strip()


def _accepted_in_window(accepted: str, filing_date: str, lo: datetime, hi: datetime) -> bool:
    """EDGAR's acceptanceDateTime carries a 'Z' but is Eastern time; both readings are tried and
    either one inside the window counts (the conservative choice for an exclusion filter)."""
    if accepted:
        try:
            naive = datetime.fromisoformat(str(accepted)[:19])
        except ValueError:
            naive = None
        if naive is not None:
            as_et, as_utc = naive.replace(tzinfo=ET), naive.replace(tzinfo=timezone.utc)
            return lo <= as_et <= hi or lo <= as_utc <= hi
    try:
        fd = date.fromisoformat(str(filing_date)[:10])
    except ValueError:
        return True                      # an undated filing cannot be cleared
    return lo.date() <= fd <= hi.date()


class SecFilings:
    """SEC EDGAR, read through the injected `get` (no network in tests). Caches per run tick.

    Unreachable SEC (no User-Agent accepted, network refused) is a state, not an error: three
    failures before any success stop further calls, and every check after that is 'unchecked'."""

    def __init__(self, get, *, sleep=time.sleep, pace_s: float = 0.12) -> None:
        self.get, self.sleep, self.pace = get, sleep, pace_s
        self.subs: Dict[int, Optional[List[Dict[str, Any]]]] = {}
        self.indexes: Dict[str, Optional[List[Dict[str, str]]]] = {}
        self.docs: Dict[str, Optional[str]] = {}
        self.ok = self.fail = self.streak = 0
        self._tickers: Optional[Dict[str, int]] = None

    @property
    def down(self) -> bool:
        return self.ok == 0 and self.streak >= 3

    def _fetch(self, url: str):
        if self.down:
            return None
        from edge.providers.public import _sec_user_agent
        for _attempt in range(2):
            try:
                r = self.get(url, headers={"User-Agent": _sec_user_agent()}, timeout=30)
            except Exception:  # noqa: BLE001 - SEC unreachable is a state, recorded by the caller
                r = None
            if self.pace:
                self.sleep(self.pace)
            code = getattr(r, "status_code", None)
            if code in (200, 404):
                self.ok += 1
                self.streak = 0
                return r
            if code == 429:
                self.sleep(1.0)
                continue
            if code in DENIED:
                break
        self.fail += 1
        self.streak += 1
        return None

    def cik_for(self, symbol: str) -> Optional[int]:
        """Fallback when Polygon's reference row had no CIK: SEC's current ticker map."""
        if self._tickers is None:
            r = self._fetch(SEC_TICKERS)
            self._tickers = {}
            if r is not None and r.status_code == 200:
                for row in (r.json() or {}).values():
                    try:
                        self._tickers[str(row["ticker"]).upper()] = int(row["cik_str"])
                    except (KeyError, TypeError, ValueError):
                        continue
        return self._tickers.get(symbol.upper().replace(".", "-"))

    def filings(self, cik: int, since: date) -> Optional[List[Dict[str, Any]]]:
        """Every filing in the submissions record that reaches back to `since`; None when unknown."""
        if cik in self.subs:
            return self.subs[cik]
        r = self._fetch(SEC_SUBMISSIONS.format(cik=cik))
        if r is None or r.status_code != 200:
            self.subs[cik] = None
            return None
        p = r.json() or {}
        f = p.get("filings") or {}
        rows = _filing_rows(f.get("recent") or {})
        oldest = min((x["filing_date"] for x in rows), default="9999")
        if oldest > since.isoformat():
            for extra in f.get("files") or []:
                if str(extra.get("filingTo") or "") < since.isoformat():
                    continue
                rr = self._fetch(SEC_SUBMISSIONS_FILE.format(name=extra.get("name")))
                if rr is None or rr.status_code != 200:
                    self.subs[cik] = None
                    return None
                rows.extend(_filing_rows(rr.json() or {}))
        self.subs[cik] = rows
        return rows

    def exhibits(self, cik: int, accession: str) -> Optional[List[Dict[str, str]]]:
        if accession in self.indexes:
            return self.indexes[accession]
        r = self._fetch(SEC_INDEX.format(cik=cik, nodash=accession.replace("-", ""), acc=accession))
        out = None if r is None or r.status_code != 200 else _parse_index(getattr(r, "text", "") or "")
        self.indexes[accession] = out
        return out

    def title(self, href: str) -> Optional[str]:
        if href in self.docs:
            return self.docs[href]
        url = href if href.startswith("http") else SEC_ARCHIVES + href.replace("/ix?doc=", "")
        r = self._fetch(url)
        out = None if r is None or r.status_code != 200 else _visible_text(getattr(r, "text", "") or "")[:TITLE_CHARS]
        self.docs[href] = out
        return out


def _filing_rows(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    forms = block.get("form") or []
    col = lambda k: block.get(k) or []   # noqa: E731
    acc, fdate, acc_t, items = col("accessionNumber"), col("filingDate"), col("acceptanceDateTime"), col("items")
    out = []
    for i, form in enumerate(forms):
        it = items[i] if i < len(items) else ""
        it = [x.strip() for x in (it if isinstance(it, list) else str(it or "").split(",")) if str(x).strip()]
        out.append({"form": form, "accession": acc[i] if i < len(acc) else "",
                    "filing_date": str(fdate[i] if i < len(fdate) else ""),
                    "accepted": str(acc_t[i] if i < len(acc_t) else ""), "items": it})
    return out


def _parse_index(page: str) -> List[Dict[str, str]]:
    """Rows of an EDGAR filing index: [{type, description, href}]."""
    out = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", page):
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
        if len(cells) < 4:
            continue
        href = re.search(r'(?i)href="([^"]+)"', cells[2])
        out.append({"description": _visible_text(cells[1]), "type": _visible_text(cells[3]),
                    "href": href.group(1) if href else ""})
    return out


def dilution_8k_filter(sec: Optional[SecFilings], *, cik: Optional[int], d1: date, day: date) -> Dict[str, Any]:
    """The preregistered dilution filter for one name on decision day `day`.

    {"status": "hit" | "clear" | "unchecked", "why", "filings"}: "hit" when an 8-K (or 8-K/A)
    accepted between D-1 00:00 ET and D 09:00 ET lists Item 3.02 or carries an exhibit whose title
    (index description, else the document's opening text) names an underwriting / securities
    purchase / registered direct / at-the-market / placement agency agreement. "unchecked" when the
    SEC record could not be read: it is never reported as "clear"."""
    if sec is None:
        return {"status": "unchecked", "why": "dilution_filter: unavailable (no SEC access configured)"}
    if sec.down:
        return {"status": "unchecked", "why": "SEC EDGAR unreachable this run"}
    if not cik:
        return {"status": "unchecked", "why": "no CIK for the ticker"}
    rows = sec.filings(int(cik), d1)
    if rows is None:
        return {"status": "unchecked", "why": "SEC submissions unavailable"}
    lo, hi = datetime(d1.year, d1.month, d1.day, 0, 0, tzinfo=ET), datetime(day.year, day.month, day.day, 9, 0,
                                                                            tzinfo=ET)
    seen, unknown = [], False
    for f in rows:
        if f["form"] not in EIGHT_K_FORMS or not _accepted_in_window(f["accepted"], f["filing_date"], lo, hi):
            continue
        seen.append({"accession": f["accession"], "accepted": f["accepted"], "items": f["items"]})
        if DILUTIVE_ITEM in f["items"]:
            return {"status": "hit", "why": f"8-K {f['accession']} lists Item 3.02", "filings": seen}
        ex = sec.exhibits(int(cik), f["accession"])
        if ex is None:
            unknown = True
            continue
        docs = 0
        for e in ex:
            typ = (e.get("type") or "").upper()
            if not typ.startswith("EX-") or typ.startswith(("EX-101", "EX-104")):
                continue
            hit = dilutive_title(e.get("description") or "")
            if not hit and e.get("href") and docs < MAX_DOCS_PER_FILING:
                docs += 1
                head = sec.title(e["href"])
                if head is None:
                    unknown = True
                else:
                    hit = dilutive_title(head)
            if hit:
                return {"status": "hit", "why": f"8-K {f['accession']} exhibit {typ}: {hit}", "filings": seen}
    if unknown:
        return {"status": "unchecked", "why": "an in-window 8-K's exhibits could not be read", "filings": seen}
    return {"status": "clear", "why": f"{len(seen)} in-window 8-K(s), none dilutive", "filings": seen}


# ------------------------------------------------------------------------------- simulation --
def _limit(ref: float) -> float:
    return round(ref * LIMIT_MULT, 6)


def _shares(limit: float) -> int:
    return int(math.floor(SIZE_USD / limit + 1e-9))


def simulate(bars: List[tuple], day: date, *, symbol: str, arm: str, ref: float, limit: float, shares: int,
             delay_min: int = 0, cost_bps: float = HEADLINE_BPS, complete: Optional[bool] = None) -> Dict[str, Any]:
    """One limit-on-open trade. The fill is the open of the 09:30 bar (09:31 with a one-minute delay)
    when it is at or below the limit; above it is NO_FILL, never chased. Exits by resolve_execution."""
    open_ts = _at(day, 9, 30) + 60 * delay_min
    first = next((b for b in bars if b[0] == open_ts), None)
    if first is None:
        return {"simulated": NO_DATA, "pnl_usd": None, "note": f"no {_iso(open_ts)[11:16]} bar"}
    fill = float(first[1])
    if fill > limit + 1e-9:
        return {"simulated": NO_FILL, "pnl_usd": None, "open": fill, "note": "opened above the limit"}
    spec = SPECS[arm]
    f = Forecast(forecast_id=forecast_id_for(spec.experiment_id, symbol, day.isoformat()),
                 experiment_id=spec.experiment_id, spec_hash=spec.spec_hash(), symbol=symbol,
                 session_date=day.isoformat(), issued_at=_at(day, 9, 0), window_start=open_ts,
                 entry_expiry=open_ts + 60, time_exit=_at(day, 15, 30), entry_ref=ref, entry_trigger=fill,
                 entry_limit=limit, target=fill * TARGET_MULT, stop=fill * STOP_MULT, shares=shares)
    x = resolve_execution(f, bars, cost_bps_per_side=cost_bps, complete=complete)
    return {"simulated": x.outcome, "pnl_usd": x.pnl_usd, "entry": x.entry_fill, "exit": x.exit_price,
            "ambiguous": x.ambiguous, "note": x.note}


# ---------------------------------------------------------------------------------- the run --
class _Run:
    def __init__(self, get, store, *, start: date, end: date, pace: float, sleep, sec_pace: float,
                 clock: Callable[[], float], budget_s: Optional[float]) -> None:
        from edge.pipeline import trading_day
        self.get, self.store, self.start, self.end = get, store, start, end
        self.pace, self.sleep, self.clock, self.budget = pace, sleep, clock, budget_s
        self.t0, self.polygon_calls = clock(), 0
        self.sec = SecFilings(get, sleep=sleep, pace_s=sec_pace)
        sessions, d = [], end
        while d >= start or len([s for s in sessions if s < start]) < REVERSE_SPLIT_SESSIONS + 1:
            if trading_day(d):
                sessions.append(d)
            d -= timedelta(days=1)
        self.sessions = sorted(sessions)
        self.idx = {d: i for i, d in enumerate(self.sessions)}
        self.days = [d for d in self.sessions if d >= start]
        self.splits: Splits = {}
        self.cs_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    # ---- Polygon, paced (the key is shared and the free tier allows ~5 calls a minute)
    def _wait(self) -> None:
        if self.polygon_calls and self.pace > 0:
            self.sleep(self.pace)
        self.polygon_calls += 1

    def _key(self, d: date) -> str:
        return f"{VERSION}|{d.isoformat()}"

    def daily(self, d: date, attempts: Dict[str, int]) -> Dict[str, Any]:
        """The session's grouped daily, from the store when it was fetched before (never twice)."""
        row = self.store.get(DAILY_TABLE, self._key(d))
        if row:
            return row
        self._wait()
        try:
            rows = PG.grouped_daily(self.get, d, adjusted=False, sleep=self.sleep)
        except Exception as exc:  # noqa: BLE001
            code = _http_status(exc)
            key = f"daily|{d.isoformat()}"
            if code in DENIED or attempts.get(key, 0) + 1 >= MAX_ATTEMPTS:
                row = {"version": VERSION, "day": d.isoformat(), "status": "denied" if code in DENIED else "error",
                       "http_status": code, "why": redact_exc(exc, 160)}
                self.store.put(DAILY_TABLE, self._key(d), row)
                return row
            raise _Transient(key, redact_exc(exc, 160))
        closes: Dict[str, float] = {}
        bars: Dict[str, List[Optional[float]]] = {}
        for r in rows:
            t, c = str(r.get("T") or ""), r.get("c")
            if not t or not c:
                continue
            closes[t] = float(c)
            v, vw = float(r.get("v") or 0), r.get("vw")
            if vw is None or v * float(vw) >= MIN_DOLLARS:
                bars[t] = [float(r.get("h") or 0), float(r.get("l") or 0), float(c), v,
                           float(vw) if vw is not None else None]
        row = {"version": VERSION, "day": d.isoformat(), "status": "ok" if closes else "empty",
               "rows": len(rows), "closes": closes, "bars": bars, "fetched_at": int(time.time())}
        self.store.put(DAILY_TABLE, self._key(d), row)
        return row

    def compact(self, d: date) -> None:
        """No later decision reads this session: keep its status, drop its prices."""
        row = self.store.get(DAILY_TABLE, self._key(d))
        if row and ("closes" in row or "bars" in row):
            self.store.put(DAILY_TABLE, self._key(d), {k: v for k, v in row.items() if k not in ("closes", "bars")}
                           | {"compacted": True})

    def _snapshot_dates(self, d1: date) -> List[date]:
        """The first session of D-1's month and of the month after (when inside the replay)."""
        firsts: Dict[Tuple[int, int], date] = {}
        for s in self.sessions:
            firsts.setdefault((s.year, s.month), s)
        ym = (d1.year, d1.month)
        nxt = (d1.year + (d1.month == 12), d1.month % 12 + 1)
        return [x for x in (firsts.get(ym), firsts.get(nxt)) if x is not None]

    def _snapshot(self, d: date, attempts: Dict[str, int]) -> Optional[Dict[str, Any]]:
        k = self._key(d)
        if k in self.cs_cache:
            return self.cs_cache[k]
        row = self.store.get(REFERENCE_TABLE, k)
        if not row:
            self._wait()
            try:
                rows = PG.tickers(self.get, d, ticker_type="CS", sleep=self.sleep, pace_s=self.pace)
                self.polygon_calls += max(0, math.ceil(len(rows) / 1000) - 1)
                row = {"version": VERSION, "day": d.isoformat(), "status": "ok",
                       "tickers": {r["ticker"]: r.get("cik") for r in rows}}
            except Exception as exc:  # noqa: BLE001
                code = _http_status(exc)
                key = f"reference|{d.isoformat()}"
                if not (code in DENIED or attempts.get(key, 0) + 1 >= MAX_ATTEMPTS):
                    raise _Transient(key, redact_exc(exc, 160))
                row = {"version": VERSION, "day": d.isoformat(), "status": "denied" if code in DENIED else "error",
                       "http_status": code, "why": redact_exc(exc, 160)}
            self.store.put(REFERENCE_TABLE, k, row)
        self.cs_cache[k] = row if row.get("status") == "ok" else None
        return self.cs_cache[k]

    def common_stock(self, d1: date, attempts: Dict[str, int]) -> Optional[Dict[str, Any]]:
        snaps = [self._snapshot(x, attempts) for x in self._snapshot_dates(d1)]
        if not snaps or snaps[0] is None:
            return None
        out: Dict[str, Any] = {}
        for s in reversed([s for s in snaps if s]):
            out.update(s["tickers"])
        return out

    # ---- one decision day
    def decide(self, day: date, attempts: Dict[str, int]) -> Dict[str, Any]:
        from edge import calendar as CAL
        i = self.idx[day]
        d1, d2, d5 = self.sessions[i - 1], self.sessions[i - 2], self.sessions[i - REVERSE_SPLIT_SESSIONS]
        rec: Dict[str, Any] = {"version": VERSION, "day": day.isoformat(), "d1": d1.isoformat(),
                               "d2": d2.isoformat(), "trades": [], "counts": {}, "minute_status": None}
        if CAL.session(day)["early_close"]:
            return {**rec, "status": "early_close", "why": "13:00 close: the 15:30 ET time exit is after the close"}
        r1, r2 = self.daily(d1, attempts), self.daily(d2, attempts)
        rec["daily_status"] = [r1.get("status"), r2.get("status")]
        if r1.get("status") != "ok" or r2.get("status") != "ok":
            return {**rec, "status": "no_daily", "why": f"grouped daily D-1 {r1.get('status')}, D-2 {r2.get('status')}"}
        cs = self.common_stock(d1, attempts)
        if cs is None:
            return {**rec, "status": "no_reference", "why": "Polygon CS ticker list unavailable for D-1's month"}
        # Selection reads ONLY sessions D-1 and D-2, splits executed by D, and filings accepted by
        # 09:00 ET on D. D's own daily bar is never fetched for D's decision.
        eligible: Dict[str, List[Dict[str, Any]]] = {MAIN: [], CONTROL: []}
        counts = {arm: {"return_qualified": 0, "excluded": {}, "eligible": 0, "walked": 0, "selected": 0}
                  for arm in eligible}
        for t, c1 in r1["closes"].items():
            c2_raw = r2["closes"].get(t)
            if not c2_raw:
                continue
            f21 = share_factor(self.splits, t, d2, d1)        # a split executed on D-1 only
            c2 = c2_raw / f21
            ret = c1 / c2 - 1
            arm = MAIN if ret >= MIN_RETURN else CONTROL if CONTROL_RETURN <= ret < MIN_RETURN else None
            if arm is None:
                continue
            counts[arm]["return_qualified"] += 1
            b = r1["bars"].get(t)
            reason = None
            if t not in cs:
                reason = "not_common_stock"
            elif not (MIN_PRICE <= c1 <= MAX_PRICE):
                reason = "price"
            elif b is None or b[4] is None or b[3] * b[4] < MIN_DOLLARS:
                reason = "dollar_volume"
            elif not (b[0] > b[1] and (c1 - b[1]) / (b[0] - b[1]) >= MIN_RANGE_POS):
                reason = "lower_half_of_range"
            elif any(mult < 1.0 and d5 <= ex <= day for ex, mult in self.splits.get(t) or []):
                reason = "reverse_split"
            if reason:
                ex_ = counts[arm]["excluded"]
                ex_[reason] = ex_.get(reason, 0) + 1
                continue
            f1d = share_factor(self.splits, t, d1, day)       # onto D's basis for the limit
            ref = c1 / f1d
            cand = {"day": day.isoformat(), "symbol": t, "arm": arm, "prior_day_pct": round(ret * 100, 2),
                    "close_d1": c1, "close_d2_restated": round(c2, 6),
                    "range_pos": round((c1 - b[1]) / (b[0] - b[1]), 4), "dollar_volume": round(b[3] * b[4], 2),
                    "ref": round(ref, 6), "limit": _limit(ref), "cik": cs.get(t)}
            if f21 != 1.0 or f1d != 1.0:
                cand["split_restated"] = {"raw_close_d2": c2_raw, "d2_share_factor": f21, "d1_to_d_share_factor": f1d}
            eligible[arm].append(cand)
        selected: List[Dict[str, Any]] = []
        for arm, rows in eligible.items():
            counts[arm]["eligible"] = len(rows)
            n = 0
            for cand in sorted(rows, key=lambda r: (-r["dollar_volume"], r["symbol"])):
                if n >= TOP_N:
                    break
                counts[arm]["walked"] += 1
                cik = cand.get("cik") or (self.sec.cik_for(cand["symbol"]) if not self.sec.down else None)
                dil = dilution_8k_filter(self.sec, cik=int(cik) if cik else None, d1=d1, day=day)
                cand["dilution"] = {k: dil[k] for k in ("status", "why")}
                counts[arm].setdefault("dilution", {})
                counts[arm]["dilution"][dil["status"]] = counts[arm]["dilution"].get(dil["status"], 0) + 1
                if dil["status"] == "hit":
                    counts[arm]["excluded"]["dilution_8k"] = counts[arm]["excluded"].get("dilution_8k", 0) + 1
                    continue
                n += 1
                cand["rank"] = n
                selected.append(cand)
            counts[arm]["selected"] = n
        rec["counts"] = counts
        if not selected:
            return {**rec, "status": "ok"}
        # Execution: raw minute bars of D, fetched only now, after the selection is fixed.
        syms = sorted({c["symbol"] for c in selected})
        try:
            raw, complete = A.bars_pages(self.get, syms, timeframe="1Min", start=_iso(_at(day, 9, 30)),
                                         end=_iso(_at(day, 16, 0)), adjustment=MINUTE_ADJUSTMENT)
        except Exception as exc:  # noqa: BLE001
            code = _http_status(exc)
            key = f"minute|{day.isoformat()}"
            if not (code in DENIED or code == 422 or attempts.get(key, 0) + 1 >= MAX_ATTEMPTS):
                raise _Transient(key, redact_exc(exc, 160))
            rec["minute_status"] = "denied" if code in DENIED or code == 422 else "error"
            raw, complete = {}, False
        from edge.backtest import _bars
        lo, hi = _at(day, 9, 30), _at(day, 16, 0)
        bars = {s: [b for b in _bars(raw.get(s) or []) if lo <= b[0] < hi] for s in syms}
        if rec["minute_status"] is None:
            rec["minute_status"] = "ok" if any(bars.values()) else "empty"
        for c in selected:
            b = bars.get(c["symbol"]) or []
            kw = dict(symbol=c["symbol"], arm=c["arm"], ref=c["ref"], limit=c["limit"], shares=_shares(c["limit"]),
                      complete=complete)
            c["shares"] = kw["shares"]
            c["base"] = simulate(b, day, cost_bps=HEADLINE_BPS, **kw)
            c["cost_25bps"] = simulate(b, day, cost_bps=STRESS_BPS, **kw)
            c["delay_1min"] = simulate(b, day, delay_min=1, cost_bps=HEADLINE_BPS, **kw)
            c.pop("cik", None)
        rec["trades"] = selected
        return {**rec, "status": "ok"}

    def out_of_time(self) -> bool:
        return self.budget is not None and self.clock() - self.t0 >= self.budget


def run(get, store, *, end_day: date = WINDOW_END, start_day: Optional[date] = None, pace_s: Optional[float] = None,
        sec_pace_s: float = 0.12, sleep=time.sleep, budget_s: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """Replay second_day_open@v1 and its control from `start_day` (EDGE_BT2_START) to `end_day`.

    Resumable: each decision day is stored as it completes; a run out of `budget_s` (or stopped by
    a transient fetch failure) stores its progress and returns {"status": "in_progress"}; the next
    call continues. The finished record is stored once per VERSION under TABLE."""
    if store.get(TABLE, VERSION):
        return {"status": "already_run"}
    start = start_day or start_day_from_env()
    pace = float(os.getenv("EDGE_PROBE_POLYGON_PACE_S", "13")) if pace_s is None else pace_s
    R = _Run(get, store, start=start, end=end_day, pace=pace, sleep=sleep, sec_pace=sec_pace_s, clock=clock,
             budget_s=budget_s)
    prog = store.get(PROGRESS_TABLE, VERSION) or {}
    if prog.get("start") != start.isoformat() or prog.get("end") != end_day.isoformat():
        prog = {"version": VERSION, "start": start.isoformat(), "end": end_day.isoformat(), "done_through": None,
                "attempts": {}, "ticks": 0, "splits": None, "started_at": int(time.time())}
    prog["ticks"] = int(prog.get("ticks") or 0) + 1
    if prog.get("splits") is None:
        try:
            # The whole split list: it restates prior sessions onto each decision day's basis and
            # names the reverse splits. A partial list would leave names on mixed bases: no list, no run.
            R._wait()
            rows = PG.splits(get, R.sessions[0] - timedelta(days=10), end_day, sleep=sleep)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "why": f"split list unavailable: {redact_exc(exc, 160)}"}
        prog["splits"] = [[r["ticker"], r["execution_date"].isoformat(), r["split_from"], r["split_to"]] for r in rows]
        store.put(PROGRESS_TABLE, VERSION, prog)
    R.splits = split_index([{"ticker": t, "execution_date": date.fromisoformat(ex), "split_from": f, "split_to": to}
                            for t, ex, f, to in prog["splits"]])
    attempts = prog.setdefault("attempts", {})
    done = prog.get("done_through")
    for day in R.days:
        if done and day.isoformat() <= done:
            continue
        if R.out_of_time():
            store.put(PROGRESS_TABLE, VERSION, prog)
            return {"status": "in_progress", "done_through": done, "why": "time budget", "version": VERSION,
                    "polygon_calls": R.polygon_calls}
        try:
            rec = R.decide(day, attempts)
        except _Transient as exc:
            attempts[exc.key] = attempts.get(exc.key, 0) + 1
            store.put(PROGRESS_TABLE, VERSION, prog)
            return {"status": "in_progress", "done_through": done, "why": f"transient: {exc.why}",
                    "version": VERSION, "attempt": attempts[exc.key]}
        store.put(SESSIONS_TABLE, R._key(day), rec)
        R.compact(R.sessions[R.idx[day] - 2])
        done = prog["done_through"] = day.isoformat()
        store.put(PROGRESS_TABLE, VERSION, prog)
    out = summarize(store, R, prog)
    store.put(TABLE, VERSION, out)
    return {"status": "complete", **{k: out[k] for k in ("version", "window", "data_availability",
                                                          "dilution_filter", "price_basis")},
            "windows": {w: {"arms": {a: {k: v for k, v in x.items() if k != "delay_1min_10bps"}
                                     for a, x in s["arms"].items()}, "gate": s["gate"]}
                        for w, s in out["windows"].items()}}


# --------------------------------------------------------------------------------- the report --
def _counts(trades: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    recs = [t[key] for t in trades if t.get(key)]
    filled = [x for x in recs if x["simulated"] in COUNTED]
    n, wins = len(filled), sum(1 for x in filled if x["simulated"] == WIN)
    lo, hi = stats.wilson(wins, n)
    pnls = [x["pnl_usd"] for x in filled if x["pnl_usd"] is not None]
    return {"selected": len(recs), "fills": n, "wins": wins, "losses": sum(1 for x in filled if x["simulated"] == LOSS),
            "time_exits": sum(1 for x in filled if x["simulated"] == TIME_EXIT),
            "no_fills": sum(1 for x in recs if x["simulated"] == NO_FILL),
            "no_data": sum(1 for x in recs if x["simulated"] in (NO_DATA, UNRESOLVED)),
            "win_rate": wins / n if n else None, "wilson_95": [lo, hi] if n else None,
            "expectancy_usd": stats.expectancy(pnls)["mean"], "total_usd": round(sum(pnls), 2)}


def arm_summary(trades: List[Dict[str, Any]], break_even: float) -> Dict[str, Any]:
    base, c25, delay = _counts(trades, "base"), _counts(trades, "cost_25bps"), _counts(trades, "delay_1min")
    return {**{k: base[k] for k in ("selected", "fills", "wins", "losses", "time_exits", "no_fills", "no_data",
                                    "win_rate", "wilson_95")},
            "expectancy_usd_10bps": base["expectancy_usd"], "expectancy_usd_25bps": c25["expectancy_usd"],
            "total_usd_10bps": base["total_usd"], "total_usd_25bps": c25["total_usd"],
            "verdict_vs_break_even": stats.break_even_verdict(base["wins"], base["fills"], break_even),
            "delay_1min_10bps": delay}


def gate(main: Dict[str, Any], control: Dict[str, Any], dilution_status: str) -> Dict[str, Any]:
    """The preregistered step-1 gate. PASS only when every condition holds; else FAIL, with reasons."""
    reasons = []
    if not main["fills"]:
        reasons.append("no filled trades")
    else:
        lo = main["wilson_95"][0]
        if lo < GATE_WILSON_LOW:
            reasons.append(f"Wilson 95% lower bound {lo:.1%} < {GATE_WILSON_LOW:.0%}")
        e25 = main["expectancy_usd_25bps"]
        if e25 is None or e25 <= 0:
            reasons.append(f"expectancy after 25 bps a side ${e25:.2f} <= $0" if e25 is not None
                           else "no expectancy after 25 bps")
        if not control["fills"]:
            reasons.append("control has no filled trades: beating it is not shown")
        else:
            for k, label in (("expectancy_usd_10bps", "expectancy at 10 bps"),
                             ("expectancy_usd_25bps", "expectancy at 25 bps"), ("win_rate", "win rate")):
                if main[k] is None or control[k] is None or main[k] <= control[k]:
                    reasons.append(f"does not beat the control on {label} ({main[k]} vs {control[k]})")
    if dilution_status != "applied":
        reasons.append(f"dilution_filter: {dilution_status} -- the preregistered rule was not applied in full")
    return {"verdict": "FAIL" if reasons else "PASS", "reasons": reasons,
            "criteria": {"wilson_low_min": GATE_WILSON_LOW, "expectancy_25bps_gt": 0.0,
                         "beats_control": "higher expectancy (10 and 25 bps) and higher win rate",
                         "break_even_reference": SECOND_DAY_OPEN.break_even_win_rate()}}


def _dilution_status(recs: List[Dict[str, Any]]) -> Tuple[str, Dict[str, int]]:
    tally: Dict[str, int] = {}
    for r in recs:
        for c in (r.get("counts") or {}).values():
            for k, v in (c.get("dilution") or {}).items():
                tally[k] = tally.get(k, 0) + v
    checked, unchecked = tally.get("clear", 0) + tally.get("hit", 0), tally.get("unchecked", 0)
    if not checked and not unchecked:
        return "not_needed", tally
    if not checked:
        return "unavailable", tally
    return ("partial" if unchecked else "applied"), tally


def _window(recs: List[Dict[str, Any]], lo: date, hi: date) -> Dict[str, Any]:
    rows = [r for r in recs if lo.isoformat() <= r["day"] <= hi.isoformat()]
    trades = [t for r in rows for t in r.get("trades") or []]
    be = SECOND_DAY_OPEN.break_even_win_rate()
    arms = {MAIN: arm_summary([t for t in trades if t["arm"] == MAIN], be),
            CONTROL: arm_summary([t for t in trades if t["arm"] == CONTROL], be)}
    dil, tally = _dilution_status(rows)
    excluded: Dict[str, Dict[str, int]] = {MAIN: {}, CONTROL: {}}
    for r in rows:
        for arm, c in (r.get("counts") or {}).items():
            for k, v in (c.get("excluded") or {}).items():
                excluded[arm][k] = excluded[arm].get(k, 0) + v
    return {"window": [lo.isoformat(), hi.isoformat()], "sessions": len(rows),
            "sessions_decided": sum(1 for r in rows if r.get("status") == "ok"),
            "sessions_with_minute_data": sum(1 for r in rows if r.get("minute_status") == "ok"),
            "arms": arms, "exclusions": excluded, "dilution_filter": dil, "dilution_checks": tally,
            "gate": gate(arms[MAIN], arms[CONTROL], dil)}


def _availability(store, recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    daily = [r for r in store.scan(DAILY_TABLE) if r.get("version") == VERSION]
    ok = sorted(r["day"] for r in daily if r.get("status") == "ok")
    bad = sorted((r["day"], r.get("status")) for r in daily if r.get("status") != "ok")
    minute_ok = sorted(r["day"] for r in recs if r.get("minute_status") == "ok")
    minute_bad = sorted((r["day"], r["minute_status"]) for r in recs if r.get("minute_status") in ("denied", "empty",
                                                                                                    "error"))
    decided = sorted(r["day"] for r in recs if r.get("status") == "ok" and r.get("minute_status") in ("ok", None))
    first_both = next((r["day"] for r in sorted(recs, key=lambda r: r["day"])
                       if r.get("status") == "ok" and r.get("minute_status") == "ok"), None)
    return {"polygon_grouped_first_session": ok[0] if ok else None,
            "polygon_grouped_unavailable": {"count": len(bad), "first": bad[:5], "last": bad[-5:]},
            "alpaca_minute_first_session": minute_ok[0] if minute_ok else None,
            "alpaca_minute_unavailable": {"count": len(minute_bad), "first": minute_bad[:5], "last": minute_bad[-5:]},
            "first_session_with_both": first_both, "first_decided_session": decided[0] if decided else None,
            "sessions_not_decided": sorted({r.get("status") for r in recs if r.get("status") != "ok"})}


def summarize(store, R: _Run, prog: Dict[str, Any]) -> Dict[str, Any]:
    recs = sorted((r for r in store.scan(SESSIONS_TABLE) if r.get("version") == VERSION), key=lambda r: r["day"])
    recs = [r for r in recs if R.start.isoformat() <= r["day"] <= R.end.isoformat()]
    windows = {"preregistered": _window(recs, PREREG_START, PREREG_END), "full": _window(recs, R.start, R.end)}
    trades = [t for r in recs for t in r.get("trades") or []]
    return {"version": VERSION, "resolver_version": RESOLVER_VERSION, "price_basis": PRICE_BASIS,
            "price_basis_detail": PRICE_BASIS_DETAIL, "window": [R.start.isoformat(), R.end.isoformat()],
            "preregistered_window": [PREREG_START.isoformat(), PREREG_END.isoformat()],
            "hypothesis": {a: {"experiment_id": s.experiment_id, "spec_hash": s.spec_hash(), "spec": asdict(s)}
                           for a, s in SPECS.items()},
            "break_even": SECOND_DAY_OPEN.break_even_win_rate(), "limits": LIMITS, "gap_baseline": GAP_BASELINE_NOTE,
            "dilution_filter": windows["full"]["dilution_filter"], "data_availability": _availability(store, recs),
            "windows": windows, "skipped": [{"day": r["day"], "status": r.get("status"), "why": r.get("why")}
                                            for r in recs if r.get("status") != "ok"][:500],
            "splits_in_window": len(prog.get("splits") or []), "ticks": prog.get("ticks"),
            "trades": trades[:6000], "completed_at": int(time.time())}
