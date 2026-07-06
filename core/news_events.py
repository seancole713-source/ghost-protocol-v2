"""core/news_events.py — structured news events (PR #134, merged Fugu+Claude plan).

Turns raw headlines into typed, timestamped, deduplicated events Ghost can
reason over — the difference between "SPCE headline exists" and "SPCE filed a
dilution event at asof_ts T".

Guardrail contract (Phase 0 of the plan — binding):
  1. Events are point-in-time: asof_ts is the article's published time, and no
     consumer may use an event whose asof_ts is after its own decision time.
  2. Duplicates count once (dedupe keys on both articles and events).
  3. Classification here is DETERMINISTIC rules only — fast, cheap, testable.
     An optional LLM layer may be added later; it classifies events, never
     predicts stocks, and never invents facts.
  4. Missing/stale news is "news unavailable", never "neutral success".
  5. Nothing in this module touches production predictions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.news_events")

# ── Event taxonomy: deterministic rules ─────────────────────────────────────
# (event_type, direction_hint, materiality 0-1, [regex patterns])
# Patterns match against lowercase "headline + summary". Order matters: first
# match per event_type wins; an article can emit multiple distinct event types.
_RULES: List[tuple] = [
    ("going_concern", "bearish", 0.95, [r"going concern"]),
    ("bankruptcy_risk", "bearish", 0.95, [r"chapter 11", r"\bbankruptcy\b"]),
    ("dilution_or_offering", "bearish", 0.90, [
        r"registered direct", r"at-the-market", r"\batm program\b",
        r"share(s)? offering", r"stock offering", r"public offering",
        r"shelf registration", r"secondary offering", r"\bdilut\w+",
        r"repay .{0,40}(debt|notes) .{0,40}(stock|shares|equity)",
        r"convert\w* .{0,30}(notes|debt) .{0,30}(equity|stock|shares)",
    ]),
    ("delisting_notice", "bearish", 0.85, [
        r"delisting", r"listing deficiency", r"non-?compliance with (nasdaq|nyse)",
    ]),
    ("reverse_split", "bearish", 0.75, [r"reverse (stock )?split"]),
    ("fda_rejection", "bearish", 0.90, [
        r"complete response letter", r"\bcrl\b", r"fda (declines|rejects|denies)",
        r"clinical hold",
    ]),
    ("fda_approval", "bullish", 0.90, [
        r"fda approv\w+", r"fda clearance", r"fda grants",
    ]),
    ("guidance_cut", "bearish", 0.85, [
        r"(cuts|lowers|slashes|withdraws|trims) .{0,30}(guidance|outlook|forecast)",
        r"guidance .{0,20}(cut|lowered|withdrawn)",
    ]),
    ("guidance_raise", "bullish", 0.80, [
        r"(raises|boosts|lifts|hikes) .{0,30}(guidance|outlook|forecast)",
        r"guidance .{0,20}raised",
    ]),
    ("earnings_miss", "bearish", 0.75, [
        r"miss\w* .{0,25}(estimates|expectations)", r"earnings miss",
        r"falls short of .{0,20}estimates",
    ]),
    ("earnings_beat", "bullish", 0.70, [
        r"beat\w* .{0,25}(estimates|expectations)", r"tops .{0,20}estimates",
        r"earnings beat",
    ]),
    ("mna_confirmed", "bullish", 0.90, [
        r"agrees to (be )?acquir\w+", r"to be acquired", r"merger agreement",
        r"definitive agreement to acquire", r"buyout agreement",
    ]),
    ("mna_rumor", "bullish", 0.70, [
        r"in talks to (be )?acquir\w+", r"exploring (a )?sale",
        r"strategic alternatives", r"takeover (talks|interest|bid)",
        r"acquisition talks", r"weighs sale",
    ]),
    ("short_report", "bearish", 0.80, [
        r"short[- ]seller report", r"short report", r"(hindenburg|muddy waters|citron)",
    ]),
    ("analyst_downgrade", "bearish", 0.60, [
        r"downgrad\w+ to", r"downgrades? \w+", r"(cuts|lowers) .{0,25}price target",
    ]),
    ("analyst_upgrade", "bullish", 0.60, [
        r"upgrad\w+ to (buy|overweight|outperform)", r"analyst upgrades?",
        r"(raises|lifts|hikes) .{0,25}price target", r"initiat\w+ .{0,25}with (a )?buy",
    ]),
    ("contract_award", "bullish", 0.70, [
        r"(awarded|wins|secures) .{0,30}(contract|order)", r"contract award",
    ]),
    ("officer_change", "bearish", 0.65, [
        r"(ceo|cfo|coo) (resigns|steps down|departs|exits)",
        r"(resigns|steps down) as (ceo|cfo|coo)",
    ]),
]
_COMPILED = [(et, d, m, [re.compile(p) for p in pats]) for et, d, m, pats in _RULES]

# Source reliability: filings > wire services > aggregators (0-1).
_SOURCE_RELIABILITY = {
    "sec": 0.98, "sec.gov": 0.98, "businesswire": 0.9, "prnewswire": 0.9,
    "globenewswire": 0.9, "reuters": 0.9, "bloomberg": 0.9, "dow jones": 0.85,
    "benzinga": 0.7, "marketwatch": 0.75, "seekingalpha": 0.6, "seeking alpha": 0.6,
}
_RUMOR_TYPES = {"mna_rumor", "short_report"}


def _source_reliability(source: str) -> float:
    s = (source or "").strip().lower()
    for k, v in _SOURCE_RELIABILITY.items():
        if k in s:
            return v
    return 0.6


def article_dedupe_key(symbol: str, headline: str) -> str:
    norm = re.sub(r"[^a-z0-9 ]", "", (headline or "").lower()).strip()
    norm = re.sub(r"\s+", " ", norm)
    return hashlib.sha1(f"{symbol.upper()}|{norm}".encode()).hexdigest()


def event_dedupe_key(symbol: str, event_type: str, asof_ts: int) -> str:
    # One event of a given type per symbol per calendar day: re-worded copies
    # of the same story must not count as multiple confirmations.
    day = int(asof_ts) // 86400
    return hashlib.sha1(f"{symbol.upper()}|{event_type}|{day}".encode()).hexdigest()


def classify_text(headline: str, summary: str = "") -> List[Dict[str, Any]]:
    """Deterministic event extraction. Pure function — no I/O, no LLM."""
    text = f"{headline or ''} {summary or ''}".lower()
    if not text.strip():
        return []
    out = []
    for event_type, direction, materiality, patterns in _COMPILED:
        for pat in patterns:
            m = pat.search(text)
            if m:
                out.append({
                    "event_type": event_type,
                    "direction_hint": direction,
                    "materiality": materiality,
                    "confirmation_status": "rumor" if event_type in _RUMOR_TYPES else "reported",
                    "evidence": m.group(0)[:120],
                })
                break
    return out


# ── Storage ──────────────────────────────────────────────────────────────────

def ensure_news_tables(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_news_raw_articles (
            id SERIAL PRIMARY KEY,
            provider TEXT NOT NULL,
            provider_article_id TEXT,
            symbol VARCHAR(20) NOT NULL,
            headline TEXT NOT NULL,
            summary TEXT,
            url TEXT,
            source TEXT,
            published_at BIGINT NOT NULL,
            ingested_at BIGINT NOT NULL,
            dedupe_key TEXT UNIQUE NOT NULL,
            raw_json JSONB
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ghost_news_events (
            id SERIAL PRIMARY KEY,
            article_id INT,
            symbol VARCHAR(20) NOT NULL,
            event_type TEXT NOT NULL,
            direction_hint TEXT,
            materiality FLOAT,
            confidence FLOAT,
            confirmation_status TEXT,
            source_reliability FLOAT,
            evidence TEXT,
            asof_ts BIGINT NOT NULL,
            extracted_at BIGINT NOT NULL,
            dedupe_key TEXT UNIQUE NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_news_events_sym_ts ON ghost_news_events(symbol, asof_ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_news_raw_sym_ts ON ghost_news_raw_articles(symbol, published_at DESC)")


def store_article_and_events(cur, art: Dict[str, Any]) -> Dict[str, Any]:
    """Insert one normalized article + its extracted events. Dedupe-safe.

    `art` shape: provider, provider_article_id, symbol, headline, summary,
    url, source, published_at (epoch s), raw (dict).
    Returns {"article_stored": bool, "events_stored": int}.
    """
    sym = (art.get("symbol") or "").upper()
    headline = art.get("headline") or ""
    published_at = int(art.get("published_at") or 0)
    if not sym or not headline or published_at <= 0:
        return {"article_stored": False, "events_stored": 0}
    now = int(time.time())
    dk = article_dedupe_key(sym, headline)
    cur.execute(
        """INSERT INTO ghost_news_raw_articles
           (provider, provider_article_id, symbol, headline, summary, url, source,
            published_at, ingested_at, dedupe_key, raw_json)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
        (art.get("provider") or "unknown", str(art.get("provider_article_id") or ""),
         sym, headline[:1000], (art.get("summary") or "")[:4000],
         (art.get("url") or "")[:1000], (art.get("source") or "")[:200],
         published_at, now, dk, json.dumps(art.get("raw") or {}, default=str)[:20000]),
    )
    row = cur.fetchone()
    if row is None:
        return {"article_stored": False, "events_stored": 0}  # duplicate
    article_id = row[0]
    stored = 0
    rel = _source_reliability(art.get("source") or "")
    for ev in classify_text(headline, art.get("summary") or ""):
        edk = event_dedupe_key(sym, ev["event_type"], published_at)
        cur.execute(
            """INSERT INTO ghost_news_events
               (article_id, symbol, event_type, direction_hint, materiality,
                confidence, confirmation_status, source_reliability, evidence,
                asof_ts, extracted_at, dedupe_key)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (dedupe_key) DO NOTHING""",
            (article_id, sym, ev["event_type"], ev["direction_hint"],
             ev["materiality"], round(ev["materiality"] * rel, 3),
             ev["confirmation_status"], rel, ev["evidence"],
             published_at, now, edk),
        )
        stored += cur.rowcount
    return {"article_stored": True, "events_stored": stored}


def recent_events_for_symbol(symbol: str, *, asof_ts: Optional[int] = None,
                             lookback_s: int = 7 * 86400,
                             cur=None) -> List[Dict[str, Any]]:
    """Events for `symbol` with asof_ts in (asof-lookback, asof].

    Point-in-time by construction: pass the decision timestamp as asof_ts and
    it is impossible to see a later headline. Returns [] on any failure —
    callers must distinguish that via news_available().
    """
    asof = int(asof_ts or time.time())
    sql = """SELECT event_type, direction_hint, materiality, confidence,
                    confirmation_status, source_reliability, evidence, asof_ts
             FROM ghost_news_events
             WHERE symbol=%s AND asof_ts > %s AND asof_ts <= %s
             ORDER BY materiality DESC, asof_ts DESC LIMIT 50"""
    args = (symbol.upper(), asof - int(lookback_s), asof)
    try:
        if cur is not None:
            cur.execute(sql, args)
            rows = cur.fetchall()
        else:
            from core.db import db_conn
            with db_conn() as conn:
                c = conn.cursor()
                ensure_news_tables(c)
                c.execute(sql, args)
                rows = c.fetchall()
    except Exception as exc:
        LOGGER.warning("recent_events_for_symbol(%s): %s", symbol, str(exc)[:120])
        return []
    keys = ("event_type", "direction_hint", "materiality", "confidence",
            "confirmation_status", "source_reliability", "evidence", "asof_ts")
    return [dict(zip(keys, r)) for r in rows]


def news_available(*, max_stale_s: int = 24 * 3600, cur=None) -> bool:
    """True when ingestion has stored ANY article recently. Guardrail #4:
    a dead feed must read as unavailable, not as 'no news is good news'."""
    try:
        sql = "SELECT MAX(ingested_at) FROM ghost_news_raw_articles"
        if cur is not None:
            cur.execute(sql)
            row = cur.fetchone()
        else:
            from core.db import db_conn
            with db_conn() as conn:
                c = conn.cursor()
                ensure_news_tables(c)
                c.execute(sql)
                row = c.fetchone()
        latest = row[0] if row else None
        return bool(latest and (time.time() - int(latest)) < max_stale_s)
    except Exception:
        return False
