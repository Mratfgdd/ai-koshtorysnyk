"""Measure a built estimate against the proposal that was actually issued.

    python scripts/compare_reference.py --root "D:/Chrome download"
    python scripts/compare_reference.py --root ... --keep-self

The object is Будьків: the drawings that produced ``scripts/demo.py``'s inputs
went to the customer as a КП totalling 741 560,00 грн, so that КП is the answer
sheet for everything downstream of those inputs.

The estimate is built twice — once priced from «2026 База 1» alone, once with
the client's rule that an article costs what it last sold for — and both are
laid against the signed proposal section by section. Two figures move the total
and they are reported apart, because they have nothing to do with each other:

* **prices**, which the rule changes;
* **coverage**, the rows the КП carries that the drawings never stated and no
  rule can derive. A missing row is not a pricing error and must not be scored
  as one.

By default Будьків's own proposal is dropped from the price history for the
run. Pricing a project from its own signed КП would reproduce that КП by
construction and prove nothing. ``--keep-self`` leaves it in, which is what a
new object would actually get.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from rapidfuzz import fuzz, process  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import CatalogItem  # noqa: E402
from app.services.estimate.builder import (  # noqa: E402
    BuildResult,
    EstimateBuilder,
    TemplateLayout,
    visible_lines,
)
from app.services.history.prices import InvoicedPrices  # noqa: E402
from app.services.history.proposals import (  # noqa: E402
    Proposal,
    parse_proposal,
    reference_proposals,
)
from app.services.rules.engine import normalize_name  # noqa: E402
from demo import OPTIONS, PLANTS, QUANTITIES, SECTIONS  # noqa: E402

PROJECT = "Будьків"

# The КП names its sections in prose; the template keys them. Same sections.
SECTION_OF = {
    "prep": ("Підготовчий етап",),
    "pathway": ("Доріжка",),
    "planting": ("озеленення", "Озеленення"),
    "lawn": ("Газон",),
}


def money(v: float) -> str:
    return f"{v:>14,.2f}".replace(",", " ")


def signed(v: float) -> str:
    return f"{v:>+14,.2f}".replace(",", " ")


def invoiced_blocks(proposal: Proposal) -> dict[tuple[str, str], float]:
    """The КП's own figures, keyed the way the builder keys its sections."""
    by_key: dict[tuple[str, str], float] = defaultdict(float)
    for row in proposal.rows:
        for key, titles in SECTION_OF.items():
            if row.section in titles:
                block = "materials" if row.block in ("materials", "plants") else "works"
                by_key[(key, block)] += row.total
                break
    return dict(by_key)


def built_blocks(built: BuildResult) -> dict[tuple[str, str], float]:
    by_key: dict[tuple[str, str], float] = defaultdict(float)
    for line in visible_lines(built.draft):
        block = "materials" if line.block in ("materials", "plants") else "works"
        by_key[(line.section, block)] += line.total
    return dict(by_key)


def build(session, layout, prices: InvoicedPrices | None, *, use: bool) -> BuildResult:
    builder = EstimateBuilder(session, layout, use_invoiced_prices=use, prices=prices)
    return builder.build(
        sections=SECTIONS, quantities=QUANTITIES, plants=PLANTS, options=OPTIONS
    )


def coverage(proposal: Proposal, built: BuildResult) -> tuple[list, list]:
    """Rows the КП has and we do not, and the other way round.

    Paired on the name, and then once more with the tolerance the price lookup
    uses, because the same article is worded slightly differently in a drawing
    and in an invoice. Without that second pass one row shows up in both lists
    and both look worse than they are.
    """
    ours = {normalize_name(l.name): l for l in visible_lines(built.draft)}
    theirs = {normalize_name(r.name): r for r in proposal.rows}

    missing = [key for key in theirs if key not in ours]
    extra = [key for key in ours if key not in theirs]
    for key in list(missing):
        near = process.extractOne(key, extra, scorer=fuzz.token_set_ratio)
        if near and near[1] >= 92.0:
            missing.remove(key)
            extra.remove(near[0])

    return (
        sorted((theirs[k] for k in missing), key=lambda r: -r.total),
        sorted((ours[k] for k in extra), key=lambda l: -l.total),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--keep-self", action="store_true",
                        help="leave this project's own КП in the price history")
    parser.add_argument("--lines", type=int, default=12)
    args = parser.parse_args()

    paths = [p for p in reference_proposals(args.root) if PROJECT in p.name]
    if not paths:
        print(f"No КП for «{PROJECT}» under {args.root}")
        return 1
    proposal = parse_proposal(paths[0])
    if not proposal.reconciles:
        print(f"The КП for «{PROJECT}» does not reconcile; nothing can be measured.")
        return 1

    init_db()
    session = SessionLocal()
    if session.query(CatalogItem).count() == 0:
        print("Каталог порожній: спочатку python scripts/import_catalog.py <Шаблон>")
        return 1
    layout = TemplateLayout.load()

    exclude = () if args.keep_self else (PROJECT,)
    prices = InvoicedPrices.from_db(session, exclude=exclude)
    if len(prices) == 0:
        print("Історія КП порожня: спочатку python scripts/import_references.py <тека>")
        return 1

    base_only = build(session, layout, None, use=False)
    last_sold = build(session, layout, prices, use=True)

    kept = "разом із власним КП" if args.keep_self else f"без власного КП «{PROJECT}»"
    print("=" * 100)
    print(f"«{PROJECT}» проти підписаного КП — {proposal.invoice.get('grand_total', 0):,.2f} грн"
          .replace(",", " "))
    print("=" * 100)
    print(f"історія цін: {len(prices)} артикулів, {kept}")
    print(f"рядків у КП: {len(proposal.rows)}   у нашому кошторисі: "
          f"{len(visible_lines(last_sold.draft))}")

    theirs = invoiced_blocks(proposal)
    a, b = built_blocks(base_only), built_blocks(last_sold)

    print(f"\n{'секція':<26} {'блок':<11} {'КП':>15} {'лише база':>15} "
          f"{'остання ціна':>15} {'різниця до КП':>15}")
    print("-" * 100)
    for key in SECTION_OF:
        for block, title in (("materials", "матеріали"), ("works", "роботи")):
            invoice = theirs.get((key, block), 0.0)
            if not invoice and not a.get((key, block)) and not b.get((key, block)):
                continue
            print(f"{layout.title(key)[:24]:<26} {title:<11} {money(invoice)} "
                  f"{money(a.get((key, block), 0.0))} {money(b.get((key, block), 0.0))} "
                  f"{signed(b.get((key, block), 0.0) - invoice)}")

    print("-" * 100)
    invoice_total = proposal.invoice.get("grand_total", proposal.grand_total)
    for label, result, built_map in (("лише база «2026 База 1»", base_only, a),
                                     ("остання продана ціна", last_sold, b)):
        total = result.totals.grand_total
        share = total / invoice_total if invoice_total else 0.0
        # The blocks are over and under the КП by turns, so the bottom line is
        # closer to it than the blocks are. Both numbers, or the netting reads
        # as accuracy it has not earned.
        gross = sum(
            abs(built_map.get(key, 0.0) - invoice)
            for key, invoice in theirs.items()
        )
        print(f"{label:<38} {money(total)}   {signed(total - invoice_total)}   "
              f"{share:>7.1%} від КП   |різниця по блоках| {money(gross)}")

    moved = last_sold.totals.grand_total - base_only.totals.grand_total
    print(f"\nправило «остання продана ціна» зрушило підсумок на {signed(moved).strip()} грн "
          f"на {len(last_sold.repriced)} позиціях")

    if last_sold.repriced:
        print(f"\n{'позиція':<48} {'база':>11} {'остання':>11} {'КП':>14}  видано")
        priced = sorted(last_sold.repriced,
                        key=lambda r: -abs(r["invoiced_price"] - r["catalog_price"]))
        for entry in priced[: args.lines]:
            print(f"  {entry['name'][:44]:<46} {entry['catalog_price']:>11,.1f} "
                  f"{entry['invoiced_price']:>11,.1f} {entry['project'][-12:]:>14}  "
                  f"{entry['issued']}")

    missing, extra = coverage(proposal, last_sold)
    lost = sum(r.total for r in missing)
    gained = sum(l.total for l in extra)
    print(f"\nПОКРИТТЯ — не ціни, а склад робіт")
    print(f"  у КП є, у нас немає : {len(missing)} позицій на {money(lost).strip()} грн")
    for row in missing[: args.lines]:
        print(f"      {row.name[:56]:<58} {money(row.total)}   {row.section}")
    print(f"  у нас є, у КП немає : {len(extra)} позицій на {money(gained).strip()} грн")
    for line in extra[: args.lines]:
        print(f"      {line.name[:56]:<58} {money(line.total)}   {line.section}")

    print(f"\nЯкби склад робіт збігався, розбіжність склала б "
          f"{signed(last_sold.totals.grand_total + lost - gained - invoice_total).strip()} грн.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
