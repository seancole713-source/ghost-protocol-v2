"""GET /api/stats/v32 — the "total" vs "resolved_total" cohort split.

Investigated 2026-09-01 as a suspected bug (live prod: wins=3/losses=6/total=9
vs resolved_wins=3/resolved_losses=7/resolved_total=10 — a one-LOSS gap). Root
cause traced against the live production database (read-only): prediction id
224020 (TSLA, UP) was predicted_at=2026-04-04T18:00:38Z, one day BEFORE the
V3_STATS_START_TS cutover (2026-04-05T00:00:00Z), and resolved_at=2026-04-07
with outcome=LOSS. get_stats_v32()'s own docstring/comment already documents
this: the "total" fields are an *issuance* cohort (predicted_at >= cutover)
and the "resolved_total" fields are a *resolution* cohort (resolved_at >=
cutover, "can include picks issued before cutover"). TSLA/224020 straddles
the boundary, so it is correctly excluded from the issuance cohort (it was
predicted before v3.2 started) and correctly included in the resolution
cohort (it closed after v3.2 started). This is one intentional dual-window
design computed by two deliberately different WHERE clauses, not two paths
that should agree — verified live, not fixed. This test locks in that
behavior so a future "unify the two queries" change does not silently erase
the resolution-window cohort's own well-documented purpose.
"""
from tests.test_wolf_app_core import QueueCursor, _patch_db_conn_with_cursor


def test_stats_v32_issuance_and_resolution_cohorts_can_legitimately_differ(monkeypatch):
    import wolf_app

    monkeypatch.setattr(wolf_app, "_v32_stats_start_ts", lambda cur: 1775347200)
    # Query order inside get_stats_v32(): (1) predicted_at-window GROUP BY
    # outcome, (2) resolved_at-window GROUP BY outcome, (3) open-picks count.
    # Fixture mirrors the live production numbers verified 2026-09-01.
    cur = QueueCursor(
        fetchall_values=[
            [("WIN", 3), ("LOSS", 6)],   # predicted_at >= cutover (issuance)
            [("WIN", 3), ("LOSS", 7)],   # resolved_at >= cutover (resolution)
        ],
        fetchone_values=[(0,)],
    )
    _patch_db_conn_with_cursor(monkeypatch, cur)

    out = wolf_app.get_stats_v32()

    assert out["ok"] is True
    assert (out["wins"], out["losses"], out["total"]) == (3, 6, 9)
    assert (out["resolved_wins"], out["resolved_losses"], out["resolved_total"]) == (3, 7, 10)
    # The resolution cohort can carry straddling picks the issuance cohort
    # never had a chance to count (predicted before cutover, resolved after)
    # -- resolved_total must never be forced equal to total.
    assert out["resolved_total"] - out["total"] == 1
    assert out["resolved_losses"] - out["losses"] == 1


def test_stats_v32_cohorts_match_when_no_straddling_picks(monkeypatch):
    """When nothing straddles the cutover the two cohorts DO agree -- the
    split is a real population difference, not a fixed offset."""
    import wolf_app

    monkeypatch.setattr(wolf_app, "_v32_stats_start_ts", lambda cur: 1775347200)
    cur = QueueCursor(
        fetchall_values=[
            [("WIN", 5), ("LOSS", 2)],
            [("WIN", 5), ("LOSS", 2)],
        ],
        fetchone_values=[(0,)],
    )
    _patch_db_conn_with_cursor(monkeypatch, cur)

    out = wolf_app.get_stats_v32()
    assert (out["wins"], out["losses"], out["total"]) == (5, 2, 7)
    assert (out["resolved_wins"], out["resolved_losses"], out["resolved_total"]) == (5, 2, 7)
