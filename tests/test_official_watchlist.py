"""Official watchlist configuration."""
from config.symbols import (
    OFFICIAL_WATCHLIST,
    OFFICIAL_WATCHLIST_CSV,
    watchlist_symbol_pairs,
    watchlist_symbols,
)


def test_official_watchlist_has_43_symbols():
    assert len(OFFICIAL_WATCHLIST) == 43
    assert len(set(OFFICIAL_WATCHLIST)) == 43


def test_official_watchlist_excludes_delisted_rdfn():
    assert "RDFN" not in OFFICIAL_WATCHLIST


def test_official_watchlist_includes_screenshot_symbols():
    expected = {
        "SPCE", "OPTU", "AMC", "FLNC", "LULU", "OPK", "TME", "IQ", "CLNE", "ODD",
        "NOK", "AI", "YMM", "SABR", "WOLF",
        "HIMS", "HOOD", "SOUN", "BB", "ARCT", "LCID", "RIOT", "PLUG", "ABCL",
        "SAP", "TAL", "PLTK", "GME", "RIG", "TLRY", "BMBL", "SNAP", "DJT", "LU",
        "PFE", "ARDT", "CVNA", "BILL", "DUOL", "XPO", "STUB", "TGTX", "ITRI",
    }
    assert expected == set(OFFICIAL_WATCHLIST)


def test_official_watchlist_excludes_old_railway_defaults():
    assert "TSLA" not in OFFICIAL_WATCHLIST
    assert "META" not in OFFICIAL_WATCHLIST
    assert "AMZN" not in OFFICIAL_WATCHLIST


def test_watchlist_pairs_match_official_list(monkeypatch):
    monkeypatch.delenv("STOCK_SYMBOLS", raising=False)
    monkeypatch.delenv("EDGE_SYMBOLS", raising=False)
    pairs = watchlist_symbol_pairs()
    assert [sym for sym, _ in pairs] == list(OFFICIAL_WATCHLIST)


def test_official_csv_roundtrip():
    assert OFFICIAL_WATCHLIST_CSV.split(",") == list(OFFICIAL_WATCHLIST)
