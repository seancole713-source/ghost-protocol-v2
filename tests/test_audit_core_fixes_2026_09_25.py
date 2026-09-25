"""Regression tests for the 2026-09-25 audit fixes to the old core engine.

F18  regime gate "below EMA200" really tests EMA200 (unknown when < 200 bars)
F08  checklist news boxes read the producer's real event shape
F26  core news classifier refuses market wraps / roundups / other-entity tags
F27  EDGAR 8-K fetcher resolves a CIK for any ticker
F25  old Finnhub news path keeps the provider's publish time and dedupes
F19  accuracy payloads carry a basis; shadow rows keep feature_schema
F41  research picks excluded from /api/picks accuracy and the gated paper book
F23  Ghost Score: a stale pick does not drive the model component or label
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time

import numpy as np
import pytest

import core.signal_engine as _se


# ── F18 ──────────────────────────────────────────────────────────────────────

def _bars(closes, end=datetime(2026, 5, 20)):
    n = len(closes)
    return [
        {"ts": (end - timedelta(days=n - 1 - i)).date().isoformat(),
         "open": c - 0.2, "high": c + 0.5, "low": c - 0.5, "close": c,
         "volume": 1000 + i * 5}
        for i, c in enumerate(closes)
    ]


def test_regime_above_ema200_matches_full_history_ema200_on_252_bars():
    from core.engine_features import regime_above_ema200
    from core.engine_indicators import _ema

    # Long decline then a partial rally: price is above EMA50 but below EMA200.
    closes = [200 - 0.5 * i for i in range(200)] + [100 + 0.6 * i for i in range(52)]
    rows = _bars(closes)
    true_flag = 1 if closes[-1] > _ema(np.array(closes, dtype=float), 200) else 0
    assert regime_above_ema200(rows) == true_flag == 0
    # The model feature (121-bar training window, ema200 aliased to EMA50)
    # disagrees -- that alias is exactly what the gate used to read.
    feats = _se._calculate_features(_se._serving_feature_bars(rows))
    assert feats["above_ema200"] == 1


def test_regime_above_ema200_is_unknown_below_200_bars():
    from core.engine_features import regime_above_ema200

    assert regime_above_ema200(_bars([100 + 0.4 * i for i in range(199)])) is None
    assert regime_above_ema200([]) is None
    assert regime_above_ema200(_bars([100 + 0.4 * i for i in range(200)])) == 1


class _Model:
    def __init__(self, p):
        self._p = p

    def predict_proba(self, X):
        return np.array([[1.0 - self._p, self._p]])


def _patch_live(monkeypatch, rows):
    monkeypatch.setattr("core.market_hours._now_ct",
                        lambda: datetime(2026, 5, 21, 8, tzinfo=ZoneInfo("America/Chicago")))
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    monkeypatch.setenv("GHOST_PREMARKET_SCAN", "0")
    for k, v in {"V3_MIN_WIN_PROBA": "0.55", "V3_MIN_EDGE": "0.0",
                 "V3_MIN_HOLDOUT_ACC": "0.0", "V3_MIN_WF_ACC_MEAN": "0.0",
                 "V3_PROVEN_SKILL_GATE": "0", "V3_OVERCONFIDENCE_GATE": "0"}.items():
        monkeypatch.setenv(k, v)
    meta = {"tier": "proven", "direction": "UP", "model_sha256": "a" * 64,
            "label_schema": _se._v3_label_schema(),
            "validation_schema": _se._v3_validation_schema(),
            "label_hold_bars": _se.V3_LABEL_HOLD_BARS,
            "edge": 0.3, "accuracy": 0.66, "wf_acc_mean": 0.64,
            "wf_edge_mean": 0.2, "wf_fold_count": 4, "trained_at": time.time()}
    monkeypatch.setattr(
        _se, "load_model",
        lambda s, direction="UP": (_Model(0.70), _se.FEATURE_COLS, dict(meta))
        if direction == "UP" else (None, None, None))
    monkeypatch.setattr(_se, "_fetch_ohlcv",
                        lambda s, a, period="5d", interval="1h", adjustment="raw": rows)
    # Force a choppy tape (ADX not trending) so Gate 1 is decided by EMA200.
    real = _se._calculate_features

    def _choppy(df):
        f = real(df)
        f["adx"] = 10.0
        f["adx_trending"] = 0
        return f
    monkeypatch.setattr(_se, "_calculate_features", _choppy)


def test_regime_gate_fails_closed_when_ema200_unknown(monkeypatch):
    # 150 rising bars: the EMA50 alias says "above EMA200"; a real EMA200
    # cannot be computed, so a choppy tape must be blocked, not waved through.
    _patch_live(monkeypatch, _bars([100 + 0.4 * i for i in range(150)]))
    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is None and reason == "regime_gate"
    assert scores["regime"]["above_ema200"] is None
    assert scores["regime"]["above_ema200_basis"].startswith("unknown_lt_200_bars")


def test_regime_gate_blocks_when_below_true_ema200_but_above_ema50(monkeypatch):
    closes = [200 - 0.5 * i for i in range(200)] + [100 + 0.6 * i for i in range(52)]
    _patch_live(monkeypatch, _bars(closes))
    scores = {}
    sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert sig is None and reason == "regime_gate"
    assert scores["regime"]["above_ema200"] == 0
    assert scores["features"]["above_ema200"] == 1   # model feature untouched


def test_regime_gate_reports_true_ema200_on_full_history(monkeypatch):
    _patch_live(monkeypatch, _bars([100 + 0.4 * i for i in range(220)]))
    scores = {}
    _sig, reason = _se.predict_live_ex("WOLF", "stock", scores=scores)
    assert reason != "regime_gate"
    assert scores["regime"]["above_ema200"] == 1
    assert scores["regime"]["above_ema200_basis"] == "close_vs_ema200_220bars"


# ── F08 ──────────────────────────────────────────────────────────────────────

def test_checklist_news_reads_real_producer_event_shape(monkeypatch):
    import core.checklist_evidence as ce

    # Exactly the keys news_events.recent_events_for_symbol emits -- no
    # 'sentiment' key anywhere.
    keys = ("event_type", "direction_hint", "materiality", "confidence",
            "confirmation_status", "source_reliability", "evidence", "asof_ts",
            "derived", "origin_symbol", "extracted_at", "dedupe_key")
    rows = [
        ("guidance_raise", "bullish", 0.9, 0.8, "reported", 0.9, "raises guidance",
         1_000, False, None, 1_010, "a"),
        ("earnings_beat", "bullish", 0.8, 0.7, "reported", 0.9, "beats estimates",
         1_200, False, None, 1_210, "b"),
    ]
    events = [dict(zip(keys, r)) for r in rows]
    monkeypatch.setattr("core.news_events.recent_events_for_symbol",
                        lambda symbol, *, asof_ts=None, **kw: events)

    out = ce._collect_news("WOLF", asof_ts=9_999)

    assert out["news_sentiment"]["value"] > 0.25        # material_news box can pass
    assert out["news_sentiment"]["_ts"] == 1_200
    assert out["guidance_direction"]["value"] == 1
    assert out["guidance_direction"]["_ts"] == 1_000


def test_checklist_news_bearish_producer_event_is_negative(monkeypatch):
    import core.checklist_evidence as ce

    monkeypatch.setattr(
        "core.news_events.recent_events_for_symbol",
        lambda symbol, *, asof_ts=None, **kw: [{
            "event_type": "guidance_cut", "direction_hint": "bearish",
            "materiality": 0.9, "source_reliability": 0.9,
            "confirmation_status": "reported", "asof_ts": 500,
        }],
    )
    out = ce._collect_news("WOLF", asof_ts=9_999)
    assert out["news_sentiment"]["value"] < -0.25
    assert out["guidance_direction"]["value"] == -1


# ── F26 ──────────────────────────────────────────────────────────────────────

def test_core_classifier_refuses_2026_09_23_market_wraps():
    from core.news_events import classify_text

    # 09-23: tagged to WHLR, and to DCOY/VKTX/QNME.
    assert classify_text("Dow Falls 100 Points; General Mills Posts Upbeat Q1 Earnings",
                         tickers=["WHLR", "GIS"], symbol="WHLR") == []
    assert classify_text("Crude Oil Down Over 1%; Thor Industries Shares Gain After Q4 Results",
                         tickers=["DCOY", "VKTX", "QNME"], symbol="DCOY") == []


def test_core_classifier_refuses_multi_ticker_roundup():
    from core.news_events import classify_text

    headline = "Acme raises guidance while Beta cuts guidance in busy session"
    assert classify_text(headline)                      # untagged text still classifies
    assert classify_text(headline, tickers=["ACME", "BETA", "CCC", "DDD"],
                         symbol="ACME") == []


def test_core_classifier_refuses_story_not_tagged_to_symbol():
    from core.news_events import classify_text

    assert classify_text("Moderna beats estimates on vaccine sales",
                         tickers=["MRNA"], symbol="PFE") == []
    assert classify_text("Moderna beats estimates on vaccine sales",
                         tickers=["MRNA"], symbol="MRNA")


def test_store_article_writes_no_direct_event_for_a_wrap():
    import core.news_events as ne

    class _Cur:
        def __init__(self):
            self.event_inserts = 0
            self.rowcount = 1

        def execute(self, sql, params=None):
            if "INSERT INTO ghost_news_events" in sql:
                self.event_inserts += 1

        def fetchone(self):
            return (1,)

    cur = _Cur()
    out = ne.store_article_and_events(cur, {
        "provider": "alpaca", "provider_article_id": "9", "symbol": "WHLR",
        "tickers": ["WHLR", "GIS"],
        "headline": "Dow Falls 100 Points; General Mills Posts Upbeat Q1 Earnings",
        "summary": "", "url": "", "source": "benzinga",
        "published_at": 1_790_000_000, "raw": {},
    })
    assert out["article_stored"] is True
    assert out["events_stored"] == 0 and cur.event_inserts == 0


def test_alpaca_ingest_carries_every_tagged_ticker(monkeypatch):
    import core.news_ingest as ni

    class _Resp:
        status_code = 200

        def json(self):
            return {"news": [{
                "id": 1, "headline": "Sector roundup", "summary": "", "url": "u",
                "source": "x", "created_at": "2026-09-23T12:00:00Z",
                "symbols": ["WOLF", "AAA", "BBB", "CCC"],
            }]}

    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setattr(ni.requests, "get", lambda *a, **k: _Resp())
    out = ni._fetch_alpaca(["WOLF"], 0)
    assert len(out) == 1
    assert out[0]["tickers"] == ["WOLF", "AAA", "BBB", "CCC"]


# ── F27 ──────────────────────────────────────────────────────────────────────

def test_edgar_cik_resolves_any_symbol_via_sec_index(monkeypatch):
    import core.edgar_integration as ei
    import core.sec_fundamentals as sf

    monkeypatch.delenv("EDGAR_CIK_ZZZQ", raising=False)
    monkeypatch.setattr(sf, "_load_ticker_index", lambda: None)
    monkeypatch.setitem(sf._ticker_index, "ZZZQ", "0000320193")
    assert ei._cik_for_symbol("ZZZQ") == "0000320193"
    assert ei._cik_for_symbol("WOLF") == "0000895419"


def test_edgar_cik_env_override_is_zero_padded(monkeypatch):
    import core.edgar_integration as ei

    monkeypatch.setenv("EDGAR_CIK_QQQZ", "320193")
    assert ei._cik_for_symbol("QQQZ") == "0000320193"


def test_fetch_recent_8k_uses_resolved_cik_and_reuses_user_agent(monkeypatch):
    import core.edgar_integration as ei
    import core.sec_fundamentals as sf

    ei._edgar_cache.pop("ZZZQ", None)
    monkeypatch.setattr(sf, "_load_ticker_index", lambda: None)
    monkeypatch.setitem(sf._ticker_index, "ZZZQ", "0000320193")
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"filings": {"recent": {"form": [], "filingDate": [],
                                           "accessionNumber": [], "items": []}}}

    def _get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["ua"] = (headers or {}).get("User-Agent")
        return _Resp()

    monkeypatch.setattr(ei.requests, "get", _get)
    out = ei.fetch_recent_8k("ZZZQ")
    ei._edgar_cache.pop("ZZZQ", None)
    assert out.get("reason") != "no_cik_mapping"
    assert seen["url"].endswith("CIK0000320193.json")
    assert seen["ua"] == sf._SEC_USER_AGENT


# ── F25 ──────────────────────────────────────────────────────────────────────

class _NewsDB:
    """Tiny in-memory stand-in for ghost_news_articles."""

    def __init__(self):
        self.rows = []

    def conn(self):
        db = self

        class _Cur:
            rowcount = 0

            def execute(self, sql, params=None):
                self._res = None
                self.rowcount = 0
                if sql.lstrip().startswith("SELECT 1 FROM ghost_news_articles"):
                    sym = params[0]
                    if len(params) == 3:
                        hit = any(r["symbol"] == sym and (r["url"] == params[1] or r["title"] == params[2])
                                  for r in db.rows)
                    else:
                        hit = any(r["symbol"] == sym and r["title"] == params[1] for r in db.rows)
                    self._res = (1,) if hit else None
                elif "INSERT INTO ghost_news_articles" in sql:
                    if any(r["article_id"] == params[0] for r in db.rows):
                        return
                    db.rows.append({"article_id": params[0], "symbol": params[1],
                                    "title": params[2], "url": params[4],
                                    "published_at": params[5], "ingested_at": params[6]})
                    self.rowcount = 1

            def fetchone(self):
                return self._res

        class _Conn:
            def cursor(self):
                return _Cur()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Conn()


def test_finnhub_headline_keeps_provider_time_and_is_stored_once(monkeypatch):
    import core.news as news
    import core.news_store as ns
    import core.db as db

    payload = [{"headline": "Wolfspeed wins supply deal", "datetime": 1_780_000_000,
                "url": "https://example.com/wolf-deal", "summary": "s", "id": 77}]

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr(news, "FINNHUB_KEY", "test")
    monkeypatch.setattr(news._finnhub_cb, "allow", lambda: True)
    monkeypatch.setattr(news.requests, "get", lambda *a, **k: _Resp())
    fetched = news._fetch_finnhub_stock("WOLF")
    assert fetched[0]["published_at"] == 1_780_000_000
    assert fetched[0]["url"] == "https://example.com/wolf-deal"

    store = _NewsDB()
    monkeypatch.setattr(ns, "ensure_news_tables", lambda: None)
    monkeypatch.setattr(db, "db_conn", store.conn)
    assert ns.upsert_fetched_articles(fetched) == 1
    # Next cycle, same payload: no new row.
    assert ns.upsert_fetched_articles(news._fetch_finnhub_stock("WOLF")) == 0
    assert len(store.rows) == 1
    assert store.rows[0]["published_at"] == 1_780_000_000


def test_fetched_headline_without_publish_time_is_not_stamped_now(monkeypatch):
    import core.news_store as ns
    import core.db as db

    store = _NewsDB()
    monkeypatch.setattr(ns, "ensure_news_tables", lambda: None)
    monkeypatch.setattr(db, "db_conn", store.conn)
    assert ns.upsert_fetched_articles([{"title": "No time", "symbols": ["WOLF"]}]) == 0
    assert store.rows == []


def test_article_id_is_symbol_scoped_and_time_independent():
    from core.news_store import _article_id

    assert _article_id("WOLF", "Headline", None, 1) == _article_id("WOLF", "Headline", None, 2)
    assert _article_id("WOLF", "Headline", "u", 1) != _article_id("AAPL", "Headline", "u", 1)
    assert _article_id("WOLF", "Headline", None) != _article_id("AAPL", "Headline", None)


# ── F19 / F41 ────────────────────────────────────────────────────────────────

def test_accuracy_basis_shape():
    from core.prediction_filters import accuracy_basis

    b = accuracy_basis(population="p", wins=3, n=9, start_ts=1_780_000_000,
                       end_ts=1_790_000_000)
    assert b["population"] == "p" and b["n"] == 9 and b["wins"] == 3
    assert b["date_range"]["start"] == "2026-05-28"
    assert 0.0 < b["wilson_95"]["low"] < 3 / 9 < b["wilson_95"]["high"] < 1.0
    assert accuracy_basis(population="p", wins=0, n=0)["wilson_95"] is None


def _picks_db(monkeypatch, captured):
    import wolf_app

    class _Cur:
        description = [("id",), ("symbol",), ("outcome",), ("entry_price",)]

        def execute(self, sql, params=None):
            self._sql = sql
            captured.append(sql)

        def fetchall(self):
            if "GROUP BY outcome" in self._sql:
                rows = [("WIN", 2, 1_780_000_000, 1_785_000_000),
                        ("LOSS", 7, 1_781_000_000, 1_786_000_000)]
                if "research_pick" not in self._sql:
                    rows[0] = ("WIN", 3, 1_780_000_000, 1_785_000_000)  # + a research WIN
                return rows
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _Ctx())
    return wolf_app


def test_api_picks_accuracy_excludes_research_and_carries_basis(monkeypatch):
    captured = []
    wolf_app = _picks_db(monkeypatch, captured)
    out = wolf_app.get_picks()
    assert out["wins"] == 2 and out["losses"] == 7 and out["total"] == 9
    assert out["accuracy_pct"] == round(2 / 9 * 100, 1)
    # UI fields unchanged
    for k in ("ok", "active", "recent", "has_more", "accuracy_pct", "wins", "losses", "total"):
        assert k in out
    basis = out["basis"]
    assert basis["n"] == 9 and basis["wins"] == 2
    assert "research" in basis["population"]
    assert basis["date_range"] == {"start": "2026-05-28", "end": "2026-08-06"}
    assert basis["wilson_95"]["low"] < 2 / 9 < basis["wilson_95"]["high"]


def test_stats_v32_carries_basis(monkeypatch):
    import wolf_app

    class _Cur:
        def __init__(self):
            self.n = 0

        def execute(self, sql, params=None):
            self._sql = sql

        def fetchall(self):
            return [("WIN", 3), ("LOSS", 5), ("EXPIRED", 1)]

        def fetchone(self):
            assert "research_pick" in self._sql      # open count excludes research
            return (0,)

    class _Ctx:
        def __enter__(self):
            class _C:
                def cursor(self_inner):
                    return _Cur()
            return _C()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _Ctx())
    monkeypatch.setattr(wolf_app, "_v32_stats_start_ts", lambda cur: 1_780_000_000)
    out = wolf_app.get_stats_v32()
    assert out["ok"] is True and out["total"] == 9 and out["wins"] == 3
    assert out["basis"]["n"] == 9 and out["basis"]["wins"] == 3
    assert "EXPIRED" in out["basis"]["outcomes_counted"]
    assert out["basis"]["date_range"]["start"] == "2026-05-28"


def test_paper_wallet_gated_book_never_mirrors_research_picks(monkeypatch):
    import core.db as db
    import core.paper_wallet as pw

    monkeypatch.setenv("PAPER_WALLET_ENABLED", "1")
    monkeypatch.setenv("PAPER_SESSION_GATE", "0")
    inserts, gated_sql = [], []

    class _Cur:
        rowcount = 0

        def execute(self, sql, params=None):
            self._last = sql
            if "INSERT INTO ghost_paper_trades" in sql and params is not None:
                inserts.append({"book": params[0], "symbol": params[1]})
                self.rowcount = 1
            else:
                self.rowcount = 0

        def fetchone(self):
            if "COUNT(*) FROM ghost_paper_trades" in self._last:
                return (0,)
            return None

        def fetchall(self):
            s = self._last
            if "FROM predictions" in s:
                gated_sql.append(s)
                # The only live signal is a research pick; a query without the
                # research filter would return it.
                if "research_pick" in s:
                    return []
                return [(501, "RSCH", 10.0, 10.5, 9.7, 9_999_999_999)]
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    monkeypatch.setattr(pw, "_cash", lambda cur, cfg: 1_000_000.0)
    monkeypatch.setattr(pw, "_max_open", lambda: 50)
    monkeypatch.setattr(pw, "get_config", lambda cur: {"starting_balance": 10000.0})
    monkeypatch.setattr(pw, "_maybe_roll_month", lambda cur, cfg: cfg)
    monkeypatch.setattr(pw, "_live_prices", lambda syms: {s.upper(): 10.0 for s in syms})
    monkeypatch.setattr(pw, "fresh_bands",
                        lambda sym, entry, **k: (entry * 1.02, entry * 0.987, 9_999_999_999))
    out = pw.run_wallet_cycle()
    assert out["ok"] is True
    assert gated_sql, "gated query did not run"
    assert not [i for i in inserts if i["book"] == "gated"]


def test_shadow_rows_keep_feature_schema(monkeypatch):
    import core.db as db
    import core.shadow_outcomes as so

    class _Cur:
        def execute(self, sql, params=None):
            assert "feature_schema" in sql

        def fetchall(self):
            return [("WOLF", 1, 0.6, "WIN", 1.0, "UP", 0.6, "sha", "lbl", "val", 3, "fs_v7")]

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db, "db_conn", lambda: _Conn())
    rows = so.load_shadow_rows(days=30)
    assert rows[0]["feature_schema"] == "fs_v7"
    agg = so.aggregate_shadow_stats(rows)
    assert "fs_v7" in repr(agg)


# ── F23 ──────────────────────────────────────────────────────────────────────

def test_ghost_score_120_day_old_losing_pick_does_not_drive_label():
    import api.wolf_endpoints as we

    now = int(time.time())
    stale = {"id": 224034, "direction": "UP", "confidence": 0.75,
             "predicted_at": now - 120 * 86400, "outcome": "LOSS",
             "expires_at": now - 117 * 86400}
    out = we.compute_ghost_score(
        latest_pick=stale, volume_ratio=3.0, sector={"signal": "wolf_lagging_up"},
        current_price=60.0, sma_5d=50.0, now_ts=now, last_scan_ts=now - 60,
    )
    assert out["components"]["model"] == 20.0
    assert out["model_view"] == "stale"
    assert out["signal"] == "NO_CURRENT_VIEW"
    assert out["signal"] not in ("BUY", "STRONG_BUY")


@pytest.mark.parametrize("pick_kw", [
    {"outcome": None, "expires_at": None, "predicted_at_age": 120 * 86400},  # no horizon, too old
    {"outcome": None, "expires_at_offset": -60, "predicted_at_age": 3600},     # past its horizon
    {"outcome": "WITHDRAWN", "expires_at_offset": 86400, "predicted_at_age": 3600},  # resolved
])
def test_ghost_score_stale_pick_variants(pick_kw):
    import api.wolf_endpoints as we

    now = 1_790_000_000
    pick = {"direction": "UP", "confidence": 0.9,
            "predicted_at": now - pick_kw["predicted_at_age"],
            "outcome": pick_kw.get("outcome")}
    if "expires_at_offset" in pick_kw:
        pick["expires_at"] = now + pick_kw["expires_at_offset"]
    out = we.compute_ghost_score(latest_pick=pick, volume_ratio=None, sector=None,
                                 current_price=None, sma_5d=None, now_ts=now)
    assert out["components"]["model"] == 20.0
    assert out["signal"] == "NO_CURRENT_VIEW"


def test_ghost_score_current_pick_still_drives_label():
    import api.wolf_endpoints as we

    now = 1_790_000_000
    pick = {"direction": "UP", "confidence": 0.95, "predicted_at": now - 3600,
            "outcome": None, "expires_at": now + 2 * 86400}
    out = we.compute_ghost_score(latest_pick=pick, volume_ratio=2.5,
                                 sector={"signal": "wolf_lagging_up"},
                                 current_price=70.0, sma_5d=65.0, now_ts=now)
    assert out["model_view"] == "current"
    assert out["components"]["model"] == 38.0
    assert out["signal"] == "STRONG_BUY"
