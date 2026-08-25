"""PR #134: structured news events — classifier, dedupe, defense policy, brain."""
import time

import core.news_events as ne
from core.news_defense import decide_defense
from core.news_events import article_dedupe_key, classify_text, event_dedupe_key
from core.super_ghost_shadow import SHADOW_MODELS, news_event_shadow, run_shadow_models


# ── deterministic classifier ────────────────────────────────────────────────

def _types(headline, summary=""):
    return {e["event_type"] for e in classify_text(headline, summary)}


def test_dilution_classification():
    assert "dilution_or_offering" in _types(
        "Virgin Galactic announces $300M at-the-market equity offering")
    assert "dilution_or_offering" in _types(
        "Company plans to repay 2027 notes with shares of common stock")


def test_going_concern_classification():
    evs = classify_text("10-Q flags substantial doubt about going concern")
    assert any(e["event_type"] == "going_concern" and e["direction_hint"] == "bearish"
               and e["materiality"] >= 0.9 for e in evs)


def test_guidance_and_earnings():
    assert "guidance_raise" in _types("Acme raises full-year guidance after strong Q2")
    assert "guidance_cut" in _types("Acme cuts outlook citing weak demand")
    assert "earnings_beat" in _types("Acme tops estimates on record revenue")
    assert "earnings_miss" in _types("Acme misses estimates as sales slow")


def test_timeline_delay_classification():
    assert "timeline_delay" in _types(
        "Acme delays commercial launch and pushes back production timeline")
    evs = classify_text("Acme delivery milestone slips to 2027 after program delay")
    assert any(e["event_type"] == "timeline_delay" and e["direction_hint"] == "bearish"
               and e["materiality"] >= 0.75 for e in evs)


def test_universal_event_scoring_flags_timeline_delay():
    from core.catalyst_scoring import score_events

    out = score_events([
        {"event_type": "earnings_beat", "direction_hint": "bullish", "materiality": 0.7,
         "source_reliability": 0.9, "confirmation_status": "reported"},
        {"event_type": "timeline_delay", "direction_hint": "bearish", "materiality": 0.8,
         "source_reliability": 0.9, "confirmation_status": "reported"},
    ])
    assert out["available"] is True
    assert out["timeline_delay_detected"] is True
    assert out["guidance_momentum_score"] < 0
    assert out["catalyst_score"] < 0


def test_mna_rumor_vs_confirmed():
    rumor = classify_text("Report: BILL exploring sale amid activist pressure")
    conf = classify_text("BILL signs merger agreement to be acquired at $55/share")
    assert any(e["event_type"] == "mna_rumor" and e["confirmation_status"] == "rumor"
               for e in rumor)
    assert any(e["event_type"] == "mna_confirmed" and e["confirmation_status"] == "reported"
               for e in conf)


def test_fda_events():
    assert "fda_approval" in _types("FDA approves Acme's lead drug")
    assert "fda_rejection" in _types("Acme receives Complete Response Letter from FDA")


def test_no_match_returns_empty():
    assert classify_text("Acme opens new office in Austin") == []
    assert classify_text("") == []


# ── dedupe keys ──────────────────────────────────────────────────────────────

def test_article_dedupe_ignores_punctuation_and_case():
    a = article_dedupe_key("SPCE", "Virgin Galactic Announces Offering!")
    b = article_dedupe_key("spce", "virgin galactic announces offering")
    assert a == b


def test_event_dedupe_one_per_type_per_day():
    ts = 1783300000
    assert event_dedupe_key("SPCE", "dilution_or_offering", ts) == \
           event_dedupe_key("SPCE", "dilution_or_offering", ts + 3600)
    assert event_dedupe_key("SPCE", "dilution_or_offering", ts) != \
           event_dedupe_key("SPCE", "dilution_or_offering", ts + 90000)


def test_event_dedupe_separates_direct_and_derived_provenance():
    ts = 1783300000
    direct = event_dedupe_key("ARCT", "fda_approval", ts)
    from_mrna = event_dedupe_key(
        "ARCT", "fda_approval", ts, derived=True, origin_symbol="MRNA"
    )
    from_bntx = event_dedupe_key(
        "ARCT", "fda_approval", ts, derived=True, origin_symbol="BNTX"
    )
    assert len({direct, from_mrna, from_bntx}) == 3


def test_derived_events_cannot_change_scoring_or_defense():
    from core.catalyst_scoring import score_events

    direct = {
        "event_type": "earnings_beat", "direction_hint": "bullish",
        "materiality": 0.7, "source_reliability": 0.9, "asof_ts": 2000,
    }
    derived_threat = {
        "event_type": "going_concern", "direction_hint": "bearish",
        "materiality": 1.0, "source_reliability": 1.0, "asof_ts": 2000,
        "derived": True, "origin_symbol": "MRNA", "decision_eligible": False,
    }
    baseline = score_events([direct])
    enriched = score_events([direct, derived_threat])
    assert enriched["score"] == baseline["score"]
    assert enriched["event_count"] == baseline["event_count"]
    assert enriched["advisory_events_excluded"] == 1
    assert decide_defense(
        [_pick()], {"SPCE": [derived_threat]}, now_ts=3000
    ) == []


class _RecentCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.args = None

    def execute(self, sql, args=None):
        self.sql = sql
        self.args = args

    def fetchall(self):
        return self.rows


def test_recent_events_enforces_extraction_time_and_direct_default():
    cur = _RecentCursor([])
    ne.recent_events_for_symbol("ARCT", asof_ts=5000, lookback_s=1000, cur=cur)
    assert "asof_ts <= %s" in cur.sql
    assert "extracted_at <= %s" in cur.sql
    assert "derived=FALSE" in cur.sql
    assert cur.args == ("ARCT", 4000, 5000, 5000)


def test_global_news_api_honors_provenance_scope(monkeypatch):
    import core.db as db
    from api.routes_data import get_news_events

    class Cursor:
        def __init__(self):
            self.sql = ""

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            return []

    class Conn:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

    class Ctx:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            return False

    cursors = []

    def connect():
        cursor = Cursor()
        cursors.append(cursor)
        return Ctx(Conn(cursor))

    monkeypatch.setattr(db, "db_conn", connect)
    monkeypatch.setattr(ne, "ensure_news_tables", lambda cur: None)
    monkeypatch.setattr(ne, "news_available", lambda: True)

    direct = get_news_events(scope="direct")
    assert direct["scope"] == "direct"
    assert "WHERE derived=FALSE" in cursors[-1].sql
    derived = get_news_events(scope="derived")
    assert derived["scope"] == "derived"
    assert "WHERE derived=TRUE" in cursors[-1].sql


# ── cross-symbol catalyst propagation (store_article_and_events) ─────────────

class _PropCursor:
    """Fake cursor capturing INSERTs into ghost_news_events."""

    def __init__(self):
        self.inserts = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        if "INSERT INTO ghost_news_events" in sql:
            self.inserts.append((sql, params))

    def fetchone(self):
        return (1,)  # article insert returns a new id


def test_store_article_propagates_fda_approval_to_peers():
    cur = _PropCursor()
    out = ne.store_article_and_events(cur, {
        "provider": "test", "provider_article_id": "1", "symbol": "MRNA",
        "headline": "FDA approves Moderna's cancer vaccine",
        "summary": "", "url": "", "source": "reuters",
        "published_at": 1780000000, "raw": {},
    })
    assert out["article_stored"] is True
    # Direct event + derived peer events (ARCT/BNTX/PFE/NVAX/MRK/BMY).
    derived = [p for _, p in cur.inserts if p and p[-2] is True]
    assert derived, "expected derived peer events"
    peer_syms = {p[1] for p in derived}
    assert "ARCT" in peer_syms
    assert "MRNA" not in peer_syms
    # Every derived event carries its origin symbol.
    assert all(p[-1] == "MRNA" for p in derived)


def test_store_article_no_propagation_for_non_catalyst():
    cur = _PropCursor()
    ne.store_article_and_events(cur, {
        "provider": "test", "provider_article_id": "2", "symbol": "MRNA",
        "headline": "Moderna opens new office in Austin",
        "summary": "", "url": "", "source": "reuters",
        "published_at": 1780000000, "raw": {},
    })
    derived = [p for _, p in cur.inserts if p and p[-2] is True]
    assert derived == []


# ── defense policy (pure) ────────────────────────────────────────────────────

def _pick(sym="SPCE", direction="UP", predicted_at=1000):
    return {"id": 1, "symbol": sym, "direction": direction, "predicted_at": predicted_at}


def _event(et="dilution_or_offering", mat=0.9, asof=2000, direction="bearish"):
    return {"event_type": et, "direction_hint": direction, "materiality": mat, "asof_ts": asof}


def test_defense_flags_fresh_bearish_event_on_up_pick():
    actions = decide_defense([_pick()], {"SPCE": [_event()]}, now_ts=3000)
    assert len(actions) == 1 and actions[0]["event_type"] == "dilution_or_offering"


def test_defense_ignores_event_before_entry():
    actions = decide_defense([_pick(predicted_at=5000)], {"SPCE": [_event(asof=2000)]},
                             now_ts=6000)
    assert actions == []


def test_defense_ignores_low_materiality_and_down_picks():
    assert decide_defense([_pick()], {"SPCE": [_event(mat=0.5)]}, now_ts=3000) == []
    assert decide_defense([_pick(direction="DOWN")], {"SPCE": [_event()]}, now_ts=3000) == []


def test_defense_ignores_stale_events():
    old = int(time.time()) - 10 * 86400
    actions = decide_defense([_pick(predicted_at=old - 100)],
                             {"SPCE": [_event(asof=old)]})
    assert actions == []


# ── news_shadow_v2 brain ─────────────────────────────────────────────────────

def _report(sym="SPCE"):
    return {"symbol": sym, "engine": "test",
            "prediction": {"direction": "HOLD", "confidence": 0.5},
            "risk_plan": {}, "checklist": []}


def test_v2_holds_when_feed_unavailable(monkeypatch):
    monkeypatch.setattr(ne, "news_available", lambda **k: False)
    out = news_event_shadow(_report())
    assert out["direction"] == "HOLD"
    assert "unavailable" in out["reason"].lower()


def test_v2_holds_when_no_events(monkeypatch):
    monkeypatch.setattr(ne, "news_available", lambda **k: True)
    monkeypatch.setattr(ne, "recent_events_for_symbol", lambda s, **k: [])
    out = news_event_shadow(_report())
    assert out["direction"] == "HOLD"
    assert "no material events" in out["reason"].lower()


def test_v2_commits_down_on_strong_bearish_events(monkeypatch):
    monkeypatch.setattr(ne, "news_available", lambda **k: True)
    monkeypatch.setattr(ne, "recent_events_for_symbol", lambda s, **k: [
        {"event_type": "going_concern", "direction_hint": "bearish", "materiality": 0.95,
         "source_reliability": 0.98, "confirmation_status": "reported", "asof_ts": 1},
        {"event_type": "dilution_or_offering", "direction_hint": "bearish", "materiality": 0.9,
         "source_reliability": 0.9, "confirmation_status": "reported", "asof_ts": 2}])
    out = news_event_shadow(_report())
    assert out["direction"] == "DOWN"
    assert 0.52 <= out["confidence"] <= 0.68
    assert out["model_id"] == "news_shadow_v2"


def test_v2_holds_on_mixed_tape(monkeypatch):
    monkeypatch.setattr(ne, "news_available", lambda **k: True)
    monkeypatch.setattr(ne, "recent_events_for_symbol", lambda s, **k: [
        {"event_type": "contract_award", "direction_hint": "bullish", "materiality": 0.7,
         "source_reliability": 0.9, "confirmation_status": "reported", "asof_ts": 1},
        {"event_type": "guidance_cut", "direction_hint": "bearish", "materiality": 0.85,
         "source_reliability": 0.9, "confirmation_status": "reported", "asof_ts": 2}])
    assert news_event_shadow(_report())["direction"] == "HOLD"


def test_v1_still_registered_and_frozen_alongside_v2(monkeypatch):
    ids = [m.model_id for m in SHADOW_MODELS]
    assert "news_shadow_v1" in ids and "news_shadow_v2" in ids
    assert "news_shadow_v3_direct" in ids
    monkeypatch.setattr(ne, "news_available", lambda **k: False)
    import core.seasonality as seas
    monkeypatch.setattr(seas, "seasonal_window_stats",
                        lambda s, **k: {"available": False, "reason": "test"})
    preds = run_shadow_models(_report())
    assert len(preds) == len(SHADOW_MODELS) == 13
