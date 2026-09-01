"""Compile the client's estimate template (``Шаблон для ШІ.xlsx``) into a
declarative rule pack.

The template sheet ``Шаблон основний!!!! 1`` is the single source of truth for:

* the section layout (which sections exist, in what order, and which rows hold
  materials vs. works vs. subtotals);
* ~180 derived-quantity formulas in column ``D`` -- these encode the company's
  real coefficients (sand volume per metre of trench, 20% geotextile overlap,
  valve-box size buckets, planting priced as a percentage of plant value, ...).

We translate those Excel formulas into a small, safe expression DSL instead of
transcribing them by hand, so that re-running this compiler after the client
updates their workbook keeps the engine in sync.

Formulas we cannot translate faithfully are **not** guessed: they are emitted
with ``status: needs_review`` and the original Excel source attached, so a human
can decide. That is the "не вигадуй" rule applied to rule extraction itself.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import openpyxl

from .formula_parser import RefResolver, Untranslatable, translate

# --- Template geometry -------------------------------------------------------
# Column meanings on the estimate sheet, read off the header rows (21, 46, ...).
COL_FLAG = 1  # A: 1/0 "row is visible"
COL_NUM = 2  # B: running item number within the block
COL_NAME = 3  # C: catalog item name (VLOOKUP key)
COL_QTY = 4  # D: quantity -- literal, or a derived formula (what we harvest)
COL_UNIT = 5  # E: unit, VLOOKUP'd from the catalog
COL_PRICE = 6  # F: sale price, VLOOKUP'd from the catalog
COL_TOTAL = 7  # G: line total = price * qty
COL_OPTION = 8  # H: boolean option toggle (lawn type, mole net, geotextile, ...)
COL_COST_TOTAL = 11  # K: cost total = unit cost * qty

HEADER_MATERIALS = {"матеріали", "рослини"}
HEADER_WORKS = {"робота"}

SUBTOTAL_MATERIALS = "разом за матеріали"
SUBTOTAL_WORKS = "разом за роботу"
SUBTOTAL_PLANTS = "разом за рослини"


@dataclass
class TemplateLine:
    """One catalog-backed row of the template."""

    row: int
    name: str
    block: str  # "materials" | "works" | "plants" | "driver"
    qty_formula: str | None = None  # original Excel formula, if any
    qty_expr: str | None = None  # translated DSL expression
    qty_status: str = "input"  # input | derived | needs_review
    option_row: bool = False  # gated by an H-column TRUE/FALSE toggle
    note: str | None = None


@dataclass
class TemplateBlock:
    kind: str  # materials | works | plants
    first_row: int
    last_row: int
    subtotal_row: int | None = None
    label: str | None = None


@dataclass
class TemplateSection:
    key: str
    title: str
    header_row: int
    total_row: int | None
    blocks: list[TemplateBlock] = field(default_factory=list)
    lines: list[TemplateLine] = field(default_factory=list)


# --- Section keys ------------------------------------------------------------
# Stable ASCII keys so rules/API/exports do not depend on Cyrillic spelling.
SECTION_KEYS = {
    "підготовчий етап": "prep",
    "рахунок водопостачання": "water_supply",
    "рахунок автоматичний полив": "irrigation",
    "рахунок освітлення": "lighting",
    "рахунок водовідведення": "drainage_storm",
    "рахунок дренаж": "drainage_ground",
    "рахунок бруківка": "paving",
    "рахунок доріжка": "pathway",
    "рахунок георешітка": "geogrid",
    "рахунок озеленення": "planting",
    "рахунок газон": "lawn",
    "рахунок кашпо": "planters",
    "рахунок бетонні роботи": "concrete",
    "рахунок зона вогню": "fire_zone",
    "рахунок додаткові роботи": "extra_works",
    "рахунок загальний": "summary",
}


def section_key(title: str) -> str:
    norm = title.strip().rstrip(":").strip().lower()
    if norm in SECTION_KEYS:
        return SECTION_KEYS[norm]
    slug = re.sub(r"[^a-z0-9]+", "_", norm)
    return slug or "section"


# --- Excel formula -> DSL translation ---------------------------------------


# --- Compiler ----------------------------------------------------------------


def _txt(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def compile_template(xlsx_path: str | Path, sheet: str = "Шаблон основний!!!! 1") -> dict[str, Any]:
    """Read the template workbook and return the compiled layout + rule pack."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=False)
    ws = wb[sheet]

    # Pass 1: locate section headers and subtotal rows.
    headers: list[tuple[int, str]] = []
    for r in range(1, ws.max_row + 1):
        b = ws.cell(r, COL_NUM).value
        if isinstance(b, str) and not b.startswith("=") and b.strip().endswith(":"):
            title = b.strip()
            if title.rstrip(":").strip().lower() in {"замовник", "адреса об'єкту", "дата рахунку",
                                                     "пропозиція дійсна протягом"}:
                continue
            headers.append((r, title))

    # Pass 2: catalog-name rows and quantity formulas.
    row_names: dict[int, str] = {}
    for r in range(1, ws.max_row + 1):
        c = ws.cell(r, COL_NAME).value
        if isinstance(c, str) and c.strip() and not c.startswith("="):
            # Skip subtotal labels that live in column D, not C.
            row_names[r] = c.strip()

    # A row is a *priced* line when column G computes price * quantity for it.
    line_rows: set[int] = set()
    for r in range(1, ws.max_row + 1):
        g = ws.cell(r, COL_TOTAL).value
        if isinstance(g, str) and g.startswith("=") and "IFERROR(F" in g.upper():
            line_rows.add(r)

    # Rows that carry a name but no sum are *driver* rows: quantities the
    # estimator types in ("Довжина траншей (м)", "Загальна площа бруківка",
    # "Площа газону (рулонного)") which other rows' formulas read. They never
    # appear as priced lines, but without them every rule that depends on them
    # would silently evaluate to zero.
    driver_rows: set[int] = set()
    for r, name in row_names.items():
        if r in line_rows:
            continue
        if _txt(ws.cell(r, COL_NUM).value) == "#":
            continue  # block header ("# | Матеріали | К-сть | ...")
        if name.strip().endswith(":"):
            continue
        driver_rows.add(r)

    # Block header rows: "# | Матеріали | К-сть | ..." etc.
    block_headers: dict[int, str] = {}
    for r in range(1, ws.max_row + 1):
        b = _txt(ws.cell(r, COL_NUM).value)
        c = _txt(ws.cell(r, COL_NAME).value).lower()
        if b == "#" and c:
            if c in HEADER_MATERIALS:
                block_headers[r] = "plants" if c == "рослини" else "materials"
            elif c in HEADER_WORKS:
                block_headers[r] = "works"

    subtotal_rows: dict[int, str] = {}
    total_rows: dict[int, str] = {}
    for r in range(1, ws.max_row + 1):
        d = ws.cell(r, 4).value
        if isinstance(d, str) and not d.startswith("="):
            n = d.strip().lower().rstrip(":")
            if n.startswith(SUBTOTAL_MATERIALS.rstrip(":")):
                subtotal_rows[r] = "materials"
            elif n.startswith(SUBTOTAL_WORKS.rstrip(":")):
                subtotal_rows[r] = "works"
            elif n.startswith(SUBTOTAL_PLANTS.rstrip(":")):
                subtotal_rows[r] = "plants"
            elif n.startswith("разом за насосну станцію"):
                subtotal_rows[r] = "pump_station"
            elif n.startswith("разом"):
                total_rows[r] = d.strip()

    # --- Pass 3: section + block geometry -----------------------------------
    sections: list[TemplateSection] = []
    bounds = [h[0] for h in headers] + [ws.max_row + 1]
    for idx, (hrow, title) in enumerate(headers):
        start, end = hrow, bounds[idx + 1] - 1
        sec = TemplateSection(
            key=section_key(title),
            title=title.rstrip(":").strip(),
            header_row=hrow,
            total_row=None,
        )
        for r in range(start, end + 1):
            if r in total_rows:
                sec.total_row = r

        open_block: TemplateBlock | None = None
        for r in range(start, end + 1):
            if r in block_headers:
                if open_block:
                    open_block.last_row = r - 1
                    sec.blocks.append(open_block)
                open_block = TemplateBlock(kind=block_headers[r], first_row=r + 1, last_row=r + 1)
            elif r in subtotal_rows and open_block:
                open_block.last_row = r - 1
                open_block.subtotal_row = r
                open_block.label = subtotal_rows[r]
                sec.blocks.append(open_block)
                open_block = None
        if open_block:
            open_block.last_row = end
            sec.blocks.append(open_block)
        sections.append(sec)

    # --- Pass 4: block-total identifiers -------------------------------------
    # A `G` reference to a subtotal row means "that block's total"; give each
    # one a stable id (e.g. "planting.plants") so rules survive row shifts.
    block_totals: dict[int, str] = {}
    row_blocks: dict[int, str] = {}
    for sec in sections:
        seen: dict[str, int] = {}
        for b in sec.blocks:
            kind = b.label or b.kind
            seen[kind] = seen.get(kind, 0) + 1
            suffix = "" if seen[kind] == 1 else f"_{seen[kind]}"
            block_id = f"{sec.key}.{kind}{suffix}"
            b.label = block_id
            for r in range(b.first_row, b.last_row + 1):
                row_blocks[r] = block_id
            if b.subtotal_row is not None:
                block_totals[b.subtotal_row] = block_id
        if sec.total_row is not None:
            block_totals[sec.total_row] = f"{sec.key}.total"

    resolver = RefResolver(row_names, block_totals, row_blocks)

    # --- Pass 5: lines and quantity rules ------------------------------------
    for idx, sec in enumerate(sections):
        start = sec.header_row
        end = bounds[idx + 1] - 1

        def block_of(row: int, _sec: TemplateSection = sec) -> str:
            for b in _sec.blocks:
                if b.first_row <= row <= b.last_row:
                    return b.kind
            return "materials"

        for r in range(start, end + 1):
            if r not in line_rows and r not in driver_rows:
                continue
            name = row_names.get(r)
            if not name:
                continue  # blank spare row kept in the template for manual adds
            block = "driver" if r in driver_rows else block_of(r)
            line = TemplateLine(row=r, name=name, block=block)
            qv = ws.cell(r, COL_QTY).value
            if isinstance(qv, str) and qv.startswith("="):
                line.qty_formula = qv
                try:
                    line.qty_expr = translate(qv, resolver)
                    line.qty_status = "derived"
                except Untranslatable as exc:
                    line.qty_status = "needs_review"
                    line.note = str(exc)
            hv = ws.cell(r, COL_OPTION).value
            if hv is not None and _txt(hv) != "":
                line.option_row = True
            sec.lines.append(line)

    return {
        "source": str(xlsx_path),
        "sheet": sheet,
        "block_totals": {str(k): v for k, v in block_totals.items()},
        "sections": [asdict(s) for s in sections],
    }


if __name__ == "__main__":  # pragma: no cover - CLI convenience
    import sys

    out = compile_template(sys.argv[1])
    print(json.dumps(out, ensure_ascii=False, indent=2))
