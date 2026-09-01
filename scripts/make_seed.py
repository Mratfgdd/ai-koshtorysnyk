"""Freeze the current catalog into a seed file the deployment can load.

The compiled rule pack and the price base are what make the system work, but
they are derived from the client's workbook, which is not in the repository.
This writes the catalog to a small JSON seed that ships with the code, so a
fresh deploy (Render, a new laptop) comes up with prices already loaded.

Usage:
    python scripts/make_seed.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import CatalogItem  # noqa: E402

SEED = ROOT / "backend" / "app" / "data" / "seed" / "catalog.json"


def main() -> int:
    init_db()
    session = SessionLocal()
    items = session.query(CatalogItem).filter(CatalogItem.active.is_(True)).all()
    if not items:
        print("Каталог порожній. Спочатку: python scripts/import_catalog.py <Шаблон>")
        return 1

    payload = [
        {
            "name": i.name,
            "name_norm": i.name_norm,
            "category": i.category,
            "unit": i.unit,
            "unit_cost": i.unit_cost,
            "unit_price": i.unit_price,
            "margin_pct": i.margin_pct,
            "margin_uah": i.margin_uah,
            "unit_cost_usd": i.unit_cost_usd,
            "kind": i.kind,
            "owner": i.owner,
            "attributes": i.attributes or {},
            "price_updated_at": i.price_updated_at.isoformat() if i.price_updated_at else None,
        }
        for i in items
    ]

    SEED.parent.mkdir(parents=True, exist_ok=True)
    SEED.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(payload)} позицій -> {SEED} ({SEED.stat().st_size / 1024:.0f} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
