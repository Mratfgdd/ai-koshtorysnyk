"""PDF proposal, laid out to match the XLSX export.

Built with PyMuPDF's Story engine rather than a new dependency: PyMuPDF is
already required for reading the drawing sets, its built-in base fonts cover
Cyrillic, and Story lays out real HTML/CSS with page breaks, so the tables flow
across pages instead of being positioned by hand.

The one glyph the built-in fonts lack is ₴, so amounts are labelled "грн".

Numbers come from the same draft and totals the XLSX export uses, and sections
are selected by the same helper, so the two documents cannot drift apart. A PDF
has no formulas: every figure is computed here, with the same arithmetic the
spreadsheet's formulas perform.
"""

from __future__ import annotations

import html
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from ..rules.engine import Draft, DraftLine
from ..rules.totals import EstimateTotals
from .xlsx import (
    BAND_BLUE,
    BAND_GREEN,
    BLOCK_ORDER,
    BLOCK_TITLES,
    GREEN,
    LOGO_PATH,
    SUBTOTAL_LABELS,
    ExportMeta,
    _sections_in_order,
)

log = logging.getLogger(__name__)

A4 = pymupdf.paper_rect("A4")
MARGIN = (36, 40, 36, 44)  # left, top, right, bottom, in points


# Thousands are grouped with a non-breaking space (U+00A0), spelled out as a
# constant rather than typed inline: an invisible character in a string
# literal is unreadable in review, and an ordinary space would let a layout
# engine break "12 601,60" across two lines inside its own cell.
NBSP = "\u00a0"


def money(value: float) -> str:
    """1234.5 -> "1 234,50" — the Ukrainian convention, as in the workbook."""
    return f"{value:,.2f}".replace(",", NBSP).replace(".", ",")


def qty(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}".replace(",", NBSP)
    return f"{value:,.3f}".replace(",", NBSP).replace(".", ",").rstrip("0").rstrip(",")


def e(text: object) -> str:
    return html.escape(str(text or ""))


@dataclass
class _Block:
    kind: str
    lines: list[DraftLine]

    @property
    def total(self) -> float:
        return sum(l.quantity * l.unit_price for l in self.lines)


@dataclass
class _Section:
    key: str
    title: str
    blocks: list[_Block] = field(default_factory=list)

    @property
    def label(self) -> str:
        low = self.title.lower()
        return self.title if low.startswith(("підготовчий", "рахунок")) else f"Рахунок {self.title}"

    @property
    def materials(self) -> float:
        return sum(b.total for b in self.blocks if b.kind in ("materials", "plants"))

    @property
    def works(self) -> float:
        return sum(b.total for b in self.blocks if b.kind == "works")

    @property
    def total(self) -> float:
        return self.materials + self.works


def collect_sections(
    draft: Draft, titles: dict[str, str], order: list[str]
) -> list[_Section]:
    """The billable rows, grouped exactly as the spreadsheet groups them."""
    out: list[_Section] = []
    for key in _sections_in_order(draft, order):
        lines = [
            l for l in draft.lines
            if l.section == key and l.quantity > 0 and l.block != "driver"
        ]
        if not lines:
            continue
        section = _Section(key=key, title=titles.get(key, key))
        for kind in sorted({l.block for l in lines}, key=lambda b: BLOCK_ORDER.get(b, 9)):
            section.blocks.append(_Block(kind, [l for l in lines if l.block == kind]))
        out.append(section)
    return out


CSS = f"""
* {{ font-family: sans-serif; }}
body {{ font-size: 9pt; color: #000; }}
h1 {{ font-size: 17pt; text-align: center; margin: 2pt 0 10pt 0; }}
.rule {{ background-color: #{GREEN}; height: 4pt; margin-bottom: 8pt; }}
table {{ width: 100%; border-collapse: collapse; margin: 0; }}
td, th {{ padding: 3pt 4pt; vertical-align: top; }}
th {{ background-color: #{GREEN}; color: #fff; font-size: 8.5pt; text-align: center; }}
td {{ border-bottom: 0.5pt solid #d0d0d0; }}
.meta td {{ border: none; padding: 1.5pt 0; font-size: 9.5pt; }}
/* Bold, not bold-italic as in the workbook: MuPDF embeds a whole font file
   per face, and a third face added ~400 KB to a two-page proposal. */
.meta .k {{ font-weight: bold; }}
.meta .v {{ text-align: right; }}
.marker {{ background-color: #{BAND_BLUE}; font-weight: bold;
           padding: 4pt 5pt; margin-top: 10pt; font-size: 9.5pt; }}
.band {{ background-color: #{BAND_GREEN}; font-weight: bold; text-align: center;
         padding: 5pt; font-size: 11pt; }}
.num {{ text-align: right; }}
.mid {{ text-align: center; }}
.sub td {{ font-weight: bold; border-top: 0.7pt solid #000;
           border-bottom: 0.7pt solid #000; background-color: #fafafa; }}
.strong td {{ font-weight: bold; font-size: 10pt;
              border-top: 1.2pt solid #{GREEN}; border-bottom: 1.2pt solid #{GREEN};
              background-color: #f2f8ef; }}
.foot {{ text-align: center; color: #{GREEN};
         font-size: 9pt; margin-top: 4pt; }}
.sign td {{ border: none; padding-top: 26pt; font-weight: bold; }}
.small {{ font-size: 8pt; color: #666; }}
"""

COLGROUP = (
    '<colgroup>'
    '<col width="5%"><col width="47%"><col width="10%">'
    '<col width="10%"><col width="13%"><col width="15%">'
    "</colgroup>"
)


def _table_head(block_label: str) -> str:
    return (
        f"<table>{COLGROUP}<tr>"
        f"<th>#</th><th>{e(block_label)}</th><th>К-сть</th>"
        f"<th>Од. вим.</th><th>Ціна</th><th>Сума</th></tr>"
    )


def _row(index: int, line: DraftLine) -> str:
    return (
        f"<tr><td class='mid'>{index}</td><td>{e(line.name)}</td>"
        f"<td class='num'>{qty(line.quantity)}</td>"
        f"<td class='mid'>{e(line.unit)}</td>"
        f"<td class='num'>{money(line.unit_price)}</td>"
        f"<td class='num'>{money(line.quantity * line.unit_price)}</td></tr>"
    )


def _subtotal(label: str, value: float, strong: bool = False) -> str:
    cls = "strong" if strong else "sub"
    return (
        f"<tr class='{cls}'><td colspan='5' class='num'>{e(label)}</td>"
        f"<td class='num'>{money(value)}</td></tr>"
    )


def build_html(
    draft: Draft,
    totals: EstimateTotals,
    meta: ExportMeta,
    titles: dict[str, str],
    order: list[str],
) -> str:
    sections = collect_sections(draft, titles, order)

    parts: list[str] = ['<div class="rule"></div>']
    if LOGO_PATH.exists():
        parts.append(
            f'<div style="text-align:right"><img src="{LOGO_PATH.name}" width="62"></div>'
        )
    parts.append(f"<h1>{e(meta.title)}</h1>")

    fields = [
        ("Замовник:", meta.client_name),
        ("Адреса об'єкту:", meta.address),
        ("Дата рахунку:", meta.date),
        ("Пропозиція дійсна протягом:", meta.valid_for),
    ]
    parts.append("<table class='meta'>")
    for key, value in fields:
        parts.append(f"<tr><td class='k'>{e(key)}</td><td class='v'>{e(value)}</td></tr>")
    parts.append("</table>")

    for section in sections:
        parts.append(f"<div class='marker'>{e(section.label)}:</div>")
        parts.append(f"<div class='band'>{e(section.label)}:</div>")
        for block in section.blocks:
            parts.append(_table_head(BLOCK_TITLES.get(block.kind, block.kind)))
            for index, line in enumerate(block.lines, start=1):
                parts.append(_row(index, line))
            parts.append(
                _subtotal(SUBTOTAL_LABELS.get(block.kind, "Разом:"), block.total)
            )
            parts.append("</table>")
        parts.append(f"<table>{COLGROUP}")
        if len([b for b in section.blocks if b.kind in ("materials", "plants")]) > 1:
            parts.append(_subtotal("Разом матеріали та рослини:", section.materials))
        parts.append(_subtotal(f"Разом {section.title}:", section.total, strong=True))
        parts.append("</table>")

    # --- Рахунок загальний ---------------------------------------------------
    materials_total = sum(s.materials for s in sections)
    works_total = sum(s.works for s in sections)
    surcharge = totals.surcharge or 0.0
    grand = materials_total + works_total + surcharge
    advance_works = math.ceil(works_total * 0.3 / 100) * 100 if works_total else 0.0

    parts.append("<div class='marker'>Рахунок загальний:</div>")
    parts.append("<div class='band'>Рахунок загальний:</div>")

    for header, amounts, label in (
        ("Матеріали за видами послуг",
         [(f"Матеріали {s.title}", s.materials) for s in sections if s.materials],
         "Разом за матеріали:"),
        ("Робота за видами послуг",
         [(f"Робота {s.title}", s.works) for s in sections if s.works],
         "Разом за роботу:"),
    ):
        parts.append(_table_head(header))
        for index, (name, amount) in enumerate(amounts, start=1):
            parts.append(
                f"<tr><td class='mid'>{index}</td><td>{e(name)}</td>"
                f"<td class='num'>1</td><td class='mid'>компл.</td>"
                f"<td class='num'>{money(amount)}</td>"
                f"<td class='num'>{money(amount)}</td></tr>"
            )
        parts.append(_subtotal(label, sum(a for _, a in amounts), strong=True))
        parts.append("</table>")

    parts.append(f"<table>{COLGROUP}")
    if surcharge:
        parts.append(_subtotal("Безготівковий розрахунок (надбавка):", surcharge))
    parts.append(_subtotal("Загальний рахунок, грн", grand, strong=True))
    parts.append(_subtotal("Аванс (на матеріали)", materials_total))
    parts.append(_subtotal("Аванс (на роботи)", advance_works))
    parts.append(_subtotal("Залишок", grand - materials_total - advance_works))
    parts.append("</table>")

    parts.append("<div class='foot'>Дякуємо, що обрали послуги нашої компанії.</div>")
    parts.append(
        "<div class='foot'>З любов'ю до вас і ваших рослин, команда Gardener — "
        "озеленимо твій простір!</div>"
    )
    parts.append("<div class='foot'>Вдалого дня!</div>")
    parts.append(
        "<table class='sign'><tr><td>&lt;&lt;Виконавець&gt;&gt;</td>"
        "<td class='num'>&lt;&lt;Замовник&gt;&gt;</td></tr></table>"
    )
    return "<html><body>" + "".join(parts) + "</body></html>"


def export_estimate_pdf(
    path: str | Path,
    draft: Draft,
    totals: EstimateTotals,
    *,
    meta: ExportMeta | None = None,
    section_titles: dict[str, str] | None = None,
    section_order: list[str] | None = None,
) -> Path:
    """Write the proposal as PDF. Returns the path written."""
    meta = meta or ExportMeta()
    titles = section_titles or {}
    order = section_order or sorted({l.section for l in draft.lines})

    body = build_html(draft, totals, meta, titles, order)

    # The archive is what lets <img src="logo.png"> resolve; Story refuses
    # absolute filesystem paths in src for the obvious reason.
    archive = pymupdf.Archive(str(LOGO_PATH.parent)) if LOGO_PATH.exists() else None
    story = pymupdf.Story(html=body, user_css=CSS, archive=archive)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    writer = pymupdf.DocumentWriter(str(path))
    left, top, right, bottom = MARGIN
    frame = A4 + (left, top, -right, -bottom)
    more = True
    guard = 0
    while more:
        guard += 1
        if guard > 200:  # pragma: no cover - a runaway layout must not hang the API
            break
        device = writer.begin_page(A4)
        more, _ = story.place(frame)
        story.draw(device)
        writer.end_page()
    writer.close()

    _subset_fonts(path)
    return path


def _subset_fonts(path: Path) -> None:
    """Keep only the glyphs the proposal uses.

    Story embeds each font whole — a two-page proposal came out at 1.3 MB, of
    which ~1.15 MB was two complete Nimbus Sans faces. Subsetting takes the same
    document to ~143 KB, which matters because this file gets emailed.

    Best-effort: a failure here costs size, not correctness, so the full-size
    document is kept rather than losing the export.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        doc = pymupdf.open(path)
        try:
            doc.subset_fonts()
            doc.save(tmp, garbage=4, deflate=True)
        finally:
            doc.close()
        tmp.replace(path)
    except Exception:  # pragma: no cover - depends on the MuPDF build
        log.warning("font subsetting failed for %s; keeping the full-size PDF", path)
        tmp.unlink(missing_ok=True)
