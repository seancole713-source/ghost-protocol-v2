"""Provider error text can embed the request URL -- and a key in its query. It must never
reach the edge read-out (MCP, Railway EDGE_VIEW log lines) or the stored error rows."""
from __future__ import annotations

from datetime import date

from edge import feeds, premarket as PM, readout as RO
from edge.ledger import MemoryStore
from shared.redaction import redact_obj

FAKE = "SyntheticKey0123456789abcdef"   # not a real credential
URL = f"https://api.polygon.io/v2/aggs/ticker/ABCD/range/1/minute/x/y?adjusted=true&apiKey={FAKE}"


class _HTTPError(Exception):
    pass


def _boom(*_a, **_k):
    raise _HTTPError(f"403 Client Error: Forbidden for url: https://h/x?apiKey={FAKE}")


def test_redact_obj_masks_every_query_key_shape_deeply():
    v = {"a": [f"x?apiKey={FAKE}", (f"y?api_key={FAKE}",)], "b": {"c": f"z?token={FAKE}&key={FAKE}"},
         "n": 3, "none": None}
    out = redact_obj(v)
    assert FAKE not in repr(out)
    assert out["n"] == 3 and out["none"] is None and isinstance(out["a"][1], tuple)


def test_the_today_view_never_returns_a_key_from_a_stored_source_error():
    store = MemoryStore()
    store.put("edge_cards", "2026-09-23", {"day": "2026-09-23", "forecasts": [], "rows": [],
                                           "source_errors": {"daily_bars": f"HTTPError: 403 for url: {URL}"}})
    out = RO.view(store, "today")
    assert FAKE not in repr(out)
    assert "apiKey=***" in out["source_errors"]["daily_bars"]


def test_logged_views_are_redacted_too():
    store = MemoryStore()
    store.put("edge_cards", "2026-09-23", {"day": "2026-09-23", "forecasts": [], "rows": [],
                                           "source_errors": {"news": f"for url: {URL}"}})
    lines = RO.views_to_log(store, {"day": "2026-09-23", "card": {"status": "issued"}}, now=1)
    assert lines and all(FAKE not in j for _n, _d, j in lines)


def test_the_feed_check_stores_no_key():
    rec = feeds.check(_boom, now=1)
    assert FAKE not in rec["why"] and rec["decided"] is True       # 403 is still read as decided


def test_premarket_error_notes_store_no_key():
    out = PM.candidates(_boom, None, day=date(2026, 9, 23), now=1)
    assert out["errors"] and FAKE not in repr(out["errors"])
