"""Orchestration: documents -> object analysis -> estimate -> validation.

This module is the only place that knows the order of the steps. Each step is
recorded on a :class:`JobRun` so a long analysis is observable and, because
every page result is cached, resumable.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Document,
    DocumentPage,
    Estimate,
    JobRun,
    ObjectAnalysis,
    Project,
    Question,
)
from ..schemas import EstimateCreate
from .ai.analyzer import DocumentAnalyzer
from .ai.schemas import (
    FACT_STATUSES_OUT_OF_ESTIMATE,
    clean_confidence,
    clean_fact_status,
    clean_question_kind,
    clean_section,
    clean_source_type,
)
from .ai.client import AIClient
from .catalog.search import CatalogSearch, normalize_unit, stem_overlap, stems
from .docs.pdf_extract import extract_document
from .estimate.builder import EstimateBuilder, TemplateLayout, _driver_unit
from .estimate.store import draft_from_estimate, group_by_section, save_draft
from .rules.engine import normalize_name
from .validation.validators import validate

log = logging.getLogger(__name__)
settings = get_settings()


# --- job bookkeeping ---------------------------------------------------------


def _start_job(session: Session, project_id: int, kind: str) -> JobRun:
    job = JobRun(
        project_id=project_id,
        kind=kind,
        status="running",
        started_at=dt.datetime.now(dt.timezone.utc),
    )
    session.add(job)
    session.commit()
    return job


def _progress(session: Session, job: JobRun):
    def report(fraction: float, step: str) -> None:
        job.progress = round(max(0.0, min(1.0, fraction)), 3)
        job.step = step[:200]
        session.commit()

    return report


def _finish(session: Session, job: JobRun, status: str, detail: dict[str, Any], error: str | None = None) -> None:
    job.status = status
    job.detail = detail
    job.error = error
    job.progress = 1.0 if status == "done" else job.progress
    job.finished_at = dt.datetime.now(dt.timezone.utc)
    session.commit()


# --- diagnosing an empty analysis --------------------------------------------

# A drawing sheet that carries quantities has a schedule on it, and a schedule
# is text. Below this, the file is a picture, not a document to cost.
_THIN_TEXT_CHARS = 400


def _diagnose_empty_analysis(
    documents: list[tuple[Any, str]], outcome: Any
) -> dict[str, Any]:
    """Say why nothing came out, and what the estimator should do about it.

    "Не вдалося сформувати аналіз об'єкта" is true but useless: it does not
    distinguish a missing API key from a one-page concept sketch from a scan
    with no text layer, and each needs a different action. The counts below
    come from the extraction that already ran, so this costs nothing.
    """
    pages = [page for doc, _ in documents for page in doc.pages]
    total_pages = len(pages)
    text_chars = sum(p.text_length for p in pages)
    with_text = sum(1 for p in pages if p.text_length > 40)
    raster_only = sum(1 for p in pages if p.text_length <= 40 and p.image_count > 0)
    analysed = len(getattr(outcome, "pages", []))
    names = ", ".join(f"«{name}»" for _, name in documents)

    stats = {
        "documents": len(documents),
        "pages": total_pages,
        "pages_with_text": with_text,
        "pages_raster_only": raster_only,
        "text_chars": text_chars,
        "pages_analysed": analysed,
    }

    if any("ANTHROPIC_API_KEY" in e for e in outcome.errors):
        return {
            "reason": "Аналіз недоступний: не налаштовано ключ ANTHROPIC_API_KEY.",
            "recommendations": [
                "Додайте ключ у файл .env на сервері та перезапустіть сервіс.",
                "Без ключа показники об'єкта можна заповнити вручну — "
                "кошторис, правила шаблону й експорт працюють і без AI.",
            ],
            "stats": stats,
        }

    if total_pages == 0:
        return {
            "reason": f"У файлі {names} немає сторінок, які вдалося прочитати.",
            "recommendations": [
                "Перевірте, чи PDF не пошкоджений і чи відкривається у переглядачі.",
                "Якщо файл захищений паролем — зніміть захист і завантажте знову.",
            ],
            "stats": stats,
        }

    if total_pages <= 2 and text_chars < _THIN_TEXT_CHARS:
        return {
            "reason": (
                f"Недостатньо вихідних даних у файлі для виділення об'ємів чи систем: "
                f"{total_pages} стор., лише {text_chars} символів тексту."
            ),
            "recommendations": [
                "Додайте аркуші з відомостями: асортиментна відомість рослин, "
                "відомість елементів покриття, специфікація обладнання.",
                "Потрібен генплан або план розпланування з розмірами та площами — "
                "з самої візуалізації об'єми зняти неможливо.",
                "Якщо є ТЗ від замовника, впишіть його у поле «Технічне завдання» "
                "проєкту: воно бере участь в аналізі нарівні з кресленнями.",
            ],
            "stats": stats,
        }

    if with_text == 0 and raster_only:
        return {
            "reason": (
                f"У файлі {names} немає текстового шару: усі {raster_only} стор. — "
                "растрові зображення (скан або експорт у картинку)."
            ),
            "recommendations": [
                "Експортуйте PDF з CAD або графічного редактора напряму, "
                "а не скануйте роздруківку — тоді підписи й відомості читаються як текст.",
                "Якщо є лише скан, прожене його через OCR перед завантаженням.",
                "Переконайтеся, що аркуші з відомостями увійшли в експорт.",
            ],
            "stats": stats,
        }

    return {
        "reason": (
            f"З {total_pages} стор. не вдалося виділити ані об'ємів, ані систем, "
            "придатних для кошторису."
        ),
        "recommendations": [
            "Перевірте, чи комплект містить відомості з кількостями, а не лише "
            "плани й візуалізації.",
            "Впишіть у «Технічне завдання» проєкту склад робіт — це дає аналізу опору.",
            "Показники об'єкта можна заповнити вручну на сторінці «Аналіз об'єкта», "
            "після чого кошторис порахується за правилами шаблону.",
        ],
        "stats": stats,
    }


# --- step 1: analyse documents ----------------------------------------------


def analyse_project(session: Session, project: Project) -> dict[str, Any]:
    """Extract, triage, analyse and fuse every document into an object model."""
    job = _start_job(session, project.id, "analyze")
    report = _progress(session, job)
    report(0.02, "Читання документів")

    documents: list[tuple[Any, str]] = []
    for document in project.documents:
        try:
            extracted = extract_document(document.stored_path)
            documents.append((extracted, document.filename))
            _sync_pages(session, document, extracted)
        except Exception as exc:  # noqa: BLE001
            document.status = "error"
            document.error = repr(exc)
            session.commit()
            log.exception("extract failed: %s", document.filename)

    if not documents:
        _finish(session, job, "error", {}, "Жоден документ не вдалося прочитати.")
        return {"status": "error", "message": "Жоден документ не вдалося прочитати."}

    analyzer = DocumentAnalyzer(AIClient(settings), settings)
    outcome = analyzer.analyse_documents(
        documents, brief=project.brief or "", on_progress=report
    )

    _store_page_findings(session, project, outcome)

    if outcome.analysis is None:
        diagnosis = _diagnose_empty_analysis(documents, outcome)
        _finish(
            session,
            job,
            "error",
            {
                "errors": outcome.errors,
                "skipped": outcome.skipped,
                "usage": outcome.usage,
                **diagnosis,
            },
            "; ".join(outcome.errors) or "Аналіз не сформовано.",
        )
        return {
            "status": "error",
            "message": diagnosis["reason"],
            "errors": outcome.errors,
            "skipped": outcome.skipped,
            **diagnosis,
        }

    analysis = _store_analysis(session, project, outcome)
    _store_questions(session, project, outcome)

    _finish(
        session,
        job,
        "done",
        {
            "pages_analysed": len([p for p in outcome.pages if p.findings]),
            "pages_failed": len([p for p in outcome.pages if p.error]),
            "skipped": outcome.skipped,
            "usage": outcome.usage,
            "errors": outcome.errors,
        },
    )
    project.status = "analysed"
    session.commit()

    result = {
        "status": "ok",
        "analysis_id": analysis.id,
        "job_id": job.id,
        "pages_analysed": len([p for p in outcome.pages if p.findings]),
        "skipped": outcome.skipped,
        "errors": outcome.errors,
        "usage": outcome.usage,
        "degraded": outcome.degraded,
    }
    if outcome.degraded:
        # The lenient pass produced something, but it rests on thin evidence.
        # Say so plainly rather than let it pass for a full analysis.
        result["reason"] = (
            "Даних у файлі мало — аналіз виконано в полегшеному режимі, "
            "більшість показників позначено як припущення."
        )
        result["recommendations"] = [
            "Перевірте кожен показник у розділі «Показники об'єкта» — "
            "припущення позначені окремим статусом.",
            "Показники, які не мають входити до КП, позначте статусом "
            "«Не враховувати в КП».",
            "Для точного розрахунку додайте аркуші з відомостями кількостей.",
        ]
    return result


def _sync_pages(session: Session, document: Document, extracted: Any) -> None:
    existing = {p.page_number: p for p in document.pages}
    for page in extracted.pages:
        row = existing.get(page.page_number)
        if row is None:
            row = DocumentPage(document_id=document.id, page_number=page.page_number)
            session.add(row)
        row.page_hash = page.page_hash
        row.text = page.text[:200_000]
        row.text_length = page.text_length
        row.image_count = page.image_count
        row.tables = page.tables[:6]
        row.page_type = page.page_type
        row.needs_vision = page.needs_vision
    document.page_count = extracted.page_count
    document.kind = extracted.kind
    document.status = "extracted"
    session.commit()


def _store_page_findings(session: Session, project: Project, outcome: Any) -> None:
    by_number: dict[int, DocumentPage] = {}
    for document in project.documents:
        for page in document.pages:
            by_number.setdefault(page.page_number, page)

    for result in outcome.pages:
        page = by_number.get(result.page_number)
        if page is None:
            continue
        if result.findings is not None:
            page.findings = result.findings.model_dump(mode="json")
            page.confidence = result.findings.confidence
            page.analysed = True
    session.commit()


def _store_analysis(session: Session, project: Project, outcome: Any) -> ObjectAnalysis:
    previous = session.scalars(
        select(ObjectAnalysis)
        .where(ObjectAnalysis.project_id == project.id)
        .order_by(ObjectAnalysis.version.desc())
        .limit(1)
    ).first()
    result = outcome.analysis

    # The aggregate schema takes plain strings for the enum-like fields (see
    # ai/schemas.py); normalise them here so nothing downstream sees a value
    # outside the template's own vocabulary.
    facts = []
    for f in result.facts:
        data = f.model_dump(mode="json")
        data["status"] = clean_fact_status(data.get("status", ""))
        data["confidence"] = clean_confidence(data.get("confidence", ""))
        data["source_type"] = clean_source_type(data.get("source_type", ""))
        data["section"] = clean_section(data.get("section", ""))
        facts.append(data)

    systems = []
    for s in result.systems:
        data = s.model_dump(mode="json")
        key = clean_section(data.get("key", ""))
        if key is None:
            # A section the template does not have cannot be estimated; keep it
            # visible as a risk rather than silently dropping or inventing it.
            result.risks.append(
                f"Модель визначила систему «{data.get('label') or data.get('key')}», "
                "якої немає у шаблоні кошторису — потрібне рішення користувача."
            )
            continue
        data["key"] = key
        data["confidence"] = clean_confidence(data.get("confidence", ""))
        systems.append(data)

    plants = []
    for p in result.plants:
        data = p.model_dump(mode="json")
        data["confidence"] = clean_confidence(data.get("confidence", ""))
        plants.append(data)

    coverage = []
    for c in result.coverage:
        data = c.model_dump(mode="json")
        data["section"] = clean_section(data.get("section", ""))
        data["confidence"] = clean_confidence(data.get("confidence", ""))
        coverage.append(data)

    analysis = ObjectAnalysis(
        project_id=project.id,
        version=(previous.version + 1) if previous else 1,
        status="draft",
        object_type=result.object_type,
        summary=result.summary,
        facts=facts,
        systems=systems,
        plants=plants,
        components=coverage,
        assumptions=list(result.assumptions),
        unknowns=list(result.unknowns),
        risks=list(result.risks),
        conflicts=[c.model_dump(mode="json") for c in result.conflicts],
    )
    session.add(analysis)
    session.commit()
    session.refresh(analysis)
    return analysis


def _store_questions(session: Session, project: Project, outcome: Any) -> None:
    existing = {
        q.code
        for q in session.scalars(select(Question).where(Question.project_id == project.id)).all()
    }
    for question in outcome.analysis.questions:
        if question.code in existing:
            continue
        session.add(
            Question(
                project_id=project.id,
                group=question.group or "general",
                code=question.code,
                text=question.text,
                why=question.why,
                kind=clean_question_kind(question.kind),
                choices=list(question.choices),
                affects=list(question.affects),
            )
        )
    for conflict in outcome.analysis.conflicts:
        code = f"conflict:{normalize_name(conflict.topic)[:80]}"
        if code in existing:
            continue
        session.add(
            Question(
                project_id=project.id,
                group="conflicts",
                code=code,
                text=conflict.question or f"Оберіть правильне значення: {conflict.topic}",
                why="Джерела дають різні значення: " + "; ".join(conflict.values),
                kind="choice",
                choices=list(conflict.values),
                affects=[conflict.impact],
            )
        )
    session.commit()


# --- step 2: plan and build --------------------------------------------------


def plan_and_build(
    session: Session, project: Project, payload: EstimateCreate
) -> dict[str, Any]:
    """Turn the object analysis into a priced, validated draft estimate."""
    job = _start_job(session, project.id, "estimate")
    report = _progress(session, job)
    report(0.05, "Підготовка даних")

    layout = TemplateLayout.load()
    builder = EstimateBuilder(session, layout)

    analysis = session.scalars(
        select(ObjectAnalysis)
        .where(ObjectAnalysis.project_id == project.id)
        .order_by(ObjectAnalysis.version.desc())
        .limit(1)
    ).first()

    sections = list(payload.sections)
    quantities = {k: dict(v) for k, v in payload.quantities.items()}
    plants = list(payload.plants)
    notes: list[str] = []

    if analysis is not None:
        derived = _from_analysis(analysis, layout, session)
        sections = sections or derived["sections"]
        for key, values in derived["quantities"].items():
            quantities.setdefault(key, {}).update(
                {k: v for k, v in values.items() if k not in quantities.get(key, {})}
            )
        plants = plants or derived["plants"]
        notes.extend(derived["notes"])
        _questions_from_coverage(session, project, derived.get("unresolved", []))

    if not sections:
        _finish(session, job, "error", {}, "Не визначено жодної секції кошторису.")
        return {
            "status": "error",
            "message": (
                "Не визначено, які секції потрібні. Заповніть аналіз об'єкта "
                "або вкажіть секції вручну."
            ),
        }

    report(0.35, "Підбір позицій та розрахунок")
    result = builder.build(
        sections=sections,
        quantities=quantities,
        plants=plants,
        options=payload.options,
        settings={**(project.settings or {}), **payload.settings},
    )

    report(0.75, "Перевірка кошторису")
    open_questions = [
        {"text": q.text, "why": q.why, "affects": q.affects or []}
        for q in session.scalars(
            select(Question).where(
                Question.project_id == project.id, Question.status == "open"
            )
        ).all()
    ]
    validation = validate(
        result.draft,
        result.totals,
        {
            "catalog_units": _catalog_units(session),
            "open_questions": open_questions,
            "conflicts": (analysis.conflicts or []) if analysis else [],
        },
    )

    previous = session.scalars(
        select(Estimate)
        .where(Estimate.project_id == project.id)
        .order_by(Estimate.version.desc())
        .limit(1)
    ).first()
    estimate = Estimate(
        project_id=project.id,
        analysis_id=analysis.id if analysis else None,
        version=(previous.version + 1) if previous else 1,
        status="draft",
    )
    session.add(estimate)
    session.commit()
    session.refresh(estimate)

    candidates = {
        entry["name"]: entry["candidates"] for entry in result.ambiguous if entry.get("candidates")
    }
    save_draft(
        session,
        estimate,
        result.draft,
        result.totals,
        section_titles=layout.titles(),
        candidates=candidates,
        report=validation,
    )
    _questions_from_build(session, project, estimate, result, layout)
    carried = _carry_over_answers(session, project, estimate)
    if carried:
        notes.append(f"Перенесено відповідей користувача: {carried}.")
        recalculated = recalculate_estimate(session, estimate)
        validation_dict = recalculated["validation"]
    else:
        validation_dict = validation.to_dict()

    _finish(
        session,
        job,
        "done",
        {
            "estimate_id": estimate.id,
            "sections": sections,
            "unmatched": len(result.unmatched),
            "ambiguous": len(result.ambiguous),
        },
    )
    project.status = "estimated"
    session.commit()

    return {
        "status": "ok",
        "estimate_id": estimate.id,
        "job_id": job.id,
        "totals": estimate.totals,
        "sections": group_by_section(estimate.lines),
        "validation": validation_dict,
        "unmatched": result.unmatched,
        "ambiguous": result.ambiguous,
        "notes": notes + result.notes,
    }


def _from_analysis(
    analysis: ObjectAnalysis, layout: TemplateLayout, session: Session
) -> dict[str, Any]:
    """Read sections, drivers and plants out of the stored object analysis.

    Only values the analysis actually carries are used. Anything missing stays
    missing so it surfaces as a question rather than as a silent default.
    """
    sections = [s.get("key") for s in (analysis.systems or []) if s.get("key")]
    sections = [s for s in sections if s in layout.sections]

    quantities: dict[str, dict[str, float]] = {}
    notes: list[str] = []
    unresolved: list[dict[str, Any]] = []
    # A drawing lists a cable in eleven runs and a template row is one line, so
    # the runs add up. But the analysis reports the same drawing twice — as
    # facts and as components — and the two views name things differently:
    # "Агрополотно 50 г/м², чорне" against "Агрополотно", one schedule row read
    # twice. Adding those gave 304 m² of fabric where the drawing says 152.
    #
    # So each view accumulates on its own and the views do not add to each
    # other: the row takes the larger of them. Eleven readings inside one view
    # are eleven runs; one reading in each view is one run seen twice.
    by_origin: dict[tuple[str, str], dict[str, float]] = {}
    counted: set[tuple[str, str, str, str]] = set()

    def record(section: str, row: str, value: float, source: str, origin: str) -> bool:
        key = (section, normalize_name(row), origin, normalize_name(source))
        if key in counted:
            return False
        counted.add(key)
        totals = by_origin.setdefault((section, row), {})
        totals[origin] = totals.get(origin, 0.0) + value
        rows = quantities.setdefault(section, {})
        rows[row] = max(totals.values())
        if section not in sections:
            sections.append(section)
        return True

    search = CatalogSearch(session)
    billing_units = {normalize_unit(i.unit) for i in search.items if i.unit}

    for fact in analysis.facts or []:
        if fact.get("status") in FACT_STATUSES_OUT_OF_ESTIMATE:
            continue
        value = _as_float(fact.get("value"))
        if value is None:
            continue  # no number: a note about the site, not a quantity
        label = str(fact.get("label", "")).strip()
        if not label:
            continue
        unit = str(fact.get("unit", "")).strip()
        target_section = fact.get("section")
        if target_section not in layout.sections:
            target_section = None

        # The section comes first. "Довжина траншей (м)" is a driver of
        # irrigation, water supply and lighting alike, so a trench length with
        # no section attached must not be handed to whichever of them is
        # checked first -- 66 m of irrigation trench would become 66 m of
        # lighting trench, silently and wrongly.
        section = target_section or _guess_section(label, layout)
        if section is None:
            unresolved.append({
                "name": label, "value": value, "unit": unit,
                "reason": "не визначено, до якої секції належить показник",
                "origin": "fact",
                "measure": normalize_name(unit) not in billing_units,
            })
            continue

        # Inside that section: its own input rows first, then the catalogue --
        # the same road a coverage row travels. A fact naming an article
        # ("Світильник стовпець С-1, Ideal Lux 306872") is not a driver and
        # never was.
        article, kind, reason = _resolve_coverage_target(label, unit, section, layout, search)
        if article is None:
            unresolved.append({
                "name": label, "value": value, "unit": unit, "section": section,
                "reason": reason or "не зіставлено з жодним рядком шаблону",
                "origin": "fact",
                "measure": normalize_name(unit) not in billing_units,
            })
            notes.append(f"«{label}» ({value:g} {unit}) — {reason}")
            continue

        if record(section, article, value, label, "fact"):
            total = quantities[section][article]
            notes.append(
                f"«{label}» → «{article}» ({kind}), {value:g} {unit}"
                + (f"; разом по рядку {total:g}" if total != value else "")
                + "."
            )

    for entry in analysis.components or []:
        name = str(entry.get("name", "")).strip()
        value = _as_float(entry.get("quantity"))
        section = entry.get("section")
        unit = str(entry.get("unit", "")).strip()
        if not name or value is None:
            continue
        if section not in layout.sections:
            section = _guess_section(name, layout)
        if section is None:
            unresolved.append({"name": name, "value": value, "unit": unit,
                               "reason": "не визначено, до якої секції належить позиція"})
            continue

        target, kind, reason = _resolve_coverage_target(name, unit, section, layout, search)
        if target is None:
            unresolved.append({"name": name, "value": value, "unit": unit,
                               "section": section, "reason": reason})
            notes.append(f"«{name}» ({value:g} {unit}) — {reason}")
            continue

        if record(section, target, value, name, "component"):
            total = quantities[section][target]
            notes.append(
                f"«{name}» → «{target}» ({kind}), {value:g} {unit}"
                + (f"; разом по рядку {total:g}" if total != value else "")
                + "."
            )

    plants = [
        {
            "name": p.get("name", ""),
            "quantity": _as_float(p.get("quantity")),
            "is_existing": bool(p.get("is_existing")),
            "reason": p.get("note", ""),
        }
        for p in (analysis.plants or [])
        if p.get("name")
    ]
    if plants and "planting" not in sections:
        sections.append("planting")

    return {
        "sections": [s for s in layout.order if s in sections],
        "quantities": quantities,
        "plants": plants,
        "notes": notes,
        "unresolved": unresolved,
    }


# Which section a coverage row belongs to, when the model did not say. Keyed on
# the vocabulary the client's own drawings use.
_SECTION_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("lawn", ("газон", "рулон", "посівн", "конюшин")),
    ("paving", ("бруків", "поребрик", "бордюр 1000", "замощен", "тротуарн")),
    ("pathway", ("плит", "терасн", "доріжк", "настил")),
    ("geogrid", ("георешіт",)),
    ("planting", ("кора", "крихт", "мульч", "агрополотн", "агроволокн", "клумб",
                  "бордюр", "декор", "рослин")),
    ("planters", ("кашпо",)),
    ("irrigation", ("полив", "дощувач", "крапельн")),
    ("lighting", ("світильник", "освітлен", "ліхтар")),
    ("drainage_ground", ("дренаж",)),
    ("drainage_storm", ("водовідвед", "дощоприйм", "лоток")),
    ("fire_zone", ("вогн", "кострищ")),
]


def _guess_section(name: str, layout: TemplateLayout) -> str | None:
    low = normalize_name(name)
    for key, markers in _SECTION_HINTS:
        if key in layout.sections and any(m in low for m in markers):
            return key
    return None


# Words that say how a template row is measured, not what it measures. Two rows
# agreeing only on one of these agree on nothing.
_MEASURE_STEMS = {"площ", "довж", "зага", "обєм", "об'є", "кіль", "к-ть", "всьо", "сума"}


def _identifying_overlap(driver: str, name: str) -> bool:
    """Do the two names agree on *what* is measured, not only on how."""
    return bool((stems(driver) - _MEASURE_STEMS) & (stems(name) - _MEASURE_STEMS))


def _resolve_coverage_target(
    name: str,
    unit: str,
    section: str,
    layout: TemplateLayout,
    search: CatalogSearch,
) -> tuple[str | None, str, str]:
    """Map a coverage-schedule row onto a template row.

    A drawing says "Газон 259 м²"; the template wants that number in its input
    row "Площа газону (рулонного)". A drawing says "Плити ходові бетонні 37 шт";
    the template wants the catalog article. So we try the section's own driver
    rows first, then the catalog.

    Returns ``(target_row_name, kind, reason)``. A ``None`` target is never a
    silent drop -- the caller records it and raises a question.
    """
    # 1. A driver row of this section. Stem comparison, because a schedule says
    #    "Газон" where the template row is "Площа газону (рулонного)".
    # Sorted on the score alone: Python's sort is stable, so drivers that tie
    # stay in the order the workbook lists them. Sorting on the tuple broke ties
    # alphabetically instead and sent "Бруківка 4 м²" to "Установка бруківки на
    # клей" over "Загальна площа бруківка", purely because У follows З.
    scored = sorted(
        ((max(stem_overlap(name, d), stem_overlap(d, name)), d) for d in layout.drivers(section)),
        key=lambda pair: -pair[0],
    )
    weak_driver: str | None = None
    if scored:
        best, driver = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        if best >= 0.75:
            return driver, "рядок-драйвер шаблону", ""
        # A drawing names an input row with extra context the template omits:
        # "Мережі поливу, L траншей" against "Довжина траншей (м)". Half the
        # driver's own vocabulary is repeated and nothing else in the section
        # comes close. That alone is not enough, and each guard below exists
        # because relaxing the threshold without it placed a real figure in the
        # wrong row on this very project:
        #
        #   * the agreement must survive dropping the measurement words. "Площа
        #     озеленення 295 м²" and "Агрополотно площа (клумб)" share nothing
        #     but "площа", and the fabric area is not the planted area — Будьків
        #     invoiced 175 against 406;
        #   * the row's own unit must be the fact's unit. "Плити 400х400 = 3 шт"
        #     is a count and "Загальна площа плит" is an area in m², so 3 would
        #     have been read as three square metres;
        #   * and the figure must be billed in a unit this company uses at all.
        #     "Загальна витрата форсунок 25,72 л/хв" shares the word "форсунок"
        #     with a driver and is a flow rate; л/хв appears nowhere in the base.
        #
        # The row must also state its own unit and agree. Without that, "Крапельна
        # трубка 92 м.п" landed in "Виводи під капельну трубу" — 92 metres of tube
        # read as 92 outlets — because that row's wording names no unit at all.
        billing = {normalize_unit(i.unit) for i in search.items if i.unit}
        driver_unit = _driver_unit(driver)
        if (
            best >= 0.5
            and (best - runner_up) >= 0.25
            and _identifying_overlap(driver, name)
            and unit
            and normalize_unit(unit) in billing
            and driver_unit
            and normalize_unit(driver_unit) == normalize_unit(unit)
        ):
            # Held back rather than returned: a confident catalogue article is
            # better evidence than half a row name, so the catalogue goes first
            # and this is what happens if it finds nothing.
            weak_driver = driver

    # 2. A catalog article. The template itself says which section an article
    #    belongs to, so a confident match in another section is placed there
    #    rather than refused.
    match = search.match(name)
    if match.status == "matched" and match.best is not None:
        candidate = match.best.item
        home = _section_of_article(candidate.name, layout, prefer=section)
        if home is None and section not in layout.sections:
            return (
                None,
                "",
                f"позиція «{candidate.name}» не входить у жодну секцію шаблону",
            )
        if unit and normalize_unit(candidate.unit) != normalize_unit(unit):
            return (
                None,
                "",
                (
                    f"одиниці не збігаються: у кресленні «{unit}», у каталозі "
                    f"«{candidate.unit}» ({candidate.name}) — потрібне перерахування, "
                    "система його не вигадує"
                ),
            )
        if home is None:
            # The catalogue prices it and the drawing says which section it
            # belongs to, but the workbook has no row for it — every article
            # added from an issued proposal is in this position, and every
            # light fitting on this project was one. Refusing left the section
            # empty over an article whose price was in hand; it is added to the
            # section the drawing put it in, as a line of its own.
            return candidate.name, "позиція каталогу (додано до секції)", ""
        kind = "позиція каталогу"
        if home != section:
            kind = f"позиція каталогу, секція «{layout.title(home)}»"
        return candidate.name, kind, ""

    # 3. No article. A template input row that half-matched and agrees on units
    #    is now the best evidence there is.
    if weak_driver is not None:
        return weak_driver, "рядок-драйвер шаблону (за основами слів)", ""

    if match.status == "ambiguous":
        options = ", ".join(c.item.name for c in match.candidates[:3])
        return None, "", f"кілька відповідників у каталозі ({options}) — потрібен вибір"

    return None, "", "позиції немає в каталозі під цією назвою"


def _section_of_article(
    name: str, layout: TemplateLayout, prefer: str | None = None
) -> str | None:
    """Which template section contains this article.

    ``prefer`` wins when it carries the article, because most of the overheads
    are in every section by design and this used to hand them all to whichever
    one the template lists first. "Транспортні витрати" appears in thirteen
    sections and always resolved to prep; "Пісок" in ten, likewise; agrofabric
    in three and always to Доріжка. So a drawing's planting fabric was billed
    under paving, and — worse — the planting section's own input row stayed at
    zero, which is what the rest of that section is derived from. Every rule
    hanging off it stayed silent: the bed preparation, the consumables, the
    transport, the logistics.
    """
    key = normalize_name(name)

    def carries(section: str) -> bool:
        return any(
            line["block"] != "driver" and normalize_name(line["name"]) == key
            for line in layout.lines(section)
        )

    if prefer and prefer != "summary" and prefer in layout.sections and carries(prefer):
        return prefer
    for section in layout.order:
        if section == "summary":
            continue
        if carries(section):
            return section
    return None


def _questions_from_coverage(
    session: Session, project: Project, unresolved: list[dict[str, Any]]
) -> None:
    """A quantity read off a drawing that we could not place is never dropped."""
    if not unresolved:
        return
    existing = {
        q.code
        for q in session.scalars(select(Question).where(Question.project_id == project.id)).all()
    }
    for entry in unresolved:
        code = f"coverage:{normalize_name(entry['name'])[:90]}"
        if code in existing:
            continue
        qty = entry.get("value")
        unit = entry.get("unit", "")
        session.add(
            Question(
                project_id=project.id,
                group="Відомість покриттів",
                code=code,
                text=(
                    f"Куди віднести «{entry['name']}» ({qty:g} {unit}) з відомості покриттів?"
                    if qty is not None
                    else f"Куди віднести «{entry['name']}» з відомості покриттів?"
                ),
                why=(
                    f"Кількість прочитана з креслення, але {entry.get('reason', 'не зіставлена')}. "
                    "Без цього позиція не потрапить у кошторис."
                ),
                kind="text",
                affects=[entry.get("section", "")],
            )
        )
    session.commit()


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _catalog_units(session: Session) -> dict[str, str]:
    from ..models import CatalogItem

    rows = session.execute(
        select(CatalogItem.name_norm, CatalogItem.unit).where(CatalogItem.active.is_(True))
    ).all()
    return {name: unit for name, unit in rows if unit}


def _plant_size_question(
    project: Project,
    estimate: Estimate,
    name: str,
    entry: dict[str, Any],
    options: list[dict[str, Any]],
) -> Question:
    """Ask which size, priced, instead of asking for a price.

    A schedule that writes "Сосна гірська" with no container has not named an
    article: the invoices carry that plant in four sizes from 950 to 3900 грн.
    The system will not pick one, but it knows all four and what each was sold
    for, so the estimator answers by choosing rather than by ringing a nursery.
    """
    return Question(
        project_id=project.id,
        estimate_id=estimate.id,
        group="Розміри рослин",
        code=f"price:{normalize_name(name)[:90]}",
        text=f"Який розмір «{name}» закладати?",
        why=(
            "У відомості розмір не вказано, а саме він визначає ціну. "
            f"Ця рослина продавалась у {len(options)} варіантах — "
            "оберіть той, що у проєкті."
        ),
        kind="choice",
        choices=[
            f"{o['name']} — {o['unit_price']:g} грн"
            + (f" ({o['issued']})" if o.get("issued") else "")
            for o in options
        ],
        affects=[entry.get("section", "planting")],
    )


def _questions_from_build(
    session: Session,
    project: Project,
    estimate: Estimate,
    result: Any,
    layout: TemplateLayout,
) -> None:
    """Turn unresolved matches and empty option toggles into grouped questions."""
    existing = {
        q.code
        for q in session.scalars(select(Question).where(Question.project_id == project.id)).all()
    }

    for entry in result.ambiguous:
        code = f"match:{normalize_name(entry['name'])[:90]}"
        if code in existing:
            continue
        session.add(
            Question(
                project_id=project.id,
                estimate_id=estimate.id,
                group="Вибір позиції каталогу",
                code=code,
                text=f"Яку позицію використати замість «{entry['name']}»?",
                why=entry["reason"],
                kind="choice",
                choices=[c["name"] for c in entry.get("candidates", [])[:5]],
                affects=[entry["section"]],
            )
        )

    for entry in result.unmatched:
        code = f"missing:{normalize_name(entry['name'])[:90]}"
        if code in existing:
            continue
        session.add(
            Question(
                project_id=project.id,
                estimate_id=estimate.id,
                group="Позиції поза каталогом",
                code=code,
                text=f"Позиції «{entry['name']}» немає в каталозі. Чим її замінити?",
                why=entry["reason"],
                kind="text",
                affects=[entry["section"]],
            )
        )

    # Plants matched but unpriced: the client's base carries no plant prices.
    # Where the invoices carry the plant in several sizes, the question is which
    # size — a choice with prices on it — not "quote me a number".
    sized = {
        normalize_name(entry["name"]): entry for entry in getattr(result, "plant_options", [])
    }
    for line in result.draft.lines:
        if line.block != "plants" or line.quantity <= 0 or line.unit_price > 0:
            continue
        code = f"price:{normalize_name(line.name)[:90]}"
        entry = sized.get(normalize_name(line.name))
        if entry:
            options = entry["options"]
            asked = _plant_size_question(project, estimate, line.name, entry, options)
            if code in existing:
                # The same plant was already asked the old way, as a bare "what
                # does it cost". That question is still open and we can now ask
                # a better one; skipping on the code would freeze the worse
                # version in place for every project that ever saw it.
                previous = session.scalars(
                    select(Question).where(
                        Question.project_id == project.id, Question.code == code
                    )
                ).first()
                if previous is None or previous.answer:
                    continue
                previous.group = asked.group
                previous.text = asked.text
                previous.why = asked.why
                previous.kind = asked.kind
                previous.choices = asked.choices
                previous.estimate_id = estimate.id
                continue
            session.add(asked)
            continue
        if code in existing:
            continue
        session.add(
            Question(
                project_id=project.id,
                estimate_id=estimate.id,
                group="Ціни рослин",
                code=code,
                text=f"Яка ціна за 1 шт для «{line.name}»?",
                why=(
                    "У базі клієнта для рослин ціни не ведуться — вони визначаються "
                    "за прайсом розсадника на конкретний проєкт."
                ),
                kind="number",
                affects=["planting"],
            )
        )

    session.commit()


# --- step 3: answers and recalculation --------------------------------------


def _carry_over_answers(session: Session, project: Project, estimate: Estimate) -> int:
    """Re-apply what the estimator has already decided to a fresh estimate.

    A rebuild starts from the object analysis and prices everything again from
    the catalogue and the price history, so it knew nothing about answers given
    to earlier versions. Answer every question on a project, press recalculate,
    and every price you entered was gone -- the plants went back to 0,00 and the
    proposal was unexportable again for exactly the reasons you had just cleared.

    Only the two kinds of answer that carry a decision about a line are
    replayed: a price and a catalogue choice. Everything else is a note for the
    estimator and is left where it is.

    The answers are also re-pointed at this estimate. They were bound to the one
    that raised the question, which by now can be several versions old, so
    answering again would have gone on writing into a superseded draft.
    """
    answered = session.scalars(
        select(Question).where(
            Question.project_id == project.id,
            Question.status == "answered",
            Question.answer.is_not(None),
        )
    ).all()

    applied = 0
    for question in answered:
        if not question.code.startswith(("price:", "match:")):
            continue
        if not (question.answer or "").strip():
            continue
        question.estimate_id = estimate.id
        apply_answer(session, estimate, question)
        applied += 1
    if applied:
        session.commit()
        _ask_about_unpriced_lines(session, project, estimate)
    return applied


def _ask_about_unpriced_lines(
    session: Session, project: Project, estimate: Estimate
) -> None:
    """Ask again for anything the replayed answers left without a price.

    An answer can rename the line it lands on: choosing "Ялина оморіка 2м" as
    the substitute for "Ялина корейська" gives the estimate an article the price
    base does not price. The question that was asked under the old name is
    answered and closed, and without this nothing would ever ask under the new
    one -- the proposal stays unexportable with no way for the estimator to see
    why from the questions list.
    """
    existing = {
        q.code
        for q in session.scalars(
            select(Question).where(Question.project_id == project.id)
        ).all()
    }
    added = False
    for line in estimate.lines:
        if line.quantity <= 0 or line.unit_price > 0 or line.block == "driver":
            continue
        code = f"price:{normalize_name(line.name)[:90]}"
        if code in existing:
            continue
        session.add(
            Question(
                project_id=project.id,
                estimate_id=estimate.id,
                group="Ціни рослин" if line.block == "plants" else "Ціни позицій",
                code=code,
                text=f"Яка ціна за 1 {line.unit or 'шт'} для «{line.name}»?",
                why=(
                    "Позиція з'явилася після вашої відповіді й не має ціни "
                    "ні в базі, ні в історії виданих КП."
                ),
                kind="number",
                affects=[line.section],
            )
        )
        existing.add(code)
        added = True

    # And close the ones whose line is gone. Answering "Сосна гірська" with a
    # substitute renames the line, so the price question raised under the old
    # name asks about something the estimate no longer contains -- and an open
    # question is itself a NEEDS_USER_INPUT, so it held the export shut over a
    # line that had been priced and renamed several rebuilds ago.
    live = {normalize_name(l.name)[:90] for l in estimate.lines if l.quantity > 0}
    stale = [
        q
        for q in session.scalars(
            select(Question).where(
                Question.project_id == project.id,
                Question.status == "open",
            )
        ).all()
        if q.code.startswith("price:") and q.code[len("price:"):] not in live
    ]
    for question in stale:
        question.status = "dismissed"
        question.answer = (
            "Позиції немає в кошторисі — її замінено іншою у відповідь на "
            "попереднє питання."
        )
        added = True

    if added:
        session.commit()


def _chosen_article(session: Session, answer: str):
    """The price-history entry the estimator picked out of a size question.

    The choices are rendered "<article> — <price> грн (<issued>)", so the name
    is what stands before the dash. Returns ``None`` when the answer is free
    text rather than one of the offered options.
    """
    from .history.prices import InvoicedPrices

    name = answer.split("—")[0].strip() if "—" in answer else answer.strip()
    if not name:
        return None
    return InvoicedPrices.from_db(session).exact(name)


def apply_answer(session: Session, estimate: Estimate, question: Question) -> None:
    """Apply one answer to the estimate it affects."""
    answer = (question.answer or "").strip()
    if not answer:
        return

    if question.code.startswith("price:"):
        target = question.code[len("price:"):]
        price = _as_float(answer)
        chosen = None
        if price is None:
            # A size question is answered by picking one of its own labels —
            # "Сосна гірська, d20-30см — 950 грн (15.06.2026)". The figure is
            # read back out of the price history by the article's name rather
            # than parsed out of the label: the history is where it came from,
            # and a label is for reading.
            chosen = _chosen_article(session, answer)
            if chosen is not None:
                price = chosen.unit_price
        if price is None:
            return
        for line in estimate.lines:
            if normalize_name(line.name).startswith(target):
                line.unit_price = price
                line.total = round(line.quantity * price, 2)
                if chosen is not None:
                    # The schedule said "Сосна гірська"; the estimate should say
                    # which one, or the proposal names a plant nobody can order.
                    line.name = chosen.name
                    line.unit = chosen.unit or line.unit
                    line.source_refs = list(line.source_refs or []) + [{
                        "source_type": "historical_estimate",
                        "source_ref": chosen.project,
                        "detail": chosen.trace(),
                    }]
                    line.reasons = list(line.reasons or []) + [
                        f"Розмір обрано користувачем: {chosen.name}."
                    ]
                else:
                    line.reasons = list(line.reasons or []) + [
                        "Ціну внесено користувачем у відповідь на питання."
                    ]
                line.confidence = "high"
        session.commit()
        return

    if question.code.startswith("match:"):
        target = question.code[len("match:"):]
        item = CatalogSearch(session).get_exact(answer)
        if item is None:
            return
        for line in estimate.lines:
            if normalize_name(line.name).startswith(target):
                line.catalog_id = item.id
                line.name = item.name
                line.unit = item.unit
                line.unit_price = item.unit_price
                line.unit_cost = item.unit_cost
                line.total = round(line.quantity * item.unit_price, 2)
                line.match_status = "matched"
                line.confidence = "high"
                line.reasons = list(line.reasons or []) + [
                    "Позицію обрано користувачем у відповідь на питання."
                ]
        session.commit()
        return

    # Anything else is recorded; the operator applies it through the estimate UI.


def recalculate_estimate(session: Session, estimate: Estimate) -> dict[str, Any]:
    """Re-run rules, totals and validation over the current stored lines."""
    layout = TemplateLayout.load()
    builder = EstimateBuilder(session, layout)

    session.refresh(estimate)
    draft = draft_from_estimate(estimate)
    result = builder.recalculate(draft, estimate.settings)

    open_questions = [
        {"text": q.text, "why": q.why, "affects": q.affects or []}
        for q in session.scalars(
            select(Question).where(
                Question.project_id == estimate.project_id, Question.status == "open"
            )
        ).all()
    ]
    validation = validate(
        result.draft,
        result.totals,
        {"catalog_units": _catalog_units(session), "open_questions": open_questions},
    )
    save_draft(
        session,
        estimate,
        result.draft,
        result.totals,
        section_titles=layout.titles(),
        report=validation,
    )
    return {
        "status": "ok",
        "estimate_id": estimate.id,
        "totals": estimate.totals,
        "sections": group_by_section(estimate.lines),
        "validation": validation.to_dict(),
    }
