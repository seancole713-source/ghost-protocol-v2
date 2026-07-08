"""PR #150 audit fix: /api/squeeze/daily-log must not return candidate+telegram
duplicates. The console dedupes for display, but the RAW endpoint returned both
(~70 dup pairs) — this asserts on the raw payload so the dup can't hide again.
"""
from core.squeeze_outcomes import _dedupe_candidate_telegram


def _row(sym, source, id_, buy=10.0, sell=10.4, stop=9.85, outcome=None,
         session_date="2026-07-08"):
    return {"id": id_, "symbol": sym, "session_date": session_date, "source": source,
            "buy": buy, "sell": sell, "stop": stop, "outcome": outcome}


def test_candidate_telegram_pair_collapses_to_one():
    rows = [_row("LCID", "candidate", 1), _row("LCID", "telegram", 2)]
    out = _dedupe_candidate_telegram(rows)
    assert len(out) == 1
    assert out[0]["source"] == "telegram"  # keep the actually-alerted row


def test_resolved_row_wins_over_unresolved():
    rows = [_row("STUB", "telegram", 4, outcome=None),
            _row("STUB", "candidate", 3, outcome="WIN")]
    out = _dedupe_candidate_telegram(rows)
    assert len(out) == 1 and out[0]["outcome"] == "WIN"


def test_distinct_picks_are_kept():
    rows = [_row("A", "telegram", 1, buy=10.0),
            _row("A", "telegram", 2, buy=12.0),   # different buy = different pick
            _row("B", "candidate", 3)]
    out = _dedupe_candidate_telegram(rows)
    assert len(out) == 3


def test_different_days_are_kept():
    rows = [_row("A", "telegram", 1, session_date="2026-07-07"),
            _row("A", "telegram", 2, session_date="2026-07-08")]
    assert len(_dedupe_candidate_telegram(rows)) == 2


def test_order_preserved():
    rows = [_row("C", "telegram", 5), _row("A", "telegram", 1),
            _row("A", "candidate", 2), _row("B", "telegram", 3)]
    out = _dedupe_candidate_telegram(rows)
    assert [r["symbol"] for r in out] == ["C", "A", "B"]
