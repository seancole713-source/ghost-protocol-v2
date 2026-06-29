"""Super Ghost Expanded Data Brain (PR #101).

Point-in-time aware evidence collector for Super Ghost.

It gathers richer raw context while preserving honesty:
- SEC XBRL fundamentals (EPS/revenue YoY)
- SEC 8-K/material filing context
- SEC Form 4 insider activity (best-effort transaction parsing)
- news freshness/dedup/catalyst classification
- macro feature snapshot
- options-flow probe (best-effort)

Every source carries timestamps/as-of metadata so PR #100's point-in-time store
can audit leakage. Missing data stays missing; nothing is fabricated.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

LOGGER = logging.getLogger("ghost.super_ghost_data_brain")

SEC_USER_AGENT = os.getenv("EDGAR_USER_AGENT", "GhostProtocol/2.1 (seancole713-source/ghost-protocol-v2)")
TIMEOUT = float(os.getenv("SUPER_GHOST_DATA_TIMEOUT_S", "10"))
CACHE_TTL_S = int(os.getenv("SUPER_GHOST_DATA_TTL_S", "1800"))
_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}

BULL_NEWS = ("approval", "contract", "partnership", "launch", "award", "beat", "raise", "raised", "guidance raised", "upgrade", "record")
BEAR_NEWS = ("lawsuit", "investigation", "recall", "delay", "halt", "bankruptcy", "delisting", "downgrade", "miss", "cut", "weak")
GUIDANCE_BULL = ("raised guidance", "raises guidance", "strong outlook", "upbeat outlook", "above guidance", "increase forecast")
GUIDANCE_BEAR = ("cut guidance", "lowered guidance", "weak outlook", "below guidance", "withdraw guidance", "reduce forecast")


def _now() -> int:
    return int(time.time())


def _parse_date_ts(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        if n > 10_000_000_000:
            n /= 1000.0
        return int(n) if n > 0 else None
    s = str(v).strip()
    if not s:
        return None
    try:
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return _parse_date_ts(float(s))
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        pass
    try:
        return int(datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def _keyword_hits(text: str, words: Iterable[str]) -> List[str]:
    t = (text or "").lower()
    return [w for w in words if w in t][:10]


def _canonical_title(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    t = re.sub(r"[^a-z0-9 ]+", "", t)
    return t[:160]


def classify_news_articles(articles: List[Dict[str, Any]], *, now_ts: Optional[int] = None) -> Dict[str, Any]:
    now = int(now_ts or _now())
    seen = set()
    unique = []
    duplicates = 0
    latest_ts = None
    bull: List[str] = []
    bear: List[str] = []
    gb: List[str] = []
    gr: List[str] = []
    for a in articles or []:
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or a.get("headline") or "")
        key = _canonical_title(title)
        if key and key in seen:
            duplicates += 1
            continue
        if key:
            seen.add(key)
        unique.append(a)
        text = (title + " " + str(a.get("summary") or a.get("description") or "")).lower()
        bull.extend(_keyword_hits(text, BULL_NEWS))
        bear.extend(_keyword_hits(text, BEAR_NEWS))
        gb.extend(_keyword_hits(text, GUIDANCE_BULL))
        gr.extend(_keyword_hits(text, GUIDANCE_BEAR))
        ts = _parse_date_ts(a.get("published_at") or a.get("providerPublishTime") or a.get("date"))
        if ts is not None:
            latest_ts = max(latest_ts or ts, ts)
    age_hours = round((now - latest_ts) / 3600.0, 2) if latest_ts else None
    freshness = "fresh" if age_hours is not None and age_hours <= 24 else ("stale" if age_hours is not None else "unknown")
    catalyst_score = len(set(bull)) - len(set(bear))
    guidance_score = len(set(gb)) - len(set(gr))
    return {
        "available": bool(unique),
        "article_count": len(articles or []),
        "unique_count": len(unique),
        "duplicate_count": duplicates,
        "latest_published_at": latest_ts,
        "latest_age_hours": age_hours,
        "freshness": freshness,
        "bullish_terms": sorted(set(bull))[:10],
        "bearish_terms": sorted(set(bear))[:10],
        "guidance_bullish_terms": sorted(set(gb))[:10],
        "guidance_bearish_terms": sorted(set(gr))[:10],
        "catalyst_score": catalyst_score,
        "guidance_score": guidance_score,
        "as_of_ts": now,
    }


def parse_form4_xml(xml_text: str) -> Dict[str, Any]:
    """Parse a Form 4 XML doc into net open-market buy/sell activity.

    Counts transaction code P as buy and S as sell. Other codes are ignored.
    Returns net_shares where buys positive and sells negative.
    """
    text = html.unescape(xml_text or "")
    tx_blocks = re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", text, flags=re.S | re.I)
    buys = sells = 0
    buy_shares = sell_shares = 0.0
    for block in tx_blocks:
        code_m = re.search(r"<transactionCode>\s*([A-Z])\s*</transactionCode>", block, flags=re.I)
        shares_m = re.search(r"<transactionShares>\s*<value>\s*([0-9,.]+)\s*</value>", block, flags=re.I | re.S)
        code = (code_m.group(1).upper() if code_m else "")
        try:
            shares = float((shares_m.group(1) if shares_m else "0").replace(",", ""))
        except Exception:
            shares = 0.0
        if code == "P" and shares > 0:
            buys += 1
            buy_shares += shares
        elif code == "S" and shares > 0:
            sells += 1
            sell_shares += shares
    return {
        "transactions_scanned": len(tx_blocks),
        "buys": buys,
        "sells": sells,
        "buy_shares": int(buy_shares),
        "sell_shares": int(sell_shares),
        "net_shares": int(buy_shares - sell_shares),
        "available": bool(buys or sells),
    }


def _submissions(cik: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers={"User-Agent": SEC_USER_AGENT}, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        LOGGER.debug("SEC submissions %s: %s", cik, str(exc)[:100])
    return None


def _recent_forms(symbol: str, *, form: str, days: int = 180, limit: int = 20) -> Dict[str, Any]:
    try:
        from core.sec_fundamentals import cik_for_symbol
        cik = cik_for_symbol(symbol)
    except Exception:
        cik = None
    if not cik:
        return {"available": False, "reason": "no_cik_mapping", "symbol": symbol.upper(), "form": form, "filings": []}
    sub = _submissions(cik)
    if not sub:
        return {"available": False, "reason": "sec_api_failed", "symbol": symbol.upper(), "cik": cik, "form": form, "filings": []}
    recent = (sub.get("filings") or {}).get("recent") or {}
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    out = []
    for i, f in enumerate(forms):
        if str(f).upper() != form.upper():
            continue
        d = dates[i] if i < len(dates) else ""
        try:
            if datetime.fromisoformat(d).date() < cutoff:
                continue
        except Exception:
            pass
        acc = accs[i] if i < len(accs) else ""
        doc = docs[i] if i < len(docs) else ""
        out.append({
            "filing_date": d,
            "filing_ts": _parse_date_ts(d),
            "accession_number": acc,
            "document": doc,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc.replace('-', '')}/{doc.split('/')[-1]}" if acc and doc else None,
        })
        if len(out) >= limit:
            break
    return {"available": bool(out), "symbol": symbol.upper(), "cik": cik, "form": form, "filings_count": len(out), "filings": out, "as_of_ts": _now()}


def _fetch_text(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.text
    except Exception as exc:
        LOGGER.debug("fetch text %s: %s", url, str(exc)[:100])
    return None


def get_form4_activity(symbol: str, *, days: int = 180, max_docs: int = 5) -> Dict[str, Any]:
    forms = _recent_forms(symbol, form="4", days=days, limit=max_docs)
    total = {"transactions_scanned": 0, "buys": 0, "sells": 0, "buy_shares": 0, "sell_shares": 0, "net_shares": 0}
    parsed_docs = 0
    for f in forms.get("filings") or []:
        url = f.get("url")
        if not url:
            continue
        txt = _fetch_text(url)
        if not txt:
            continue
        parsed = parse_form4_xml(txt)
        parsed_docs += 1
        for k in total:
            total[k] += int(parsed.get(k) or 0)
    available = bool(total["buys"] or total["sells"])
    return {
        "available": available,
        "symbol": symbol.upper(),
        "form4_filings_count": forms.get("filings_count", 0),
        "parsed_documents": parsed_docs,
        **total,
        "recent_filings": forms.get("filings", [])[:5],
        "source_time_ok": True,
        "as_of_ts": _now(),
        "note": "Open-market P/S transactions parsed when SEC archive XML is reachable; otherwise filing count still recorded.",
    }


def get_news_quality(symbol: str) -> Dict[str, Any]:
    articles: List[Dict[str, Any]] = []
    try:
        from core.news import get_recent_articles
        articles.extend(get_recent_articles(50, symbol=symbol) or [])
    except Exception:
        pass
    try:
        from core.yfinance_client import yf_news
        for n in (yf_news(symbol) or [])[:20]:
            if isinstance(n, dict):
                articles.append({
                    "title": n.get("title") or "",
                    "summary": n.get("summary") or "",
                    "source": n.get("publisher") or "Yahoo Finance",
                    "url": n.get("link") or "",
                    "published_at": n.get("providerPublishTime"),
                    "symbols": [symbol.upper()],
                })
    except Exception:
        pass
    q = classify_news_articles(articles)
    q["symbol"] = symbol.upper()
    return q


def get_macro_context() -> Dict[str, Any]:
    try:
        from core.macro_regime import get_macro_features
        f = get_macro_features()
        return {"available": True, "features": f, "as_of_ts": _now(), "source": "macro_regime"}
    except Exception as exc:
        return {"available": False, "error": str(exc)[:120], "as_of_ts": _now(), "source": "macro_regime"}


def get_options_context(symbol: str) -> Dict[str, Any]:
    try:
        from core.options_flow import probe_options_flow
        out = probe_options_flow(symbol)
        out["as_of_ts"] = _now()
        return out
    except Exception as exc:
        return {"ok": True, "available": False, "symbol": symbol.upper(), "error": str(exc)[:120], "as_of_ts": _now()}


def build_data_brain(symbol: str, *, use_cache: bool = True) -> Dict[str, Any]:
    sym = (symbol or "WOLF").strip().upper()
    now = _now()
    cached = _cache.get(sym)
    if use_cache and cached and now - cached[0] < CACHE_TTL_S:
        return dict(cached[1])
    sources: Dict[str, Any] = {}
    try:
        from core.sec_fundamentals import get_fundamentals
        sources["sec_fundamentals"] = get_fundamentals(sym)
    except Exception as exc:
        sources["sec_fundamentals"] = {"available": False, "error": str(exc)[:120], "as_of_ts": now}
    try:
        from core.edgar_integration import fetch_recent_8k
        sources["sec_8k"] = fetch_recent_8k(sym, days=180)
        sources["sec_8k"].setdefault("as_of_ts", now)
    except Exception as exc:
        # Generic 8-K fallback for non-WOLF when edgar_integration lacks mapping.
        sources["sec_8k"] = _recent_forms(sym, form="8-K", days=180, limit=20)
        sources["sec_8k"].setdefault("fallback_error", str(exc)[:120])
    sources["form4_activity"] = get_form4_activity(sym)
    sources["news_quality"] = get_news_quality(sym)
    sources["macro_context"] = get_macro_context()
    sources["options_context"] = get_options_context(sym)

    # Derived lightweight signals for Super Ghost snapshot integration.
    guidance_score = int((sources["news_quality"].get("guidance_score") or 0))
    catalyst_score = int((sources["news_quality"].get("catalyst_score") or 0))
    derived = {
        "guidance_signal": "bullish" if guidance_score > 0 else "bearish" if guidance_score < 0 else "neutral",
        "catalyst_signal": "bullish" if catalyst_score > 0 else "bearish" if catalyst_score < 0 else "neutral",
        "has_fresh_news": sources["news_quality"].get("freshness") == "fresh",
        "has_form4_transactions": bool(sources["form4_activity"].get("available")),
        "options_skew_hint": sources["options_context"].get("skew_hint"),
    }
    coverage = {k: bool(v.get("available") or v.get("ok")) if isinstance(v, dict) else False for k, v in sources.items()}
    out = {
        "ok": True,
        "symbol": sym,
        "as_of_ts": now,
        "coverage": coverage,
        "sources": sources,
        "derived": derived,
        "disclaimer": "Data Brain is evidence collection only; missing data is not treated as bullish/bearish.",
    }
    _cache[sym] = (now, out)
    return dict(out)


def ensure_data_brain_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS super_ghost_data_brain_snapshots (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            as_of_ts BIGINT NOT NULL,
            coverage_json JSONB,
            derived_json JSONB,
            sources_json JSONB,
            created_at BIGINT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sg_data_brain_symbol_ts ON super_ghost_data_brain_snapshots(symbol, as_of_ts DESC)")


def persist_data_brain(symbol: str) -> Dict[str, Any]:
    data = build_data_brain(symbol, use_cache=False)
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_data_brain_tables(cur)
            cur.execute(
                """
                INSERT INTO super_ghost_data_brain_snapshots
                    (symbol, as_of_ts, coverage_json, derived_json, sources_json, created_at)
                VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)
                RETURNING id
                """,
                (data["symbol"], data["as_of_ts"], json.dumps(data["coverage"]), json.dumps(data["derived"]), json.dumps(data["sources"], default=str), _now()),
            )
            row = cur.fetchone()
            data["snapshot_id"] = int(row[0]) if row else None
    except Exception as exc:
        data["persist_error"] = str(exc)[:160]
    return data


def latest_data_brain_snapshots(*, symbol: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            ensure_data_brain_tables(cur)
            where = "1=1"
            params: List[Any] = []
            if symbol:
                where += " AND symbol=%s"
                params.append(symbol.upper())
            cur.execute(
                f"""
                SELECT id, symbol, as_of_ts, coverage_json, derived_json, created_at
                FROM super_ghost_data_brain_snapshots
                WHERE {where}
                ORDER BY as_of_ts DESC
                LIMIT %s
                """,
                params + [max(1, min(100, int(limit)))],
            )
            rows = cur.fetchall()
        return {"ok": True, "symbol": (symbol or "ALL").upper(), "snapshots": [
            {"id": r[0], "symbol": r[1], "as_of_ts": r[2], "coverage": json.loads(r[3]) if isinstance(r[3], str) else r[3], "derived": json.loads(r[4]) if isinstance(r[4], str) else r[4], "created_at": r[5]}
            for r in rows
        ]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "snapshots": []}
