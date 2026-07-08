"""PR #155: real-fire proven-skill blocker."""
import core.proven_skill_gate as g


def test_review_blocks_too_few_rows(monkeypatch):
    monkeypatch.delenv("V3_PROVEN_SKILL_MIN_RESOLVED", raising=False)
    out = g.review("GME", resolved=9, wins=9, avg_pnl_pct=1.0)
    assert out["ok"] is False
    assert out["fail_reason"].startswith("resolved<")


def test_review_blocks_low_tp_rate(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_RESOLVED", "10")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_TP_RATE", "0.55")
    out = g.review("GME", resolved=30, wins=10, avg_pnl_pct=0.2)
    assert out["ok"] is False
    assert out["tp_rate"] == 0.3333
    assert out["fail_reason"].startswith("tp_rate<")


def test_review_blocks_negative_avg_pnl(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_RESOLVED", "10")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_TP_RATE", "0.55")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_AVG_PNL_PCT", "0.0")
    out = g.review("XYZ", resolved=20, wins=12, avg_pnl_pct=-0.1)
    assert out["ok"] is False
    assert out["fail_reason"].startswith("avg_pnl_pct<")


def test_review_allows_proven_symbol(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_RESOLVED", "10")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_TP_RATE", "0.55")
    monkeypatch.setenv("V3_PROVEN_SKILL_MIN_AVG_PNL_PCT", "0.0")
    # ITRI public shadow stats: n=28 includes EXPIRED; resolved WIN/LOSS is 20
    # (15 wins, 5 losses), so TP rate is 75%.
    out = g.review("ITRI", resolved=20, wins=15, avg_pnl_pct=0.807)
    assert out["ok"] is True
    assert out["tp_rate"] >= 0.55


def test_symbol_review_disabled(monkeypatch):
    monkeypatch.setenv("V3_PROVEN_SKILL_GATE", "0")
    out = g.symbol_review("ANY")
    assert out["ok"] is True and out["disabled"] is True
