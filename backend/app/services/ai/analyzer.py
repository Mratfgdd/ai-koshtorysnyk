"""Document analysis: pages in parallel, then one aggregation pass.

Pipeline shape, chosen for the ~10 minute target on a 60-page drawing set::

    extract (cheap, local)
      -> classify pages (cheap, local)
      -> analyse only the pages that carry data, concurrently, cached
      -> aggregate into one object model (single call)

Pages that fail are recorded and skipped, not retried forever; the run stays
resumable because every page result is cached by page hash.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ...config import Settings, get_settings
from ..docs.pdf_extract import ExtractedDocument, ExtractedPage, render_page
from .client import AIClient, AIUnavailable, image_block, text_block
from .prompts import OBJECT_ANALYSIS_SYSTEM, PAGE_ANALYSIS_SYSTEM
from .schemas import (
    ObjectAnalysisResult,
    PageFindings,
    ReviewModel,
    ScheduleModel,
    SiteModel,
)

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

# Added to the aggregation prompt on the second pass, when the first found no
# page worth aggregating. It lowers the evidence bar without licensing
# invention: a guessed area must arrive labelled as a guess, so the estimator
# sees exactly what to check rather than an empty screen.
LENIENT_PREAMBLE = """

УВАГА: вхідних даних мало — жодна сторінка не дала повного набору показників.
Це може бути ескіз, концепція або одиничний аркуш. Працюй із тим, що є:
* сформуй аналіз навіть за фрагментарними даними;
* кожне неточне значення познач status = "assumption", а не "confirmed";
* якщо показник вивести неможливо — status = "unknown" і опиши, чого бракує,
  у полі unknowns; не вигадуй числа;
* у assumptions поясни, з чого саме зроблено кожне припущення.
"""


@dataclass
class PageResult:
    page_number: int
    findings: PageFindings | None
    error: str | None = None
    used_vision: bool = False


@dataclass
class AnalysisOutcome:
    pages: list[PageResult] = field(default_factory=list)
    analysis: ObjectAnalysisResult | None = None
    errors: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    # True when the object model came from the lenient second pass rather than
    # from pages the page-level analysis judged useful. The caller warns the
    # estimator that the result rests on thin evidence.
    degraded: bool = False

    @property
    def pages_with_findings(self) -> list[PageResult]:
        return [p for p in self.pages if p.findings]

    @property
    def useful_pages(self) -> list[PageResult]:
        return [p for p in self.pages if p.findings and p.findings.is_useful]


class DocumentAnalyzer:
    def __init__(self, ai: AIClient | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.ai = ai or AIClient(self.settings)

    # -- page level -----------------------------------------------------------
    def analyse_page(
        self, doc: ExtractedDocument, page: ExtractedPage, source_name: str
    ) -> PageResult:
        """Analyse one page, using vision only when the page is raster."""
        parts: list[dict[str, Any]] = []
        used_vision = False

        if page.needs_vision:
            try:
                image = render_page(
                    doc.path,
                    page.page_number,
                    self.settings.page_image_dir,
                    dpi=self.settings.page_render_dpi,
                    max_px=self.settings.page_render_max_px,
                    page_hash=page.page_hash,
                )
                parts.append(image_block(image))
                used_vision = True
            except Exception as exc:  # noqa: BLE001
                log.warning("render failed for page %s: %s", page.page_number, exc)

        context = [
            f"Документ: {source_name}",
            f"Сторінка {page.page_number} з {doc.page_count}",
            f"Попередня класифікація: {page.page_type} ({page.reason})",
        ]
        if page.text.strip():
            context.append("\nТекст, витягнутий зі сторінки:\n" + page.text[:12000])
        else:
            context.append("\nТекстового шару немає — читай зі зображення.")
        if page.tables:
            context.append(
                "\nТаблиці, розпізнані векторно:\n"
                + json.dumps(page.tables[:4], ensure_ascii=False)[:8000]
            )
        parts.append(text_block("\n".join(context)))

        try:
            findings = self.ai.structured(
                schema_model=PageFindings,
                system=PAGE_ANALYSIS_SYSTEM,
                content=parts,
                max_tokens=8000,
            )
            return PageResult(page.page_number, findings, used_vision=used_vision)
        except AIUnavailable as exc:
            return PageResult(page.page_number, None, error=str(exc), used_vision=used_vision)
        except Exception as exc:  # noqa: BLE001
            log.exception("page %s analysis failed", page.page_number)
            return PageResult(page.page_number, None, error=repr(exc), used_vision=used_vision)

    # -- document level -------------------------------------------------------
    def select_pages(self, doc: ExtractedDocument) -> tuple[list[ExtractedPage], list[dict[str, Any]]]:
        """Pick which pages earn an AI call, and say why the rest were dropped.

        When a drawing set exceeds the vision budget, the pages that carry
        quantities win: schedules and plans before generic sheets.
        """
        priority = {
            "plant_schedule": 0,
            "spec_table": 1,
            "layout_plan": 2,
            "master_plan": 3,
            "planting_plan": 4,
            "dendro_plan": 5,
            "engineering": 6,
            "concept": 7,
            "brief": 8,
            "unknown": 9,
        }
        candidates = [p for p in doc.pages if p.needs_vision or p.text_length > 250]
        candidates.sort(key=lambda p: (priority.get(p.page_type, 99), p.page_number))

        budget = self.settings.max_vision_pages
        chosen = candidates[:budget]
        dropped = candidates[budget:]

        skipped = [
            {
                "page": p.page_number,
                "type": p.page_type,
                "reason": p.reason,
            }
            for p in doc.pages
            if p not in chosen
        ]
        for p in dropped:
            skipped.append(
                {
                    "page": p.page_number,
                    "type": p.page_type,
                    "reason": (
                        f"Перевищено ліміт візуального аналізу ({budget} сторінок). "
                        "Сторінку не аналізовано — перевірте вручну."
                    ),
                }
            )
        chosen.sort(key=lambda p: p.page_number)
        return chosen, skipped

    def analyse_documents(
        self,
        documents: Sequence[tuple[ExtractedDocument, str]],
        *,
        brief: str = "",
        on_progress: ProgressFn | None = None,
    ) -> AnalysisOutcome:
        """Analyse every uploaded document and fuse them into one object model."""
        outcome = AnalysisOutcome()

        if not self.ai.available:
            outcome.errors.append(
                "Ключ ANTHROPIC_API_KEY не налаштовано — аналіз документів недоступний. "
                "Дані з документів не вигадуються; заповніть аналіз об'єкта вручну."
            )
            return outcome

        jobs: list[tuple[ExtractedDocument, ExtractedPage, str]] = []
        for doc, name in documents:
            chosen, skipped = self.select_pages(doc)
            outcome.skipped.extend({**s, "document": name} for s in skipped)
            jobs.extend((doc, page, name) for page in chosen)

        if not jobs:
            outcome.errors.append("У завантажених документах немає сторінок із даними.")
            return outcome

        total = len(jobs)
        done = 0

        def run(job: tuple[ExtractedDocument, ExtractedPage, str]) -> PageResult:
            d, p, n = job
            return self.analyse_page(d, p, n)

        def progress(_index: int, _value: Any) -> None:
            nonlocal done
            done += 1
            if on_progress:
                on_progress(0.15 + 0.65 * done / total, f"Аналіз сторінок: {done}/{total}")

        if on_progress:
            on_progress(0.15, f"Аналіз сторінок: 0/{total}")

        results = self.ai.map_parallel(run, jobs, on_result=progress)
        outcome.pages = [r for r in results if isinstance(r, PageResult)]
        outcome.errors.extend(
            f"Сторінка {r.page_number}: {r.error}" for r in outcome.pages if r.error
        )

        useful = outcome.useful_pages
        fallback = outcome.pages_with_findings

        if not useful and not fallback:
            outcome.errors.append(
                "Жодна сторінка не дала корисних даних для кошторису."
            )
            outcome.usage = self.ai.usage.to_dict()
            return outcome

        if on_progress:
            on_progress(0.85, "Формування аналізу об'єкта")

        if useful:
            outcome.analysis = self._aggregate(useful, documents, brief)
        else:
            # Nothing cleared the "useful" bar — a concept sketch, a single
            # sheet, a scan with little text. Rather than give up, try once
            # more over everything the pages did yield, telling the model to
            # work with what there is and mark every gap as an assumption.
            # A thin object model the estimator can correct beats a dead end.
            log.info("no useful pages; retrying aggregation in lenient mode")
            outcome.errors.append(
                "Жодна сторінка не дала повних даних — аналіз виконано в "
                "полегшеному режимі за наявними фрагментами."
            )
            outcome.degraded = True
            outcome.analysis = self._aggregate(fallback, documents, brief, lenient=True)

        outcome.usage = self.ai.usage.to_dict()
        if on_progress:
            on_progress(1.0, "Аналіз завершено")
        return outcome

    def _aggregate(
        self,
        pages: list[PageResult],
        documents: Sequence[tuple[ExtractedDocument, str]],
        brief: str,
        lenient: bool = False,
    ) -> ObjectAnalysisResult | None:
        """Fuse per-page findings into one site model.

        Three calls, not one: a single combined output schema exceeds the API's
        grammar limit (see ``ai/schemas.py``). Each call shares the same cached
        prefix, so the split costs little and each part gets a sharper task.
        """
        names = {id(doc): name for doc, name in documents}
        payload = []
        for result in pages:
            if not result.findings:
                continue
            payload.append(
                {
                    "page": result.page_number,
                    "kind": result.findings.page_kind,
                    "summary": result.findings.summary,
                    "measurements": [m.model_dump() for m in result.findings.measurements],
                    "plants": [p.model_dump() for p in result.findings.plants],
                    "coverage": [c.model_dump() for c in result.findings.coverage],
                    "systems": [s.model_dump() for s in result.findings.systems],
                    "zones": result.findings.zones,
                    "notes": result.findings.notes,
                    "unreadable": result.findings.unreadable,
                }
            )

        shared = (
            "Файли: " + ", ".join(sorted(set(names.values())))
            + (f"\n\nТехнічне завдання від замовника:\n{brief}" if brief.strip() else "")
            + (LENIENT_PREAMBLE if lenient else "")
            + "\n\nРезультати посторінкового аналізу:\n"
            + json.dumps(payload, ensure_ascii=False)[:180000]
        )

        def call(model: type, task: str, max_tokens: int):
            return self.ai.structured(
                schema_model=model,
                system=OBJECT_ANALYSIS_SYSTEM,
                content=[text_block(shared + "\n\n" + task)],
                max_tokens=max_tokens,
            )

        try:
            site = call(
                SiteModel,
                "ЗАВДАННЯ ЦЬОГО КРОКУ: визнач тип об'єкта, стислий опис, показники "
                "(площі, довжини, кількості), потрібні секції кошторису та зони. "
                "Показник, що трапляється на кількох сторінках з різними значеннями, "
                "познач як status = needs_user_input.",
                12000,
            )
            schedules = call(
                ScheduleModel,
                "ЗАВДАННЯ ЦЬОГО КРОКУ: зведи асортиментну відомість рослин та відомість "
                "елементів покриття. Об'єднай дублікати однієї позиції й підсумуй "
                "кількості з дендроплану. Позиції з приміткою «існуючі» залиши у списку, "
                "але з is_existing = true.",
                12000,
            )
            review = call(
                ReviewModel,
                "ЗАВДАННЯ ЦЬОГО КРОКУ: перелічи припущення, невідоме, ризики, конфлікти "
                "даних і питання користувачу. Не більше 8 питань, згрупованих за темою.",
                10000,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("aggregation failed")
            raise AIUnavailable(f"Не вдалося сформувати аналіз об'єкта: {exc}") from exc

        return ObjectAnalysisResult(
            object_type=site.object_type,
            summary=site.summary,
            facts=site.facts,
            systems=site.systems,
            zones=site.zones,
            confidence=site.confidence,
            plants=schedules.plants,
            coverage=schedules.coverage,
            assumptions=review.assumptions,
            unknowns=review.unknowns,
            risks=review.risks,
            conflicts=review.conflicts,
            questions=review.questions,
        )


def source_ref(document_name: str, page_number: int) -> str:
    return f"{Path(document_name).name}, с. {page_number}"
