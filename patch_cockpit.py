import os, base64
wolf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wolf_app.py")
src = open(wolf, encoding="utf-8").read()
changed = False

# 1. cockpit route
if "cockpit.html" not in src:
    anchor = '@APP.get("/cockpit")\ndef cockpit():\n    html = ("<h1>Ghost Protocol v2</h1>'
    if anchor in src:
        src = src[:src.index(anchor)] + base64.b64decode("QEFQUC5nZXQoIi9jb2NrcGl0IiwgcmVzcG9uc2VfY2xhc3M9SFRNTFJlc3BvbnNlKQpkZWYgY29ja3BpdCgpOgogICAgaW1wb3J0IG9zIGFzIF9vcwogICAgX3AgPSBfb3MucGF0aC5qb2luKF9vcy5wYXRoLmRpcm5hbWUoX29zLnBhdGguYWJzcGF0aChfX2ZpbGVfXykpLCAiY29ja3BpdC5odG1sIikKICAgIHdpdGggb3BlbihfcCwgZW5jb2Rpbmc9InV0Zi04IikgYXMgX2Y6CiAgICAgICAgcmV0dXJuIEhUTUxSZXNwb25zZShjb250ZW50PV9mLnJlYWQoKSk=").decode("utf-8") + "\n"
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
if "model_retrain" not in src:
    src = src + base64.b64decode("CmZyb20gY29yZS5tb2RlbCBpbXBvcnQgcmV0cmFpbl9pZl9yZWFkeQpzY2hlZHVsZXIucmVnaXN0ZXIoIm1vZGVsX3JldHJhaW4iLCByZXRyYWluX2lmX3JlYWR5LCA2MDQ4MDApICAjIHdlZWtseQo=").decode("utf-8")
    changed = True; print("[patch] model_retrain scheduler registered")
else: print("[patch] model_retrain already registered")

if changed:
    open(wolf,"w",encoding="utf-8").write(src)
    print("[patch] wolf_app.py written OK")
