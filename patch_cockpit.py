import os

src_path = "wolf_app.py"
with open(src_path) as f:
        src = f.read()

changed = False

# Patch 1: replace placeholder cockpit with cockpit.html file-read
OLD = "@APP.get(\"/cockpit\")\ndef cockpit():\n    html = (\"<h1>Ghost Protocol v2</h1><ul>\"\n             \"<li><a href=/health>/health</a></li>\"\n             \"<li><a href=/api/picks>/api/picks</a></li>\"\n             \"<li><a href=/api/history>/api/history</a></li>\"\n             \"<li><a href=/api/news>/api/news</a></li>\"\n             \"<li><a href=/api/schema>/api/schema</a></li>\"\n             \"</ul><p>Full dashboard coming Week 4.</p>\")\n    return HTMLResponse(html)"
NEW = "@APP.get(\"/cockpit\", include_in_schema=False)\ndef cockpit():\n    import os as _os\n    with open(_os.path.join(_os.path.dirname(__file__), \"cockpit.html\")) as _f:\n        return HTMLResponse(_f.read())"
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
        src += "\nfrom core.portfolio_routes import portfolio_router as _pr\nAPP.include_router(_pr)\n"
        changed = True
        print("[patch] portfolio router included")
else:
        print("[patch] portfolio router already included")

# Patch 4: model_retrain
bad = 'scheduler.register("model_retrain", retrain_if_ready, 604800) # weekly'
if bad in src:
        src = src.replace(bad, 'from core import scheduler as _sched; _sched.register("model_retrain", retrain_if_ready, 604800)')
        changed = True
        print("[patch] model_retrain fixed")
elif "model_retrain" not in src:
        src += "\nfrom core.model import retrain_if_ready as _rtr\nfrom core import scheduler as _sched2\n_sched2.register(\"model_retrain\", _rtr, 604800)\n"
        changed = True
        print("[patch] model_retrain registered")
else:
        print("[patch] model_retrain already registered")

# Patch 5: dedup-picks endpoint
if "dedup-picks" not in src:
        src += "\n\n@APP.post(\"/api/dedup-picks\", include_in_schema=False)\ndef dedup_picks():\n    import time\n    from core.db import db_conn\n    try:\n        with db_conn() as conn:\n            cur = conn.cursor()\n            cur.execute(\"SELECT id, symbol, confidence FROM predictions WHERE outcome IS NULL AND expires_at > %s ORDER BY symbol, confidence DESC\", (int(time.time()),))\n            rows = cur.fetchall()\n            seen = {}\n            to_expire = []\n            for pid, sym, conf in rows:\n                if sym not in seen:\n                    seen[sym] = pid\n                else:\n                    to_expire.append(pid)\n            if to_expire:\n                cur.execute(\"UPDATE predictions SET outcome='EXPIRED', resolved_at=%s WHERE id = ANY(%s)\", (int(time.time()), to_expire))\n            return {\"ok\": True, \"expired\": len(to_expire), \"kept\": len(seen)}\n    except Exception as e:\n        return {\"ok\": False, \"error\": str(e)}\n"
        changed = True
        print("[patch] dedup endpoint added")
else:
        print("[patch] dedup endpoint present")

# Patch 6: /api/cockpit/context route
if "/api/cockpit/context" not in src:
        src += "\n\n@APP.get(\"/api/cockpit/context\", include_in_schema=False)\ndef cockpit_context():\n    from core.db import db_conn\n    try:\n        with db_conn() as conn:\n            cur = conn.cursor()\n            cur.execute(\"SELECT COUNT(*) FROM predictions WHERE outcome IS NULL AND expires_at > extract(epoch from now())\")\n            open_picks = cur.fetchone()[0]\n            cur.execute(\"SELECT COUNT(*) FROM predictions WHERE resolved_at > extract(epoch from now()) - 86400\")\n            resolved_24h = cur.fetchone()[0]\n            cur.execute(\"SELECT outcome, COUNT(*) FROM predictions WHERE resolved_at > extract(epoch from now()) - 604800 GROUP BY outcome\")\n            outcomes = {r[0]: r[1] for r in cur.fetchall()}\n        regime = _check_regime() if callable(globals().get('_check_regime')) else \"unknown\"\n        return {\"ok\": True, \"open_picks\": open_picks, \"resolved_24h\": resolved_24h, \"weekly_outcomes\": outcomes, \"regime\": regime}\n    except Exception as e:\n        return {\"ok\": False, \"error\": str(e)}\n"
        changed = True
        print("[patch] cockpit /api/cockpit/context added")
else:
        print("[patch] /api/cockpit/context already present")

# Patch 7: /api/regime route
if "/api/regime" not in src:
        src += "\n\n@APP.get(\"/api/regime\", include_in_schema=False)\ndef api_regime():\n    try:\n        regime = _check_regime() if callable(globals().get('_check_regime')) else \"unknown\"\n        return {\"ok\": True, \"regime\": regime}\n    except Exception as e:\n        return {\"ok\": False, \"error\": str(e)}\n"
        changed = True
        print("[patch] /api/regime added")
else:
        print("[patch] /api/regime already present")

# Write back — MUST be last
if changed:
        with open(src_path, "w") as f:
                    f.write(src)
                print("[patch] wolf_app.py written OK")
else:
    print("[patch] no changes needed")
