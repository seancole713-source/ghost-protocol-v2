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


def readout_for_snapshots(orders, paper_record=None):
    """Exercise the production readout boundary, with no broker or database."""
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
    store.put("edge_paper", "f1|paper", {"state": "submitted", "order_id": "entry1", **(paper_record or {})})
    store.put("edge_paper_orders", day, {"orders": orders})
    before = deepcopy(store._t)
    result = paper(store, day)
    assert store._t == before
    return result


def entry_snapshot():
    r = row()
    return dict(r["broker_entry"], client_order_id="f1-entry", legs=r["broker_exits"])


def test_readout_preserves_same_client_conflicting_entry_prices_in_both_orders():
    a, b = entry_snapshot(), entry_snapshot()
    b["filled_avg_price"] = "9"
    summaries = []
    for snapshots in ([a, b], [b, a]):
        out = readout_for_snapshots(snapshots)
        summary = out["financial_summary"]
        assert summary["completed_orders"] == 0
        assert summary["unreconciled_orders"] == 1 and summary["issues"]
        assert len(out["orders"][0]["broker_snapshots"]) == 2
        summaries.append(summary)
    assert summaries[0] == summaries[1]


def test_readout_links_broker_and_client_aliases_without_double_counting():
    from itertools import permutations

    both = entry_snapshot()
    both["legs"][0]["client_order_id"] = "f1-exit"
    broker_only, client_only = deepcopy(both), deepcopy(both)
    del broker_only["client_order_id"]
    del broker_only["legs"][0]["client_order_id"]
    del client_only["id"]
    del client_only["legs"][0]["id"]
    summaries = []
    for snapshots in permutations([both, broker_only, client_only]):
        out = readout_for_snapshots(list(snapshots))["financial_summary"]
        assert out["broker_orders"] == out["completed_orders"] == 1
        assert out["gross_pnl_usd"] == 10
        assert out["duplicate_order_rows_excluded"] == 2
        assert out["issues"] == []
        summaries.append(out)
    assert all(s == summaries[0] for s in summaries)


def test_readout_different_or_missing_exit_sets_never_use_first_snapshot():
    for kind in ("empty", "partial", "different_complete"):
        a, b = entry_snapshot(), entry_snapshot()
        if kind == "empty":
            b["legs"] = []
        elif kind == "partial":
            b["legs"][0]["filled_qty"] = "5"
        else:
            b["legs"][0]["id"] = "another-exit"
        summaries = []
        for snapshots in ([a, b], [b, a]):
            out = readout_for_snapshots(snapshots)["financial_summary"]
            assert out["completed_orders"] == out["gross_pnl_usd"] == 0
            assert out["unreconciled_orders"] == 1 and out["issues"]
            summaries.append(out)
        assert summaries[0] == summaries[1]


def test_readout_conflicting_broker_ids_for_one_client_are_unreconciled():
    a, b = entry_snapshot(), entry_snapshot()
    b["id"] = "different-broker-id"
    out = readout_for_snapshots([a, b])["financial_summary"]
    assert out["broker_orders"] == out["unreconciled_orders"] == 1
    assert out["completed_orders"] == 0 and out["issues"]


def test_identity_links_do_not_escape_the_store_snapshot():
    first = readout_for_snapshots([entry_snapshot()])["financial_summary"]
    other = entry_snapshot()
    other["filled_avg_price"] = "9"
    second = readout_for_snapshots([other])["financial_summary"]
    assert first["gross_pnl_usd"] == 10
    assert second["gross_pnl_usd"] == 20
    assert first["completed_orders"] == second["completed_orders"] == 1
    assert first["issues"] == second["issues"] == []


def test_duplicate_helper_rows_with_different_complete_exit_sets_are_unreconciled():
    a, b = row(), row()
    b["broker_exits"][0]["id"] = "other-exit"
    for snapshots in ([a, b], [b, a]):
        out = paper_summary(snapshots)
        assert out["completed_orders"] == out["gross_pnl_usd"] == 0
        assert out["unreconciled_orders"] == 1 and out["issues"]


def test_helper_broker_and_client_aliases_count_one_cash_order():
    both = row()
    both["broker_entry"]["client_order_id"] = "entry-client"
    both["broker_exits"][0]["client_order_id"] = "exit-client"
    client_only = deepcopy(both)
    del client_only["broker_entry"]["id"]
    del client_only["broker_exits"][0]["id"]
    for snapshots in ([both, client_only], [client_only, both]):
        out = paper_summary(snapshots)
        assert out["completed_orders"] == out["broker_orders"] == 1
        assert out["gross_pnl_usd"] == 10
        assert out["duplicate_order_rows_excluded"] == 1 and out["issues"] == []


def test_top_level_exit_alias_conflicts_reach_readout_accounting():
    from itertools import permutations, product

    for suffix, (field, value) in product(("tx", "px", "pf"),
                                         (("filled_avg_price", "8"), ("filled_qty", "5"))):
        entry = entry_snapshot()
        entry["legs"] = []
        exit_order = {"id": "time-exit", "client_order_id": f"f1-{suffix}",
                      "filled_qty": "10", "filled_avg_price": "11", "type": "limit"}
        hidden = dict(exit_order)
        hidden[field] = value
        del hidden["client_order_id"]
        summaries = []
        for orders in permutations([entry, exit_order, hidden]):
            out = readout_for_snapshots(list(orders))
            assert len(out["orders"][0]["broker_exits"]) == 2
            summary = out["financial_summary"]
            assert summary["completed_orders"] == summary["gross_pnl_usd"] == 0
            assert summary["unreconciled_orders"] == 1 and summary["issues"]
            summaries.append(summary)
        assert all(s == summaries[0] for s in summaries)


def test_top_level_exit_alias_identical_snapshots_count_once():
    for suffix in ("tx", "px", "pf"):
        entry = entry_snapshot()
        entry["legs"] = []
        exit_order = {"id": "exit1", "client_order_id": f"f1-{suffix}",
                      "filled_qty": "10", "filled_avg_price": "11"}
        alias = dict(exit_order)
        del alias["client_order_id"]
        for orders in ([entry, exit_order, alias], [alias, exit_order, entry]):
            out = readout_for_snapshots(orders)["financial_summary"]
            assert out["completed_orders"] == 1 and out["gross_pnl_usd"] == 10
            assert out["unreconciled_orders"] == 0 and out["issues"] == []


def test_protective_child_broker_alias_conflict_is_not_hidden():
    entry = entry_snapshot()
    entry["legs"] = []
    protective = {"id": "protective", "client_order_id": "f1-px", "filled_qty": "0",
                  "legs": [{"id": "child", "filled_qty": "10", "filled_avg_price": "11"}]}
    hidden = {"id": "child", "filled_qty": "10", "filled_avg_price": "8"}
    for orders in ([entry, protective, hidden], [hidden, protective, entry]):
        out = readout_for_snapshots(orders)["financial_summary"]
        assert out["completed_orders"] == out["gross_pnl_usd"] == 0
        assert out["unreconciled_orders"] == 1 and out["issues"]


def test_bracket_child_broker_alias_conflict_reaches_accounting():
    entry = entry_snapshot()
    hidden = {"id": "exit1", "filled_qty": "10", "filled_avg_price": "8"}
    for orders in ([entry, hidden], [hidden, entry]):
        out = readout_for_snapshots(orders)["financial_summary"]
        assert out["completed_orders"] == out["gross_pnl_usd"] == 0
        assert out["unreconciled_orders"] == 1 and out["issues"]


def test_position_close_client_only_alias_conflict_reaches_accounting():
    entry = entry_snapshot()
    entry["legs"] = []
    close = {"id": "position-close", "client_order_id": "manual-close",
             "filled_qty": "10", "filled_avg_price": "11"}
    hidden = {"client_order_id": "manual-close", "filled_qty": "10", "filled_avg_price": "8"}
    record = {"tx_closes": [{"order_id": "position-close"}]}
    for orders in ([entry, close, hidden], [hidden, close, entry]):
        out = readout_for_snapshots(orders, record)["financial_summary"]
        assert out["completed_orders"] == out["gross_pnl_usd"] == 0
        assert out["unreconciled_orders"] == 1 and out["issues"]
