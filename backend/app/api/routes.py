"""HTTP API.

Deliberately a thin layer: it validates input, calls a service, and returns
JSON. All the estimating logic lives in ``app.services`` so it stays testable
without a running server.
"""

from __future__ import annotations

import datetime as dt
import errno
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import (
    CatalogItem,
    Document,
    DocumentPage,
    Estimate,
    EstimateLine,
    Issue,
    JobRun,
    ObjectAnalysis,
    Project,
    Question,
)
from ..schemas import (
    AnalysisUpdate,
    AnswerSubmit,
    BulkDismiss,
    BulkSameAnswer,
    EstimateCreate,
    IssueResolve,
    LineCreate,
    LineUpdate,
    OptionsUpdate,
    ProjectCreate,
    ProjectUpdate,
)
from ..services.catalog.search import CatalogSearch
from ..services.docs.pdf_extract import PAGE_TYPES, extract_document, file_hash, triage_summary
from ..services.estimate.builder import TemplateLayout
from ..services.estimate.store import (
    draft_from_estimate,
    group_by_section,
    line_to_dict,
)
from ..services.pipeline import analyse_project, plan_and_build, recalculate_estimate
from ..services.rules.engine import normalize_name
from ..services.validation.validators import validate

_MULTIPART_OVERHEAD = 8 * 1024  # boundary + headers slack

log = logging.getLogger(__name__)


def _storage_problem(directory: Path, exc: OSError) -> str:
    """A filesystem refusal, phrased so the person reading it can act.

    Uploads land in DATA_DIR, which the service does not own: it is created by
    whoever set the machine up, and a maintenance script run as root leaves
    directories root can write and the service cannot. The upload then fails on
    the first byte with "Permission denied" and nothing else, which reads like a
    bug in the PDF handling and is not one.
    """
    if isinstance(exc, PermissionError):
        return (
            f"Немає прав на запис у теку «{directory}». Її створено іншим "
            "користувачем — імовірно, скриптом обслуговування від root. "
            "Виправлення на сервері: "
            f"chown -R estimator:estimator {get_settings().data_dir}"
        )
    if getattr(exc, "errno", None) == errno.ENOSPC:
        return f"На диску немає місця для «{directory}»."
    return f"Не вдалося записати у «{directory}»: {exc.strerror or exc}."


def _unwritable_storage() -> list[str]:
    """Directories the service is expected to write to and cannot.

    Checked with ``os.access`` rather than by writing a probe file: this runs on
    every health poll, and the answer is wanted before a user finds it by losing
    an upload. Per-project upload directories are included because that is where
    the fault appeared -- the tree root stayed writable while two directories
    inside it did not.
    """
    roots = [settings.data_dir, settings.upload_dir, settings.cache_dir,
             settings.page_image_dir]
    if settings.upload_dir.is_dir():
        roots += [p for p in settings.upload_dir.iterdir() if p.is_dir()]
    return [
        str(p) for p in roots
        if p.exists() and not os.access(p, os.W_OK | os.X_OK)
    ]


router = APIRouter()
settings = get_settings()


def _project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"Проєкт {project_id} не знайдено")
    return project


def _estimate(session: Session, estimate_id: int) -> Estimate:
    estimate = session.get(Estimate, estimate_id)
    if estimate is None:
        raise HTTPException(404, f"Кошторис {estimate_id} не знайдено")
    return estimate


def _layout() -> TemplateLayout:
    try:
        return TemplateLayout.load()
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc


# --- system ------------------------------------------------------------------


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, Any]:
    from ..services.ai.client import AIClient

    catalog_count = session.scalar(select(func.count()).select_from(CatalogItem)) or 0
    try:
        layout = TemplateLayout.load()
        sections = len(layout.order)
        rules = sum(
            1
            for s in layout.data["sections"]
            for l in s["lines"]
            if l.get("qty_status") == "derived"
        )
    except FileNotFoundError:
        sections = rules = 0

    unwritable = _unwritable_storage()
    return {
        "status": "ok" if not unwritable else "degraded",
        # Named directly rather than as a bare boolean: the operator needs the
        # path to fix it, and an upload that lands in an unwritable directory
        # fails on its first byte with nothing but "Permission denied".
        "storage_unwritable": unwritable,
        "catalog_items": catalog_count,
        "template_sections": sections,
        "quantity_rules": rules,
        "ai_available": AIClient(settings).available,
        "ai_model": settings.ai_model,
        # The clarification box hides its microphone when this is false, rather
        # than offering a button that can only fail.
        "voice_available": bool(settings.openai_api_key),
        "voice_model": settings.openai_transcribe_model,
    }


@router.get("/meta/sections")
def sections() -> list[dict[str, Any]]:
    layout = _layout()
    return [
        {
            "key": key,
            "title": layout.title(key),
            "drivers": layout.drivers(key),
            "options": layout.option_rows(key),
            "line_count": len(layout.lines(key)),
        }
        for key in layout.order
        if key != "summary"
    ]


# --- dashboard ---------------------------------------------------------------


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session)) -> dict[str, Any]:
    projects = session.scalars(select(Project).order_by(Project.updated_at.desc()).limit(8)).all()
    total_projects = session.scalar(select(func.count()).select_from(Project)) or 0
    total_estimates = session.scalar(select(func.count()).select_from(Estimate)) or 0
    open_questions = (
        session.scalar(
            select(func.count()).select_from(Question).where(Question.status == "open")
        )
        or 0
    )
    open_issues = (
        session.scalar(
            select(func.count())
            .select_from(Issue)
            .where(Issue.resolved.is_(False), Issue.severity.in_(("ERROR", "NEEDS_USER_INPUT")))
        )
        or 0
    )
    catalog_count = session.scalar(select(func.count()).select_from(CatalogItem)) or 0
    unpriced = (
        session.scalar(
            select(func.count()).select_from(CatalogItem).where(CatalogItem.unit_price <= 0)
        )
        or 0
    )

    return {
        "totals": {
            "projects": total_projects,
            "estimates": total_estimates,
            "open_questions": open_questions,
            "open_issues": open_issues,
            "catalog_items": catalog_count,
            "catalog_without_price": unpriced,
        },
        "recent_projects": [_project_dict(p) for p in projects],
    }


# --- projects ----------------------------------------------------------------


def _project_dict(p: Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "client_name": p.client_name,
        "address": p.address,
        "manager": p.manager,
        "brief": p.brief,
        "status": p.status,
        "settings": p.settings or {},
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "document_count": len(p.documents),
        "estimate_count": len(p.estimates),
    }


@router.get("/projects")
def list_projects(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    projects = session.scalars(select(Project).order_by(Project.updated_at.desc())).all()
    return [_project_dict(p) for p in projects]


@router.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, session: Session = Depends(get_session)):
    project = Project(**payload.model_dump())
    session.add(project)
    session.commit()
    session.refresh(project)
    return _project_dict(project)


@router.get("/projects/{project_id}")
def get_project(project_id: int, session: Session = Depends(get_session)):
    project = _project(session, project_id)
    analysis = session.scalars(
        select(ObjectAnalysis)
        .where(ObjectAnalysis.project_id == project_id)
        .order_by(ObjectAnalysis.version.desc())
        .limit(1)
    ).first()
    estimate = session.scalars(
        select(Estimate)
        .where(Estimate.project_id == project_id)
        .order_by(Estimate.version.desc())
        .limit(1)
    ).first()
    return {
        **_project_dict(project),
        "documents": [_document_dict(d) for d in project.documents],
        "latest_analysis_id": analysis.id if analysis else None,
        "latest_estimate_id": estimate.id if estimate else None,
    }


@router.patch("/projects/{project_id}")
def update_project(project_id: int, payload: ProjectUpdate, session: Session = Depends(get_session)):
    project = _project(session, project_id)
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(project, key, value)
    session.commit()
    session.refresh(project)
    return _project_dict(project)


# 204 means "no content", so the handler must return an empty Response and the
# route must declare it. A `-> None` annotation makes FastAPI infer NoneType as
# the response model, and it then refuses the route outright:
#   AssertionError: Status code 204 must not have a response body
# Newer FastAPI tolerates it; 0.115.x does not, which is what we deploy.
@router.delete("/projects/{project_id}", status_code=204, response_class=Response)
def delete_project(project_id: int, session: Session = Depends(get_session)) -> Response:
    session.delete(_project(session, project_id))
    session.commit()
    return Response(status_code=204)


# --- documents ---------------------------------------------------------------


def _document_dict(d: Document) -> dict[str, Any]:
    return {
        "id": d.id,
        "filename": d.filename,
        "kind": d.kind,
        "status": d.status,
        "page_count": d.page_count,
        "size_bytes": d.size_bytes,
        "error": d.error,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


@router.post("/projects/{project_id}/documents", status_code=201)
async def upload_document(
    project_id: int,
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    project = _project(session, project_id)
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(415, "Підтримуються лише PDF-файли.")

    limit = settings.max_upload_mb * 1024 * 1024

    # Reject on the declared size before reading a byte. Drawing sets run to
    # 150 MB+, and on shared hosting writing an oversized file to disk only to
    # delete it can exhaust the account's quota.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + _MULTIPART_OVERHEAD:
        raise HTTPException(
            413,
            f"Файл більший за {settings.max_upload_mb} МБ "
            f"({int(declared) / 1024 / 1024:.0f} МБ).",
        )

    target_dir = settings.upload_dir / str(project.id)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(500, _storage_problem(target_dir, exc)) from exc
    target = target_dir / Path(file.filename).name

    # Stream in chunks and stop the moment the limit is passed, rather than
    # copying the whole body first and checking afterwards.
    size = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        413, f"Файл більший за {settings.max_upload_mb} МБ."
                    )
                out.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    except PermissionError as exc:
        # The directory exists but the service cannot write into it, so there is
        # nothing to clean up and no point retrying. Say which directory and who
        # has to own it: a bare "Permission denied" sends the operator looking
        # for a bug in the parser, which is where this was hunted for once.
        log.error("upload refused by the filesystem: %s", target, exc_info=True)
        raise HTTPException(500, _storage_problem(target_dir, exc)) from exc
    except Exception:
        target.unlink(missing_ok=True)
        raise

    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "Файл порожній.")

    document = Document(
        project_id=project.id,
        filename=target.name,
        stored_path=str(target),
        size_bytes=size,
        content_hash=file_hash(target),
        status="uploaded",
    )
    session.add(document)
    session.commit()
    session.refresh(document)

    # Cheap pass immediately: the operator sees page types before spending on AI.
    try:
        extracted = extract_document(target)
        document.page_count = extracted.page_count
        document.kind = extracted.kind
        document.status = "extracted"
        for page in extracted.pages:
            session.add(
                DocumentPage(
                    document_id=document.id,
                    page_number=page.page_number,
                    page_hash=page.page_hash,
                    text=page.text[:200_000],
                    text_length=page.text_length,
                    image_count=page.image_count,
                    tables=page.tables[:6],
                    page_type=page.page_type,
                    needs_vision=page.needs_vision,
                )
            )
        session.commit()
    except Exception as exc:  # noqa: BLE001
        document.status = "error"
        document.error = repr(exc)
        session.commit()
        log.exception("extraction failed for %s", target)

    session.refresh(document)
    return _document_dict(document)


@router.get("/documents/{document_id}")
def get_document(document_id: int, session: Session = Depends(get_session)):
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Документ не знайдено")
    return {
        **_document_dict(document),
        "pages": [
            {
                "page_number": p.page_number,
                "page_type": p.page_type,
                "page_type_label": PAGE_TYPES.get(p.page_type, p.page_type),
                "text_length": p.text_length,
                "image_count": p.image_count,
                "needs_vision": p.needs_vision,
                "analysed": p.analysed,
                "confidence": p.confidence,
                "findings": p.findings or {},
            }
            for p in sorted(document.pages, key=lambda x: x.page_number)
        ],
    }


@router.get("/documents/{document_id}/triage")
def document_triage(document_id: int, session: Session = Depends(get_session)):
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Документ не знайдено")
    try:
        return triage_summary(extract_document(document.stored_path))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Не вдалося прочитати документ: {exc}") from exc


@router.delete("/documents/{document_id}", status_code=204, response_class=Response)
def delete_document(document_id: int, session: Session = Depends(get_session)) -> Response:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Документ не знайдено")
    Path(document.stored_path).unlink(missing_ok=True)
    session.delete(document)
    session.commit()
    return Response(status_code=204)


# --- analysis ----------------------------------------------------------------


@router.post("/projects/{project_id}/analyze")
def analyze(project_id: int, session: Session = Depends(get_session)):
    """Run the document pipeline and produce an object analysis."""
    project = _project(session, project_id)
    if not project.documents:
        raise HTTPException(400, "Спочатку завантажте документи.")
    return analyse_project(session, project)


@router.get("/projects/{project_id}/analysis")
def get_analysis(project_id: int, session: Session = Depends(get_session)):
    analysis = session.scalars(
        select(ObjectAnalysis)
        .where(ObjectAnalysis.project_id == project_id)
        .order_by(ObjectAnalysis.version.desc())
        .limit(1)
    ).first()
    if analysis is None:
        raise HTTPException(404, "Аналіз об'єкта ще не сформовано.")
    return _analysis_dict(analysis)


def _analysis_dict(a: ObjectAnalysis) -> dict[str, Any]:
    return {
        "id": a.id,
        "project_id": a.project_id,
        "version": a.version,
        "status": a.status,
        "object_type": a.object_type,
        "summary": a.summary,
        "facts": a.facts or [],
        "systems": a.systems or [],
        "components": a.components or [],
        "plants": a.plants or [],
        "assumptions": a.assumptions or [],
        "unknowns": a.unknowns or [],
        "risks": a.risks or [],
        "conflicts": a.conflicts or [],
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


@router.patch("/analysis/{analysis_id}")
def update_analysis(
    analysis_id: int, payload: AnalysisUpdate, session: Session = Depends(get_session)
):
    """Operator edits to the object model. Estimates are rebuilt from this."""
    analysis = session.get(ObjectAnalysis, analysis_id)
    if analysis is None:
        raise HTTPException(404, "Аналіз не знайдено")
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(analysis, key, value)
    session.commit()
    session.refresh(analysis)
    return _analysis_dict(analysis)


# --- estimates ---------------------------------------------------------------


@router.post("/projects/{project_id}/estimates", status_code=201)
def create_estimate(
    project_id: int, payload: EstimateCreate, session: Session = Depends(get_session)
):
    project = _project(session, project_id)
    return plan_and_build(session, project, payload)


@router.get("/estimates/{estimate_id}")
def get_estimate(estimate_id: int, session: Session = Depends(get_session)):
    estimate = _estimate(session, estimate_id)
    issues = session.scalars(select(Issue).where(Issue.estimate_id == estimate.id)).all()
    return {
        "id": estimate.id,
        "project_id": estimate.project_id,
        "version": estimate.version,
        "title": estimate.title,
        "status": estimate.status,
        "options": estimate.options or {},
        "settings": estimate.settings or {},
        "totals": estimate.totals or {},
        "sections": group_by_section(estimate.lines),
        "issues": [
            {
                "id": i.id,
                "code": i.code,
                "severity": i.severity,
                "title": i.title,
                "detail": i.detail,
                "fix_hint": i.fix_hint,
                "impact": i.impact,
                "resolved": i.resolved,
            }
            for i in issues
        ],
    }


@router.get("/estimates/{estimate_id}/lines/{line_id}")
def get_line(estimate_id: int, line_id: int, session: Session = Depends(get_session)):
    row = session.get(EstimateLine, line_id)
    if row is None or row.estimate_id != estimate_id:
        raise HTTPException(404, "Позицію не знайдено")
    return line_to_dict(row)


@router.patch("/estimates/{estimate_id}/lines/{line_id}")
def update_line(
    estimate_id: int, line_id: int, payload: LineUpdate, session: Session = Depends(get_session)
):
    """Edit a line, then recompute everything that depends on it."""
    estimate = _estimate(session, estimate_id)
    row = session.get(EstimateLine, line_id)
    if row is None or row.estimate_id != estimate_id:
        raise HTTPException(404, "Позицію не знайдено")

    data = payload.model_dump(exclude_none=True)

    if "catalog_id" in data:
        item = session.get(CatalogItem, data["catalog_id"])
        if item is None:
            raise HTTPException(404, "Позицію каталогу не знайдено")
        row.catalog_id = item.id
        row.name = item.name
        row.unit = item.unit
        row.unit_price = item.unit_price
        row.unit_cost = item.unit_cost
        row.attributes = dict(item.attributes or {})
        row.match_status = "matched"
        row.confidence = "high"
        row.reasons = list(row.reasons or []) + ["Позицію обрано користувачем."]

    for key in ("quantity", "unit_price", "unit", "name", "section", "block", "comment"):
        if key in data:
            setattr(row, key, data[key])

    if "quantity" in data:
        # An operator-entered quantity outranks the rule that produced it.
        row.locked = True
        row.qty_source = "user_input"
        row.qty_trace = "Кількість введена користувачем вручну."
    if "locked" in data:
        row.locked = bool(data["locked"])

    row.total = round(row.quantity * row.unit_price, 2)
    session.commit()
    return recalculate_estimate(session, estimate)


@router.post("/estimates/{estimate_id}/lines", status_code=201)
def add_line(estimate_id: int, payload: LineCreate, session: Session = Depends(get_session)):
    estimate = _estimate(session, estimate_id)
    layout = _layout()
    search = CatalogSearch(session)

    item = session.get(CatalogItem, payload.catalog_id) if payload.catalog_id else None
    if item is None:
        item = search.get_exact(payload.name)

    row = EstimateLine(
        estimate_id=estimate.id,
        position=len(estimate.lines),
        section=payload.section,
        section_title=layout.title(payload.section),
        block=payload.block,
        name=item.name if item else payload.name,
        catalog_id=item.id if item else None,
        unit=payload.unit or (item.unit if item else ""),
        quantity=payload.quantity,
        unit_price=payload.unit_price if payload.unit_price is not None else (
            item.unit_price if item else 0.0
        ),
        unit_cost=item.unit_cost if item else 0.0,
        qty_source="user_input",
        qty_trace="Позицію додано користувачем вручну.",
        locked=True,
        match_status="matched" if item else "not_in_catalog",
        confidence="high" if item else "low",
        comment=payload.comment,
        reasons=["Додано користувачем."],
    )
    row.total = round(row.quantity * row.unit_price, 2)
    session.add(row)
    session.commit()
    return recalculate_estimate(session, estimate)


@router.delete("/estimates/{estimate_id}/lines/{line_id}")
def delete_line(estimate_id: int, line_id: int, session: Session = Depends(get_session)):
    estimate = _estimate(session, estimate_id)
    row = session.get(EstimateLine, line_id)
    if row is None or row.estimate_id != estimate_id:
        raise HTTPException(404, "Позицію не знайдено")
    session.delete(row)
    session.commit()
    return recalculate_estimate(session, estimate)


@router.post("/estimates/{estimate_id}/options")
def set_options(
    estimate_id: int, payload: OptionsUpdate, session: Session = Depends(get_session)
):
    """Set the template's boolean toggles (lawn type, mole net, geotextile...)."""
    estimate = _estimate(session, estimate_id)
    estimate.options = {**(estimate.options or {}),
                        **{normalize_name(k): v for k, v in payload.options.items()}}
    estimate.settings = {**(estimate.settings or {}), **payload.settings}
    session.commit()
    return recalculate_estimate(session, estimate)


@router.post("/estimates/{estimate_id}/recalculate")
def recalculate(estimate_id: int, session: Session = Depends(get_session)):
    return recalculate_estimate(session, _estimate(session, estimate_id))


@router.post("/estimates/{estimate_id}/approve")
def approve(estimate_id: int, session: Session = Depends(get_session)):
    estimate = _estimate(session, estimate_id)
    draft = draft_from_estimate(estimate)
    from ..services.rules.totals import compute_totals

    layout = _layout()
    totals = compute_totals(draft, layout.titles(), estimate.settings)
    report = validate(draft, totals)
    if not report.exportable:
        raise HTTPException(
            409,
            {
                "message": "Кошторис не можна затвердити: є помилки або відкриті питання.",
                "report": report.to_dict(),
            },
        )
    estimate.status = "approved"
    session.commit()
    return {"status": "approved", "report": report.to_dict()}


@router.get("/estimates/{estimate_id}/validate")
def validate_estimate(estimate_id: int, session: Session = Depends(get_session)):
    estimate = _estimate(session, estimate_id)
    from ..services.rules.totals import compute_totals

    draft = draft_from_estimate(estimate)
    layout = _layout()
    totals = compute_totals(draft, layout.titles(), estimate.settings)
    return validate(draft, totals).to_dict()


# --- questions ---------------------------------------------------------------


def _question_dict(q: Question) -> dict[str, Any]:
    return {
        "id": q.id,
        "group": q.group,
        "code": q.code,
        "kind_key": q.code.split(":", 1)[0] if ":" in q.code else "general",
        "text": q.text,
        "why": q.why,
        "kind": q.kind,
        "choices": q.choices or [],
        "default_value": q.default_value,
        "answer": q.answer,
        "status": q.status,
        "affects": q.affects or [],
        "subject": q.code.split(":", 1)[1] if ":" in q.code else "",
    }


# Question families, so the UI can filter instead of scrolling 80 cards.
QUESTION_FILTERS = {
    "price": "Рослини без ціни",
    "match": "Кілька відповідників",
    "missing": "Немає в каталозі",
    "coverage": "Відомість покриттів",
    "conflict": "Розбіжності у кресленнях",
}


@router.get("/projects/{project_id}/questions")
def list_questions(
    project_id: int,
    status: str | None = Query(default=None),
    family: str | None = Query(default=None, description="price|match|missing|coverage|conflict"),
    q: str | None = Query(default=None, description="substring of the question text"),
    session: Session = Depends(get_session),
):
    stmt = select(Question).where(Question.project_id == project_id)
    if status:
        stmt = stmt.where(Question.status == status)
    if family:
        stmt = stmt.where(Question.code.startswith(f"{family}:"))
    if q:
        stmt = stmt.where(Question.text.ilike(f"%{q}%"))
    questions = session.scalars(stmt.order_by(Question.group, Question.id)).all()
    return [_question_dict(x) for x in questions]


@router.get("/projects/{project_id}/questions/summary")
def questions_summary(project_id: int, session: Session = Depends(get_session)):
    """Counts per family, so the UI can offer meaningful filters."""
    rows = session.scalars(
        select(Question).where(Question.project_id == project_id)
    ).all()
    families: dict[str, dict[str, Any]] = {
        key: {"family": key, "label": label, "open": 0, "total": 0}
        for key, label in QUESTION_FILTERS.items()
    }
    for row in rows:
        key = row.code.split(":", 1)[0] if ":" in row.code else "conflict"
        entry = families.setdefault(
            key, {"family": key, "label": key, "open": 0, "total": 0}
        )
        entry["total"] += 1
        if row.status == "open":
            entry["open"] += 1
    return {
        "total": len(rows),
        "open": sum(1 for r in rows if r.status == "open"),
        "families": [f for f in families.values() if f["total"]],
    }


@router.post("/projects/{project_id}/questions/bulk-answer")
def bulk_answer(
    project_id: int, payload: BulkSameAnswer, session: Session = Depends(get_session)
):
    """Apply one answer to many questions, then recalculate once.

    Recalculating after every individual answer would be both slow and
    confusing; the operator sets a default plant price for twenty rows and sees
    a single updated total.
    """
    _project(session, project_id)
    questions = session.scalars(
        select(Question).where(
            Question.project_id == project_id, Question.id.in_(payload.question_ids)
        )
    ).all()
    if not questions:
        raise HTTPException(404, "Питання не знайдено")

    now = dt.datetime.now(dt.timezone.utc)
    estimates: set[int] = set()
    for question in questions:
        question.answer = payload.answer
        question.status = "answered"
        question.answered_at = now
        if question.estimate_id:
            estimates.add(question.estimate_id)
    session.commit()

    from ..services.pipeline import apply_answer

    for estimate_id in estimates:
        estimate = session.get(Estimate, estimate_id)
        if estimate is None:
            continue
        for question in questions:
            if question.estimate_id == estimate_id:
                apply_answer(session, estimate, question)

    result = None
    for estimate_id in estimates:
        estimate = session.get(Estimate, estimate_id)
        if estimate is not None:
            result = recalculate_estimate(session, estimate)

    return {
        "status": "ok",
        "answered": len(questions),
        "estimate": result,
    }


@router.post("/projects/{project_id}/questions/dismiss")
def dismiss_questions(
    project_id: int, payload: BulkDismiss, session: Session = Depends(get_session)
):
    """Mark questions as deliberately skipped.

    Recorded as a decision with its reason, not deleted -- the audit trail has
    to show that a human chose to leave it open.
    """
    _project(session, project_id)
    questions = session.scalars(
        select(Question).where(
            Question.project_id == project_id, Question.id.in_(payload.question_ids)
        )
    ).all()
    now = dt.datetime.now(dt.timezone.utc)
    for question in questions:
        question.status = "dismissed"
        question.answer = payload.reason or "Пропущено користувачем"
        question.answered_at = now
    session.commit()
    return {"status": "ok", "dismissed": len(questions)}


@router.post("/questions/{question_id}/answer")
def answer_question(
    question_id: int, payload: AnswerSubmit, session: Session = Depends(get_session)
):
    """Answering a question re-drives the estimate that depends on it."""
    question = session.get(Question, question_id)
    if question is None:
        raise HTTPException(404, "Питання не знайдено")
    question.answer = payload.answer
    question.status = "answered"
    question.answered_at = dt.datetime.now(dt.timezone.utc)
    session.commit()

    if question.estimate_id:
        estimate = session.get(Estimate, question.estimate_id)
        if estimate is not None:
            from ..services.pipeline import apply_answer

            apply_answer(session, estimate, question)
            return recalculate_estimate(session, estimate)
    return {"status": "answered"}


# --- catalog -----------------------------------------------------------------


@router.get("/catalog")
def list_catalog(
    q: str = Query(default=""),
    kind: str | None = Query(default=None),
    category: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    if q.strip():
        search = CatalogSearch(session)
        candidates = search.search(q, kind=kind, category=category, limit=limit)
        return {
            "total": len(candidates),
            "items": [c.to_dict() for c in candidates],
            "ranked": True,
        }

    stmt = select(CatalogItem).where(CatalogItem.active.is_(True))
    if kind:
        stmt = stmt.where(CatalogItem.kind == kind)
    if category:
        stmt = stmt.where(CatalogItem.category == category)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = session.scalars(stmt.order_by(CatalogItem.category, CatalogItem.name)
                            .offset(offset).limit(limit)).all()
    return {
        "total": total,
        "ranked": False,
        "items": [
            {
                "catalog_id": i.id,
                "name": i.name,
                "category": i.category,
                "unit": i.unit,
                "kind": i.kind,
                "unit_price": i.unit_price,
                "unit_cost": i.unit_cost,
                "margin_pct": i.margin_pct,
                "owner": i.owner,
                "price_updated_at": i.price_updated_at.isoformat() if i.price_updated_at else None,
            }
            for i in items
        ],
    }


@router.get("/catalog/categories")
def catalog_categories(session: Session = Depends(get_session)):
    rows = session.execute(
        select(CatalogItem.category, CatalogItem.kind, func.count())
        .where(CatalogItem.active.is_(True))
        .group_by(CatalogItem.category, CatalogItem.kind)
        .order_by(CatalogItem.category)
    ).all()
    out: dict[str, dict[str, Any]] = {}
    for category, kind, count in rows:
        entry = out.setdefault(category or "—", {"category": category or "—", "total": 0})
        entry[kind] = count
        entry["total"] += count
    return list(out.values())


@router.get("/catalog/match")
def catalog_match(
    q: str = Query(min_length=1),
    kind: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    """Expose the matcher itself, so the UI can offer alternatives on a line."""
    return CatalogSearch(session).match(q, kind=kind).to_dict()


# --- export ------------------------------------------------------------------


def _export_context(estimate_id: int, session: Session, suffix: str):
    """Everything both exporters need, so the two cannot drift apart."""
    from ..services.export.xlsx import ExportMeta
    from ..services.rules.totals import compute_totals

    estimate = _estimate(session, estimate_id)
    project = session.get(Project, estimate.project_id)
    layout = _layout()

    draft = draft_from_estimate(estimate)
    totals = compute_totals(draft, layout.titles(), estimate.settings)
    report = validate(draft, totals)

    out_dir = settings.data_dir / "exports"
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    safe = "".join(c for c in (project.name if project else "kp") if c.isalnum() or c in " -_")[:60]
    target = out_dir / f"КП {safe.strip() or 'проєкт'} v{estimate.version} {stamp}{suffix}"

    meta = ExportMeta(
        client_name=project.client_name if project else "",
        address=project.address if project else "",
        project_name=project.name if project else "",
        manager=project.manager if project else "",
        date=dt.date.today().strftime("%d.%m.%Y"),
    )
    return target, draft, totals, report, meta, layout


@router.get("/estimates/{estimate_id}/export")
def export(estimate_id: int, session: Session = Depends(get_session)):
    from ..services.export.xlsx import export_estimate

    target, draft, totals, report, meta, layout = _export_context(
        estimate_id, session, ".xlsx"
    )
    export_estimate(
        target,
        draft,
        totals,
        meta=meta,
        section_titles=layout.titles(),
        section_order=layout.order,
        report=report,
    )
    return FileResponse(
        target,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=target.name,
    )


@router.get("/estimates/{estimate_id}/export/pdf")
def export_pdf(estimate_id: int, session: Session = Depends(get_session)):
    from ..services.export.pdf import export_estimate_pdf

    target, draft, totals, _report, meta, layout = _export_context(
        estimate_id, session, ".pdf"
    )
    export_estimate_pdf(
        target,
        draft,
        totals,
        meta=meta,
        section_titles=layout.titles(),
        section_order=layout.order,
    )
    return FileResponse(target, media_type="application/pdf", filename=target.name)


# --- clarifications: voice in, structured edits out ---------------------------


@router.post("/audio/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    """Speech to text for the clarification box, via OpenAI Whisper."""
    from ..services.ai.openai_speech import OpenAIClient, OpenAIUnavailable

    client = OpenAIClient(settings)
    if not client.available:
        raise HTTPException(503, "Не налаштовано OPENAI_API_KEY — голосове введення вимкнено.")

    limit = settings.max_audio_mb * 1024 * 1024
    declared = None
    if hasattr(file, "size") and file.size:
        declared = file.size
    if declared and declared > limit:
        raise HTTPException(413, f"Запис довший за {settings.max_audio_mb} МБ.")

    audio = await file.read()
    if not audio:
        raise HTTPException(400, "Порожній аудіозапис.")
    if len(audio) > limit:
        raise HTTPException(413, f"Запис довший за {settings.max_audio_mb} МБ.")

    try:
        text = client.transcribe(audio, filename=file.filename or "clarification.webm")
    except OpenAIUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc

    return {"text": text, "chars": len(text)}


@router.post("/projects/{project_id}/issues/{issue_id}/resolve")
def resolve_issue(
    project_id: int,
    issue_id: int,
    payload: IssueResolve,
    session: Session = Depends(get_session),
):
    """Apply a written or dictated clarification, then rebuild the estimate.

    ``issue_id`` is the conflict's position in the analysis: conflicts are a
    JSON list on the object analysis, not rows, so they have no id of their own.
    """
    from ..services.ai.openai_speech import OpenAIClient, OpenAIUnavailable
    from ..services.resolution import ConflictNotFound, resolve_conflict

    project = _project(session, project_id)
    comment = payload.comment.strip()
    if not comment:
        raise HTTPException(400, "Порожнє уточнення — нема чого застосовувати.")

    client = OpenAIClient(settings)
    if not client.available:
        raise HTTPException(503, "Не налаштовано OPENAI_API_KEY — розбір уточнень вимкнено.")

    try:
        return resolve_conflict(
            session, project, issue_id, comment, client=client, rebuild=payload.recalculate
        )
    except ConflictNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except OpenAIUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc


# --- jobs --------------------------------------------------------------------


@router.get("/projects/{project_id}/jobs")
def list_jobs(project_id: int, session: Session = Depends(get_session)):
    jobs = session.scalars(
        select(JobRun).where(JobRun.project_id == project_id).order_by(JobRun.id.desc()).limit(20)
    ).all()
    return [
        {
            "id": j.id,
            "kind": j.kind,
            "status": j.status,
            "progress": j.progress,
            "step": j.step,
            "detail": j.detail or {},
            "error": j.error,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }
        for j in jobs
    ]
