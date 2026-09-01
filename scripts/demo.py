"""End-to-end demo without a browser or an API key.

Rebuilds the Budkiv estimate from the quantities stated on its drawings, runs
the 233 template rules, validates the result and writes a real XLSX.

Usage:
    python scripts/demo.py [output.xlsx]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import CatalogItem  # noqa: E402
from app.services.estimate.builder import (  # noqa: E402
    EstimateBuilder,
    TemplateLayout,
    visible_lines,
)
from app.services.export.xlsx import ExportMeta, export_estimate  # noqa: E402
from app.services.validation.validators import validate  # noqa: E402

# --- exactly what the drawings say -------------------------------------------

PLANTS = [
    {"name": "Кизильник горизонтальний", "quantity": 21},
    {"name": "Сосна чорна 1м", "quantity": 11},
    {"name": 'Сосна гірська "Мопс" d30-40', "quantity": 8},
    {"name": "Верба японська Hakuro nishiki 1,5м", "quantity": 3},
    {"name": "Ірга Ламарка 2м", "quantity": 1},
    {"name": "Гортензія волотиста с5 білий", "quantity": 12},
    {"name": "Спірея японська с3", "quantity": 10},
    {"name": "Ірис сибірський", "quantity": 12},
    {"name": "Костриця сиза", "quantity": 21},
    {"name": "Пеннісетум лисохвостий", "quantity": 24},
    {"name": "Котовник", "quantity": 37},
    {"name": "Гераль Кембриджська", "quantity": 14},
    {"name": "Піон деревовидний", "quantity": 2, "is_existing": True},
    {"name": "Плодові дерева", "quantity": 6, "is_existing": True},
    {"name": "Троянди", "quantity": 4, "is_existing": True},
]

QUANTITIES = {
    "planting": {
        "Садовий бордюр Макспол L 1м": 147,
        "Агрополотно площа (клумб)": 175,
        "Декор крихта сіра ,фр.-5-20": 1800,
        "Декор кора соснова середня": 136,
    },
    "lawn": {"Площа газону (рулонного)": 259},
    "pathway": {
        "Плита ходова бетонна 120х40х6см": 37,
        "Загальна площа плит": 18,
        "Гідрофобізатор для бетонних плит, 5л": 1,
    },
    "prep": {
        "Мішок будівельний білий": 100,
        "Плівка поліетиленова  1,5x100 м чорна 150 мкм": 80,
        "Спецтехніка (бобкат)": 20,
        "Спецтехніка (мініекскаватор 5т)": 20,
    },
}

OPTIONS = {"Газон рулонний універсальний": True, "Сітка від кротів": True}
SECTIONS = ["prep", "pathway", "planting", "lawn"]


def money(v: float) -> str:
    return f"{v:>14,.2f}".replace(",", " ")


def main() -> int:
    init_db()
    session = SessionLocal()

    if session.query(CatalogItem).count() == 0:
        print("Каталог порожній. Спочатку: python scripts/import_catalog.py <Шаблон для ШІ.xlsx>")
        return 1
    try:
        layout = TemplateLayout.load()
    except FileNotFoundError as exc:
        print(exc)
        return 1

    rules = sum(
        1 for s in layout.data["sections"] for l in s["lines"] if l.get("qty_status") == "derived"
    )
    print("=" * 78)
    print("ДЕМО: кошторис по кресленнях об'єкта у с. Будьків")
    print("=" * 78)
    print(f"каталог : {session.query(CatalogItem).count()} позицій")
    print(f"шаблон  : {len(layout.order)} секцій, {rules} правил кількостей")
    print(f"вхідні  : {sum(len(v) for v in QUANTITIES.values())} чисел із креслень "
          f"+ {len([p for p in PLANTS if not p.get('is_existing')])} рослин")

    builder = EstimateBuilder(session, layout)
    result = builder.build(
        sections=SECTIONS, quantities=QUANTITIES, plants=PLANTS, options=OPTIONS
    )

    derived = [l for l in visible_lines(result.draft) if l.qty_source == "rule"]
    print(f"вихідні : {len(visible_lines(result.draft))} позицій, "
          f"з них {len(derived)} обчислено правилами\n")

    for section in result.totals.sections:
        print(f"  {section.title:<28} матеріали {money(section.materials + section.plants)}"
              f"   роботи {money(section.works)}   разом {money(section.total)}")

    t = result.totals
    print()
    print(f"  {'Разом за матеріали':<28} {money(t.materials_total)}")
    print(f"  {'Разом за роботу':<28} {money(t.works_total)}")
    print(f"  {'ЗАГАЛЬНИЙ РАХУНОК':<28} {money(t.grand_total)}")
    print(f"  {'Аванс (матеріали)':<28} {money(t.prepayment_materials)}")
    works = f"{t.works_total:,.0f}".replace(",", " ")
    print(f"  {'Аванс (роботи)':<28} {money(t.prepayment_works)}   "
          f"= ОКРВГОРУ({works}×0,3; 100)")
    print(f"  {'Залишок':<28} {money(t.balance)}")

    print("\nПриклади обчислених кількостей (правило -> результат):")
    samples = [
        ("planting", "Анкера для садового бордюру"),
        ("planting", "Агрополотно 50г/м² (чорне)"),
        ("planting", "Гачок Металевий (для Сітки та Агрополотна)"),
        ("planting", "Висадка рослин, % від вартості "),
        ("lawn", "Газон рулонний універсальний"),
        ("lawn", "Сітка від кротів"),
        ("lawn", "Доставка газону (великий маніпулятор)"),
        ("pathway", "Підготовка подушки під плити"),
        ("pathway", "Позиціонування та монтаж плит"),
        ("prep", "Доставка спецтехніки (Bobcat)"),
    ]
    for section, name in samples:
        line = result.draft.by_name(name, section)
        if line is None:
            continue
        print(f"  {line.name[:52]:<54} = {line.quantity:>9g} {line.unit or '':<5}")
        if line.qty_expr:
            print(f"      {line.qty_expr[:110]}")

    report = validate(result.draft, result.totals)
    print(f"\nПеревірка: {report.status} "
          f"(помилок {len(report.errors)}, питань {len(report.questions)}, "
          f"попереджень {len(report.warnings)})")
    for finding in report.sorted()[:6]:
        print(f"  [{finding.severity}] {finding.title}")
        if finding.fix_hint:
            print(f"      -> {finding.fix_hint}")
    if len(report.findings) > 6:
        print(f"  … і ще {len(report.findings) - 6}")

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "exports" / "demo-budkiv.xlsx"
    export_estimate(
        target,
        result.draft,
        result.totals,
        meta=ExportMeta(
            client_name="Галина",
            address="с. Будьків, Львівська область",
            project_name="Благоустрій прибудинкової території",
        ),
        section_titles=layout.titles(),
        section_order=layout.order,
        report=report,
    )
    print(f"\nXLSX: {target}  ({target.stat().st_size / 1024:.0f} КБ, 3 аркуші)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
