"""core/catalyst_graph.py — cross-symbol catalyst propagation.

Ghost's news classifier tags events per-symbol, but a catalyst often reprices a
whole *sector* (the ARCT miss: Moderna/Merck cancer-vaccine results lifted the
mRNA cohort, but Ghost classified zero ARCT catalyst events because it never
propagated the catalyst from the origin company to its peers).

This module maps symbols to a sector/peer group and, when a high-materiality
catalyst event lands on one member, emits a *derived* catalyst signal for the
peers. Derived signals are:
  - point-in-time (asof_ts = the origin event's asof_ts),
  - explicitly marked `derived=True` + `origin_symbol` so they are never
    mistaken for a direct company event,
  - advisory only — they feed the detection/WATCH tier and rescoring, never
    the trade gate directly.

The mapping is a deterministic, curated table (no LLM). It is intentionally
small and conservative: only relationships strong enough to justify a sector
repricing (drug class, direct competitor, shared supply chain).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.catalyst_graph")

# Sector/peer groups. A catalyst on any member propagates to the others.
# Keys are canonical group names; values are the member symbols.
SECTOR_GROUPS: Dict[str, List[str]] = {
    "mrna_vaccine": ["MRNA", "BNTX", "ARCT", "PFE", "NVAX"],
    "cancer_immunotherapy": ["MRNA", "BNTX", "ARCT", "MRK", "BMY"],
    "ev": ["TSLA", "RIVN", "LCID", "NIO", "XPEV"],
    "meme_retail": ["GME", "AMC", "BBBY", "KOSS", "EXPR"],
    "crypto_miners": ["RIOT", "MARA", "CLSK", "HUT", "BTBT"],
    "semiconductor": ["NVDA", "AMD", "INTC", "MU", "MRVL", "AVGO", "TXN", "QCOM"],
    "solar": ["FSLR", "ENPH", "SEDG", "RUN", "NOVA"],
    "cannabis": ["TLRY", "CGC", "ACB", "CRON", "SNDL"],
}

# Reverse index: symbol -> set of group names it belongs to.
_SYMBOL_TO_GROUPS: Dict[str, List[str]] = {}
for _grp, _members in SECTOR_GROUPS.items():
    for _m in _members:
        _SYMBOL_TO_GROUPS.setdefault(_m.upper(), []).append(_grp)

# Event types strong enough to propagate across a sector (drug-class results,
# clinical readouts, regulatory approvals/rejections, M&A that re-rates a space).
_PROPAGATING_EVENT_TYPES = {
    "fda_approval", "fda_rejection", "mna_confirmed", "mna_rumor",
    "contract_award", "guidance_raise", "guidance_cut",
}

# Minimum materiality for a catalyst to propagate (avoid noise).
_PROPAGATE_MIN_MATERIALITY = 0.70


def groups_for_symbol(symbol: str) -> List[str]:
    """Return the sector/peer group names a symbol belongs to."""
    return list(_SYMBOL_TO_GROUPS.get((symbol or "").upper(), []))


def peers_of(symbol: str) -> List[str]:
    """Return the distinct peer symbols (excluding self) across all groups."""
    sym = (symbol or "").upper()
    peers: set = set()
    for grp in _SYMBOL_TO_GROUPS.get(sym, []):
        for m in SECTOR_GROUPS[grp]:
            if m.upper() != sym:
                peers.add(m.upper())
    return sorted(peers)


def propagate_catalyst(
    origin_symbol: str,
    event_type: str,
    *,
    materiality: float,
    asof_ts: Optional[int] = None,
    direction_hint: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Derive peer catalyst signals from a high-materiality origin event.

    Returns a list of derived signals, one per peer, each marked `derived=True`
    and carrying the origin symbol + event type. Pure function — no I/O.
    """
    et = (event_type or "").strip().lower()
    if et not in _PROPAGATING_EVENT_TYPES:
        return []
    if (materiality or 0) < _PROPAGATE_MIN_MATERIALITY:
        return []
    ts = int(asof_ts or time.time())
    out: List[Dict[str, Any]] = []
    for peer in peers_of(origin_symbol):
        out.append({
            "symbol": peer,
            "event_type": et,
            "direction_hint": direction_hint,
            "materiality": round(float(materiality) * 0.7, 3),  # sector repricing < direct
            "derived": True,
            "origin_symbol": (origin_symbol or "").upper(),
            "asof_ts": ts,
        })
    return out
