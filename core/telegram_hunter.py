"""core/telegram_hunter.py — compatibility shim.

WHY THIS EXISTS
---------------
``core/squeeze_monitor.py`` and ``core/wolf_monitor.py`` import the alert sender
as ``from core.telegram_hunter import send_telegram_message``. That module was
never created — the real implementation lives in ``core.telegram`` — so every
live squeeze/wolf alert failed at runtime with::

    [SqueezeMonitor] Telegram failed [AMC:squeeze_forming]:
        No module named 'core.telegram_hunter'

The imports are lazy and wrapped in ``try/except``, so the unit suite stayed
green while production alerts silently broke. This shim re-exports the canonical
sender from ``core.telegram`` so the live alert path resolves. New code should
import directly from ``core.telegram``.
"""
from __future__ import annotations

from core.telegram import send_telegram_message

__all__ = ["send_telegram_message"]
