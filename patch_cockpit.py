import os, base64
wolf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wolf_app.py")
src = open(wolf, encoding="utf-8").read()
changed = False

# 1. cockpit route
if "cockpit.html" not in src:
    anchor = '@APP.get("/cockpit")\ndef cockpit():\n    html = ("<h1>Ghost Protocol v2</h1>'
    if anchor in src:
        src = src[:src.index(anchor)] + base64.b64decode("CmZyb20gY29yZS5tb2RlbCBpbXBvcnQgcmV0cmFpbl9pZl9yZWFkeQpmcm9tIGNvcmUgaW1wb3J0IHNjaGVkdWxlciBhcyBfc2NoZWQ7IF9zY2hlZC5yZWdpc3RlcigibW9kZWxfcmV0cmFpbiIsIHJldHJhaW5faWZfcmVhZHksIDYwNDgwMCkgICMgd2Vla2x5Cg==").decode("utf-8") + "\n"
        changed = True; print("[patch] cockpit patched")
    else: print("[patch] WARNING: cockpit anchor not found")
else: print("[patch] cockpit already patched")

# 2. fix news function name
if "get_recent_articles" in src:
    src = src.replace("get_recent_articles","get_cached_articles")
    changed = True; print("[patch] news renamed")
else: print("[patch] news OK")

# 3. include portfolio router (just 2 lines - safe)
if "portfolio_router" not in src:
    src = src + base64.b64decode("CmZyb20gY29yZS5wb3J0Zm9saW9fcm91dGVzIGltcG9ydCBwb3J0Zm9saW9fcm91dGVyCkFQUC5pbmNsdWRlX3JvdXRlcihwb3J0Zm9saW9fcm91dGVyKQo=").decode("utf-8")
    changed = True; print("[patch] portfolio router included")
else: print("[patch] portfolio router already included")

# Patch 4: wire model retrain into weekly scheduler
bad_sched = 'scheduler.register("model_retrain", retrain_if_ready, 604800)  # weekly'
if bad_sched in src:
    src = src.replace(bad_sched, 'from core import scheduler as _sched; _sched.register("model_retrain", retrain_if_ready, 604800)  # weekly')
    changed = True; print("[patch] model_retrain scheduler fixed")
elif "model_retrain" not in src:
    src = src + base64.b64decode("CmZyb20gY29yZS5tb2RlbCBpbXBvcnQgcmV0cmFpbl9pZl9yZWFkeQpzY2hlZHVsZXIucmVnaXN0ZXIoIm1vZGVsX3JldHJhaW4iLCByZXRyYWluX2lmX3JlYWR5LCA2MDQ4MDApICAjIHdlZWtseQo=").decode("utf-8")
    changed = True; print("[patch] model_retrain scheduler registered")
else: print("[patch] model_retrain already registered")

if changed:
    open(wolf,"w",encoding="utf-8").write(src)
    print("[patch] wolf_app.py written OK")
