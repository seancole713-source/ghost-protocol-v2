"""Accuracy-contract integrity — regression tests for the 4 audit findings.

F1  Sentiment may only BRAKE confidence (never inflate position size).
F2  Research picks are never alerted as live BUY/SELL signals.
F3  Research picks are excluded from every outcome-based display metric.
F4  Holdout train/calib slices are purged so the precision gate's proof
    is not leak-inflated by look-ahead labels.
"""
import inspect

import wolf_app


# ── F1 · sentiment is brake-only ─────────────────────────────────────────

def test_sentiment_adjustment_never_positive():
    from core.prediction import sentiment_confidence_adjustment as adj
    # Aligned news (UP trade, positive sentiment) must NOT raise confidence.
    assert adj(0.8, "UP") == 0.0
    assert adj(0.3, "BUY") == 0.0
    # DOWN trade with negative news is aligned → also no boost.
    assert adj(-0.8, "DOWN") == 0.0


def test_sentiment_adjustment_brakes_against_trade():
    from core.prediction import sentiment_confidence_adjustment as adj
    # News against the trade still reduces confidence (risk brake).
    assert adj(-0.8, "UP") == -0.08
    assert adj(-1.0, "UP") == -0.10          # capped at -0.10
    assert adj(0.8, "DOWN") == -0.08          # positive news vs short


def test_sentiment_adjustment_dead_zone_and_garbage():
    from core.prediction import sentiment_confidence_adjustment as adj
    assert adj(0.05, "UP") == 0.0             # inside ±0.1 dead zone
    assert adj(-0.1, "UP") == 0.0
    assert adj("not-a-number", "UP") == 0.0   # garbage in → neutral out


# ── F2 · research picks never alerted ────────────────────────────────────

class _AlertCursor:
    """Scripts wolf_signal_alert_check's SQL path; captures executed SQL."""

    def __init__(self):
        self.executed = []
        self.last_sql = ""

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.executed.append((sql, params))

    def fetchone(self):
        if "SELECT COUNT(*) FROM wolf_signal_alerts" in self.last_sql:
            return (0,)
        return None

    def fetchall(self):
        return []


def test_alert_candidate_query_excludes_research_picks(monkeypatch):
    """The Telegram/email sweep must filter research picks — they are learning
    probes fired below the accuracy contract, not actionable signals."""
    monkeypatch.setenv("CRON_SECRET", "")
    cur = _AlertCursor()

    class _Conn:
        def cursor(self):
            return cur

    class _DbCtx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _DbCtx())
    wolf_app.wolf_signal_alert_check(x_cron_secret="")
    candidate_sql = next(
        (sql for sql, _p in cur.executed
         if "FROM predictions" in sql and "LEFT JOIN wolf_signal_alerts" in sql),
        None,
    )
    assert candidate_sql is not None, "candidate query never executed"
    assert "research_pick" in candidate_sql, (
        "alert sweep must exclude scores->>'research_pick' = 'true' rows"
    )


def test_non_research_where_alias_helper():
    from core.prediction_filters import NON_RESEARCH_WHERE, non_research_where
    # Unaliased form is semantically identical to the constant.
    assert non_research_where() == NON_RESEARCH_WHERE
    assert "p.scores->>'research_pick'" in non_research_where("p")


# ── F3 · display metrics exclude research picks ──────────────────────────

def test_headline_stats_exclude_research_picks():
    """post_v32 / lifetime win-rate queries must carry the research filter so
    the number users see matches the number the gates use."""
    src = inspect.getsource(wolf_app._compute_get_stats)
    assert src.count("NON_RESEARCH_WHERE") >= 3, (
        "all three outcome queries in _compute_get_stats need NON_RESEARCH_WHERE"
    )


def test_direction_stats_exclude_research_picks():
    import core.stats_direction as sd
    src = inspect.getsource(sd.compute_stats_by_direction)
    assert "NON_RESEARCH_WHERE" in src


def test_stats_v32_endpoint_excludes_research_picks():
    src = inspect.getsource(wolf_app.get_stats_v32)
    assert src.count("NON_RESEARCH_WHERE") >= 2, (
        "both WIN/LOSS queries in /api/stats/v32 need NON_RESEARCH_WHERE"
    )


# ── F4 · purged holdout slices ───────────────────────────────────────────

def test_purged_holdout_bounds_drop_lookahead_tails(monkeypatch):
    import core.signal_engine as se
    # n=250 default split: train_end=175, calib_end=212; purge=3 →
    # train fits on [:172], calib on [175:209], gate untouched at [212:].
    train_fit_end, calib_fit_end = se._purged_holdout_bounds(250, 175, 212, 3)
    assert train_fit_end == 172
    assert calib_fit_end == 209


def test_purged_holdout_bounds_purge_zero_is_identity():
    import core.signal_engine as se
    assert se._purged_holdout_bounds(250, 175, 212, 0) == (175, 212)


def test_purged_holdout_bounds_tiny_n_guards():
    import core.signal_engine as se
    # Guards keep >=1 train row and a non-empty calib start on tiny datasets.
    train_fit_end, calib_fit_end = se._purged_holdout_bounds(6, 2, 4, 5)
    assert train_fit_end >= 1
    assert calib_fit_end >= 3   # train_end + 1


def test_training_path_uses_purged_bounds():
    """Tripwire: the training path must slice through the purge helper."""
    import core.signal_engine as se
    src = inspect.getsource(se._train_one_direction)
    assert "_purged_holdout_bounds" in src
    assert "rows[:train_fit_end]" in src
