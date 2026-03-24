import os, base64
wolf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wolf_app.py")
src = open(wolf, encoding="utf-8").read()
changed = False

if "cockpit.html" not in src:
    anchor = '@APP.get("/cockpit")\ndef cockpit():\n    html = ("<h1>Ghost Protocol v2</h1>'
    if anchor in src:
        src = src[:src.index(anchor)] + base64.b64decode("QEFQUC5nZXQoIi9jb2NrcGl0IiwgcmVzcG9uc2VfY2xhc3M9SFRNTFJlc3BvbnNlKQpkZWYgY29ja3BpdCgpOgogICAgaW1wb3J0IG9zIGFzIF9vcwogICAgX3AgPSBfb3MucGF0aC5qb2luKF9vcy5wYXRoLmRpcm5hbWUoX29zLnBhdGguYWJzcGF0aChfX2ZpbGVfXykpLCAiY29ja3BpdC5odG1sIikKICAgIHdpdGggb3BlbihfcCwgZW5jb2Rpbmc9InV0Zi04IikgYXMgX2Y6CiAgICAgICAgcmV0dXJuIEhUTUxSZXNwb25zZShjb250ZW50PV9mLnJlYWQoKSk=").decode("utf-8") + "\n"
        changed = True; print("[patch] cockpit patched")
    else: print("[patch] WARNING: cockpit anchor not found")
else: print("[patch] cockpit already patched")

if "get_recent_articles" in src:
    src = src.replace("get_recent_articles","get_cached_articles")
    changed = True; print("[patch] news function renamed")
else: print("[patch] news function already correct")

if "/api/v2/recent" not in src:
    src = src + base64.b64decode("CgpAQVBQLmdldCgiL2FwaS92Mi9yZWNlbnQiKQpkZWYgdjJfcmVjZW50KCk6CiAgICAiIiJSZXNvbHZlZCB2MiBwaWNrcyBxdWVyeWluZyBwcmVkaWN0aW9ucyB0YWJsZSBkaXJlY3RseS4iIiIKICAgIHdpdGggZGJfY29ubigpIGFzIGNvbm46CiAgICAgICAgY3VyID0gY29ubi5jdXJzb3IoKQogICAgICAgIGN1ci5leGVjdXRlKCIiIgogICAgICAgICAgICBTRUxFQ1QgaWQsc3ltYm9sLGRpcmVjdGlvbixjb25maWRlbmNlLGVudHJ5X3ByaWNlLAogICAgICAgICAgICAgICAgICAgZXhpdF9wcmljZSxwbmxfcGN0LG91dGNvbWUscHJlZGljdGVkX2F0LGV4cGlyZXNfYXQsYXNzZXRfdHlwZQogICAgICAgICAgICBGUk9NIHByZWRpY3Rpb25zCiAgICAgICAgICAgIFdIRVJFIG91dGNvbWUgSVMgTk9UIE5VTEwgQU5EIHByZWRpY3RlZF9hdCBJUyBOT1QgTlVMTAogICAgICAgICAgICBPUkRFUiBCWSBleHBpcmVzX2F0IERFU0MgTlVMTFMgTEFTVCBMSU1JVCA1MAogICAgICAgICIiIikKICAgICAgICByb3dzID0gY3VyLmZldGNoYWxsKCkKICAgIHRyYWRlcz1bXTsgd2lucz1sb3NzZXM9MAogICAgZm9yIHIgaW4gcm93czoKICAgICAgICBvPXJbN107IHBubD1mbG9hdChyWzZdIG9yIDApCiAgICAgICAgaWYgbz09IldJTiI6IHdpbnMrPTEKICAgICAgICBlbGlmIG8gaW4gKCJMT1NTIiwiU1RPUCIsIkVYUElSRUQiKTogbG9zc2VzKz0xCiAgICAgICAgdHJhZGVzLmFwcGVuZCh7ImlkIjpyWzBdLCJzeW1ib2wiOnJbMV0sImRpcmVjdGlvbiI6clsyXSwiY29uZmlkZW5jZSI6clszXSwKICAgICAgICAgICAgImVudHJ5X3ByaWNlIjpmbG9hdChyWzRdIG9yIDApLCJleGl0X3ByaWNlIjpmbG9hdChyWzVdIG9yIDApIGlmIHJbNV0gZWxzZSBOb25lLAogICAgICAgICAgICAicG5sX3BjdCI6cm91bmQocG5sLDMpLCJvdXRjb21lIjpvLCJwcmVkaWN0ZWRfYXQiOnJbOF0sImV4cGlyZXNfYXQiOnJbOV0sImFzc2V0X3R5cGUiOnJbMTBdfSkKICAgIHRvdGFsPXdpbnMrbG9zc2VzCiAgICByZXR1cm4geyJvayI6VHJ1ZSwidHJhZGVzIjp0cmFkZXMsInRvdGFsIjp0b3RhbCwid2lucyI6d2lucywibG9zc2VzIjpsb3NzZXMsCiAgICAgICAgIndpbl9yYXRlX3BjdCI6cm91bmQod2lucy90b3RhbCoxMDAsMSkgaWYgdG90YWwgZWxzZSAwfQo=").decode("utf-8")
    changed = True; print("[patch] /api/v2/recent appended")
else: print("[patch] /api/v2/recent already present")

if changed:
    open(wolf,"w",encoding="utf-8").write(src)
    print("[patch] wolf_app.py written OK")
