import os

src_path = "wolf_app.py"
with open(src_path) as f:
    src = f.read()

changed = False

# Patch 1: replace placeholder cockpit with cockpit.html file-read
OLD = "@APP.get(\"/cockpit\")\ndef cockpit():\n    html = (\"<h1>Ghost Protocol v2</h1><ul>\"\n           \"<li><a href=/health>/health</a></li>\"\n           \"<li><a href=/api/picks>/api/picks</a></li>\"\n           \"<li><a href=/api/history>/api/history</a></li>\"\n           \"<li><a href=/api/news>/api/news</a></li>\"\n           \"<li><a href=/api/schema>/api/schema</a></li>\"\n           \"</ul><p>Full dashboard coming Week 4.</p>\")\n    return HTMLResponse(html)"
NEW = (
    "@APP.get(\"/cockpit\", include_in_schema=False)\n"
    "def cockpit():\n"
    "    import os as _os\n"
    "    _p = _os.path.join(_os.path.dirname(__file__), \"cockpit.html\")\n"
    "    with open(_p, encoding=\"utf-8\") as _f:\n"
    "        return HTMLResponse(_f.read())"
)
if 'cockpit.html' not in src:
    if OLD in src:
        src = src.replace(OLD, NEW)
        changed = True
        print("[patch] cockpit patched")
    else:
        print("[patch] WARN: cockpit anchor not found, appending")
        src += "\n" + NEW + "\n"
        changed = True
else:
    print("[patch] cockpit.html already wired")

# Patch 3: portfolio router
if "portfolio_routes" not in src:
    src += """
from core.portfolio_routes import portfolio_router as _pr
APP.include_router(_pr)
"""
    changed = True
    print("[patch] portfolio router included")
else:
    print("[patch] portfolio router already included")

# Patch 4: model_retrain
bad = 'scheduler.register("model_retrain", retrain_if_ready, 604800)  # weekly'
if bad in src:
    src = src.replace(bad, 'from core import scheduler as _sched; _sched.register("model_retrain", retrain_if_ready, 604800)')
    changed = True
    print("[patch] model_retrain fixed")
elif "model_retrain" not in src:
    src += """
from core.model import retrain_if_ready as _rtr
from core import scheduler as _sched2
_sched2.register("model_retrain", _rtr, 604800)
"""
    changed = True
    print("[patch] model_retrain registered")
else:
    print("[patch] model_retrain already registered")

# Patch 5 (/api/dedup-picks) lives in committed wolf_app.py

if changed:
    with open(src_path, "w") as f:
        f.write(src)
    print("[patch] wolf_app.py written OK")
else:
    print("[patch] no changes needed")
