"""PDF extraction and page triage.

Cost discipline, in order:

1. **Cheap pass, always.** PyMuPDF gives text, vector tables and image counts
   for a 60-page drawing set in a second or two. The client's concept PDFs are
   part vector: the plant schedule and the technical indicators table come out
   as real text, with exact quantities, for free.
2. **Classify.** Title pages, contents, marketing spreads and blank sheets are
   identified and never sent to a model.
3. **Vision only where it pays.** Their drawing packages (46-58 pages, 150 MB+)
   are one full-page raster per sheet with no text at all -- those genuinely
   need vision, so we render them down to a sane resolution and cache by page
   hash so a re-run or a resumed job never pays twice.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

# --- Page types --------------------------------------------------------------
# Named after the client's own drawing register ("ВІДОМІСТЬ КРЕСЛЕНЬ").
PAGE_TYPES = {
    "title": "Титул / обкладинка",
    "contents": "Відомість креслень / зміст",
    "marketing": "Маркетинговий розворот",
    "photo": "Фотофіксація",
    "concept": "Концептуальне планування",
    "visual": "Візуалізація",
    "master_plan": "Генплан",
    "layout_plan": "Схема розпланування",
    "dendro_plan": "Дендроплан",
    "plant_schedule": "Асортиментна відомість рослин",
    "planting_plan": "Посадковий план",
    "engineering": "Інженерні мережі / розрізи",
    "spec_table": "Специфікація / відомість",
    "brief": "Технічне завдання / текст",
    "blank": "Порожня сторінка",
    "unknown": "Не визначено",
}

# Signals lifted from the actual drawing sets we were given.
TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("plant_schedule", re.compile(r"асортиментн\w*\s+відоміст|асортимент\s+рослин", re.I)),
    ("dendro_plan", re.compile(r"дендроплан", re.I)),
    ("planting_plan", re.compile(r"посадков\w+\s+план", re.I)),
    ("master_plan", re.compile(r"генплан|генеральн\w+\s+план", re.I)),
    ("layout_plan", re.compile(r"схема\s+розплануванн|розпланув", re.I)),
    ("contents", re.compile(r"відоміст\w*\s+креслень|зміст\b", re.I)),
    ("photo", re.compile(r"фотофіксац", re.I)),
    ("concept", re.compile(r"концептуальн\w+\s+планув", re.I)),
    ("visual", re.compile(r"візуалізац", re.I)),
    ("engineering", re.compile(r"схема\s+(поливу|освітленн|дренаж|водовідвед)|розріз|вузол", re.I)),
    ("spec_table", re.compile(r"відоміст\w*\s+елементів|специфікац|експлікац|техніко-економічн", re.I)),
]

# Page types that carry quantities and therefore justify a vision call.
VISION_WORTHY = {
    "master_plan",
    "layout_plan",
    "dendro_plan",
    "plant_schedule",
    "planting_plan",
    "engineering",
    "spec_table",
    "concept",
    "unknown",
}

NEVER_VISION = {"title", "contents", "marketing", "blank", "visual", "photo"}

MARKETING_MARKERS = re.compile(
    r"унікальніст|комплексніст|швидкіст|з любов|дякуємо, що обрали|обирайте свій комплекс", re.I
)


@dataclass
class ExtractedPage:
    page_number: int
    text: str = ""
    tables: list[list[list[str]]] = field(default_factory=list)
    image_count: int = 0
    width: float = 0.0
    height: float = 0.0
    page_hash: str = ""
    page_type: str = "unknown"
    type_confidence: str = "medium"
    needs_vision: bool = False
    reason: str = ""

    @property
    def text_length(self) -> int:
        return len(self.text.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "text": self.text,
            "text_length": self.text_length,
            "tables": self.tables,
            "image_count": self.image_count,
            "page_hash": self.page_hash,
            "page_type": self.page_type,
            "type_confidence": self.type_confidence,
            "needs_vision": self.needs_vision,
            "reason": self.reason,
        }


@dataclass
class ExtractedDocument:
    path: str
    page_count: int
    content_hash: str
    pages: list[ExtractedPage] = field(default_factory=list)
    kind: str = "unknown"

    @property
    def vision_pages(self) -> list[ExtractedPage]:
        return [p for p in self.pages if p.needs_vision]

    @property
    def text_pages(self) -> list[ExtractedPage]:
        return [p for p in self.pages if p.text_length > 40]


def file_hash(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _clean(text: str) -> str:
    text = text.replace("­", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def classify_page(page: ExtractedPage, index: int, total: int) -> tuple[str, str, str]:
    """Return ``(page_type, confidence, reason)`` for a page.

    Cheap and explainable on purpose: this decides what we spend money on, so
    it should be auditable without a model in the loop.
    """
    text = page.text
    if page.text_length < 5 and page.image_count == 0:
        return "blank", "high", "Порожня сторінка: немає ні тексту, ні зображень."

    if MARKETING_MARKERS.search(text):
        return "marketing", "high", "Маркетинговий текст без технічних даних."

    for ptype, pattern in TYPE_PATTERNS:
        m = pattern.search(text)
        if m:
            return ptype, "high", f"Заголовок сторінки містить «{m.group(0).strip()}»."

    if index == 0 and page.text_length < 200:
        return "title", "medium", "Перша сторінка з коротким заголовком."

    if page.text_length < 60 and page.image_count >= 1:
        return (
            "unknown",
            "low",
            "Растрове креслення без тексту — потрібен візуальний аналіз.",
        )

    if page.text_length > 600:
        return "brief", "medium", "Сторінка з великим обсягом тексту."

    return "unknown", "low", "Тип сторінки не визначено за текстом."


def _extract_tables(page: pymupdf.Page) -> list[list[list[str]]]:
    """Pull vector tables. Their concept PDFs put real schedules here."""
    try:
        finder = page.find_tables()
    except Exception:
        return []
    out: list[list[list[str]]] = []
    for table in getattr(finder, "tables", [])[:6]:
        try:
            rows = table.extract()
        except Exception:
            continue
        cleaned = [
            [(c or "").strip().replace("\n", " ") for c in row]
            for row in rows
            if any((c or "").strip() for c in row)
        ]
        if len(cleaned) >= 2:
            out.append(cleaned)
    return out


def extract_document(path: str | Path, *, with_tables: bool = True) -> ExtractedDocument:
    """Run the cheap pass over a whole PDF."""
    path = Path(path)
    doc = pymupdf.open(path)
    try:
        result = ExtractedDocument(
            path=str(path), page_count=doc.page_count, content_hash=file_hash(path)
        )
        for index, page in enumerate(doc):
            text = _clean(page.get_text("text"))
            extracted = ExtractedPage(
                page_number=index + 1,
                text=text,
                image_count=len(page.get_images(full=True)),
                width=page.rect.width,
                height=page.rect.height,
                page_hash=hashlib.sha256(
                    f"{result.content_hash}:{index}:{text}".encode("utf-8")
                ).hexdigest(),
            )
            if with_tables and extracted.text_length > 40:
                extracted.tables = _extract_tables(page)

            ptype, conf, reason = classify_page(extracted, index, doc.page_count)
            extracted.page_type = ptype
            extracted.type_confidence = conf
            extracted.reason = reason
            extracted.needs_vision = ptype in VISION_WORTHY and ptype not in NEVER_VISION
            result.pages.append(extracted)

        result.kind = _guess_kind(result)
        return result
    finally:
        doc.close()


def _guess_kind(doc: ExtractedDocument) -> str:
    """Label the document so the UI and pipeline can treat it appropriately."""
    text = " ".join(p.text for p in doc.pages[:6]).lower()
    if "комерційна пропозиція" in text and "разом за матеріали" in " ".join(
        p.text.lower() for p in doc.pages
    ):
        return "estimate"
    types = {p.page_type for p in doc.pages}
    if types & {"master_plan", "dendro_plan", "layout_plan", "planting_plan"}:
        return "drawings"
    if "концептуальн" in text:
        return "concept"
    if doc.page_count <= 5 and any(p.text_length > 400 for p in doc.pages):
        return "brief"
    return "unknown"


def render_page(
    path: str | Path,
    page_number: int,
    out_dir: str | Path,
    *,
    dpi: int = 130,
    max_px: int = 1600,
    page_hash: str = "",
) -> Path:
    """Render one page to JPEG for vision, caching by page hash.

    Their drawing sets are 150 MB of full-bleed raster; sending those at native
    resolution would be slow and pointless. ``max_px`` caps the long edge.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = page_hash or f"{Path(path).stem}-{page_number}"
    target = out_dir / f"{stem[:48]}-p{page_number}.jpg"
    if target.exists() and target.stat().st_size > 0:
        return target

    doc = pymupdf.open(path)
    try:
        page = doc[page_number - 1]
        scale = dpi / 72.0
        longest = max(page.rect.width, page.rect.height) * scale
        if longest > max_px:
            scale *= max_px / longest
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        pix.save(target, jpg_quality=82)
        return target
    finally:
        doc.close()


def triage_summary(doc: ExtractedDocument) -> dict[str, Any]:
    """What the pipeline is about to do, and what it is skipping. For the UI."""
    counts: dict[str, int] = {}
    for p in doc.pages:
        counts[p.page_type] = counts.get(p.page_type, 0) + 1
    return {
        "page_count": doc.page_count,
        "kind": doc.kind,
        "by_type": {PAGE_TYPES.get(k, k): v for k, v in sorted(counts.items())},
        "vision_pages": [p.page_number for p in doc.vision_pages],
        "skipped_pages": [
            {"page": p.page_number, "type": PAGE_TYPES.get(p.page_type, p.page_type),
             "reason": p.reason}
            for p in doc.pages
            if not p.needs_vision
        ],
        "text_only_pages": [p.page_number for p in doc.text_pages if not p.needs_vision],
    }
