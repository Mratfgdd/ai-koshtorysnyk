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
import math
import re
import xml.etree.ElementTree as ET
import zipfile
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

# Rules that close a "Разом …" row: a plain one for block subtotals, a heavier
# one for a section total and the final invoice figures.
RULE = Side(style="thin", color=INK)
RULE_STRONG = Side(style="double", color=GREEN)

# Number formats.
#
# The grouping separator in a format code is the comma token `,` — Excel and
# LibreOffice render it in the viewer's locale (a space in uk-UA). The previous
# codes grouped with a *literal* space (`# ##0.00`), which is not the grouping
# token: the leading `#` matched nothing, the literal space was emitted anyway,
# and small numbers came out as " 5" or "5," depending on the renderer.
#
# The em dash for an exact zero is kept: it is the convention in the client's
# own issued proposal. It was never the cause of the blank totals — those were
# formulas with no cached result, which is fixed in _inject_cached_values.
MONEY = '#,##0.00;-#,##0.00;"—"'
# Currency is spelled out on the summary lines only; repeating ₴ in every cell
# of a 70-row proposal is noise, and it is what makes columns overflow to ###.
MONEY_STRONG = '#,##0.00\\ "₴";-#,##0.00\\ "₴";"—"'
QTY = '#,##0.###;-#,##0.###;"—"'

FONT = "Calibri"

BLOCK_TITLES = {"materials": "Матеріали", "plants": "Рослини", "works": "Робота"}
BLOCK_ORDER = {"plants": 0, "materials": 1, "works": 2}
SUBTOTAL_LABELS = {
    "plants": "Разом за рослини:",
    "works": "Разом за роботу:",
    "materials": "Разом за матеріали:",
}

HEADERS = ["#", "Матеріали", "К-сть", "Од. вим.", "Ціна", "Сума"]
# Floor and ceiling for the auto-fit. Column B wraps, so it is pinned: letting
# it grow to the longest article name would push Ціна and Сума off the page.
WIDTHS = [5, 58, 10, 12, 12, 16]
MAX_WIDTHS = [6, 58, 14, 14, 20, 22]

# The proposal is portrait A4; content stops at column F.
LAST_COL = 6


class _Formulas:
    """Every formula written to the workbook, with the number it evaluates to.

    openpyxl has no formula engine: it writes ``<f>SUM(F20:F22)</f><v/>`` — a
    formula whose *cached result is empty*. Excel recalculates on load and shows
    the right number, which is why this went unnoticed. Nothing else does:
    LibreOffice and Google Sheets honour the empty cache, a PDF or thumbnail
    preview shows blanks, and ``load_workbook(data_only=True)`` — the obvious
    way to verify the file — returns ``None`` for every sum in the document.

    So each formula is recorded here with the value it evaluates to, and the
    cache is filled in after the workbook is saved. The formulas stay live, so
    changing a quantity in the delivered file still updates the totals.
    """

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], float] = {}

    def write(
        self,
        ws: Worksheet,
        row: int,
        col: int,
        formula: str,
        value: float,
        number_format: str = MONEY,
    ):
        cell = ws.cell(row, col)
        cell.value = f"={formula}"
        cell.number_format = number_format
        self.values[(ws.title, cell.coordinate)] = float(value)
        return cell

    def at(self, ws: Worksheet, row: int, col: int = LAST_COL) -> float:
        """What the formula already written in that cell evaluates to.

        The summary rolls up rows the section writer produced, so it reads the
        amounts back from here instead of recomputing them from the draft and
        risking a total that disagrees with the rows above it.
        """
        return self.values.get((ws.title, f"{get_column_letter(col)}{row}"), 0.0)


# openpyxl writes a formula cell as `<f>…</f>` followed by an empty `<v/>`
# (or `<v></v>`); anything else is left alone.
#
# `[^>/]` in the attributes, not `[^>]`, so an empty self-closing cell —
# `<c r="B22" s="17"/>`, which openpyxl emits for the placeholders inside a
# merged range — cannot match. With `[^>]` the `/` was swallowed as an
# attribute, the body then ran on to the *next* cell's `</c>`, and the sum in
# column F of every merged "Разом …" row was hidden inside that match and never
# patched. Cell attributes are only r/s/t, so none of them contains a slash.
_CELL_RE = re.compile(rb'<c r="(?P<ref>[A-Z]+[0-9]+)"(?P<attrs>[^>/]*)>(?P<body>.*?)</c>', re.S)
_EMPTY_V_RE = re.compile(rb"<v\s*/>|<v></v>")


def _num(value: float) -> bytes:
    """Excel stores raw numbers; trim float noise without changing the value."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return (text or "0").encode("ascii")


def _sheet_files(zf: zipfile.ZipFile) -> dict[str, str]:
    """Map worksheet name -> its XML path, via the workbook relationships."""
    ns = {
        "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    rels = {
        rel.get("Id"): rel.get("Target")
        for rel in ET.fromstring(zf.read("xl/_rels/workbook.xml.rels")).findall("pr:Relationship", ns)
    }
    out: dict[str, str] = {}
    for sheet in ET.fromstring(zf.read("xl/workbook.xml")).findall("m:sheets/m:sheet", ns):
        target = rels.get(sheet.get(f"{{{ns['r']}}}id"), "")
        if target:
            out[sheet.get("name", "")] = "xl/" + target.lstrip("/").removeprefix("xl/")
    return out


def _inject_cached_values(path: Path, fx: _Formulas) -> None:
    """Fill in the cached result of every formula openpyxl left empty."""
    if not fx.values:
        return

    with zipfile.ZipFile(path) as zf:
        members = zf.infolist()
        content = {m.filename: zf.read(m.filename) for m in members}
        sheet_files = _sheet_files(zf)

    for title, cell_values in _by_sheet(fx.values).items():
        name = sheet_files.get(title)
        if name is None or name not in content:  # pragma: no cover - defensive
            continue

        def patch(match: "re.Match[bytes]") -> bytes:
            ref = match.group("ref").decode("ascii")
            value = cell_values.get(ref)
            body = match.group("body")
            # Only numeric formula cells; never touch a shared-string cell.
            if value is None or b"<f" not in body or b't="s"' in match.group("attrs"):
                return match.group(0)
            cached = b"<v>" + _num(value) + b"</v>"
            body = _EMPTY_V_RE.sub(cached, body) if _EMPTY_V_RE.search(body) else body + cached
            return (
                b'<c r="' + match.group("ref") + b'"' + match.group("attrs") + b">"
                + body
                + b"</c>"
            )

        content[name] = _CELL_RE.sub(patch, content[name])

    # Rewrite the archive: zipfile cannot replace a member in place.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for member in members:
            out.writestr(member, content[member.filename])


def _by_sheet(values: dict[tuple[str, str], float]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for (title, ref), value in values.items():
        out.setdefault(title, {})[ref] = value
    return out


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
    fx = _Formulas()
    row = _write_header(ws, meta)

    # (title, row carrying the materials subtotal, row carrying the works one)
    anchors: list[tuple[str, int, int]] = []

    for key in _sections_in_order(draft, order):
        lines = [
            l for l in draft.lines
            if l.section == key and l.quantity > 0 and l.block != "driver"
        ]
        if not lines:
            continue
        title = titles.get(key, key)
        row, material_anchor, works_row = _write_section(ws, row, title, lines, flags, fx)
        anchors.append((title, material_anchor, works_row))
        row += 1

    row = _write_summary(ws, row, anchors, totals, fx)
    _write_footer(ws, row + 1)

    _finish_sheet(ws, fx)
    _write_audit_sheet(wb, draft, report)
    if report and report.findings:
        _write_issue_sheet(wb, report)
    _write_totals_sheet(wb, totals)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    # Must come after save(): openpyxl rewrites the whole archive.
    _inject_cached_values(path, fx)
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
    fx: _Formulas,
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
    amounts: dict[str, float] = {}
    for block in sorted({l.block for l in lines}, key=lambda b: BLOCK_ORDER.get(b, 9)):
        block_lines = [l for l in lines if l.block == block]
        if not block_lines:
            continue
        row = _write_table_header(ws, row, BLOCK_TITLES.get(block, block))
        first = row
        for index, line in enumerate(block_lines, start=1):
            _write_line(ws, row, index, line, flags, fx)
            row += 1
        last = row - 1
        amounts[block] = sum(l.quantity * l.unit_price for l in block_lines)
        row = _write_subtotal(ws, row, SUBTOTAL_LABELS.get(block, "Разом:"),
                              f"SUM(F{first}:F{last})", amounts[block], fx)
        subtotals[block] = row - 1
        _group(ws, first, last)

    plants_row = subtotals.get("plants", 0)
    materials_row = subtotals.get("materials", 0)
    works_row = subtotals.get("works", 0)

    if plants_row and materials_row:
        row = _write_subtotal(
            ws, row, "Разом матеріали та рослини:",
            f"F{plants_row}+F{materials_row}",
            amounts.get("plants", 0.0) + amounts.get("materials", 0.0), fx,
        )
        material_anchor = row - 1
        material_amount = amounts.get("plants", 0.0) + amounts.get("materials", 0.0)
    else:
        material_anchor = materials_row or plants_row
        material_amount = amounts.get("materials", 0.0) or amounts.get("plants", 0.0)

    parts = [f"F{r}" for r in (material_anchor, works_row) if r]
    section_amount = (material_amount if material_anchor else 0.0) + (
        amounts.get("works", 0.0) if works_row else 0.0
    )
    row = _write_subtotal(ws, row, f"Разом {title}:", "+".join(parts) or "0",
                          section_amount, fx, strong=True)
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
                flags: dict[str, str], fx: _Formulas) -> None:
    ws.cell(row, 1, index).alignment = Alignment(horizontal="center", vertical="center")
    name = ws.cell(row, 2, line.name)
    name.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.cell(row, 3, line.quantity).alignment = Alignment(horizontal="right", vertical="center")
    ws.cell(row, 4, line.unit).alignment = Alignment(horizontal="center", vertical="center")
    ws.cell(row, 5, line.unit_price).alignment = Alignment(horizontal="right", vertical="center")

    # Live formula, as in the company's own workbook, carrying the value it
    # evaluates to so the cell is never blank outside Excel.
    total = fx.write(ws, row, 6, f"C{row}*E{row}", line.quantity * line.unit_price)
    total.alignment = Alignment(horizontal="right", vertical="center")

    ws.cell(row, 3).number_format = QTY
    ws.cell(row, 5).number_format = MONEY  # a price is money, not a count

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


def _write_subtotal(ws: Worksheet, row: int, label: str, formula: str, value: float,
                    fx: _Formulas, strong: bool = False) -> int:
    """Right-aligned bold label with its value, as in the source proposal.

    Every "Разом …" row is ruled off above and below so the eye stops there;
    without a border these lines were indistinguishable from a data row.
    """
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    cell = ws.cell(row, 1, label)
    cell.font = _f(12 if strong else 11, bold=True, italic=not strong)
    cell.alignment = Alignment(horizontal="right", vertical="center")

    total = fx.write(ws, row, 6, formula, value, MONEY_STRONG if strong else MONEY)
    total.font = _f(12 if strong else 11, bold=True, italic=not strong)
    total.alignment = Alignment(horizontal="right", vertical="center")

    rule = RULE_STRONG if strong else RULE
    for col in range(1, LAST_COL + 1):
        ws.cell(row, col).border = Border(top=rule, bottom=rule)
    ws.row_dimensions[row].height = 19
    return row + 1


def _group(ws: Worksheet, first: int, last: int) -> None:
    for r in range(first, last + 1):
        ws.row_dimensions[r].outlineLevel = 1


# --- summary -----------------------------------------------------------------


def _write_summary(
    ws: Worksheet, row: int, anchors: list[tuple[str, int, int]], totals: EstimateTotals,
    fx: _Formulas,
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

    row, materials_total_row, materials_amount = _write_rollup(
        ws, row, "Матеріали за видами послуг",
        [(f"Матеріали {t}", r) for t, r, _ in anchors if r],
        "Разом за матеріали:", fx,
    )
    row, works_total_row, works_amount = _write_rollup(
        ws, row, "Робота за видами послуг",
        [(f"Робота {t}", r) for t, _, r in anchors if r],
        "Разом за роботу:", fx,
    )
    row += 1

    if totals.surcharge:
        row = _write_subtotal(ws, row, "Безготівковий розрахунок (надбавка):",
                              str(totals.surcharge), totals.surcharge, fx)

    grand = f"F{materials_total_row}+F{works_total_row}" + (
        f"+{totals.surcharge}" if totals.surcharge else ""
    )
    grand_amount = materials_amount + works_amount + (totals.surcharge or 0.0)
    grand_row = row
    row = _write_subtotal(ws, row, "Загальний рахунок", grand, grand_amount, fx, strong=True)
    row = _write_subtotal(ws, row, "Аванс (на матеріали)",
                          f"F{materials_total_row}", materials_amount, fx)
    advance_works_row = row
    # CEILING(x, 100) in Excel — round the works deposit up to a whole hundred.
    advance_works = math.ceil(works_amount * 0.3 / 100) * 100 if works_amount else 0.0
    row = _write_subtotal(ws, row, "Аванс (на роботи)",
                          f"CEILING(F{works_total_row}*0.3,100)", advance_works, fx)
    row = _write_subtotal(ws, row, "Залишок",
                          f"F{grand_row}-F{materials_total_row}-F{advance_works_row}",
                          grand_amount - materials_amount - advance_works, fx)
    return row


def _write_rollup(
    ws: Worksheet, row: int, header: str, entries: list[tuple[str, int]], total_label: str,
    fx: _Formulas,
) -> tuple[int, int, float]:
    """One line per section, carrying that section's subtotal up to the invoice.

    Every column of the table is filled: leaving К-сть, Од. вим. and Ціна blank
    under a header that announces them read as missing data rather than as a
    roll-up.
    """
    row = _write_table_header(ws, row, header)
    first = row
    for index, (label, source_row) in enumerate(entries, start=1):
        amount = fx.at(ws, source_row)
        ws.cell(row, 1, index).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(row, 2, label).alignment = Alignment(
            horizontal="left", vertical="center", wrap_text=True
        )
        qty = ws.cell(row, 3, 1)
        qty.number_format = QTY
        qty.alignment = Alignment(horizontal="right", vertical="center")
        ws.cell(row, 4, "компл.").alignment = Alignment(horizontal="center", vertical="center")

        price = fx.write(ws, row, 5, f"F{source_row}", amount)
        price.alignment = Alignment(horizontal="right", vertical="center")
        value = fx.write(ws, row, 6, f"C{row}*E{row}", amount)
        value.alignment = Alignment(horizontal="right", vertical="center")

        for col in range(1, LAST_COL + 1):
            cell = ws.cell(row, col)
            cell.border = BOX
            if not cell.font.bold:
                cell.font = _f(11)
        row += 1
    last = row - 1
    total_row = row
    amount = sum(fx.at(ws, r) for _, r in entries)
    row = _write_subtotal(ws, row, total_label,
                          f"SUM(F{first}:F{last})" if entries else "0",
                          amount, fx, strong=True)
    return row, total_row, amount


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


def _autofit(ws: Worksheet, fx: _Formulas) -> None:
    """Widen each column to its content, within the floor/ceiling above.

    A money column narrower than its longest amount renders as ``###`` in Excel,
    which is how a correct total still reads as a broken one. Formula cells hold
    ``=SUM(...)``, not a number, so their width is measured from the value the
    formula evaluates to.
    """
    merged = {
        cell
        for rng in ws.merged_cells.ranges
        for cell in rng.cells
    }

    for col in range(1, LAST_COL + 1):
        widest = len(HEADERS[col - 1])
        for row in range(1, ws.max_row + 1):
            # A merged label spans several columns; measuring it here would
            # blow out the first one.
            if (row, col) in merged:
                continue
            value = ws.cell(row, col).value
            if value is None or value == "":
                continue
            if isinstance(value, str) and value.startswith("="):
                number = fx.at(ws, row, col)
                text = f"{number:,.2f}".replace(",", " ")
            elif isinstance(value, (int, float)):
                text = f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".")
            else:
                text = str(value)
            widest = max(widest, len(text))
        floor, ceiling = WIDTHS[col - 1], MAX_WIDTHS[col - 1]
        ws.column_dimensions[get_column_letter(col)].width = min(
            max(widest + 2, floor), ceiling
        )


def _finish_sheet(ws: Worksheet, fx: _Formulas) -> None:
    _autofit(ws, fx)

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
