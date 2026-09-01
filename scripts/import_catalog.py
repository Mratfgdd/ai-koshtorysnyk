"""Import the client's price base into the local database.

Usage:
    python scripts/import_catalog.py "path/to/Шаблон для ШІ.xlsx"
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import init_db, rebuild_fts, session_scope  # noqa: E402
from app.models import CatalogItem  # noqa: E402
from app.services.ingest.catalog_import import import_catalog  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    init_db()
    with session_scope() as session:
        report = import_catalog(session, sys.argv[1])
        items = session.query(CatalogItem).filter(CatalogItem.active.is_(True)).all()

        print(f"inserted   : {report.inserted}")
        print(f"updated    : {report.updated}")
        print(f"skipped    : {report.skipped}")
        print(f"active     : {len(items)}")

        kinds = Counter(i.kind for i in items)
        print("\nby kind:")
        for k, v in kinds.most_common():
            print(f"  {k:10s}: {v}")

        cats = Counter(i.category for i in items)
        print("\nby category:")
        for k, v in cats.most_common():
            print(f"  {v:4d}  {k}")

        no_price = [i for i in items if i.unit_price <= 0]
        bad_margin = [i for i in items if i.unit_cost > i.unit_price > 0]
        print(f"\nno sale price       : {len(no_price)}")
        print(f"cost above price    : {len(bad_margin)}")
        for i in bad_margin[:10]:
            print(f"    {i.name[:60]:62s} cost={i.unit_cost} price={i.unit_price}")

        if report.conflicts:
            print(f"\nconflicts ({len(report.conflicts)}):")
            for c in report.conflicts[:10]:
                print(f"    {c['name'][:60]:62s} {c['field']}={c['values']}")

        indexed = rebuild_fts(session)
        print(f"\nFTS rows   : {indexed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
