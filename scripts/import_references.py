"""Load the issued proposals into the price history the engine prices from.

Usage:
    python scripts/import_references.py "D:/Chrome download"

Run it after ``import_catalog.py``. The catalogue says what the company sells;
this says what it last sold each thing for, which is the price the estimate
uses wherever the history has one. A proposal whose blocks do not reconcile
with their own printed subtotals is rejected and named, never imported.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import init_db, session_scope  # noqa: E402
from app.services.history.prices import InvoicedPrices  # noqa: E402
from app.services.history.store import import_references  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    root = Path(sys.argv[1])
    if not root.exists():
        print(f"Немає такої теки: {root}")
        return 2

    init_db()
    with session_scope() as session:
        report = import_references(session, root)
        print(f"imported   : {report.imported}")
        print(f"updated    : {report.updated}")
        print(f"rows       : {report.lines}")
        if report.rejected:
            print(f"\nrejected ({len(report.rejected)}) — читання не сходиться, "
                  f"ціни з них не беруться:")
            for name, why in report.rejected:
                print(f"    {name[:66]:68s} {why}")

        prices = InvoicedPrices.from_db(session)
        contested = prices.contested()
        print(f"\narticles with a sold price : {len(prices)}")
        print(f"of them sold at >1 price   : {len(contested)} "
              f"(розв'язано за останньою датою)")

        undated = [s for name in prices.names for s in prices.sales_of(name)
                   if s.issued is None]
        if undated:
            projects = sorted({s.project for s in undated})
            print(f"\nбез дати у назві файлу ({len(projects)}) — вважаються "
                  f"найстарішими:")
            for name in projects:
                print(f"    {name[:80]}")

        blocks = Counter(
            s.block for name in prices.names for s in prices.sales_of(name)
        )
        print("\nby block:")
        for block, count in blocks.most_common():
            print(f"  {block:10s}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
