import os, base64
wolf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wolf_app.py")
src = open(wolf, encoding="utf-8").read()
if "cockpit.html" in src:
    print("[patch_cockpit] already patched, skipping")
else:
    # anchor is the start of the old cockpit function
    anchor = '@APP.get("/cockpit")\ndef cockpit():\n    html = ("<h1>Ghost Protocol v2</h1>'
    if anchor not in src:
        print("[patch_cockpit] ERROR: anchor not found")
    else:
        # new function decoded from b64 - zero quoting issues
        new_fn = base64.b64decode("QEFQUC5nZXQoIi9jb2NrcGl0IiwgcmVzcG9uc2VfY2xhc3M9SFRNTFJlc3BvbnNlKQpkZWYgY29ja3BpdCgpOgogICAgaW1wb3J0IG9zIGFzIF9vcwogICAgX3AgPSBfb3MucGF0aC5qb2luKF9vcy5wYXRoLmRpcm5hbWUoX29zLnBhdGguYWJzcGF0aChfX2ZpbGVfXykpLCAiY29ja3BpdC5odG1sIikKICAgIHdpdGggb3BlbihfcCwgZW5jb2Rpbmc9InV0Zi04IikgYXMgX2Y6CiAgICAgICAgcmV0dXJuIEhUTUxSZXNwb25zZShjb250ZW50PV9mLnJlYWQoKSk=").decode("utf-8")
        # find end of old function (end of file since it is last)
        start = src.index(anchor)
        patched = src[:start] + new_fn + "\n"
        open(wolf, "w", encoding="utf-8").write(patched)
        print("[patch_cockpit] OK - cockpit route patched")
