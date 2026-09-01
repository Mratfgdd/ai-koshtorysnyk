"""Professional XLSX export, styled to the client's own proposal.

The layout, palette and typography are taken from the issued proposal
"КП 2026 (1 Квартал) Максим Скленар ПМ - КП Галина Будьків.pdf":

    banner / table header   #6AA84F   green
    section marker band     #CFE2F3   pale blue
    section title band      #E2EFD9   pale green
    logo                    extracted from that PDF -> app/data/brand/logo.png

Structure per section: a marker band, a title band, then ``Матеріали`` /
``Рослини`` / ``Робота`` tables with the columns ``# | ... | К-сть | Од. вим. |
Ціна | Сума``, each closed by a right-aligned bold-italic subtotal, then the
section total. The document ends with ``Рахунок загальний`` and the payment
schedule.

Sums are written as **live Excel formulas**, not baked values, so the estimator
can change a quantity in the delivered file and watch the totals follow --
which is how the company works today.

Two further sheets carry the audit trail (rule, source, confidence, margin) and
the validation findings, keeping the proposal sheet clean enough to send to a
customer.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from ...config import BACKEND_DIR
from ..rules.engine import Draft, DraftLine
from ..rules.totals import EstimateTotals
from ..validation.validators import ERROR, NEEDS_USER_INPUT, WARNING, ValidationReport

# --- brand -------------------------------------------------------------------

GREEN = "6AA84F"          # banner and table headers
BAND_BLUE = "CFE2F3"      # section marker
BAND_GREEN = "E2EFD9"     # section title
INK = "000000"
GREY = "808080"
RED = "B42318"
AMBER = "B54708"

LOGO_PATH = BACKEND_DIR / "app" / "data" / "brand" / "logo.png"
LOGO_ASPECT = 646 / 601      # native size of the extracted mark
LOGO_HEIGHT_PX = 104
LOGO_ANCHOR = "E4"           # top-right, inside columns E-F
LOGO_ROW_HEIGHT = 15         # points; rows 4-9 give the mark ~120pt of space

FILL_GREEN = PatternFill("solid", fgColor=GREEN)
FILL_BLUE = PatternFill("solid", fgColor=BAND_BLUE)
FILL_BAND = PatternFill("solid", fgColor=BAND_GREEN)
FILL_ERROR = PatternFill("solid", fgColor="FDE8E6")
FILL_WARN = PatternFill("solid", fgColor="FEF3E2")

THIN = Side(style="thin", color="A6A6A6")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

# Accounting format, as in the source proposal: space-grouped, em dash for zero.
MONEY = r'_-* # ##0.00_-;-* # ##0.00_-;_-* "—"_-;_-@_-'
QTY = '# ##0.###;-# ##0.###;"—"'

FONT = "Calibri"

BLOCK_TITLES = {"materials": "Матеріали", "plants": "Рослини", "works": "Робота"}
BLOCK_ORDER = {"plants": 0, "materials": 1, "works": 2}
SUBTOTAL_LABELS = {
    "plants": "Разом за рослини:",
    "works": "Разом за роботу:",
    "materials": "Разом за матеріали:",
}

HEADERS = ["#", "Матеріали", "К-сть", "Од. вим.", "Ціна", "Сума"]
WIDTHS = [5, 58, 10, 12, 12, 16]

# The proposal is portrait A4; content stops at column F.
LAST_COL = 6


@dataclass
class ExportMeta:
    client_name: str = ""
    address: str = ""
    project_name: str = ""
    date: str = ""
    valid_for: str = "3 дні"
    title: str = "Комерційна пропозиція"
    manager: str = ""


def _f(size: int = 11, bold: bool = False, italic: bool = False,
       color: str = INK, underline: str | None = None) -> Font:
    return Font(name=FONT, size=size, bold=bold, italic=italic, color=color, underline=underline)


def export_estimate(
    path: str | Path,
    draft: Draft,
    totals: EstimateTotals,
    *,
    meta: ExportMeta | None = None,
    section_titles: dict[str, str] | None = None,
    section_order: list[str] | None = None,
    report: ValidationReport | None = None,
) -> Path:
    """Write the proposal workbook. Returns the path written."""
    meta = meta or ExportMeta()
    titles = section_titles or {}
    order = section_order or sorted({l.section for l in draft.lines})

    wb = Workbook()
    ws = wb.active
    ws.title = "Кошторис"

    flags = _severity_by_line(report)
    row = _write_header(ws, meta)

    anchors: list[tuple[str, int, int]] = []  # (title, materials_row, works_row)

    for key in _sections_in_order(draft, order):
        lines = [
            l for l in draft.lines
            if l.section == key and l.quantity > 0 and l.block != "driver"
        ]
        if not lines:
            continue
        title = titles.get(key, key)
        row, material_anchor, works_row = _write_section(ws, row, title, lines, flags)
        anchors.append((title, material_anchor, works_row))
        row += 1

    row = _write_summary(ws, row, anchors, totals)
    _write_footer(ws, row + 1)

    _finish_sheet(ws)
    _write_audit_sheet(wb, draft, report)
    if report and report.findings:
        _write_issue_sheet(wb, report)
    _write_totals_sheet(wb, totals)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _sections_in_order(draft: Draft, order: list[str]) -> list[str]:
    present = {l.section for l in draft.lines if l.quantity > 0 and l.block != "driver"}
    ordered = [s for s in order if s in present]
    ordered.extend(sorted(present - set(ordered)))
    return ordered


# --- header ------------------------------------------------------------------


def _write_header(ws: Worksheet, meta: ExportMeta) -> int:
    """Green banner, logo, title and the customer block."""
    for r in (1, 2, 3):
        for c in range(1, 4):
            ws.cell(r, c).fill = FILL_GREEN
        ws.row_dimensions[r].height = 13
    ws.merge_cells(start_row=1, start_column=1, end_row=3, end_column=3)

    # The logo block: rows 4-9 reserved, mark anchored top-right so it sits
    # inside columns E-F and never overlaps the title or the customer block.
    for r in range(4, 10):
        ws.row_dimensions[r].height = LOGO_ROW_HEIGHT

    if LOGO_PATH.exists():
        try:
            from openpyxl.drawing.image import Image as XLImage

            logo = XLImage(str(LOGO_PATH))
            logo.height = LOGO_HEIGHT_PX
            logo.width = round(LOGO_HEIGHT_PX * LOGO_ASPECT)
            ws.add_image(logo, LOGO_ANCHOR)
        except Exception:  # pragma: no cover - Pillow missing or image unreadable
            cell = ws.cell(5, 5, "GARDENER")
            cell.font = _f(14, bold=True, color=GREEN)
            cell.alignment = Alignment(horizontal="right", vertical="center")

    ws.row_dimensions[10].height = 8

    ws.merge_cells(start_row=11, start_column=1, end_row=11, end_column=LAST_COL)
    title = ws.cell(11, 1, meta.title)
    title.font = _f(16, bold=True)
    title.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[11].height = 26

    # Exactly the four blocks the company's own proposal carries.
    fields = [
        ("Замовник:", meta.client_name),
        ("Адреса об'єкту:", meta.address),
        ("Дата рахунку:", meta.date or dt.date.today().strftime("%d.%m.%Y")),
        ("Пропозиція дійсна протягом:", meta.valid_for),
    ]

    r = 12
    for label, value in fields:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        cell = ws.cell(r, 1, label)
        cell.font = _f(11, bold=True, italic=True, underline="single")
        cell.alignment = Alignment(horizontal="left", vertical="center")

        ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=LAST_COL)
        val = ws.cell(r, 4, value)
        val.font = _f(11, italic=True)
        val.alignment = Alignment(horizontal="right", vertical="center")
        ws.row_dimensions[r].height = 18
        r += 1
    return r + 1


# --- sections ----------------------------------------------------------------


def _write_section(
    ws: Worksheet,
    row: int,
    title: str,
    lines: list[DraftLine],
    flags: dict[str, str],
) -> tuple[int, int, int]:
    label = title if title.lower().startswith(("підготовчий", "рахунок")) else f"Рахунок {title}"

    # Pale blue marker band, left portion only -- as in the source proposal.
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
    marker = ws.cell(row, 1, f"{label}:")
    marker.font = _f(11, bold=True, italic=True)
    marker.alignment = Alignment(vertical="center")
    for c in range(1, 4):
        ws.cell(row, c).fill = FILL_BLUE
    ws.row_dimensions[row].height = 20
    row += 1

    # Pale green title band across the whole table width.
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=LAST_COL)
    banner = ws.cell(row, 1, f"{label}:")
    banner.font = _f(13, bold=True)
    banner.alignment = Alignment(horizontal="center", vertical="center")
    for c in range(1, LAST_COL + 1):
        ws.cell(row, c).fill = FILL_BAND
    ws.row_dimensions[row].height = 22
    row += 1

    subtotals: dict[str, int] = {}
    for block in sorted({l.block for l in lines}, key=lambda b: BLOCK_ORDER.get(b, 9)):
        block_lines = [l for l in lines if l.block == block]
        if not block_lines:
            continue
        row = _write_table_header(ws, row, BLOCK_TITLES.get(block, block))
        first = row
        for index, line in enumerate(block_lines, start=1):
            _write_line(ws, row, index, line, flags)
            row += 1
        last = row - 1
        row = _write_subtotal(ws, row, SUBTOTAL_LABELS.get(block, "Разом:"),
                              f"SUM(F{first}:F{last})")
        subtotals[block] = row - 1
        _group(ws, first, last)

    plants_row = subtotals.get("plants", 0)
    materials_row = subtotals.get("materials", 0)
    works_row = subtotals.get("works", 0)

    if plants_row and materials_row:
        row = _write_subtotal(ws, row, "Разом матеріали та рослини:",
                              f"F{plants_row}+F{materials_row}")
        material_anchor = row - 1
    else:
        material_anchor = materials_row or plants_row

    parts = [f"F{r}" for r in (material_anchor, works_row) if r]
    row = _write_subtotal(ws, row, f"Разом {title}:", "+".join(parts) or "0", strong=True)
    return row, material_anchor, works_row


def _write_table_header(ws: Worksheet, row: int, block_label: str) -> int:
    headers = list(HEADERS)
    headers[1] = block_label
    for col, text in enumerate(headers, start=1):
        cell = ws.cell(row, col, text)
        cell.font = _f(11, bold=True, color="FFFFFF")
        cell.fill = FILL_GREEN
        cell.border = BOX
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 19
    return row + 1


def _write_line(ws: Worksheet, row: int, index: int, line: DraftLine,
                flags: dict[str, str]) -> None:
    ws.cell(row, 1, index).alignment = Alignment(horizontal="center", vertical="center")
    name = ws.cell(row, 2, line.name)
    name.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.cell(row, 3, line.quantity).alignment = Alignment(horizontal="center", vertical="center")
    ws.cell(row, 4, line.unit).alignment = Alignment(horizontal="center", vertical="center")
    ws.cell(row, 5, line.unit_price).alignment = Alignment(horizontal="center", vertical="center")
    total = ws.cell(row, 6)
    total.value = f"=C{row}*E{row}"  # live formula, as in the company's own workbook
    total.alignment = Alignment(horizontal="right", vertical="center")

    ws.cell(row, 3).number_format = QTY
    ws.cell(row, 5).number_format = QTY
    total.number_format = MONEY

    for col in range(1, LAST_COL + 1):
        cell = ws.cell(row, col)
        cell.border = BOX
        if not cell.font.bold:
            cell.font = _f(11)
    ws.row_dimensions[row].height = 17

    severity = flags.get(line.name)
    if severity == ERROR:
        for col in range(1, LAST_COL + 1):
            ws.cell(row, col).fill = FILL_ERROR
        name.font = _f(11, color=RED)
    elif severity in (WARNING, NEEDS_USER_INPUT):
        for col in range(1, LAST_COL + 1):
            ws.cell(row, col).fill = FILL_WARN
        name.font = _f(11, color=AMBER)

    note = line.qty_trace or (line.reasons[0] if line.reasons else "")
    if note:
        name.comment = _comment(note)


def _comment(text: str):
    from openpyxl.comments import Comment

    c = Comment(text[:1200], "AI Кошторисник")
    c.width, c.height = 380, 140
    return c


def _write_subtotal(ws: Worksheet, row: int, label: str, formula: str,
                    strong: bool = False) -> int:
    """Right-aligned bold-italic label with its value, as in the source proposal."""
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    cell = ws.cell(row, 1, label)
    cell.font = _f(12 if strong else 11, bold=True, italic=True)
    cell.alignment = Alignment(horizontal="right", vertical="center")

    value = ws.cell(row, 6)
    value.value = f"={formula}"
    value.number_format = MONEY
    value.font = _f(12 if strong else 11, bold=strong, italic=not strong)
    value.alignment = Alignment(horizontal="right", vertical="center")
    ws.row_dimensions[row].height = 19
    return row + 1


def _group(ws: Worksheet, first: int, last: int) -> None:
    for r in range(first, last + 1):
        ws.row_dimensions[r].outlineLevel = 1


# --- summary -----------------------------------------------------------------


def _write_summary(
    ws: Worksheet, row: int, anchors: list[tuple[str, int, int]], totals: EstimateTotals
) -> int:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
    marker = ws.cell(row, 1, "Рахунок загальний:")
    marker.font = _f(11, bold=True, italic=True)
    for c in range(1, 4):
        ws.cell(row, c).fill = FILL_BLUE
    ws.row_dimensions[row].height = 20
    row += 1

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=LAST_COL)
    banner = ws.cell(row, 1, "Рахунок загальний:")
    banner.font = _f(13, bold=True)
    banner.alignment = Alignment(horizontal="center", vertical="center")
    for c in range(1, LAST_COL + 1):
        ws.cell(row, c).fill = FILL_BAND
    ws.row_dimensions[row].height = 22
    row += 1

    row, materials_total_row = _write_rollup(
        ws, row, "Матеріали за видами послуг",
        [(f"Матеріали {t}", r) for t, r, _ in anchors if r],
        "Разом за матеріали:",
    )
    row, works_total_row = _write_rollup(
        ws, row, "Робота за видами послуг",
        [(f"Робота {t}", r) for t, _, r in anchors if r],
        "Разом за роботу:",
    )
    row += 1

    if totals.surcharge:
        row = _write_subtotal(ws, row, "Безготівковий розрахунок (надбавка):",
                              str(totals.surcharge))

    grand = f"F{materials_total_row}+F{works_total_row}" + (
        f"+{totals.surcharge}" if totals.surcharge else ""
    )
    grand_row = row
    row = _write_subtotal(ws, row, "Загальний рахунок", grand, strong=True)
    row = _write_subtotal(ws, row, "Аванс (на матеріали)", f"F{materials_total_row}")
    advance_works_row = row
    row = _write_subtotal(ws, row, "Аванс (на роботи)",
                          f"CEILING(F{works_total_row}*0.3,100)")
    row = _write_subtotal(ws, row, "Залишок",
                          f"F{grand_row}-F{materials_total_row}-F{advance_works_row}")
    return row


def _write_rollup(
    ws: Worksheet, row: int, header: str, entries: list[tuple[str, int]], total_label: str
) -> tuple[int, int]:
    row = _write_table_header(ws, row, header)
    first = row
    for index, (label, source_row) in enumerate(entries, start=1):
        ws.cell(row, 1, index).alignment = Alignment(horizontal="center")
        ws.cell(row, 2, label).alignment = Alignment(horizontal="left", vertical="center")
        value = ws.cell(row, 6)
        value.value = f"=F{source_row}"
        value.number_format = MONEY
        value.alignment = Alignment(horizontal="right")
        for col in range(1, LAST_COL + 1):
            ws.cell(row, col).border = BOX
            if not ws.cell(row, col).font.bold:
                ws.cell(row, col).font = _f(11)
        row += 1
    last = row - 1
    total_row = row
    row = _write_subtotal(ws, row, total_label,
                          f"SUM(F{first}:F{last})" if entries else "0", strong=True)
    return row, total_row


def _write_footer(ws: Worksheet, row: int) -> None:
    for text in (
        "Дякуємо, що обрали послуги нашої компанії.",
        "З любов'ю до вас і ваших рослин, команда Gardener — озеленимо твій простір!",
        "Вдалого дня!",
    ):
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=LAST_COL)
        cell = ws.cell(row, 1, text)
        cell.font = _f(11, italic=True, color=GREEN)
        cell.alignment = Alignment(horizontal="center")
        row += 1

    row += 2
    ws.cell(row, 2, "<<Виконавець>>").font = _f(11, bold=True)
    ws.cell(row, 5, "<<Замовник>>").font = _f(11, bold=True)


def _finish_sheet(ws: Worksheet) -> None:
    for col, width in enumerate(WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width

    # No frozen panes and no repeating print titles on the proposal sheet.
    # Both pinned the 17-row letterhead: on screen it hung over the tables,
    # and on paper it reprinted on every page, breaking the row flow. The
    # document is meant to read as one continuous letter.
    ws.freeze_panes = None
    ws.print_title_rows = None

    ws.sheet_view.showGridLines = True
    ws.sheet_properties.outlinePr.summaryBelow = True
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = ws.page_margins.right = 0.4


# --- audit sheet -------------------------------------------------------------

AUDIT_HEADERS = [
    "Секція", "Блок", "Найменування", "Од. вим.", "К-сть", "Ціна", "Сума",
    "Собівартість", "Маржа, грн", "Джерело кількості", "Правило", "Обґрунтування",
    "Статус зіставлення", "Впевненість", "Джерела", "Коментар",
]
AUDIT_WIDTHS = [16, 12, 52, 10, 10, 12, 14, 13, 12, 18, 46, 60, 18, 13, 40, 30]


def _write_audit_sheet(wb: Workbook, draft: Draft, report: ValidationReport | None) -> None:
    ws = wb.create_sheet("Обґрунтування")
    for col, text in enumerate(AUDIT_HEADERS, start=1):
        cell = ws.cell(1, col, text)
        cell.font = _f(11, bold=True, color="FFFFFF")
        cell.fill = FILL_GREEN
        cell.alignment = Alignment(horizontal="center", wrap_text=True, vertical="center")

    flags = _severity_by_line(report)
    row = 2
    for line in draft.lines:
        if line.quantity <= 0 or line.block == "driver":
            continue
        values = [
            line.section,
            BLOCK_TITLES.get(line.block, line.block),
            line.name,
            line.unit,
            line.quantity,
            line.unit_price,
            line.total,
            line.unit_cost,
            round((line.unit_price - line.unit_cost) * line.quantity, 2),
            line.qty_source,
            line.qty_expr or "",
            line.qty_trace or "",
            line.match_status,
            line.confidence,
            "; ".join(
                f"{r.get('source_type', '')}: {r.get('source_ref', '')}" for r in line.source_refs
            ),
            "; ".join(line.reasons),
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row, col, value)
            cell.font = _f(10)
            cell.alignment = Alignment(wrap_text=col in (3, 11, 12, 15, 16), vertical="top")
        ws.cell(row, 5).number_format = QTY
        for col in (6, 7, 8, 9):
            ws.cell(row, col).number_format = MONEY

        severity = flags.get(line.name)
        fill = FILL_ERROR if severity == ERROR else (
            FILL_WARN if severity in (WARNING, NEEDS_USER_INPUT) else None
        )
        if fill:
            for col in range(1, len(AUDIT_HEADERS) + 1):
                ws.cell(row, col).fill = fill
        row += 1

    for col, width in enumerate(AUDIT_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "D2"  # a working grid, unlike the proposal sheet
    ws.sheet_view.showGridLines = True
    ws.auto_filter.ref = f"A1:{get_column_letter(len(AUDIT_HEADERS))}{max(row - 1, 1)}"


def _write_issue_sheet(wb: Workbook, report: ValidationReport) -> None:
    ws = wb.create_sheet("Перевірка")
    headers = ["Статус", "Код", "Проблема", "Чому", "Що зробити", "Вплив", "Позиція", "Секція"]
    widths = [18, 22, 50, 55, 50, 42, 44, 16]
    for col, text in enumerate(headers, start=1):
        cell = ws.cell(1, col, text)
        cell.font = _f(11, bold=True, color="FFFFFF")
        cell.fill = FILL_GREEN
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    for row, finding in enumerate(report.sorted(), start=2):
        values = [
            finding.severity, finding.code, finding.title, finding.detail,
            finding.fix_hint, finding.impact, finding.line_name or "", finding.section or "",
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row, col, value)
            cell.font = _f(10)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        fill = FILL_ERROR if finding.severity == ERROR else (
            FILL_WARN if finding.severity in (WARNING, NEEDS_USER_INPUT) else None
        )
        if fill:
            for col in range(1, len(headers) + 1):
                ws.cell(row, col).fill = fill

    for col, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = True
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(report.findings) + 1}"


def _write_totals_sheet(wb: Workbook, totals: EstimateTotals) -> None:
    ws = wb.create_sheet("Підсумки")
    for col, text in enumerate(("Показник", "Сума", "Формула"), start=1):
        cell = ws.cell(1, col, text)
        cell.font = _f(11, bold=True, color="FFFFFF")
        cell.fill = FILL_GREEN

    rows = [
        ("Разом за матеріали", totals.materials_total, totals.formulas.get("materials_total", "")),
        ("Разом за роботу", totals.works_total, totals.formulas.get("works_total", "")),
        ("Підсумок", totals.subtotal, totals.formulas.get("subtotal", "")),
        ("Надбавка", totals.surcharge, totals.formulas.get("surcharge", "")),
        ("Загальний рахунок", totals.grand_total, totals.formulas.get("grand_total", "")),
        ("Аванс (матеріали)", totals.prepayment_materials,
         totals.formulas.get("prepayment_materials", "")),
        ("Аванс (роботи)", totals.prepayment_works, totals.formulas.get("prepayment_works", "")),
        ("Залишок", totals.balance, totals.formulas.get("balance", "")),
        ("Собівартість", totals.cost_total, "Σ (собівартість × к-сть)"),
        ("Маржа", totals.margin, "Підсумок − собівартість"),
        ("Маржинальність", totals.margin_pct, "Маржа / підсумок"),
    ]
    for row, (label, value, formula) in enumerate(rows, start=2):
        strong = label == "Загальний рахунок"
        ws.cell(row, 1, label).font = _f(11, bold=strong)
        cell = ws.cell(row, 2, value)
        cell.number_format = "0.00%" if label == "Маржинальність" else MONEY
        cell.font = _f(11, bold=strong)
        note = ws.cell(row, 3, formula)
        note.font = _f(10, color=GREY)
        note.alignment = Alignment(wrap_text=True)

    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 70
    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = True


def _severity_by_line(report: ValidationReport | None) -> dict[str, str]:
    if not report:
        return {}
    out: dict[str, str] = {}
    for finding in report.findings:
        if not finding.line_name:
            continue
        current = out.get(finding.line_name)
        if current is None or _rank(finding.severity) < _rank(current):
            out[finding.line_name] = finding.severity
    return out


def _rank(severity: str) -> int:
    return {ERROR: 0, NEEDS_USER_INPUT: 1, WARNING: 2}.get(severity, 3)
