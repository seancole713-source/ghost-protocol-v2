"""Tests for shadow scoring (core.shadow_outcomes) — pure helpers, no DB."""
import json

from core.shadow_outcomes import (
    PROB_FLOOR,
    _bucket_for,
    _eval_entry_price,
    aggregate_shadow_stats,
    pick_daily_first,
)


def test_pick_daily_first_keeps_earliest_per_symbol_day():
    base = 1781000000  # all same CT day
    evals = [
        {"symbol": "STUB", "eval_ts": base + 3600},
        {"symbol": "STUB", "eval_ts": base},          # earliest — kept
        {"symbol": "STUB", "eval_ts": base + 7200},
        {"symbol": "FLNC", "eval_ts": base + 100},
    ]
    out = pick_daily_first(evals)
    by_sym = {e["symbol"]: e for e in out}
    assert len(out) == 2
    assert by_sym["STUB"]["eval_ts"] == base
    assert by_sym["FLNC"]["eval_ts"] == base + 100


def test_pick_daily_first_separate_days_kept():
    evals = [
        {"symbol": "STUB", "eval_ts": 1781000000},
        {"symbol": "STUB", "eval_ts": 1781000000 + 3 * 86400},
    ]
    assert len(pick_daily_first(evals)) == 2


def test_eval_entry_price_prefers_fired_entry():
    ev = {"entry_price": 65.21, "scores": {"price": 64.0}}
    assert _eval_entry_price(ev) == 65.21


def test_eval_entry_price_falls_back_to_scan_price():
    assert _eval_entry_price({"entry_price": None, "scores": {"price": 12.5}}) == 12.5
    # psycopg may hand back JSON as a string
    assert _eval_entry_price({"scores": json.dumps({"price": 3.3})}) == 3.3
    assert _eval_entry_price({"scores": {}}) is None
    assert _eval_entry_price({}) is None


def test_bucket_for_edges():
    assert _bucket_for(None) == "unknown"
    assert _bucket_for(0.40) == "weak"
    assert _bucket_for(0.50) == "near"
    assert _bucket_for(PROB_FLOOR) == "fireable"
    assert _bucket_for(0.80) == "fireable"


def test_aggregate_shadow_stats_per_symbol_and_buckets():
    rows = [
        {"symbol": "STUB", "eval_ts": 1, "up_prob": 0.56, "outcome": "WIN", "pnl_pct": 2.0},
        {"symbol": "STUB", "eval_ts": 2, "up_prob": 0.57, "outcome": "LOSS", "pnl_pct": -1.3},
        {"symbol": "STUB", "eval_ts": 3, "up_prob": 0.52, "outcome": "WIN", "pnl_pct": 2.0},
        {"symbol": "FLNC", "eval_ts": 1, "up_prob": 0.51, "outcome": "EXPIRED", "pnl_pct": 0.4},
        {"symbol": "AMC", "eval_ts": 9, "up_prob": 0.49, "outcome": None, "pnl_pct": None},
    ]
    out = aggregate_shadow_stats(rows)
    assert out["resolved"] == 4
    assert out["pending"] == 1

    stub = next(s for s in out["symbols"] if s["symbol"] == "STUB")
    assert stub["n"] == 3
    assert stub["wins"] == 2 and stub["losses"] == 1
    assert stub["tp_rate_pct"] == 66.7
    assert stub["last_outcome"] == "WIN"

    flnc = next(s for s in out["symbols"] if s["symbol"] == "FLNC")
    assert flnc["expired"] == 1
    assert flnc["tp_rate_pct"] is None  # no TP/SL decisions yet

    assert out["buckets"]["fireable"]["n"] == 2
    assert out["buckets"]["fireable"]["tp_rate_pct"] == 50.0
    assert out["buckets"]["near"]["n"] == 2


def test_aggregate_shadow_stats_sorts_by_tp_rate():
    rows = [
        {"symbol": "AAA", "eval_ts": 1, "up_prob": 0.6, "outcome": "LOSS", "pnl_pct": -1},
        {"symbol": "BBB", "eval_ts": 1, "up_prob": 0.6, "outcome": "WIN", "pnl_pct": 2},
    ]
    out = aggregate_shadow_stats(rows)
    assert [s["symbol"] for s in out["symbols"]] == ["BBB", "AAA"]


def test_format_candidate_lines():
    from core.telegram_cards import format_candidate_lines

    lines = format_candidate_lines([
        {"symbol": "STUB", "up_prob": 0.5483, "min_win_proba": 0.55, "skip_code": "v3_prob_low"},
        {"symbol": "HOOD", "up_prob": 0.58, "min_win_proba": 0.55, "fired": True},
        {"symbol": "GME", "up_prob": None},
    ])
    assert lines[0] == "1. STUB 54.8% (needs 55%) — prob below floor"
    assert lines[1] == "2. HOOD 58.0% (needs 55%) — FIRED"
    assert len(lines) == 2  # missing up_prob dropped


def test_silence_card_includes_leaderboard():
    from core.telegram_cards import format_silence_card

    out = format_silence_card({
        "ghost_score": 46,
        "reason": "v3 model prob below BUY floor (floor 55%)",
        "top_candidates": [
            {"symbol": "STUB", "up_prob": 0.5483, "min_win_proba": 0.55, "skip_code": "v3_prob_low"},
        ],
    })
    assert "Closest candidates today:" in out
    assert "STUB 54.8%" in out
    assert "Next scan:" in out


def test_silence_card_no_leaderboard_when_empty():
    from core.telegram_cards import format_silence_card

    out = format_silence_card({"ghost_score": 46, "reason": "x"})
    assert "Closest candidates" not in out
