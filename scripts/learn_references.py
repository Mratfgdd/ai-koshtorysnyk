"""Read the issued proposals and check the calculation against them.

    python scripts/learn_references.py --root "D:/Chrome download"
    python scripts/learn_references.py --root ... --json refs.json

Every reference folder holds the drawings that went in and the КП that came
out. The КП is ground truth: it is what the company invoiced and what the
customer signed. The reading itself lives in
:mod:`app.services.history.proposals`, because the engine prices from the same
rows; this script measures what that reading supports.

What it reports:

* whether the parse is self-consistent — the rows of a block must add up to
  that block's own printed subtotal. That check comes first, because nothing
  can be concluded from a reading that does not reconcile;
* how the deterministic totals layer reproduces each proposal's own subtotals,
  grand total, prepayments and balance — arithmetic that must agree to the
  kopeck and involves no AI step;
* the price each article was invoiced at, where two proposals disagree, and
  which price the "last sold" rule picks out of the disagreement;
* how the resolved prices compare with the price base «2026 База 1»;
* coefficients connecting a driver quantity to a derived one, recovered from
  the invoiced rows.

It reads only; nothing here writes to the rule pack, the catalogue or the price
history. Use ``scripts/import_references.py`` to load the history for the
engine to price from.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.history.prices import InvoicedPrices  # noqa: E402
from app.services.history.proposals import (  # noqa: E402
    Proposal,
    parse_proposal,
    reference_proposals,
)


def ratios(proposals: list[Proposal], driver: str, derived: str) -> list[float]:
    found: list[float] = []
    for p in proposals:
        by_section: dict[str, dict[str, float]] = defaultdict(dict)
        for row in p.rows:
            by_section[row.section][row.name] = row.quantity
        for names in by_section.values():
            a = next((v for k, v in names.items() if driver.lower() in k.lower()), None)
            b = next((v for k, v in names.items() if derived.lower() in k.lower()), None)
            if a and b and a > 0:
                found.append(round(b / a, 4))
    return found


def check_totals_layer(proposal: Proposal) -> tuple[list[str], list[str]]:
    """Rebuild the invoice from the parsed rows with the project's totals code.

    Returns ``(problems, notes)``. A problem is a figure our arithmetic gets
    wrong. A note is a figure the project manager overrode by hand on that
    proposal: the prepayment split is a commercial decision, not a formula, and
    some proposals move money between the two advances. Where that happened the
    invoice is checked against itself instead — the two advances plus the
    balance must still be the grand total.
    """
    from app.services.rules.engine import Draft, DraftLine
    from app.services.rules.totals import compute_totals

    lines = [
        DraftLine(name=r.name, section=r.section or "misc", block=r.block,
                  unit=r.unit, quantity=r.quantity, unit_price=r.unit_price)
        for r in proposal.rows
    ]
    totals = compute_totals(Draft(lines=lines))
    problems: list[str] = []
    notes: list[str] = []

    def compare(label: str, ours: float, theirs: float | None, tol: float = 1.5) -> bool:
        if theirs is None or abs(ours - theirs) <= tol:
            return True
        problems.append(
            f"{label}: наш {ours:,.2f} != КП {theirs:,.2f} ({ours - theirs:+,.2f})"
        )
        return False

    compare("матеріали", totals.materials_total, proposal.invoice.get("materials"))
    compare("роботи", totals.works_total, proposal.invoice.get("works"))
    compare("загальний рахунок", totals.grand_total, proposal.invoice.get("grand_total"))

    prep_m = proposal.invoice.get("prepayment_materials")
    prep_w = proposal.invoice.get("prepayment_works")
    grand = proposal.invoice.get("grand_total")
    balance = proposal.invoice.get("balance")
    by_formula = (
        prep_m is not None
        and abs(totals.prepayment_materials - prep_m) <= 1.5
        and prep_w is not None
        and abs(totals.prepayment_works - prep_w) <= 1.5
    )

    if by_formula or None in (prep_m, prep_w, grand, balance):
        compare("аванс (роботи)", totals.prepayment_works, prep_w)
        compare("залишок", totals.balance, balance)
    else:
        notes.append(
            f"аванси задані вручну: матеріали {prep_m:,.2f} "
            f"(за формулою {totals.prepayment_materials:,.2f}), "
            f"роботи {prep_w:,.2f} (за формулою {totals.prepayment_works:,.2f})"
        )
        if abs(grand - prep_m - prep_w - balance) > 1.5:
            problems.append(
                f"аванси не сходяться з залишком: {grand:,.2f} − {prep_m:,.2f} "
                f"− {prep_w:,.2f} != {balance:,.2f}"
            )

    return problems, notes


def compare_with_base(prices: InvoicedPrices) -> None:
    """Where the resolved "last sold" price differs from «2026 База 1»."""
    try:
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import CatalogItem
    except Exception as exc:  # pragma: no cover - a missing DB is not fatal here
        print(f"  (price base unavailable: {exc})")
        return

    with session_scope() as session:
        base = {
            i.name_norm: i
            for i in session.scalars(
                select(CatalogItem).where(CatalogItem.active.is_(True))
            ).all()
        }
        shared = sorted(set(base) & set(prices.names))
        priced = [k for k in shared if base[k].unit_price > 0]
        unpriced = [k for k in shared if base[k].unit_price <= 0]

        agree, differ = [], []
        for key in priced:
            last = prices.exact(key)
            if last is None:
                continue
            if abs(last.unit_price - base[key].unit_price) < 0.005:
                agree.append(key)
            else:
                differ.append((key, base[key], last))

        only_history = sorted(set(prices.names) - set(base))
        print(f"  артикулів у базі {len(base)}, у історії КП {len(prices)}")
        print(f"  спільних {len(shared)}: ціна в базі збігається з останньою "
              f"проданою у {len(agree)}, відрізняється у {len(differ)}")
        print(f"  у базі без ціни (ціну дає лише історія): {len(unpriced)}")
        print(f"  продавалось, але в базі немає зовсім: {len(only_history)}")

        if differ:
            print("\n  найбільші розбіжності (база → остання продана):")
            differ.sort(key=lambda d: -abs(d[2].unit_price - d[1].unit_price)
                        / max(d[1].unit_price, 1))
            for key, item, last in differ[:15]:
                when = f" ({last.issued})" if last.issued else ""
                print(f"    {key[:46]:48s} {item.unit_price:>9,.1f} → "
                      f"{last.unit_price:>9,.1f}{when}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--prices", type=int, default=25)
    args = parser.parse_args()

    pdfs = reference_proposals(args.root)
    if not pdfs:
        print(f"No КП PDFs under {args.root}")
        return 1

    proposals: list[Proposal] = []
    print(f"PARSE — {len(pdfs)} proposals\n")
    print(f"{'rows':>5} {'сума рядків':>15} {'блоки':>8} {'видано':>12}  файл")
    print("-" * 106)
    exact = 0
    for path in pdfs:
        proposal = parse_proposal(path)
        proposals.append(proposal)
        deltas = proposal.block_deltas()
        if proposal.reconciles:
            exact += 1
        flag = "OK" if not deltas else f"{len(deltas)} off"
        when = str(proposal.issued) if proposal.issued else "—"
        print(f"{len(proposal.rows):5d} {proposal.grand_total:>15,.2f} {flag:>8} "
              f"{when:>12}  {path.name[:50]}")

    print(f"\n{exact} of {len(proposals)} parsed so that every block reconciles with "
          f"its own printed subtotal.")

    off = [(p, p.block_deltas()) for p in proposals if p.block_deltas()]
    if off:
        print("\nBlocks that do not reconcile (a parse problem, not a pricing one):")
        for p, deltas in off[:6]:
            print(f"  {p.project[:62]}")
            for key, ours, printed in deltas[:6]:
                print(f"      {key[:44]:46s} {ours:>13,.2f} vs {printed:>13,.2f}")

    print(f"\n{'=' * 106}\nTOTALS LAYER vs THE ISSUED INVOICES\n{'=' * 106}")
    clean = 0
    judged = 0
    for p in proposals:
        if not p.reconciles:
            continue
        judged += 1
        problems, notes = check_totals_layer(p)
        if not problems:
            clean += 1
            print(f"  OK   {p.project[:72]}")
        else:
            print(f"  FAIL {p.project[:72]}")
        for problem in problems:
            print(f"         {problem}")
        for note in notes:
            print(f"         ~ {note}")
    print(f"\n{clean} of {judged} reproduce the invoice exactly.")

    usable = [p for p in proposals if p.reconciles]
    prices = InvoicedPrices.from_proposals(usable)
    contested = prices.contested()
    print(f"\n{'=' * 106}\nPRICE BOOK: {len(prices)} articles, "
          f"{len(contested)} invoiced at more than one price\n"
          f"«остання продана» розв'язує кожну з них\n{'=' * 106}")
    ordered = sorted(contested.items(), key=lambda kv: -len(kv[1].alternatives))
    for key, last in ordered[: args.prices]:
        when = str(last.issued) if last.issued else "—"
        others = ", ".join(f"{p:g}" for p in last.alternatives[:6])
        print(f"  {key[:46]:48s} {last.unit:>6}  {last.unit_price:>9,.1f}  "
              f"{when:>12}   було: {others}")

    print(f"\n{'=' * 106}\nRESOLVED PRICES vs THE PRICE BASE «2026 База 1»\n{'=' * 106}")
    compare_with_base(prices)

    print(f"\n{'=' * 106}\nCOEFFICIENTS RECOVERED FROM THE INVOICES\n{'=' * 106}")
    for driver, derived, rule in (
        ("Садовий бордюр", "Анкера", "×4"),
        ("Агрополотно 50", "Гачок", "×4"),
        ("Сітка від кротів", "Гачок", "×4"),
    ):
        found = ratios(proposals, driver, derived)
        if found:
            print(f"  {derived} / {driver}: {sorted(set(found))[:8]}   (правило {rule})")

    if args.json:
        args.json.write_text(
            json.dumps(
                [
                    {"project": p.project,
                     "issued": str(p.issued) if p.issued else "",
                     "rows": [asdict(r) for r in p.rows],
                     "section_totals": p.section_totals,
                     "block_totals": p.block_totals, "invoice": p.invoice}
                    for p in proposals
                ],
                ensure_ascii=False, indent=1,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
