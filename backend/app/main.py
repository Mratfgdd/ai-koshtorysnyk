"""Application entry point.

    uvicorn app.main:app --reload --port 8000

Serves the JSON API under ``/api`` and, when the frontend has been built, the
single-page app from ``frontend/dist`` so the whole system runs as one process.
"""

from __future__ import annotations

import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import get_settings
from .db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("estimator")

settings = get_settings()

@asynccontextmanager
async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
    init_db()
    log.info("database ready at %s", settings.sqlalchemy_url)
    try:
        from .services.estimate.builder import TemplateLayout

        layout = TemplateLayout.load()
        rules = sum(
            1
            for s in layout.data["sections"]
            for l in s["lines"]
            if l.get("qty_status") == "derived"
        )
        log.info("template loaded: %d sections, %d quantity rules", len(layout.order), rules)
    except FileNotFoundError:
        log.warning(
            "template not compiled -- run: python scripts/compile_template.py <Шаблон для ШІ.xlsx>"
        )
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "AI-кошторисник для ландшафтних проєктів: аналіз креслень, "
        "правила з шаблону замовника, каталог, перевірка та експорт у XLSX."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # so the browser sees the export filename
)

app.include_router(router, prefix="/api")


@app.exception_handler(Exception)
async def _unhandled(request, exc: Exception):  # type: ignore[no-untyped-def]
    """Never leak a stack trace to the UI, but never hide the failure either."""
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "message": "Внутрішня помилка сервера.",
            "detail": repr(exc),
            "path": request.url.path,
        },
    )


# --- static frontend ---------------------------------------------------------

_DIST = settings.data_dir.parent / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str):  # type: ignore[no-untyped-def]
        candidate = _DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
