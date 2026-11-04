from copy import deepcopy

from edge.accounting import paper_summary


def row(pnl=10, counted=True):
    return {
        "forecast_id": "f1",
        "symbol": "ABC",
        "actual": "WIN" if pnl > 0 else "LOSS",
        "actual_counted": counted,
        "actual_pnl_usd": pnl,
        "broker_entry": {"id": "entry1", "filled_qty": "10", "filled_avg_price": "10"},
        "broker_exits": [
            {"id": "exit1", "filled_qty": "10", "filled_avg_price": str(10 + pnl / 10)}
        ],
    }


def test_outside_rule_losses_still_count_financially():
    out = paper_summary([row(-20, False)])
    assert out["gross_pnl_usd"] == out["outside_rule_pnl_usd"] == -20
    assert out["completed_orders"] == out["outside_rule_completed"] == 1
    assert out["profitable_rate"] == 0


def test_shared_and_duplicate_results_are_not_extra_money():
    out = paper_summary(
        [row(), row(), {"shared_order_source": "f1", "actual_pnl_usd": 10}]
    )
    assert out["gross_pnl_usd"] == 10
    assert (
        out["completed_orders"]
        == out["shared_results_excluded"]
        == out["duplicate_order_rows_excluded"]
        == 1
    )


def test_partial_exit_is_not_a_completed_trade():
    r = row()
    r["broker_exits"][0]["filled_qty"] = "5"
    out = paper_summary([r])
    assert out["completed_orders"] == 0 and out["unreconciled_orders"] == 1


def test_exit_dedup_and_disagreement_reported():
    r = row()
    r["broker_exits"].append(deepcopy(r["broker_exits"][0]))
    r["actual_pnl_usd"] = 99
    out = paper_summary([r])
    assert out["gross_pnl_usd"] == 10 and out["issues"]


def test_nan_never_enters_pnl():
    r = row()
    r["broker_entry"]["filled_qty"] = "nan"
    assert paper_summary([r])["completed_orders"] == 0


def test_actual_loss_without_entry_snapshot_is_exposed_not_clean_zero():
    r = row(-20)
    r["broker_entry"] = None
    out = paper_summary([r])
    assert out["completed_orders"] == 0
    assert out["missing_entry_orders"] == out["unreconciled_orders"] == 1
    assert out["issues"] == ["f1: broker entry evidence missing"]
    # An outcome is not a substitute for broker fills in financial totals.
    assert out["gross_pnl_usd"] == 0


def test_submitted_order_without_entry_or_outcome_is_unreconciled():
    out = paper_summary([{"forecast_id": "pending", "paper_state": "submitted"}])
    assert out["missing_entry_orders"] == out["unreconciled_orders"] == 1
    assert out["issues"]


def test_no_order_and_shared_outcomes_do_not_create_missing_entry_alarms():
    out = paper_summary([
        {"forecast_id": "observe-only"},
        {"forecast_id": "no-fill", "actual": "NO_FILL", "actual_pnl_usd": 0},
        {"forecast_id": "shared", "shared_order_source": "f1",
         "actual": "LOSS", "actual_pnl_usd": -20},
    ])
    assert out["missing_entry_orders"] == out["unreconciled_orders"] == 0
    assert out["shared_results_excluded"] == 1
    assert out["issues"] == []


def test_forecast_identity_cannot_replace_missing_broker_order_identity():
    r = row()
    del r["broker_entry"]["id"]
    out = paper_summary([r])
    assert out["broker_orders"] == out["completed_orders"] == 0
    assert out["unreconciled_orders"] == 1
    assert "lacks broker order identity" in out["issues"][0]


def test_client_order_identity_is_accepted_when_broker_id_is_absent():
    r = row()
    r["broker_entry"]["client_order_id"] = r["broker_entry"].pop("id")
    assert paper_summary([r])["completed_orders"] == 1


def test_conflicting_duplicate_entry_fills_are_unreconciled_in_either_order():
    for field, value in (("filled_avg_price", "11"), ("filled_qty", "5")):
        first, second = row(), row()
        second["broker_entry"][field] = value
        for rows in ([first, second], [second, first]):
            out = paper_summary(rows)
            assert out["completed_orders"] == out["gross_pnl_usd"] == 0
            assert out["unreconciled_orders"] == 1
            assert out["duplicate_order_rows_excluded"] == 1
            assert "conflicting broker fill snapshots" in out["issues"][0]


def test_conflicting_duplicate_exit_prices_and_quantities_are_not_first_wins():
    for field, value in (("filled_avg_price", "8"), ("filled_qty", "5")):
        r = row()
        duplicate = deepcopy(r["broker_exits"][0])
        duplicate[field] = value
        r["broker_exits"].append(duplicate)
        for exits in (r["broker_exits"], list(reversed(r["broker_exits"]))):
            out = paper_summary([dict(r, broker_exits=exits)])
            assert out["completed_orders"] == out["gross_pnl_usd"] == 0
            assert out["unreconciled_orders"] == 1 and out["issues"]


def test_conflicting_exit_snapshots_across_duplicate_entry_rows_are_exposed():
    first, second = row(), row()
    second["broker_exits"][0]["filled_avg_price"] = "8"
    out = paper_summary([first, second])
    assert out["completed_orders"] == 0 and out["unreconciled_orders"] == 1
    assert "conflicting broker fill snapshots" in out["issues"][0]


def test_paper_readout_preserves_outside_rule_loss_and_deduplicates_shared_result():
    from edge.ledger import MemoryStore
    from edge.readout import paper

    store = MemoryStore()
    day = "2026-10-22"
    forecast = {
        "symbol": "ABC",
        "session_date": day,
        "issued_at": 100,
        "entry_expiry": 200,
        "entry_trigger": 10,
        "entry_limit": 10.1,
        "target": 11,
        "stop": 8,
        "shares": 10,
    }
    for fid in ("f1", "f2"):
        store.put("forecasts", fid, dict(forecast, forecast_id=fid, experiment_id=fid))
        store.put("outcomes", fid + "|actual", {"outcome": "LOSS", "pnl_usd": -20})
        store.put("outcomes", fid + "|forecast", {"outcome": "NO_FILL"})
    store.put("edge_paper", "f1|paper", {"state": "submitted"})
    store.put(
        "edge_paper_orders",
        day,
        {
            "orders": [
                {
                    "id": "entry1",
                    "client_order_id": "f1-entry",
                    "filled_qty": "10",
                    "filled_avg_price": "10",
                    "legs": [
                        {
                            "id": "exit1",
                            "type": "stop",
                            "filled_qty": "10",
                            "filled_avg_price": "8",
                        }
                    ],
                }
            ]
        },
    )
    before = deepcopy(store.scan("outcomes"))
    out = paper(store, day)
    assert out["financial_summary"]["gross_pnl_usd"] == -20
    assert out["financial_summary"]["outside_rule_pnl_usd"] == -20
    assert out["financial_summary"]["completed_orders"] == 1
    assert out["financial_summary"]["shared_results_excluded"] == 1
    assert all(not r["actual_counted"] for r in out["orders"])
    assert store.scan("outcomes") == before


def test_paper_readout_exposes_missing_snapshot_for_submitted_actual_loss():
    from edge.ledger import MemoryStore
    from edge.readout import paper

    store = MemoryStore()
    day = "2026-10-22"
    store.put("forecasts", "f1", {
        "forecast_id": "f1", "experiment_id": "test", "symbol": "ABC",
        "session_date": day, "issued_at": 100, "entry_expiry": 200,
        "entry_trigger": 10, "entry_limit": 10.1, "target": 11, "stop": 8,
        "shares": 10,
    })
    store.put("edge_paper", "f1|paper", {"state": "submitted"})
    store.put("outcomes", "f1|actual", {"outcome": "LOSS", "pnl_usd": -20})
    before = deepcopy(store._t)
    out = paper(store, day)
    assert out["orders"][0]["actual_pnl_usd"] == -20
    assert out["orders"][0]["broker_entry"] is None
    assert out["financial_summary"]["missing_entry_orders"] == 1
    assert out["financial_summary"]["unreconciled_orders"] == 1
    assert out["financial_summary"]["issues"]
    assert store._t == before
