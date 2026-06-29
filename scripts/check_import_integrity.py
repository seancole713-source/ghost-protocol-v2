#!/usr/bin/env python3
"""Static import-integrity gate for first-party modules.

WHY THIS EXISTS
---------------
Ghost has 427 green unit tests, yet production logged:

    [SqueezeMonitor] Telegram failed [AMC:squeeze_forming]:
        No module named 'core.telegram_hunter'

Root cause: ``core/squeeze_monitor.py`` and ``core/wolf_monitor.py`` import
``core.telegram_hunter`` *lazily* (inside the alert function). That module does
not exist (the real module is ``core.telegram``). Because the import only runs
when an alert actually fires, no unit test ever executes the line, so the test
suite stays green while the alert path is broken in production.

This checker walks every first-party ``.py`` file, parses it with ``ast`` (no
code execution), collects EVERY import — including imports nested inside
functions/methods/try-blocks — and verifies that each first-party target module
actually resolves with ``importlib.util.find_spec``. It exits non-zero if any
first-party import points at a module that cannot be found.

It does NOT import third-party packages or run any module code, so it is safe to
run in CI without network or heavy ML deps.

Usage:
    python3 scripts/check_import_integrity.py            # human output
    python3 scripts/check_import_integrity.py --json     # machine output
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Top-level first-party roots. A dotted import whose first segment is one of
# these (or a bare module matching a root-level .py file) is treated as
# first-party and MUST resolve.
FIRST_PARTY_PACKAGES = {"core", "api", "engines", "config", "mcp", "scripts"}

REPO_ROOT = Path(__file__).resolve().parent.parent

# Resolve first-party modules the same way the app does at runtime: uvicorn is
# launched from the repo root, so the repo root must be importable. When this
# script is run as ``python3 scripts/check_import_integrity.py`` the default
# ``sys.path[0]`` is the ``scripts/`` dir, NOT the repo root, which would make
# every ``core.*`` / ``api.*`` import look unresolvable. Put the repo root first.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _root_level_modules() -> set:
    """Bare module names importable from the repo root (e.g. ``wolf_app``)."""
    mods = set()
    for p in REPO_ROOT.glob("*.py"):
        mods.add(p.stem)
    return mods


def _iter_first_party_files() -> List[Path]:
    files: List[Path] = []
    for pkg in sorted(FIRST_PARTY_PACKAGES):
        d = REPO_ROOT / pkg
        if d.is_dir():
            files.extend(sorted(d.rglob("*.py")))
    for p in sorted(REPO_ROOT.glob("*.py")):
        files.append(p)
    out: List[Path] = []
    seen = set()
    for f in files:
        if "__pycache__" in f.parts or ".venv" in f.parts or "node_modules" in f.parts:
            continue
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _is_first_party(module: str, root_modules: set) -> bool:
    if not module:
        return False
    top = module.split(".")[0]
    if top in FIRST_PARTY_PACKAGES:
        return True
    return module in root_modules or top in root_modules


def _collect_targets(tree: ast.AST) -> List[Dict[str, Any]]:
    """Return every imported module name with its line number.

    ``ast.walk`` reaches imports nested inside functions, methods, and
    try/except blocks — exactly where the telegram_hunter bug hides.
    Relative imports (``from . import x``) are skipped: they resolve against
    package context and are not the failure mode we guard here.
    """
    targets: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append({"module": alias.name, "lineno": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import — out of scope
            if node.module:
                targets.append({"module": node.module, "lineno": node.lineno})
    return targets


def _resolvable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False
    except Exception:
        return False


def scan() -> Dict[str, Any]:
    root_modules = _root_level_modules()
    missing: List[Dict[str, Any]] = []
    checked = 0
    files = _iter_first_party_files()
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - defensive
            missing.append(
                {
                    "file": str(path.relative_to(REPO_ROOT)),
                    "lineno": getattr(exc, "lineno", 0) or 0,
                    "module": "<syntax-error>",
                    "error": str(exc),
                }
            )
            continue
        for tgt in _collect_targets(tree):
            module = tgt["module"]
            if not _is_first_party(module, root_modules):
                continue
            checked += 1
            if not _resolvable(module):
                missing.append(
                    {
                        "file": str(path.relative_to(REPO_ROOT)),
                        "lineno": tgt["lineno"],
                        "module": module,
                        "error": "first-party module does not resolve",
                    }
                )
    return {
        "files_scanned": len(files),
        "first_party_imports_checked": checked,
        "missing": missing,
        "ok": not missing,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    result = scan()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"import-integrity: scanned {result['files_scanned']} files, "
            f"checked {result['first_party_imports_checked']} first-party imports"
        )
        if result["ok"]:
            print("PASS: all first-party imports resolve")
        else:
            print(f"FAIL: {len(result['missing'])} broken first-party import(s):")
            for m in result["missing"]:
                print(f"  - {m['file']}:{m['lineno']} -> {m['module']} ({m['error']})")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
