"""Import the client's price base (``2026 База 1``) into the catalog.

Column layout, read off row 6 of the sheet::

    A Категорія            F Маржинальність у %      L обєм кашпо
    B Матеріал             G Маржинальність, (грн)   M агрік для кашпо
    C Од. виміру           H Собівартість 1од($)     N геотекстиль для кашпо
    D Собівартість 1од     I Відповідальний
    E Ціна реалізації 1од  J Дата оновлення цін

Materials and works share the sheet; we classify them by unit and by name,
because the template needs to know which block a line belongs to. The rules in
:func:`classify_kind` come from the data itself -- units like ``послуга``,
``год``, ``день`` only ever appear on labour rows.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import openpyxl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import CatalogItem
from ..rules.engine import normalize_name

SHEET = "2026 База 1"
HEADER_ROW = 6
FIRST_DATA_ROW = 7

COL = {
    "category": 1,
    "name": 2,
    "unit": 3,
    "unit_cost": 4,
    "unit_price": 5,
    "margin_pct": 6,
    "margin_uah": 7,
    "unit_cost_usd": 8,
    "owner": 9,
    "price_updated": 10,
    "attr_volume": 12,  # L: обєм кашпо
    "attr_agro": 13,  # M: агрік для кашпо
    "attr_geotex": 14,  # N: геотекстиль для кашпо
}

# Units that only ever appear on labour lines in the client's base.
WORK_UNITS = {"послуга", "год", "день", "днів", "робота", "поверх"}

# Name markers for labour that is billed per m²/m.п./шт and so cannot be told
# apart by unit alone.
WORK_PREFIXES = (
    "монтаж", "демонтаж", "встановлення", "установка", "прокладання", "укладання",
    "підготовка", "організація", "висадка", "посів", "зрізання", "корчування",
    "зняття", "нівелювання", "трамбування", "розбивання", "прибирання", "миття",
    "чистове", "чорнове", "засипка", "зворотня засипка", "траншейні", "бетонування",
    "герметизація", "сверління", "зарізка", "підключення", "пусконалагоджувальні",
    "обробка", "розвантаження", "перенесення", "позиціонування", "підв'язування",
    "укріплення", "заповнення швів", "ручне завантаження", "перевезення",
)

PLANT_CATEGORIES = {"рослини"}


@dataclass
class ImportReport:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    duplicates: list[str] = None  # type: ignore[assignment]
    conflicts: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.duplicates = self.duplicates or []
        self.conflicts = self.conflicts or []


def classify_kind(name: str, unit: str, category: str) -> str:
    """Decide whether a catalog row is a material, a work or a plant."""
    cat = (category or "").strip().lower()
    if cat in PLANT_CATEGORIES:
        return "plant"
    u = (unit or "").strip().lower().rstrip(".")
    if u in WORK_UNITS:
        return "work"
    low = normalize_name(name)
    if low.startswith(WORK_PREFIXES):
        return "work"
    if "% від вартості" in low:
        return "work"
    return "material"


def _num(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    try:
        return float(s) if s not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def _txt(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def read_rows(xlsx_path: str | Path, sheet: str = SHEET) -> Iterator[dict[str, Any]]:
    """Yield one normalised dict per catalog row."""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    ws = wb[sheet]
    for row in ws.iter_rows(min_row=FIRST_DATA_ROW, values_only=False):
        cells = {k: row[i - 1].value if i - 1 < len(row) else None for k, i in COL.items()}
        name = _txt(cells["name"])
        if not name:
            continue
        # Header repeats and helper labels ("обєм кашпо") are not products.
        if name.lower() in ("матеріал", "обєм кашпо", "агрік для кашпо", "геотекстиль для кашпо"):
            continue

        updated = cells["price_updated"]
        if isinstance(updated, str):
            updated = None
        if isinstance(updated, dt.date) and not isinstance(updated, dt.datetime):
            updated = dt.datetime.combine(updated, dt.time())

        attributes: dict[str, float] = {}
        for key, col in (("volume", "attr_volume"), ("agro_fabric", "attr_agro"),
                         ("geotextile", "attr_geotex")):
            val = cells[col]
            if isinstance(val, (int, float)):
                attributes[key] = float(val)

        category = _txt(cells["category"])
        unit = _txt(cells["unit"])
        yield {
            "name": name,
            "name_norm": normalize_name(name),
            "category": category,
            "unit": unit,
            "unit_cost": _num(cells["unit_cost"]),
            "unit_price": _num(cells["unit_price"]),
            "margin_pct": _num(cells["margin_pct"]),
            "margin_uah": _num(cells["margin_uah"]),
            "unit_cost_usd": _num(cells["unit_cost_usd"]),
            "owner": _txt(cells["owner"]),
            "price_updated_at": updated,
            "kind": classify_kind(name, unit, category),
            "attributes": attributes,
        }
    wb.close()


def import_catalog(session: Session, xlsx_path: str | Path, sheet: str = SHEET) -> ImportReport:
    """Upsert the price base into ``catalog_items``.

    Conflicting duplicates (same name, different price) are reported rather
    than silently resolved -- picking one at random would quietly change what
    the company charges.
    """
    report = ImportReport()
    source = str(xlsx_path)

    existing = {c.name_norm: c for c in session.scalars(select(CatalogItem)).all()}
    seen: dict[str, dict[str, Any]] = {}

    for row in read_rows(xlsx_path, sheet):
        key = row["name_norm"]
        if key in seen:
            prev = seen[key]
            if abs(prev["unit_price"] - row["unit_price"]) > 0.005:
                report.conflicts.append(
                    {
                        "name": row["name"],
                        "field": "unit_price",
                        "values": [prev["unit_price"], row["unit_price"]],
                        "detail": "У базі дві позиції з однаковою назвою і різною ціною.",
                    }
                )
            else:
                report.duplicates.append(row["name"])
            report.skipped += 1
            continue
        seen[key] = row

        item = existing.get(key)
        if item is None:
            item = CatalogItem(**row, source_file=source, active=True)
            session.add(item)
            report.inserted += 1
        else:
            for field_name, value in row.items():
                setattr(item, field_name, value)
            item.source_file = source
            item.active = True
            report.updated += 1

    # Anything no longer present in the sheet is deactivated, never deleted:
    # historical estimates still reference it.
    for key, item in existing.items():
        if key not in seen:
            item.active = False

    session.commit()
    return report
