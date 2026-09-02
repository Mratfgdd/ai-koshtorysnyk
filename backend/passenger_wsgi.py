"""Entry point for cPanel's "Setup Python App" (Phusion Passenger).

Why this file exists: Passenger loads a **WSGI** callable named ``application``.
FastAPI is **ASGI**. Handing Passenger the FastAPI object directly does not
half-work -- it fails on the first request, because ASGI apps are awaited and
WSGI apps are called. ``a2wsgi.ASGIMiddleware`` runs the ASGI app on its own
event loop and exposes a WSGI callable, which is what Passenger needs.

Passenger also starts the process with the *system* interpreter unless the
virtualenv is re-entered explicitly, so this re-execs into the venv Python that
cPanel created before importing anything from the app.

If the plan allows a long-running process instead, prefer plain uvicorn behind
a reverse proxy -- it is faster and supports streaming uploads properly. See
docs/HOSTIQ.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

# --- 1. re-enter the virtualenv ---------------------------------------------
# cPanel creates it under ~/virtualenv/<app>/<pyver>. It sets VIRTUAL_ENV, so
# prefer that; fall back to a sibling `venv` for a manual setup.
_venv = os.environ.get("VIRTUAL_ENV") or str(APP_DIR / "venv")
_python = Path(_venv) / "bin" / "python"

if _python.exists() and not sys.executable.startswith(str(_venv)):
    os.execl(str(_python), str(_python), *sys.argv)

# --- 2. make the package importable -----------------------------------------
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# --- 3. bridge ASGI -> WSGI --------------------------------------------------
from a2wsgi import ASGIMiddleware  # noqa: E402

from app.main import app as _asgi_app, bootstrap  # noqa: E402

# --- 4. run startup by hand --------------------------------------------------
# WSGI has no lifespan protocol, so FastAPI's startup handler never fires here.
# Without this the schema is never created and every request dies with
# "no such table: catalog_items".
bootstrap()

# Passenger looks for this exact name.
application = ASGIMiddleware(_asgi_app)
