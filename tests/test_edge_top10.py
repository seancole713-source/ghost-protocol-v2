"""The daily Top 10: ranked by setup quality, graded after the close, never traded."""
from __future__ import annotations

from edge import catalysts as C
from edge import notify as N
from edge import readout as R
from edge import scorecard as SC
from edge import top10 as T
from edge.ledger import MemoryStore


def row(sym, ref, prev, dollars, kinds=(), catalyst=None):
    return {"symbol": sym, "ref_price": ref, "prev_close": prev, "avg_dollars": dollars,
            "catalyst": catalyst, "inputs": {"events": [{"kind": k} for k in kinds]}}


def test_quality_beats_raw_percent_gain():
    """2026-09-24: the radar's top-by-% list was filled with +100-200% sub-$5 names while a
    clean +7% earnings gap on a liquid stock never made it. Quality must rank first."""
    clean = row("BB", 8.47, 7.94, 60e6, [C.EARNINGS])                  # +6.7%, earnings, liquid
    rocket = row("APUS", 6.16, 2.29, 1.0e6)                            # +169%, no news, thin
    diluted = row("GRML", 13.15, 11.19, 40e6, [C.OFFERING])            # +17.5%, but selling shares
    nonews = row("GLND", 3.10, 2.91, 44e6)                             # +6.5%, no dated news
    out = T.build({"day": "2026-09-24", "issued_at": 1, "rows": [rocket, diluted, nonews, clean]})
    order = [x["symbol"] for x in out["list"]]
    assert order[0] == "BB" and order[-1] == "APUS"
    assert order.index("GLND") < order.index("GRML")        # dilution costs more than no news
    top = out["list"][0]
    assert top["rank"] == 1 and top["catalyst_kind"] == C.EARNINGS and any("sweet spot" in r for r in top["reasons"])


def test_only_priced_gainers_are_ranked_and_at_most_ten():
    rows = [row(f"S{i}", 10 + i * 0.1, 9.5, 10e6) for i in range(14)]
    rows += [row("DOWN", 9.0, 10.0, 50e6), row("NOPRICE", None, 10.0, 50e6)]
    out = T.build({"day": "2026-09-24", "rows": rows})
    syms = [x["symbol"] for x in out["list"]]
    assert len(syms) == 10 and "DOWN" not in syms and "NOPRICE" not in syms
    assert out["considered"] == 14 and out["method"] == T.METHOD


def test_an_exhausted_gap_and_a_bad_price_are_penalised():
    assert T.score(row("X", 1.5, 1.0, 50e6))["score"] < T.score(row("Y", 11.0, 10.0, 50e6))["score"]
    assert any("exhausted" in r for r in T.score(row("X", 1.5, 1.0, 50e6))["reasons"])


def test_the_view_joins_the_after_close_grades_and_the_scorecard_compares():
    st = MemoryStore()
    st.put("edge_top10", "2026-09-24", T.build({"day": "2026-09-24", "rows": [
        row("BB", 8.47, 7.94, 60e6, [C.EARNINGS]), row("GLND", 3.10, 2.91, 44e6)]}))
    assert R.view(st, "top10", "2026-09-24")["graded"] is False
    st.put("edge_card_outcomes", "2026-09-24", {"day": "2026-09-24", "rows": [
        {"symbol": "BB", "outcome": "WIN", "baseline": "ELIGIBLE"},
        {"symbol": "GLND", "outcome": "NO_FILL", "baseline": "ELIGIBLE"},
        {"symbol": "OTHER", "outcome": "LOSS", "baseline": "ELIGIBLE"}]})
    v = R.view(st, "top10")
    assert v["graded"] is True and {x["symbol"]: x["outcome"] for x in v["list"]} == {"BB": "WIN", "GLND": "NO_FILL"}
    sc = SC.scorecard(st)["top10"]
    assert sc["approved"]["wins"] == 1 and sc["rejected"]["decided"] == 1
    assert sc["verdict"].startswith("not enough data")


def test_the_phone_card_lists_the_top_10():
    text = N.card_text({"day": "2026-09-24", "forecasts": [], "top10": ["BB", "GLND"]})
    assert "Top 10" in text and "1.BB, 2.GLND" in text and "No trade today" in text
    ranked = N.card_text({"day": "2026-09-24", "forecasts": [], "top10": ["BB", "GLND"],
                          "top10_ranked": [{"rank": 1, "symbol": "BB", "score": 72.4},
                                           {"rank": 2, "symbol": "GLND", "score": 65.0}]})
    assert "1.BB 72, 2.GLND 65" in ranked
