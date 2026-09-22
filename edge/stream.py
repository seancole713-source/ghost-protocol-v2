"""Streaming market data (Alpaca v2 websocket) -- the fast path for when it pays.

On the free plan the stream is IEX-only, one exchange's trades, and the radar's
5-minute polling sees nearly as much. With consolidated (SIP) data purchased,
set EDGE_STREAM_FEED=sip and the stream becomes the low-latency source. Off
unless EDGE_STREAM_ENABLED is set.

Protocol (Alpaca market data v2): connect wss://stream.data.alpaca.markets/v2/<feed>
-> [{"T":"success","msg":"connected"}]; send {"action":"auth","key","secret"}
-> [{"T":"success","msg":"authenticated"}]; send {"action":"subscribe","trades":[..],"bars":[..]}.
Messages arrive as JSON arrays: trades {"T":"t","S","p","s","t"}, bars
{"T":"b","S","o","h","l","c","v","t"}, errors {"T":"error","code","msg"}.

Alpaca allows a limited number of concurrent data connections per account.
Error 406 ("connection limit exceeded") therefore STOPS the stream and records
why -- it never retries into a fight with another consumer of the same account.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

URL = "wss://stream.data.alpaca.markets/v2/{feed}"
FATAL_CODES = {401, 402, 404, 406, 409}   # auth failed / not authorized / limit / insufficient sub


def _epoch(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class StreamState:
    """What the stream has seen. Thread-safe reads for the radar."""
    feed: str = "iex"
    connected: bool = False
    authenticated: bool = False
    stopped_reason: Optional[str] = None
    last_msg_at: Optional[float] = None
    last_trade: Dict[str, Dict[str, float]] = field(default_factory=dict)
    minute_bars: Dict[str, Dict[int, List[float]]] = field(default_factory=dict)   # sym -> {minute_ts: [o,h,l,c,v]}
    subscribed: Set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def handle(self, raw: str, *, now: Optional[float] = None) -> List[str]:
        """Apply one websocket frame. Returns actions for the connection: 'auth', 'stop'."""
        actions: List[str] = []
        try:
            msgs = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return actions
        if isinstance(msgs, dict):
            msgs = [msgs]
        with self._lock:
            self.last_msg_at = now if now is not None else time.time()
            for m in msgs:
                t = m.get("T")
                if t == "success" and m.get("msg") == "connected":
                    self.connected = True
                    actions.append("auth")
                elif t == "success" and m.get("msg") == "authenticated":
                    self.authenticated = True
                elif t == "error":
                    code = int(m.get("code") or 0)
                    if code in FATAL_CODES:
                        self.stopped_reason = f"{code} {m.get('msg')}"
                        actions.append("stop")
                elif t == "subscription":
                    self.subscribed = set(m.get("trades") or []) | set(m.get("bars") or [])
                elif t == "t":
                    ts = _epoch(m.get("t"))
                    sym, px, sz = m.get("S"), m.get("p"), m.get("s") or 0
                    if sym and px is not None and ts is not None:
                        self.last_trade[sym] = {"price": float(px), "ts": ts}
                        minute = int(ts // 60 * 60)
                        bar = self.minute_bars.setdefault(sym, {}).get(minute)
                        if bar is None:
                            self.minute_bars[sym][minute] = [float(px), float(px), float(px), float(px), float(sz)]
                        else:
                            bar[1], bar[2] = max(bar[1], px), min(bar[2], px)
                            bar[3], bar[4] = float(px), bar[4] + float(sz)
                elif t == "b":
                    ts = _epoch(m.get("t"))
                    sym = m.get("S")
                    if sym and ts is not None:
                        self.minute_bars.setdefault(sym, {})[int(ts)] = [
                            float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"]), float(m.get("v") or 0)]
        return actions

    def price(self, sym: str, *, max_age_s: float = 120.0, now: Optional[float] = None) -> Optional[Dict[str, float]]:
        with self._lock:
            t = self.last_trade.get(sym.upper())
        now = now if now is not None else time.time()
        return dict(t) if t and now - t["ts"] <= max_age_s else None

    def health(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = now if now is not None else time.time()
        return {"feed": self.feed, "connected": self.connected, "authenticated": self.authenticated,
                "stopped": self.stopped_reason, "symbols": len(self.subscribed),
                "idle_s": (now - self.last_msg_at) if self.last_msg_at else None}


class Streamer:
    """Runs the websocket in a daemon thread with capped exponential backoff."""

    def __init__(self, state: StreamState, *, key: str, secret: str) -> None:
        self.state, self.key, self.secret = state, key, secret
        self.want: Set[str] = set()
        self._thread: Optional[threading.Thread] = None

    def subscribe(self, symbols) -> None:
        self.want = {s.upper() for s in symbols}

    def backoff(self, attempt: int) -> float:
        return min(300.0, 2.0 ** min(attempt, 8))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="edge-stream", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import asyncio
        attempt = 0
        while self.state.stopped_reason is None:
            try:
                asyncio.run(self._session())
                attempt = 0
            except Exception:  # noqa: BLE001 - reconnect with backoff, never crash the host process
                self.state.connected = self.state.authenticated = False
                time.sleep(self.backoff(attempt))
                attempt += 1

    async def _session(self) -> None:
        import websockets
        async with websockets.connect(URL.format(feed=self.state.feed), ping_interval=20) as ws:
            sent: Set[str] = set()
            while self.state.stopped_reason is None:
                try:
                    import asyncio
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    for action in self.state.handle(raw):
                        if action == "auth":
                            await ws.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
                        elif action == "stop":
                            return
                except Exception as exc:  # timeout: fall through to (re)subscribe
                    if type(exc).__name__ not in ("TimeoutError", "CancelledError"):
                        raise
                if self.state.authenticated and self.want != sent:
                    add, drop = self.want - sent, sent - self.want
                    if drop:
                        await ws.send(json.dumps({"action": "unsubscribe", "trades": sorted(drop), "bars": sorted(drop)}))
                    if add:
                        await ws.send(json.dumps({"action": "subscribe", "trades": sorted(add), "bars": sorted(add)}))
                    sent = set(self.want)


_SINGLETON: Dict[str, Any] = {}


def maybe_start() -> Optional[StreamState]:
    """Start the process-wide stream once, if enabled and keyed. Returns its state."""
    if os.getenv("EDGE_STREAM_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    if "state" in _SINGLETON:
        return _SINGLETON["state"]
    key, sec = (os.getenv("ALPACA_KEY_ID") or "").strip(), (os.getenv("ALPACA_SECRET_KEY") or "").strip()
    if not key or not sec:
        return None
    st = StreamState(feed=(os.getenv("EDGE_STREAM_FEED") or "iex").strip().lower())
    s = Streamer(st, key=key, secret=sec)
    s.start()
    _SINGLETON.update(state=st, streamer=s)
    return st


def watch(symbols) -> None:
    s = _SINGLETON.get("streamer")
    if s is not None:
        s.subscribe(symbols)
