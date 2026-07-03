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


# PR #125 audit: dead code removed (stock_engine.py, world_feed_fusion.py,
# engines/startup.py, model.py, routes/schema.py). All first-party imports
# now resolve. Baseline cleared — the ratchet starts fresh.
KNOWN_BROKEN_BASELINE: set[str] = set()


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
    a stale name. When the baseline is empty (all imports clean), this is a PASS.
    """
    scanner = _load_scanner()
    result = scanner.scan()
    still_broken = {m["module"] for m in result["missing"]} & KNOWN_BROKEN_BASELINE
    if KNOWN_BROKEN_BASELINE:
        assert still_broken, (
            "Every module in KNOWN_BROKEN_BASELINE now resolves — clear the baseline."
        )
    # Empty baseline + no broken imports = clean state. This is the goal.


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
