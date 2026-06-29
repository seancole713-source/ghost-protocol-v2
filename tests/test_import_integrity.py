"""Import-integrity regression gate.

WHY THIS TEST EXISTS
--------------------
The 427-test suite was green while production logged:

    [SqueezeMonitor] Telegram failed [AMC:squeeze_forming]:
        No module named 'core.telegram_hunter'

Both ``core/squeeze_monitor.py`` and ``core/wolf_monitor.py`` import
``core.telegram_hunter`` lazily inside a ``try/except`` in the alert function.
No unit test fires an alert, so the broken import never executes and the suite
stays green. ``scripts/check_import_integrity.py`` statically catches this.

This test wires that scanner into pytest with two guarantees:

1. RATCHET: the set of broken first-party imports must not GROW. It is pinned to
   a known baseline (today's reality) so the suite stays green, but any NEW
   broken first-party import added later fails CI immediately.
2. LIVE-PATH XFAIL: the telegram alert modules are on the live runtime path
   (started by wolf_app). Their broken import is tracked as a strict xfail so it
   shows up in reports and auto-converts to a real PASS the moment someone adds
   ``core/telegram_hunter.py`` (or repoints the import at ``core.telegram``).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPT = REPO_ROOT / "scripts" / "check_import_integrity.py"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("_ghost_import_integrity", SCRIPT)
    assert spec and spec.loader, "cannot load check_import_integrity.py"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Known-broken first-party modules as of the audit that introduced this test.
# Most live in legacy/optional paths (engines/startup.py is not imported by the
# app; core/stock_engine.py is referenced only by a backtest comment) and are
# all guarded by try/except EXCEPT core.pattern_tracker. Shrinking this set is
# encouraged; growing it should require a deliberate baseline update.
KNOWN_BROKEN_BASELINE = {
    "core.accuracy_tracker",
    "core.ai_advisor.scanner",
    "core.ai_memory",
    "core.auto_calibrate_scheduler",
    "core.auto_prediction_loop",
    "core.confidence_calibrator",
    "core.data_pillars.feature_orchestrator",
    "core.db_pool",
    "core.economic_calendar",
    "core.ensemble_predictor",
    "core.feedback_loop",
    "core.goals_tracker",
    "core.heartbeat",
    "core.learning_loop",
    "core.migration_runner",
    "core.ml_trainer",
    "core.online_calibrator",
    "core.paper_tracker",
    "core.pattern_tracker",
    "core.position_sizer",
    "core.prediction_evaluator",
    "core.prediction_killswitch",
    "core.prediction_store",
    "core.regime_detector",
    "core.sector_momentum",
    "core.shutdown_handler",
    "core.stock_gates",
    "core.v2_quality",
    "core.watchlist_prediction_scheduler",
}


def test_no_new_broken_first_party_imports():
    """Ratchet: broken first-party imports must not grow beyond the baseline."""
    scanner = _load_scanner()
    result = scanner.scan()
    broken = {m["module"] for m in result["missing"]}
    new_broken = broken - KNOWN_BROKEN_BASELINE
    assert not new_broken, (
        "New broken first-party import(s) introduced: "
        + ", ".join(sorted(new_broken))
        + ". Fix the import or, if intentional, update KNOWN_BROKEN_BASELINE."
    )


def test_baseline_does_not_silently_overstate():
    """If every baseline module gets fixed, clear the baseline.

    Keeps the allowlist honest so it can't hide a future real regression behind
    a stale name. Fails only if EVERY listed module now resolves.
    """
    scanner = _load_scanner()
    result = scanner.scan()
    still_broken = {m["module"] for m in result["missing"]} & KNOWN_BROKEN_BASELINE
    assert still_broken, (
        "Every module in KNOWN_BROKEN_BASELINE now resolves — clear the baseline."
    )


def test_live_alert_path_imports_resolve():
    """The Telegram alert modules are started by wolf_app at runtime.

    Previously a strict ``xfail``: ``core.telegram_hunter`` did not exist, so the
    squeeze/wolf alert paths failed at runtime. Fixed by adding
    ``core/telegram_hunter.py`` (a compatibility shim re-exporting
    ``send_telegram_message`` from ``core.telegram``). This now asserts the live
    alert import resolves AND that the shim exposes the expected callable.
    """
    assert importlib.util.find_spec("core.telegram_hunter") is not None
    from core.telegram_hunter import send_telegram_message
    from core.telegram import send_telegram_message as canonical
    assert callable(send_telegram_message)
    assert send_telegram_message is canonical
