import re, os

src_path = "wolf_app.py"
with open(src_path) as f:
    src = f.read()

changed = False

# Patch 1: cockpit route — replace placeholder function with cockpit.html file-read
if 'cockpit.html' not in src:
    if 'Full dashboard coming Week 4' in src:
        # Replace the entire cockpit function using regex
        new_fn = '''@APP.get("/cockpit", include_in_schema=False)
async def cockpit():
    from fastapi.responses import HTMLResponse as _HR
    with open("cockpit.html") as _f:
        return _HR(_f.read())

'''
        src = re.sub(
            r'@APP\.get\(["\']+/?cockpit["\']+\).*?(?=\n@APP|Z)',
            new_fn,
            src,
            flags=re.DOTALL
        )
        changed = True
        print("[patch] cockpit patched with file-read")
    else:
        # No cockpit route at all - append one
        src += '''
from fastapi.responses import HTMLResponse as _HTMLResponse

@APP.get("/cockpit", include_in_schema=False)
async def cockpit():
    with open("cockpit.html") as _f:
        return _HTMLResponse(_f.read())

@APP.get("/", include_in_schema=False)
async def root_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/cockpit")
'''
        changed = True
        print("[patch] cockpit route appended")
else:
    print("[patch] cockpit.html already wired")

# Patch 2: / redirect — add if missing
if '"/" ' not in src and "'/'" not in src and 'root_redirect' not in src:
    src += '''
@APP.get("/", include_in_schema=False)
async def root_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/cockpit")
'''
    changed = True
    print("[patch] root redirect added")
else:
    print("[patch] root redirect OK")

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

# Patch 4: model_retrain scheduler
bad = 'scheduler.register("model_retrain", retrain_if_ready, 604800)  # weekly'
if bad in src:
    src = src.replace(bad, 'from core import scheduler as _sched; _sched.register("model_retrain", retrain_if_ready, 604800)')
    changed = True
    print("[patch] model_retrain scheduler fixed")
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

if changed:
    with open(src_path, "w") as f:
        f.write(src)
    print("[patch] wolf_app.py written OK")
else:
    print("[patch] no changes needed")
