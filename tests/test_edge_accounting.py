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
