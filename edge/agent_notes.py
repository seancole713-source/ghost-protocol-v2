"""Append-only notes written by the scheduled AI agents.

The briefer, watchdog, evening reporter, miss investigator and research
scientist each start from nothing on every run. What one run learns, the next
reads here -- and so does the operator. A note is written once and never
edited or deleted: an agent that changes its mind writes a new note.

Notes are commentary, never evidence. Nothing in the ledger, the card or any
gate reads them.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from edge.contracts import ET

KINDS = ("brief", "report", "watchdog", "miss_investigation", "hypothesis", "scorecard")
MAX_BODY, MAX_AUTHOR = 8000, 64
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class NoteError(ValueError):
    pass


def write(store, *, kind: str, author: str, body: str, now: int, day: Optional[str] = None,
          symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    kind, author, body = str(kind or "").strip(), str(author or "").strip(), str(body or "").strip()
    if kind not in KINDS:
        raise NoteError(f"kind must be one of {KINDS}")
    if not author or len(author) > MAX_AUTHOR:
        raise NoteError(f"author is required, at most {MAX_AUTHOR} characters")
    if not body:
        raise NoteError("body is required")
    if len(body) > MAX_BODY:
        raise NoteError(f"body is at most {MAX_BODY} characters")
    day = (day or datetime.fromtimestamp(now, tz=ET).date().isoformat()).strip()
    if not _DAY.match(day):
        raise NoteError("day must be YYYY-MM-DD")
    syms = sorted({str(s).strip().upper() for s in symbols or [] if str(s).strip()})[:50]
    digest = hashlib.sha256(f"{kind}|{author}|{body}".encode()).hexdigest()[:10]
    key = f"{day}|{kind}|{now}|{digest}"
    if store.get("edge_notes", key):
        raise NoteError("that note already exists; notes are never overwritten")
    rec = {"id": key, "day": day, "kind": kind, "author": author, "body": body, "symbols": syms,
           "written_at": now}
    store.put("edge_notes", key, rec)
    return {"status": "written", "id": key}


def recent(store, *, day: Optional[str] = None, kind: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    where = {k: v for k, v in (("day", day), ("kind", kind)) if v}
    rows = sorted(store.scan("edge_notes", **where), key=lambda r: r["written_at"], reverse=True)
    return {"notes": rows[:max(1, min(int(limit), 100))], "total": len(rows),
            "label": "agent commentary, never evidence: no ledger, card or gate reads these"}
