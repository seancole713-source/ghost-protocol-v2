"""Cockpit squeeze-radar tables: neutral level names, and the unvalidated numbers say so.

The radar's buy/sell/stop numbers and its P(+3% 60m) figure have no forward record behind
them. Column headers must not read as trade instructions or as a validated probability.
"""
from __future__ import annotations

from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "cockpit.html").read_text(encoding="utf-8")


def test_no_buy_sell_column_headers_or_bare_probability_header():
    for bad in ("<th>Buy</th>", "<th>Sell</th>", "<th>Stop</th>", "<th>P(+3% 60m)</th>", "<th>Alert buy</th>"):
        assert bad not in HTML, bad


def test_radar_headers_use_the_notification_wording():
    assert HTML.count("<th>Watch level</th>") >= 4
    assert HTML.count("<th>Upside reference</th>") >= 4
    assert HTML.count("<th>Invalidation level</th>") >= 2
    assert HTML.count("<th>Setup score (unvalidated)</th>") >= 4
    assert HTML.count("P(+3% 60m) · unvalidated proxy</th>") >= 4
    assert "Not a validated probability" in HTML
