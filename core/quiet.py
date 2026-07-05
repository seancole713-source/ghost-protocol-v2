"""core/quiet.py — make best-effort exception swallowing auditable (PR #130).

The codebase intentionally uses many best-effort try/except blocks (a failed
side-write must never kill a scan cycle). The problem was silence: 85+ bare
`except: pass` blocks were impossible to audit. Every one of them now calls
note_suppressed() instead, which:

  - logs the swallowed exception at DEBUG on the "ghost.suppressed" logger
    (invisible at production INFO level — zero log-noise change), and
  - counts occurrences per call site in COUNTS for live diagnostics.

To audit in production: raise the logger level
    logging.getLogger("ghost.suppressed").setLevel(logging.DEBUG)
or inspect core.quiet.COUNTS for hot suppression sites.
"""
import collections
import logging
import sys

LOGGER = logging.getLogger("ghost.suppressed")

# call-site ("file.py:123") -> number of exceptions swallowed there
COUNTS: "collections.Counter[str]" = collections.Counter()


def note_suppressed(context: str = "") -> None:
    """Record a deliberately swallowed exception. Never raises."""
    try:
        f = sys._getframe(1)
        site = f"{f.f_code.co_filename.rsplit('/', 1)[-1]}:{f.f_lineno}"
        COUNTS[site] += 1
        LOGGER.debug("suppressed at %s %s", site, context, exc_info=True)
    except Exception:
        pass  # the auditor must never become a new failure mode
