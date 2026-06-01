#!/usr/bin/env python3
"""Poll production for new WOLF resolves and append to docs/wolf_calibration.md.

Usage:
  python3 scripts/watch_wolf_calibration.py          # one check
  python3 scripts/watch_wolf_calibration.py --loop 900  # every 15m

State: docs/wolf_calibration.state.json (last_logged_id).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SHEET = ROOT / "docs" / "wolf_calibration.md"
STATE = ROOT / "docs" / "wolf_calibration.state.json"
V32_MIN_ID = 223438
CT = ZoneInfo("America/Chicago")
BASE = os.getenv("BASE_URL", "https://ghost-protocol-v2-production.up.railway.app").rstrip("/")
PENDING_MARKER = "| _pending_ |"


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _ts_ct(ts) -> str:
    if not ts:
        return "—"
    try:
        t = float(ts)
        if t > 1e12:
            t /= 1000.0
        return datetime.fromtimestamp(t, tz=CT).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _pct(v) -> str:
    if v is None:
        return "0"
    x = float(v)
    return ("+" if x >= 0 else "") + format(x, ".2f") + "%"


def _price(v) -> str:
    if v is None:
        return "—"
    return format(float(v), ".2f")


def _load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"last_logged_id": 224034}


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2) + "\n")


def _fetch_resolved() -> list:
    data = _get("/api/history?limit=80")
    trades = data.get("trades") or data.get("rows") or []
    out = []
    for t in trades:
        pid = int(t.get("id") or 0)
        if pid < V32_MIN_ID:
            continue
        oc = t.get("outcome")
        if not oc:
            continue
        out.append(t)
    out.sort(key=lambda x: int(x["id"]))
    return out


def _row(t: dict) -> str:
    conf = int(round(float(t.get("confidence") or 0) * 100))
    oc = str(t.get("outcome") or "").upper()
    counts = "yes" if oc in ("WIN", "LOSS") else "no"
    notes = ""
    if oc == "EXPIRED":
        notes = "no outcome fill"
    return (
        f"| {t['id']} | {_ts_ct(t.get('predicted_at'))} | — | {oc} | {conf}% | "
        f"{_pct(t.get('pnl_pct'))} | {_price(t.get('entry_price'))} | {_price(t.get('exit_price'))} | "
        f"{_price(t.get('target_price'))} | {_price(t.get('stop_price'))} | {counts} | {notes} |"
    )


def _append_rows(rows: list[str]) -> None:
    text = SHEET.read_text()
    if PENDING_MARKER not in text:
        raise SystemExit("calibration sheet missing pending marker row")
    pending_tail = (
        PENDING_MARKER
        + " — | — | — | — | — | — | — | — | — | — | — | awaiting next resolve |"
    )
    block = "\n".join(rows) + "\n" + pending_tail + "\n"
    text = text.replace(
        PENDING_MARKER
        + " — | — | — | — | — | — | — | — | — | — | — | log on first post-cooldown resolve |",
        block,
    )
    text = re.sub(
        r"\| Last logged pick \| `(\d+)` \|",
        f"| Last logged pick | `{max(int(r.split('|')[1].strip()) for r in rows)}` |",
        text,
        count=1,
    )
    # refresh WIN/LOSS count from sheet rows with counts_n yes
    wins = losses = 0
    for line in text.splitlines():
        if not line.startswith("|") or "pick_id" in line or "---" in line or "_pending_" in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 12:
            continue
        try:
            int(parts[1])
        except ValueError:
            continue
        if parts[10] == "yes":
            if parts[4] == "WIN":
                wins += 1
            elif parts[4] == "LOSS":
                losses += 1
    tot = wins + losses
    wr_line = f"| WIN/LOSS toward n≥8 | **{wins}W / {losses}L** ({tot} of 8)"
    if tot >= 8:
        wr_line += " — calibration count met; keep logging for drift |"
    else:
        wr_line += f" — need {8 - tot} more WIN/LOSS |"
    text = re.sub(
        r"\| WIN/LOSS toward n≥8 \|[^\n]+\|",
        wr_line,
        text,
        count=1,
    )
    SHEET.write_text(text)


def run_once() -> int:
    st = _load_state()
    last = int(st.get("last_logged_id") or 0)
    new = [t for t in _fetch_resolved() if int(t["id"]) > last]
    if not new:
        print(f"no new resolves (watermark={last})")
        return 0
    rows = [_row(t) for t in new]
    _append_rows(rows)
    st["last_logged_id"] = max(int(t["id"]) for t in new)
    st["logged_at"] = int(time.time())
    _save_state(st)
    for t in new:
        print(f"logged pick {t['id']} {t.get('outcome')} pnl={t.get('pnl_pct')}")
    return len(new)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="poll interval")
    args = ap.parse_args()
    if args.loop:
        print(f"watching {BASE} every {args.loop}s → {SHEET}")
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"error: {e}", file=sys.stderr)
            time.sleep(args.loop)
    return 0 if run_once() >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
