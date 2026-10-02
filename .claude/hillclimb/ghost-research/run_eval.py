#!/usr/bin/env python3
"""Runner for the Ghost pre-card research eval.

Calls the REAL entry point (edge.research_worker.research_symbol) with the production prompts, model and
tools, a fresh in-memory store per (case, rep), and the Claude reviewer (http=None). Nothing is
reimplemented; a recording wrapper around the client captures the transcript, usage and served model.

    python run_eval.py --dry-run oracle            # no network, no spend: stub client
    python run_eval.py --variant baseline --reps 2 # LIVE: spends money, needs ANTHROPIC_API_KEY and --approve-harness once

Output (per the eval skill's contract): <out>/results.jsonl, <out>/traces/<id>_rep<k>.json, <out>/errors.jsonl.
Attempts that never produced a scorable answer go to errors.jsonl, never to results.jsonl.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import random
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from edge import research_worker as W  # noqa: E402
from edge.ledger import MemoryStore  # noqa: E402
import grading as G  # noqa: E402

AUTHOR_MARK, REVIEWER_MARK = "You research one stock", "You check another researcher's"
_lock = threading.Lock()


class ServedModelMismatch(Exception):
    pass


class Recorder:
    """Wraps a client: records every request/response, asserts the served model is the requested one."""

    def __init__(self, inner, *, allow_missing_model: bool = False):
        self.inner, self.calls, self.allow_missing = inner, [], allow_missing_model
        self.mismatch: Optional[str] = None   # the worker swallows client exceptions, so remember this one
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        t0 = time.monotonic()
        resp = self.inner.beta.messages.create(**kw)
        served = str(getattr(resp, "model", "") or "")
        if served != kw["model"] and not (self.allow_missing and not served):
            self.mismatch = f"asked {kw['model']}, served {served or '<none>'}"
            raise ServedModelMismatch(self.mismatch)
        first = kw["messages"][0]["content"]
        role = "author" if str(first).startswith(AUTHOR_MARK) else "reviewer" if str(first).startswith(REVIEWER_MARK) else "other"
        self.calls.append({"role": role, "resp": resp, "latency_s": time.monotonic() - t0,
                           "prompt": first if len(self.calls) == 0 or role != "other" else None})
        return resp


def _usage_totals(calls) -> Dict[str, int]:
    t = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    for c in calls:
        u = getattr(c["resp"], "usage", None)
        for k in t:
            t[k] += int(getattr(u, k, 0) or 0)
    return t


def _searches(calls) -> int:
    n = 0
    for c in calls:
        stu = getattr(getattr(c["resp"], "usage", None), "server_tool_use", None)
        n += int(getattr(stu, "web_search_requests", 0) or 0)
    return n


def _trace(calls) -> List[Dict[str, Any]]:
    turns: List[Dict[str, Any]] = []
    for c in calls:
        if c["prompt"]:
            turns.append({"role": "user", "content": f"[{c['role']}]\n{c['prompt']}"})
        for b in getattr(c["resp"], "content", None) or []:
            kind = getattr(b, "type", "")
            if kind == "text":
                turns.append({"role": "assistant", "content": getattr(b, "text", "")})
            elif kind == "server_tool_use":
                turns.append({"role": "tool_call", "name": str(getattr(b, "name", "")),
                              "content": json.dumps(getattr(b, "input", {}), indent=2, default=str)})
            elif kind.endswith("_tool_result"):
                turns.append({"role": "tool_result", "content": json.dumps(getattr(b, "content", None), default=str)[:4000]})
    return turns


def _author_raw(calls) -> Optional[Dict[str, Any]]:
    text = None
    for c in calls:
        if c["role"] == "author" and getattr(c["resp"], "stop_reason", "") != "pause_turn":
            text = W._text(c["resp"]) or text
    return W._json(text)


def run_case(case: Dict[str, Any], rep: int, client_factory, *, allow_missing_model: bool) -> Dict[str, Any]:
    """-> {"row": {...}, "trace": [...]} on a scorable result, or {"error": {...}}."""
    rec_client = Recorder(client_factory(case), allow_missing_model=allow_missing_model)
    store = MemoryStore()                                   # fresh state per (case, rep)
    now = G.cutoff_epoch(case["cutoff_et"])
    base = {"prompt_id": case["id"], "rep": rep}
    t0 = time.monotonic()
    try:
        out = W.research_symbol(rec_client, store, symbol=case["symbol"], day=case["day"], now=now, http=None)
    except ServedModelMismatch as exc:
        return {"error": {**base, "failure_class": "served_model_mismatch", "detail": str(exc),
                          "usage": _usage_totals(rec_client.calls)}}
    except Exception as exc:  # noqa: BLE001
        return {"error": {**base, "failure_class": "harness_error", "detail": f"{type(exc).__name__}: {exc}",
                          "usage": _usage_totals(rec_client.calls)}}
    latency = time.monotonic() - t0
    if rec_client.mismatch:
        return {"error": {**base, "failure_class": "served_model_mismatch", "detail": rec_client.mismatch,
                          "usage": _usage_totals(rec_client.calls)}}
    rec = store.get("edge_research", f"{case['day']}|{case['symbol'].upper()}") or {}
    usage, searches = _usage_totals(rec_client.calls), _searches(rec_client.calls)
    if rec.get("author_stop") == "refusal":
        failure = "refusal"                                  # a graded outcome, not plumbing
    elif rec.get("status") == W.NOT_RESEARCHED:
        # The worker could not look (failed call, dead search tool, unusable output): not a "no catalyst".
        return {"error": {**base, "failure_class": "harness_error",
                          "detail": str(rec.get("not_researched_reason")), "usage": usage, "web_searches": searches}}
    else:
        failure = None
    # issued_at after everything the worker wrote: the card never reads hindsight, but grading may.
    verdict = W.verdict(store, day=case["day"], symbol=case["symbol"], issued_at=now + 6 * 3600)
    graded = G.grade_case(case, rec=rec, verdict=verdict, author_raw=_author_raw(rec_client.calls))
    if failure == "refusal":
        graded["grade"]["catalyst_correct"] = 0
        graded["meta"]["outcome"] = "unknown"
    stop = str(rec.get("author_stop") or "")
    row = {**base, "prompt": f"Research {case['symbol']} as of {case['cutoff_et']} ET ({case['day']}).",
           "tags": case["tags"], "status": "truncated" if stop == "max_tokens" else "ok", "stop_reason": stop,
           "model": W.MODEL, "grade": graded["grade"], "latency_s": round(latency, 2), "web_searches": searches,
           "usage": usage,
           "meta": {**graded["meta"], "expected_catalyst": case["expected"]["catalyst"], "failure_class": failure,
                    "worker_cost_usd": rec.get("cost_usd"), "gold_source": case["gold_source"],
                    "gold_verified": case["gold_verified"], "reviewer": rec.get("reviewer")}}
    row["cost_usd"] = rec.get("cost_usd")
    return {"row": row, "trace": _trace(rec_client.calls)}


# ---------- dry-run stubs: no network ----------
def _reply(text: str, stop: str = "end_turn"):
    return NS(model=W.MODEL, stop_reason=stop, content=[NS(type="text", text=text)],
              usage=NS(input_tokens=12000, output_tokens=900, cache_creation_input_tokens=0,
                       cache_read_input_tokens=0, server_tool_use=NS(web_search_requests=3)))


class _Stub:
    def __init__(self, replies):
        self.replies = list(replies)
        self.beta = NS(messages=NS(create=lambda **kw: self.replies.pop(0)))


def stub_factory(mode: str):
    def make(case):
        cut = G.cutoff_epoch(case["cutoff_et"])
        iso = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S-04:00", time.gmtime(cut - s - 4 * 3600))  # noqa: E731
        if mode == "empty":
            return _Stub([_reply("")] * 4)                    # no answer at all
        if mode == "constant_none" or case["expected"]["catalyst"] is not True:
            return _Stub([_reply(json.dumps({"claims": [], "unknowns": ["no company-specific event inside the window"]}))])
        claim = {"kind": case["oracle_kind"] or "contract", "statement": f"{case['symbol']} announced a dated event.",
                 "value": None, "unit": None, "unknowns": [],
                 "citations": [{"url": "https://www.sec.gov/a", "quote": "q1", "published_at": iso(3600)},
                               {"url": "https://www.reuters.com/b", "quote": "q2", "published_at": iso(7200)}]}
        review = {"entity_ok": True, "contradictions": [], "dilution_found": False, "stale": False, "notes": ""}
        return _Stub([_reply(json.dumps({"claims": [claim], "unknowns": []})), _reply(json.dumps(review))])
    return make


# ---------- harness integrity gate ----------
def harness_sha(state: Dict[str, Any]) -> str:
    h = hashlib.sha256()
    for rel in sorted(state.get("harness_paths") or []):
        p = (HERE / rel).resolve()
        h.update(rel.encode())
        h.update(p.read_bytes() if p.exists() else b"<missing>")
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="baseline")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--timeout-s", type=float, default=420.0, help="hard per-case wall-clock ceiling")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--ids", default="", help="comma-separated case ids; default all scored cases")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", choices=["oracle", "empty", "constant_none"], help="stub client, no network, no spend")
    ap.add_argument("--out", default="")
    ap.add_argument("--approve-harness", action="store_true", help="operator-only: record the harness sha")
    a = ap.parse_args(argv)

    state = json.loads((HERE / "_state.json").read_text())
    cases = [json.loads(x) for x in (HERE / "cases.jsonl").read_text().splitlines() if x.strip()]
    cases = [c for c in cases if c["scored"] and c["cutoff_et"] and c["expected"]["catalyst"] is not None]
    if a.ids:
        want = set(a.ids.split(","))
        cases = [c for c in cases if c["id"] in want]
    if a.limit:
        cases = cases[:a.limit]

    if a.dry_run:
        out = Path(a.out or HERE / "_dryrun" / a.dry_run)
        factory, allow_missing = stub_factory(a.dry_run), False
    else:
        if not (os.getenv("ANTHROPIC_API_KEY") or "").strip():
            print("ANTHROPIC_API_KEY is not set: a live run cannot start (no spend happened).", file=sys.stderr)
            return 3
        sha_file, sha = HERE / ".harness_sha", harness_sha(state)
        if a.approve_harness:
            sha_file.write_text(sha)
        elif not sha_file.exists() or sha_file.read_text().strip() != sha:
            print("Harness not approved (or changed since approval). The operator runs once with --approve-harness.", file=sys.stderr)
            return 2
        out = Path(a.out or HERE / a.variant)
        os.environ.setdefault("EDGE_RESEARCH_DAILY_USD", "1000")      # per-case stores are fresh; no shared cap
        client = W._client()
        factory, allow_missing = (lambda case: client), False

    (out / "traces").mkdir(parents=True, exist_ok=True)
    res_p, err_p = out / "results.jsonl", out / "errors.jsonl"
    done = set()
    if res_p.exists():
        for line in res_p.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["prompt_id"], r["rep"]))
    todo = [(c, k) for k in range(a.reps) for c in cases if (c["id"], k) not in done]
    print(f"{len(cases)} cases x {a.reps} reps; {len(todo)} to run, {len(done)} already done -> {out}")

    def one(item):
        case, rep = item
        for attempt in range(4):                     # jittered backoff on 429 / overloaded, retries counted
            try:
                r = run_case(case, rep, factory, allow_missing_model=allow_missing)
            except Exception as exc:  # noqa: BLE001
                r = {"error": {"prompt_id": case["id"], "rep": rep, "failure_class": "harness_error", "detail": repr(exc)}}
            det = str((r.get("error") or {}).get("detail", ""))
            if "429" in det or "overloaded" in det.lower():
                time.sleep(min(60, 2 ** attempt * 5) * (0.5 + random.random()))
                continue
            r.setdefault("attempts", attempt + 1)
            return r
        return r

    ex = cf.ThreadPoolExecutor(max_workers=a.concurrency)
    futs = {ex.submit(one, it): it for it in todo}
    try:
        for fut in cf.as_completed(futs):
            case, rep = futs[fut]
            try:
                r = fut.result(timeout=a.timeout_s)
            except cf.TimeoutError:
                r = {"error": {"prompt_id": case["id"], "rep": rep, "failure_class": "timeout",
                               "detail": f"exceeded {a.timeout_s}s wall clock (the call may still be running and billed)"}}
            with _lock:
                if "row" in r:
                    r["row"]["attempts"] = r.get("attempts", 1)
                    (out / "traces" / f"{case['id']}_rep{rep}.json").write_text(json.dumps(r["trace"], indent=1))
                    with res_p.open("a") as f:
                        f.write(json.dumps(r["row"]) + "\n")
                else:
                    with err_p.open("a") as f:
                        f.write(json.dumps(r["error"]) + "\n")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    rows = [json.loads(x) for x in res_p.read_text().splitlines() if x.strip()] if res_p.exists() else []
    nerr = sum(1 for x in err_p.read_text().splitlines() if x.strip()) if err_p.exists() else 0
    s = G.summarize(rows, nerr)
    (out / "summary.json").write_text(json.dumps(s, indent=1))
    print(json.dumps(s, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
