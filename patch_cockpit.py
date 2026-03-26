import base64, sys, os

src_path = "wolf_app.py"
with open(src_path) as f:
    src = f.read()

changed = False

# Patch 1: cockpit route — append if /cockpit route not defined
if '@APP.get("/cockpit")' not in src and "@APP.get('/cockpit')" not in src:
    cockpit_route = '''

from fastapi.responses import HTMLResponse as _HTMLResponse

@APP.get("/cockpit", response_class=_HTMLResponse, include_in_schema=False)
async def serve_cockpit_ui():
    with open("cockpit.html") as _f:
        return _f.read()

@APP.get("/", include_in_schema=False)
async def root_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/cockpit")
'''
    src = src + cockpit_route
    changed = True
    print("[patch] cockpit route added")
else:
    print("[patch] cockpit already present")

# Patch 2: news function rename
if "get_symbol_sentiment" in src and "get_sentiment_for_symbol = get_symbol_sentiment" not in src:
    src = src.replace(
        "from core.news import get_symbol_sentiment",
        "from core.news import get_symbol_sentiment, get_sentiment_for_symbol"
    )
    changed = True
    print("[patch] news alias added")
else:
    print("[patch] news OK")

# Patch 3: portfolio router
if "portfolio_routes" not in src:
    src = src + """
from core.portfolio_routes import portfolio_router as _pr
APP.include_router(_pr)
"""
    changed = True
    print("[patch] portfolio router included")
else:
    print("[patch] portfolio router already included")

# Patch 4: model_retrain scheduler
bad_sched = 'scheduler.register("model_retrain", retrain_if_ready, 604800)  # weekly'
if bad_sched in src:
    src = src.replace(bad_sched, 'from core import scheduler as _sched; _sched.register("model_retrain", retrain_if_ready, 604800)  # weekly')
    changed = True
    print("[patch] model_retrain scheduler fixed")
elif 'model_retrain' not in src:
    src = src + """
from core.model import retrain_if_ready as _rtr
from core import scheduler as _sched2
_sched2.register("model_retrain", _rtr, 604800)  # weekly
"""
    changed = True
    print("[patch] model_retrain scheduler registered")
else:
    print("[patch] model_retrain already registered")

if changed:
    with open(src_path, "w") as f:
        f.write(src)
    print("[patch] wolf_app.py written OK")
else:
    print("[patch] no changes needed")
