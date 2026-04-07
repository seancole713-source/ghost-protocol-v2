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

# Patch 5: dedup-picks endpoint
if "dedup-picks" not in src:
    src = src + "\n\n@APP.post(\"/api/dedup-picks\", include_in_schema=False)\ndef dedup_picks():\n    import time\n    from core.db import db_conn\n    try:\n        with db_conn() as conn:\n            cur = conn.cursor()\n            cur.execute(\"SELECT id, symbol, confidence FROM predictions WHERE outcome IS NULL AND expires_at > %s ORDER BY symbol, confidence DESC\", (int(time.time()),))\n            rows = cur.fetchall()\n            seen = {}\n            to_expire = []\n            for pid, sym, conf in rows:\n                if sym not in seen:\n                    seen[sym] = pid\n                else:\n                    to_expire.append(pid)\n            if to_expire:\n                cur.execute(\"UPDATE predictions SET outcome='EXPIRED', resolved_at=%s WHERE id = ANY(%s)\", (int(time.time()), to_expire))\n        return {\"ok\": True, \"expired\": len(to_expire), \"kept\": len(seen)}\n    except Exception as e:\n        return {\"ok\": False, \"error\": str(e)}\n"
    changed = True; print("[patch] dedup endpoint added")
else: print("[patch] dedup endpoint present")

# Patch 6: /api/cockpit/context route
if "/api/cockpit/context" not in src:
    src += """

@APP.get("/api/cockpit/context", include_in_schema=False)
def cockpit_context():
    from core.db import db_conn
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM predictions WHERE outcome IS NULL AND expires_at > extract(epoch from now())")
            open_picks = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM predictions WHERE resolved_at > extract(epoch from now()) - 86400")
            resolved_24h = cur.fetchone()[0]
            cur.execute("SELECT outcome, COUNT(*) FROM predictions WHERE resolved_at > extract(epoch from now()) - 604800 GROUP BY outcome")
            outcomes = {r[0]: r[1] for r in cur.fetchall()}
        try:
            from core.prediction import _check_regime
            regime = {"ok": True, **_check_regime()}
        except Exception as _re:
            regime = {"ok": False, "error": str(_re)[:120], "block_crypto_buys": False, "reason": "", "btc_24h_pct": 0.0}
        return {"ok": True, "open_picks": open_picks, "resolved_24h": resolved_24h, "weekly_outcomes": outcomes, "regime": regime}
    except Exception as e:
        return {"ok": False, "error": str(e)}
"""
    changed = True
    print("[patch] cockpit /api/cockpit/context added")
else:
    print("[patch] /api/cockpit/context already present")

# Patch 7: /api/regime route
if "/api/regime" not in src:
    src += """

@APP.get("/api/regime", include_in_schema=False)
def api_regime():
    try:
        from core.prediction import _check_regime
        return {"ok": True, **_check_regime()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "block_crypto_buys": False, "reason": "", "btc_24h_pct": 0.0}
"""
    changed = True
    print("[patch] /api/regime added")
else:
    print("[patch] /api/regime already present")

if changed:
    with open(src_path, "w") as f:
        f.write(src)
    print("[patch] wolf_app.py written OK")
else:
    print("[patch] no changes needed")
