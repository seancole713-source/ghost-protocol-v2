from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest

from core import signal_engine as engine


@pytest.mark.parametrize("feed", ["sip", "iex"])
def test_model_bars_request_split_price_and_volume(monkeypatch, feed):
    monkeypatch.setenv("ALPACA_KEY_ID", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    calls = []

    class Response:
        status_code = 200

        def __init__(self, query):
            self.query = query

        def json(self):
            if self.query["feed"] != [feed]:
                return {"bars": []}
            split = self.query.get("adjustment") == ["split"]
            price, volume = (120.84, 1000) if split else (1208.42, 100)
            return {"bars": [{"t": "2024-06-07T04:00:00Z", "o": price,
                              "h": price, "l": price, "c": price, "v": volume}]}

    def get(url, **kwargs):
        query = parse_qs(urlparse(url).query)
        calls.append(query)
        return Response(query)

    monkeypatch.setattr("requests.get", get)
    rows = engine._fetch_ohlcv_once("NVDA", "stock", adjustment="split")
    assert rows[0]["close"] == 120.84
    assert rows[0]["volume"] == 1000
    assert all(q["adjustment"] == ["split"] for q in calls)


def test_cache_and_default_preserve_distinct_price_bases(monkeypatch):
    engine.clear_ohlcv_cache()
    calls = []

    def fetch(*args, adjustment="raw", **kwargs):
        calls.append(adjustment)
        price = 10 if adjustment == "split" else 100
        return [{"ts": "2026-09-15", "open": price, "high": price,
                 "low": price, "close": price, "volume": 100}]

    monkeypatch.setattr(engine, "_fetch_ohlcv_once", fetch)
    assert engine._fetch_ohlcv("AAA", "stock")[0]["close"] == 100
    assert engine._fetch_ohlcv("AAA", "stock", adjustment="split")[0]["close"] == 10
    assert engine._fetch_ohlcv("AAA", "stock")[0]["close"] == 100
    assert calls == ["raw", "split"]


def test_unknown_adjustment_fails_closed():
    with pytest.raises(ValueError, match="adjustment"):
        engine._fetch_ohlcv("AAA", "stock", adjustment="made-up")
    with pytest.raises(ValueError, match="adjustment"):
        engine._fetch_ohlcv_once("AAA", "stock", adjustment="made-up")


def test_target_and_sector_request_same_split_basis(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "_fetch_ohlcv", lambda *a, **kw: calls.append(kw) or [])
    engine.backtest_symbol("AAA", "stock")
    engine._fetch_sector_series(period="5y")
    assert len(calls) == 2
    assert all(kw["adjustment"] == "split" for kw in calls)
    assert calls[1]["period"] == "5y"


def test_old_contract_identity_preserved_and_ineligible():
    from core.research_contracts import (
        CURRENT_LIVE_CONTRACT_VERSION, get_contract, is_live_compatible,
    )

    old = get_contract("tp_sl_swing", "v3")
    new = get_contract("tp_sl_swing", CURRENT_LIVE_CONTRACT_VERSION)
    assert new.version == "v4"
    assert new.feature_schema == old.feature_schema + "+alpaca_split_v1"
    assert old.lifecycle == "RETIRED" and not is_live_compatible(old)
    assert replace(new, version="v3", feature_schema=old.feature_schema).contract_id() == old.contract_id()
    assert new.contract_id() != old.contract_id()
