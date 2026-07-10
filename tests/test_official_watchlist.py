"""Official watchlist configuration."""
from config.symbols import (
    OFFICIAL_WATCHLIST,
    OFFICIAL_WATCHLIST_CSV,
    watchlist_symbol_pairs,
)


def test_official_watchlist_has_100_symbols():
    # 74 + 26 liquid mega/large caps (PR #164) — universe width is the
    # shadow-evidence rate (one row per symbol per day).
    assert len(OFFICIAL_WATCHLIST) == 100
    assert len(set(OFFICIAL_WATCHLIST)) == 100


def test_official_watchlist_excludes_delisted_rdfn():
    assert "RDFN" not in OFFICIAL_WATCHLIST


def test_official_watchlist_includes_screenshot_symbols():
    expected = {
        "SPCE", "OPTU", "AMC", "FLNC", "LULU", "OPK", "TME", "IQ", "CLNE", "ODD",
        "NOK", "AI", "YMM", "SABR", "WOLF",
        "HIMS", "HOOD", "SOUN", "BB", "ARCT", "LCID", "RIOT", "PLUG", "ABCL",
        "SAP", "TAL", "PLTK", "GME", "RIG", "TLRY", "BMBL", "SNAP", "DJT", "LU",
        "PFE", "ARDT", "CVNA", "BILL", "DUOL", "XPO", "STUB", "TGTX", "ITRI",
        "DOMO", "BTGO",
    }
    assert expected <= set(OFFICIAL_WATCHLIST)  # base names present; watchlist has since grown


def test_official_watchlist_includes_mega_caps_added_2026_07_08():
    # Operator explicitly added mega-caps; the old "exclude railway defaults"
    # rule was reversed. RDFN stays OUT (delisted into RKT 2025-07).
    for s in ("TSLA", "META", "AMZN", "AAPL", "NVDA", "MSFT"):
        assert s in OFFICIAL_WATCHLIST, s
    assert "RDFN" not in OFFICIAL_WATCHLIST


def test_watchlist_pairs_match_official_list(monkeypatch):
    monkeypatch.delenv("STOCK_SYMBOLS", raising=False)
    monkeypatch.delenv("EDGE_SYMBOLS", raising=False)
    pairs = watchlist_symbol_pairs()
    assert [sym for sym, _ in pairs] == list(OFFICIAL_WATCHLIST)


def test_official_csv_roundtrip():
    assert OFFICIAL_WATCHLIST_CSV.split(",") == list(OFFICIAL_WATCHLIST)
