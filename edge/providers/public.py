"""Free public sources that need no paid key.

FINRA daily short-sale volume -- https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt
  Pipe-delimited: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market.
  This is the share of a day's VOLUME executed as short sales. It is NOT short
  interest (open short positions, reported twice a month) and must never be
  used as a stand-in for it.

IBKR shortable-stock file -- ftp3.interactivebrokers.com, user "shortstock", file usa.txt
  Pipe-delimited with a #SYM header: SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE.
  Interactive Brokers' OWN lendable inventory and borrow fee -- one broker's
  view, not the market's. Coverage and meaning are recorded as such.

SEC EDGAR current 8-K feed -- https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom
  SEC requires a descriptive User-Agent with contact details (SEC_USER_AGENT).
"""
from __future__ import annotations

import ftplib
import io
import os
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from edge.providers import base as B


# ---------------------------------------------------------------- FINRA --
def finra_short_volume_url(day: date) -> str:
    return f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day:%Y%m%d}.txt"


def parse_finra_short_volume(text: str) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 5 or parts[0] == "Date" or not parts[0].isdigit():
            continue
        try:
            short, total = float(parts[2]), float(parts[4])
        except ValueError:
            continue
        out[parts[1].upper()] = {"short_volume": short, "total_volume": total,
                                 "short_ratio": (short / total) if total else None}
    return out


def probe_finra(get: Optional[B.HttpGet] = None, *, today: Optional[date] = None) -> B.Probe:
    get = get or B.default_get()
    d = (today or date.today()) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    r, ms, err = B.timed_get(get, finra_short_volume_url(d), headers={"User-Agent": "edge-probe"})
    if r is None:
        return B.Probe(B.SHORT_VOLUME, "finra", B.ERROR, latency_ms=ms, note=err)
    if r.status_code == 404:
        return B.Probe(B.SHORT_VOLUME, "finra", B.EMPTY, http_status=404, latency_ms=ms,
                       note=f"no file for {d} (holiday or not yet published)")
    st = B.classify(r.status_code)
    if st != B.OK:
        return B.Probe(B.SHORT_VOLUME, "finra", st, http_status=r.status_code, latency_ms=ms)
    rows = parse_finra_short_volume(r.text)
    return B.Probe(B.SHORT_VOLUME, "finra", B.OK if rows else B.EMPTY, http_status=r.status_code,
                   latency_ms=ms, rows=len(rows), note=f"session {d}; volume, not short interest")


# ----------------------------------------------------------------- IBKR --
def parse_ibkr_shortstock(text: str) -> Dict[str, Dict[str, Optional[float]]]:
    out: Dict[str, Dict[str, Optional[float]]] = {}
    cols: Optional[List[str]] = None
    for line in text.splitlines():
        if line.startswith("#SYM"):
            cols = [c.strip().upper() for c in line.lstrip("#").split("|")]
            continue
        if line.startswith("#") or not cols:
            continue
        parts = [p.strip() for p in line.split("|")]
        row = dict(zip(cols, parts))
        sym = (row.get("SYM") or "").upper()
        if not sym:
            continue
        avail_raw = row.get("AVAILABLE") or ""
        avail = float(re.sub(r"[^0-9.]", "", avail_raw)) if re.search(r"\d", avail_raw) else None
        try:
            fee = float(row.get("FEERATE")) if row.get("FEERATE") not in (None, "", "NA") else None
        except ValueError:
            fee = None
        out[sym] = {"fee_rate_pct": fee, "available_shares": avail,
                    "available_is_floor": avail_raw.startswith(">")}
    return out


def fetch_ibkr_shortstock(*, host: str = "ftp3.interactivebrokers.com", timeout: float = 20.0) -> str:
    buf = io.BytesIO()
    with ftplib.FTP(host, timeout=timeout) as ftp:
        ftp.login(user="shortstock", passwd="")
        ftp.retrbinary("RETR usa.txt", buf.write)
    return buf.getvalue().decode("utf-8", errors="replace")


def probe_ibkr(fetch=fetch_ibkr_shortstock) -> B.Probe:
    import time
    t0 = time.monotonic()
    try:
        text = fetch()
    except Exception as exc:  # noqa: BLE001
        return B.Probe(B.BORROW, "ibkr_shortstock", B.ERROR,
                       latency_ms=int((time.monotonic() - t0) * 1000), note=type(exc).__name__)
    rows = parse_ibkr_shortstock(text)
    return B.Probe(B.BORROW, "ibkr_shortstock", B.OK if rows else B.EMPTY,
                   latency_ms=int((time.monotonic() - t0) * 1000), rows=len(rows),
                   note="IBKR's own inventory and fee -- one broker's view")


# ----------------------------------------------------------------- SEC --
EDGAR_8K_ATOM = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=40&output=atom"


def _sec_user_agent() -> str:
    return (os.getenv("SEC_USER_AGENT") or "edge-research contact@example.invalid").strip()


def probe_edgar(get: Optional[B.HttpGet] = None) -> B.Probe:
    get = get or B.default_get()
    r, ms, err = B.timed_get(get, EDGAR_8K_ATOM, headers={"User-Agent": _sec_user_agent()})
    if r is None:
        return B.Probe(B.FILINGS, "sec_edgar", B.ERROR, latency_ms=ms, note=err)
    st = B.classify(r.status_code)
    if st != B.OK:
        return B.Probe(B.FILINGS, "sec_edgar", st, http_status=r.status_code, latency_ms=ms,
                       note="SEC refuses requests without a descriptive User-Agent (set SEC_USER_AGENT)")
    entries = r.text.count("<entry>")
    stamps = re.findall(r"<updated>([^<]+)</updated>", r.text)
    newest = None
    for s in stamps:
        try:
            newest = max(newest or 0, int(datetime.fromisoformat(s.strip()).timestamp()))
        except ValueError:
            continue
    ua_note = "" if os.getenv("SEC_USER_AGENT") else "set SEC_USER_AGENT to a real contact"
    return B.Probe(B.FILINGS, "sec_edgar", B.OK if entries else B.EMPTY, http_status=r.status_code,
                   latency_ms=ms, rows=entries, newest_ts=newest, note=ua_note)
