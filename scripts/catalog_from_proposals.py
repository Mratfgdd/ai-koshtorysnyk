"""Add articles the price base does not carry, priced from issued proposals.

    python scripts/catalog_from_proposals.py --root "/var/lib/estimator/references"
    python scripts/catalog_from_proposals.py --root ... --only "Катерина"
    python scripts/catalog_from_proposals.py --root ... --apply

A project's drawings name goods the company buys per object — the designer's
light fittings, a pergola, a fire bowl, planters. «2026 База 1» does not list
them, so the estimate cannot price them and the section stays empty however
many questions are answered. What does carry them is the КП that was issued for
that object: it is what the company charged, signed by the customer.

This reads the issued proposals, finds every article missing from the base, and
adds it at the price it was last sold for — the same rule the estimate already
uses for everything else.

Nothing is invented. An article is only added when a parsed proposal carries it
with a quantity, a unit and a price, and only from a proposal whose blocks
reconcile with their own printed subtotals. There is no default price, no
rounding and no filling of gaps: an article no proposal priced is not added.

Every row records the proposal it came from in ``source_file``, so a price can
be traced to a document and so a measurement can exclude an object's own КП —
scoring a project against a proposal whose prices you have just imported
measures nothing.

Dry run by default; ``--apply`` writes.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import init_db, rebuild_fts, session_scope  # noqa: E402
from app.models import CatalogItem  # noqa: E402
from app.services.history.prices import InvoicedPrices  # noqa: E402
from app.services.history.proposals import (  # noqa: E402
    parse_proposal,
    reference_proposals,
)
from app.services.ingest.catalog_import import classify_kind  # noqa: E402
from app.services.rules.engine import normalize_name  # noqa: E402

# Marks a catalogue row that came from an invoice rather than from the price
# workbook, so a later import of «2026 База 1» can tell them apart.
SOURCE_PREFIX = "КП:"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--only", default="",
                        help="restrict to proposals whose name contains this")
    parser.add_argument("--apply", action="store_true", help="write to the catalogue")
    parser.add_argument("--show", type=int, default=40)
    args = parser.parse_args()

    paths = reference_proposals(args.root)
    if args.only:
        paths = [p for p in paths if args.only.lower() in p.name.lower()]
    if not paths:
        print(f"No proposals under {args.root}"
              + (f" matching «{args.only}»" if args.only else ""))
        return 1

    proposals = []
    for path in paths:
        proposal = parse_proposal(path)
        if not proposal.reconciles:
            print(f"  SKIP (не сходиться) {proposal.project[:64]}")
            continue
        proposals.append(proposal)
    print(f"read {len(proposals)} of {len(paths)} proposals")
    if not proposals:
        return 1

    prices = InvoicedPrices.from_proposals(proposals)

    init_db()
    with session_scope() as session:
        existing = {i.name_norm for i in session.query(CatalogItem).all()}
        print(f"catalogue: {len(existing)} articles")

        additions = []
        for key in prices.names:
            if key in existing:
                continue
            last = prices.exact(key)
            if last is None or last.unit_price <= 0 or not last.unit:
                continue
            additions.append(last)

        additions.sort(key=lambda p: -p.unit_price)
        print(f"missing from the base, priced by a proposal: {len(additions)}\n")
        print(f"{'артикул':<50} {'од':<6} {'ціна':>10}  {'вид':<9} джерело")
        print("-" * 108)
        for last in additions[: args.show]:
            kind = classify_kind(last.name, last.unit, "")
            print(f"{last.name[:48]:<50} {last.unit:<6} {last.unit_price:>10,.2f}  "
                  f"{kind:<9} {last.project[-34:]}")
        if len(additions) > args.show:
            print(f"… ще {len(additions) - args.show}")

        kinds = Counter(classify_kind(a.name, a.unit, "") for a in additions)
        print(f"\nby kind: {dict(kinds)}")

        if not args.apply:
            print("\nDry run. Додати у каталог: --apply")
            return 0

        for last in additions:
            session.add(
                CatalogItem(
                    name=last.name,
                    name_norm=normalize_name(last.name),
                    category="З виданих КП",
                    unit=last.unit,
                    unit_price=last.unit_price,
                    # An invoice records what was charged, never what it cost.
                    # Left at zero on purpose: a guessed cost would make the
                    # margin report lie.
                    unit_cost=0.0,
                    kind=classify_kind(last.name, last.unit, ""),
                    price_updated_at=None,
                    attributes={},
                    source_file=f"{SOURCE_PREFIX}{last.project}",
                    active=True,
                )
            )
        session.commit()
        indexed = rebuild_fts(session)
        total = session.query(CatalogItem).count()
        print(f"\nadded {len(additions)}; catalogue now {total} articles, "
              f"FTS rows {indexed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
