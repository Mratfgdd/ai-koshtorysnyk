"""Strict numeric audit: drawings -> rules -> Python -> Excel.

Recomputes every figure three independent ways and refuses to pass unless they
agree to the kopeck:

    1. line by line, straight from the draft;
    2. through the deterministic totals layer;
    3. by evaluating the live formulas written into the exported workbook.

Usage:
    python scripts/audit.py [estimate.xlsx]
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import openpyxl  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import CatalogItem  # noqa: E402
from app.services.estimate.builder import (  # noqa: E402
    EstimateBuilder,
    TemplateLayout,
    visible_lines,
)
from app.services.export.xlsx import ExportMeta, export_estimate  # noqa: E402
from app.services.validation.validators import validate  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from demo import OPTIONS, PLANTS, QUANTITIES, SECTIONS  # noqa: E402

TOL = 0.005  # half a kopeck
failures: list[str] = []
checks = 0


def check(label: str, a: float, b: float, tol: float = TOL) -> None:
    global checks
    checks += 1
    ok = abs(a - b) <= tol
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label:<52} {a:>15,.2f}  vs {b:>15,.2f}".replace(",", " "))
    if not ok:
        failures.append(f"{label}: {a:.2f} != {b:.2f}")


# --- 1. build ----------------------------------------------------------------

init_db()
session = SessionLocal()
if session.query(CatalogItem).count() == 0:
    print("Каталог порожній: спочатку python scripts/import_catalog.py <Шаблон>")
    raise SystemExit(1)

layout = TemplateLayout.load()
builder = EstimateBuilder(session, layout)
built = builder.build(
    sections=SECTIONS, quantities=QUANTITIES, plants=PLANTS, options=OPTIONS
)
lines = visible_lines(built.draft)
t = built.totals

print("=" * 92)
print("АУДИТ ЧИСЕЛ: креслення -> правила шаблону -> Python -> Excel")
print("=" * 92)
print(f"позицій у кошторисі: {len(lines)}   секцій: {len(t.sections)}\n")

# --- 2. line arithmetic ------------------------------------------------------

print("Крок 1. Арифметика позицій (ціна × кількість):")
bad = 0
for line in lines:
    expected = round(line.quantity * line.unit_price, 2)
    if abs(expected - line.total) > TOL:
        bad += 1
        failures.append(f"line «{line.name}»: {line.total} != {expected}")
checks += 1
print(f"  [{'OK  ' if bad == 0 else 'FAIL'}] усі {len(lines)} позицій узгоджені "
      f"(розбіжностей: {bad})\n")

# --- 3. section and invoice totals ------------------------------------------

print("Крок 2. Підсумки секцій:")
for section in t.sections:
    section_lines = [l for l in lines if l.section == section.key]
    materials = sum(l.total for l in section_lines if l.block in ("materials", "plants"))
    works = sum(l.total for l in section_lines if l.block == "works")
    check(f"{section.title} — матеріали", section.materials + section.plants, materials)
    check(f"{section.title} — роботи", section.works, works)
    check(f"{section.title} — разом", section.total, materials + works)

print("\nКрок 3. Підсумки кошторису:")
materials_total = sum(l.total for l in lines if l.block in ("materials", "plants"))
works_total = sum(l.total for l in lines if l.block == "works")
check("Разом за матеріали", t.materials_total, materials_total)
check("Разом за роботу", t.works_total, works_total)
check("Підсумок", t.subtotal, materials_total + works_total)
check("Загальний рахунок", t.grand_total, t.subtotal + t.surcharge)
check("Аванс (матеріали)", t.prepayment_materials, t.materials_total)
check("Аванс (роботи)", t.prepayment_works, math.ceil(t.works_total * 0.3 / 100) * 100)
check("Залишок", t.balance, t.grand_total - t.prepayment_materials - t.prepayment_works)
check("Собівартість", t.cost_total, sum(l.cost_total for l in lines))
check("Маржа", t.margin, t.subtotal - t.cost_total)

# --- 4. the workbook's own formulas -----------------------------------------

target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "exports" / "audit.xlsx"
export_estimate(
    target,
    built.draft,
    t,
    meta=ExportMeta(client_name="Галина", address="с. Будьків, Львівська область"),
    section_titles=layout.titles(),
    section_order=layout.order,
    report=validate(built.draft, t),
)

ws = openpyxl.load_workbook(target)["Кошторис"]
cells: dict[str, float] = {}


def value_of(ref: str) -> float:
    """Evaluate one cell of the sheet, following the formulas we wrote."""
    if ref in cells:
        return cells[ref]
    col, row = re.match(r"([A-Z]+)(\d+)", ref).groups()
    raw = ws[f"{col}{row}"].value
    if raw is None:
        result = 0.0
    elif isinstance(raw, (int, float)):
        result = float(raw)
    elif isinstance(raw, str) and raw.startswith("="):
        result = evaluate(raw[1:])
    else:
        result = 0.0
    cells[ref] = result
    return result


def evaluate(expr: str) -> float:
    expr = expr.strip()
    m = re.fullmatch(r"SUM\(([A-Z]+)(\d+):([A-Z]+)(\d+)\)", expr)
    if m:
        c1, r1, _c2, r2 = m.groups()
        return sum(value_of(f"{c1}{r}") for r in range(int(r1), int(r2) + 1))
    m = re.fullmatch(r"CEILING\(([A-Z]+\d+)\*([\d.]+),(\d+)\)", expr)
    if m:
        ref, factor, step = m.groups()
        step = float(step)
        return math.ceil(value_of(ref) * float(factor) / step) * step
    # plain arithmetic over cell references and literals
    tokens = re.split(r"([+\-*/])", expr)
    total, op = 0.0, "+"
    for token in tokens:
        token = token.strip()
        if token in "+-*/":
            op = token
            continue
        if not token:
            continue
        val = value_of(token) if re.fullmatch(r"[A-Z]+\d+", token) else float(token)
        total = {"+": total + val, "-": total - val,
                 "*": total * val, "/": total / val if val else 0.0}[op]
    return total


labels: dict[str, int] = {}
for row in range(1, ws.max_row + 1):
    label = ws.cell(row, 1).value
    if isinstance(label, str) and label.strip():
        labels.setdefault(label.strip(), row)

print("\nКрок 4. Живі формули у згенерованому XLSX:")
for label, expected in (
    ("Разом за матеріали:", t.materials_total),
    ("Разом за роботу:", t.works_total),
    ("Загальний рахунок", t.grand_total),
    ("Аванс (на матеріали)", t.prepayment_materials),
    ("Аванс (на роботи)", t.prepayment_works),
    ("Залишок", t.balance),
):
    # The roll-up block at the end of the sheet is the authoritative one.
    rows = [r for r, lbl in ((r, ws.cell(r, 1).value) for r in range(1, ws.max_row + 1))
            if isinstance(lbl, str) and lbl.strip() == label]
    if not rows:
        failures.append(f"XLSX: рядок «{label}» відсутній")
        print(f"  [FAIL] {label:<52} відсутній у файлі")
        checks += 1
        continue
    check(f"XLSX «{label}»", value_of(f"F{rows[-1]}"), expected)

print(f"\nXLSX: {target}")

# --- 5. provenance -----------------------------------------------------------
# Every derived quantity must trace back to a cell of the client's workbook.
# A rule with no original formula would mean the system invented a coefficient.

print("\nКрок 5. Походження коефіцієнтів (жоден не вигаданий):")
rule_index: dict[tuple[str, str], dict] = {}
for section in layout.data["sections"]:
    for tpl in section.get("lines", []):
        rule_index[(section["key"], tpl["name"])] = tpl

derived = [l for l in lines if l.qty_source == "rule"]
orphans = [
    l for l in derived
    if not (rule_index.get((l.section, l.name)) or {}).get("qty_formula")
]
checks += 1
print(f"  [{'OK  ' if not orphans else 'FAIL'}] {len(derived)} обчислених кількостей, "
      f"усі мають вихідну формулу Excel (без джерела: {len(orphans)})")
for line in orphans:
    failures.append(f"rule without source formula: {line.section}/{line.name}")

sample = [
    ("planting", "Анкера для садового бордюру"),
    ("planting", "Агрополотно 50г/м² (чорне)"),
    ("lawn", "Газон рулонний універсальний"),
    ("lawn", "Сітка від кротів"),
    ("pathway", "Підготовка подушки під плити"),
]
print(f"\n  {'позиція':<44} {'к-сть':>8}   рядок шаблону та вихідна формула")
for section, name in sample:
    line = built.draft.by_name(name, section)
    tpl = rule_index.get((section, name))
    if not line or not tpl:
        continue
    print(f"  {name[:42]:<44} {line.quantity:>8g}   "
          f"{layout.data['sheet']}!D{tpl['row']}  {tpl['qty_formula']}")

total_rules = sum(
    1 for s in layout.data["sections"] for l in s["lines"] if l.get("qty_status") == "derived"
)
review = ROOT / "backend" / "app" / "data" / "rules" / "needs_review.json"
guessed = len(json.loads(review.read_text(encoding="utf-8"))) if review.exists() else 0
checks += 1
print(f"\n  [{'OK  ' if guessed == 0 else 'FAIL'}] правил у пакеті: {total_rules}, "
      f"перекладено з Excel: {total_rules}, вгадано: {guessed}")
if guessed:
    failures.append(f"{guessed} formulas were not translated from the workbook")

# --- 6. verdict --------------------------------------------------------------

print("\n" + "=" * 92)
if failures:
    print(f"РЕЗУЛЬТАТ: {len(failures)} РОЗБІЖНОСТЕЙ із {checks} перевірок")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print(f"РЕЗУЛЬТАТ: усі {checks} перевірок пройдено, розбіжностей немає (точність 0,005 грн)")
