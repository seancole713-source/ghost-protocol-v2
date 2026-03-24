import os, base64
wolf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wolf_app.py")
src = open(wolf, encoding="utf-8").read()
changed = False

# Patch 1: cockpit route -> serve cockpit.html
if "cockpit.html" not in src:
    anchor = '@APP.get("/cockpit")\ndef cockpit():\n    html = ("<h1>Ghost Protocol v2</h1>'
    if anchor in src:
        new_fn = base64.b64decode("QEFQUC5nZXQoIi9jb2NrcGl0IiwgcmVzcG9uc2VfY2xhc3M9SFRNTFJlc3BvbnNlKQpkZWYgY29ja3BpdCgpOgogICAgaW1wb3J0IG9zIGFzIF9vcwogICAgX3AgPSBfb3MucGF0aC5qb2luKF9vcy5wYXRoLmRpcm5hbWUoX29zLnBhdGguYWJzcGF0aChfX2ZpbGVfXykpLCAiY29ja3BpdC5odG1sIikKICAgIHdpdGggb3BlbihfcCwgZW5jb2Rpbmc9InV0Zi04IikgYXMgX2Y6CiAgICAgICAgcmV0dXJuIEhUTUxSZXNwb25zZShjb250ZW50PV9mLnJlYWQoKSk=").decode("utf-8")
        src = src[:src.index(anchor)] + new_fn + "\n"
        changed = True
        print("[patch] cockpit route patched")
    else:
        print("[patch] WARNING: cockpit anchor not found")
else:
    print("[patch] cockpit already patched")

# Patch 2: fix get_recent_articles -> get_cached_articles
if "get_recent_articles" in src:
    src = src.replace("get_recent_articles", "get_cached_articles")
    changed = True
    print("[patch] news: get_recent_articles -> get_cached_articles")
else:
    print("[patch] news function name already correct")

# Patch 3: fix history endpoint to query predictions table (v2 resolved picks)
hist_anchor = '@APP.get("/api/history")'
if hist_anchor in src and "FROM predictions" not in src[src.index(hist_anchor):src.index(hist_anchor)+800]:
    # Find the full old history function
    hist_start = src.index(hist_anchor)
    # Find next @APP. after it
    next_app = src.find("\n@APP.", hist_start + 10)
    if next_app == -1:
        next_app = len(src)
    new_hist = base64.b64decode("QEFQUC5nZXQoIi9hcGkvaGlzdG9yeSIpCmRlZiBoaXN0b3J5KCk6CiAgICAiIiJSZXNvbHZlZCB2MiBwaWNrcyBmcm9tIHByZWRpY3Rpb25zIHRhYmxlLiIiIgogICAgd2l0aCBkYl9jb25uKCkgYXMgY29ubjoKICAgICAgICBjdXIgPSBjb25uLmN1cnNvcigpCiAgICAgICAgY3VyLmV4ZWN1dGUoCiAgICAgICAgICAgICIiIgogICAgICAgICAgICBTRUxFQ1QgaWQsIHN5bWJvbCwgZGlyZWN0aW9uLCBjb25maWRlbmNlLCBlbnRyeV9wcmljZSwKICAgICAgICAgICAgICAgICAgIGV4aXRfcHJpY2UsIHBubF9wY3QsIG91dGNvbWUsIHByZWRpY3RlZF9hdCwgZXhwaXJlc19hdCwgYXNzZXRfdHlwZQogICAgICAgICAgICBGUk9NIHByZWRpY3Rpb25zCiAgICAgICAgICAgIFdIRVJFIG91dGNvbWUgSVMgTk9UIE5VTEwKICAgICAgICAgICAgICBBTkQgcHJlZGljdGVkX2F0IElTIE5PVCBOVUxMCiAgICAgICAgICAgIE9SREVSIEJZIGV4cGlyZXNfYXQgREVTQwogICAgICAgICAgICBMSU1JVCA1MAogICAgICAgICAgICAiIiIKICAgICAgICApCiAgICAgICAgcm93cyA9IGN1ci5mZXRjaGFsbCgpCiAgICB0cmFkZXMgPSBbXQogICAgd2lucyA9IGxvc3NlcyA9IDAKICAgIHRvdGFsX3BubCA9IDAuMAogICAgZm9yIHIgaW4gcm93czoKICAgICAgICBvdXRjb21lID0gcls3XQogICAgICAgIHBubCA9IGZsb2F0KHJbNl0gb3IgMCkKICAgICAgICBpZiBvdXRjb21lID09ICJXSU4iOiB3aW5zICs9IDEKICAgICAgICBlbGlmIG91dGNvbWUgaW4gKCJMT1NTIiwiU1RPUCIpOiBsb3NzZXMgKz0gMQogICAgICAgIHRvdGFsX3BubCArPSBwbmwKICAgICAgICB0cmFkZXMuYXBwZW5kKHsKICAgICAgICAgICAgImlkIjogclswXSwgInN5bWJvbCI6IHJbMV0sICJkaXJlY3Rpb24iOiByWzJdLAogICAgICAgICAgICAiY29uZmlkZW5jZSI6IHJbM10sICJlbnRyeV9wcmljZSI6IGZsb2F0KHJbNF0gb3IgMCksCiAgICAgICAgICAgICJleGl0X3ByaWNlIjogZmxvYXQocls1XSBvciAwKSBpZiByWzVdIGVsc2UgTm9uZSwKICAgICAgICAgICAgInBubF9wY3QiOiByb3VuZChwbmwsIDMpLCAib3V0Y29tZSI6IG91dGNvbWUsCiAgICAgICAgICAgICJwcmVkaWN0ZWRfYXQiOiByWzhdLCAiZXhwaXJlc19hdCI6IHJbOV0sCiAgICAgICAgICAgICJhc3NldF90eXBlIjogclsxMF0KICAgICAgICB9KQogICAgdG90YWwgPSB3aW5zICsgbG9zc2VzCiAgICByZXR1cm4gewogICAgICAgICJvayI6IFRydWUsICJ0cmFkZXMiOiB0cmFkZXMsICJ0b3RhbCI6IHRvdGFsLAogICAgICAgICJ3aW5zIjogd2lucywgImxvc3NlcyI6IGxvc3NlcywKICAgICAgICAid2luX3JhdGVfcGN0Ijogcm91bmQod2lucy90b3RhbCoxMDAsIDEpIGlmIHRvdGFsIGVsc2UgMCwKICAgICAgICAidG90YWxfcG5sX3BjdCI6IHJvdW5kKHRvdGFsX3BubCwgMikKICAgIH0=").decode("utf-8")
    src = src[:hist_start] + new_hist + "\n" + src[next_app:]
    changed = True
    print("[patch] history endpoint fixed to query predictions table")
else:
    print("[patch] history endpoint already correct or anchor not found")

if changed:
    open(wolf, "w", encoding="utf-8").write(src)
    print("[patch] wolf_app.py written OK")
