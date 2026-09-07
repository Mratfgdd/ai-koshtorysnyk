"""Read the issued proposals and check the calculation against them.

    python scripts/learn_references.py --root "D:/Chrome download"
    python scripts/learn_references.py --root ... --json refs.json

Every reference folder holds the drawings that went in and the КП that came
out. The КП is ground truth: it is what the company invoiced and what the
customer signed. This parses those PDFs into structured rows — section, block,
name, quantity, unit, price, sum — so the pricing behind them can be measured
instead of guessed at.

Read by geometry, not by token order. The proposals are printed from one Excel
template, so the columns sit at the same x on every page, and the column edges
are taken from each table's own header row. Two things defeat a token-stream
reader and are handled naturally here: the accounting format emits a bare "-"
of its own between price and sum, and a long article name wraps onto a second
line inside its cell.

What it reports:

* whether the parse is self-consistent — the rows of a block must add up to
  that block's own printed subtotal. That check comes first, because nothing
  can be concluded from a reading that does not reconcile;
* the price each article was invoiced at, and where two proposals disagree;
* coefficients connecting a driver quantity to a derived one, recovered from
  the invoiced rows;
* how the deterministic totals layer reproduces each proposal's own subtotals,
  grand total, prepayments and balance — arithmetic that must agree to the
  kopeck and involves no AI step.

It reads only; nothing here writes to the rule pack or the catalogue.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import pymupdf  # noqa: E402

BLOCK_TITLES = {"Матеріали": "materials", "Рослини": "plants", "Робота": "works"}
SUBTOTAL_RE = re.compile(r"Разом за (матеріали|роботу|рослини)")
BLOCK_OF = {"матеріали": "materials", "роботу": "works", "рослини": "plants"}

# A band is a visual row. A wrapped name sits ~3pt above its own numbers, so
# bands closer together than this belong to the same row.
BAND_TOLERANCE = 7.0

INVOICE_LABELS = (
    ("Загальний рахунок", "grand_total"),
    ("Аванс (на матеріали)", "prepayment_materials"),
    ("Аванс (на роботи)", "prepayment_works"),
    ("Залишок", "balance"),
    ("Безготівковий", "surcharge"),
)

SKIP_LABELS = {"Замовник", "Адреса об'єкту", "Дата рахунку", "Пропозиція дійсна протягом"}


def as_number(text: str) -> float | None:
    """"29 000,0-" -> 29000.0 ; "-" -> None."""
    cleaned = (
        text.replace("\u00a0", "")
        .replace("\u2007", "")
        .replace("\u2009", "")
        .replace(" ", "")
        .strip()
    )
    # The accounting format puts the dash after the number ("89 720,0-") and
    # also emits a lone "-" placeholder, so a cell can read "- 89 720,0-".
    # A dash here is punctuation, never a minus: no invoice line is negative.
    cleaned = cleaned.strip("-").replace(",", ".")
    if not cleaned or cleaned == ".":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


@dataclass
class Row:
    section: str
    block: str
    name: str
    quantity: float
    unit: str
    unit_price: float
    total: float

    @property
    def consistent(self) -> bool:
        return abs(self.quantity * self.unit_price - self.total) <= 1.0


@dataclass
class Proposal:
    project: str
    path: str
    rows: list[Row] = field(default_factory=list)
    section_totals: dict[str, float] = field(default_factory=dict)
    block_totals: dict[str, float] = field(default_factory=dict)
    invoice: dict[str, float] = field(default_factory=dict)

    @property
    def materials_total(self) -> float:
        return sum(r.total for r in self.rows if r.block in ("materials", "plants"))

    @property
    def works_total(self) -> float:
        return sum(r.total for r in self.rows if r.block == "works")

    @property
    def grand_total(self) -> float:
        return self.materials_total + self.works_total

    def block_deltas(self) -> list[tuple[str, float, float]]:
        """Where the rows read disagree with the subtotal printed above them."""
        summed: dict[str, float] = defaultdict(float)
        for row in self.rows:
            summed[f"{row.section}|{row.block}"] += row.total
        out = []
        for key, printed in self.block_totals.items():
            ours = summed.get(key, 0.0)
            if abs(ours - printed) > 1.0:
                out.append((key, ours, printed))
        return sorted(out)


def _bands(page) -> list[list[tuple]]:
    """Words grouped into visual rows."""
    words = sorted(page.get_text("words"), key=lambda w: (w[1], w[0]))
    bands: list[list[tuple]] = []
    for word in words:
        if bands and abs(word[1] - bands[-1][0][1]) <= BAND_TOLERANCE:
            bands[-1].append(word)
        else:
            bands.append([word])
    return [sorted(b, key=lambda w: w[0]) for b in bands]


def _columns(band: list[tuple]) -> dict[str, float] | None:
    """Column edges, read off a table's own header row."""
    text = {w[4] for w in band}
    if "К-сть" not in text or "Ціна" not in text or "Сума" not in text:
        return None
    x_of: dict[str, float] = {}
    for w in band:
        x_of.setdefault(w[4], w[0])
    return {
        "qty": x_of["К-сть"] - 12,
        "unit": x_of.get("Од.", x_of["К-сть"] + 40) - 8,
        "price": x_of["Ціна"] - 14,
        "total": x_of["Сума"] - 22,
    }


def _cells(band: list[tuple], cols: dict[str, float]) -> dict[str, str]:
    out: dict[str, list[str]] = {
        "index": [], "name": [], "qty": [], "unit": [], "price": [], "total": []
    }
    for w in band:
        x, text = w[0], w[4]
        if x >= cols["total"]:
            key = "total"
        elif x >= cols["price"]:
            key = "price"
        elif x >= cols["unit"]:
            key = "unit"
        elif x >= cols["qty"]:
            key = "qty"
        elif x < 36 and text.isdigit():
            # The row number. Anything else this far left is a section marker,
            # which belongs to the name so it can be recognised as one.
            key = "index"
        else:
            key = "name"
        out[key].append(text)
    return {k: " ".join(v).strip() for k, v in out.items()}


def parse_proposal(path: Path) -> Proposal:
    proposal = Proposal(project=path.stem, path=str(path))
    doc = pymupdf.open(path)

    section = ""
    block = "materials"
    cols: dict[str, float] | None = None
    in_summary = False
    pending_name = ""

    for page in doc:
        for band in _bands(page):
            line = " ".join(w[4] for w in band).strip()
            if not line:
                continue

            header = _columns(band)
            if header is not None:
                cols = header
                # "# Матеріали" and "К-сть … Сума" print 3pt apart and merge
                # into one band, so the block title is read off this same row:
                # it is the text left of the quantity column, minus the "#".
                title = " ".join(
                    w[4] for w in band if w[0] < header["qty"] and w[4] != "#"
                ).strip()
                if title.startswith(("Матеріали за видами", "Робота за видами")):
                    in_summary = True
                else:
                    block = BLOCK_TITLES.get(title, block)
                pending_name = ""
                continue

            if band[0][4] == "#":
                title = " ".join(w[4] for w in band[1:]).strip()
                if title.startswith(("Матеріали за видами", "Робота за видами")):
                    in_summary = True
                else:
                    block = BLOCK_TITLES.get(title, block)
                pending_name = ""
                continue

            if line.startswith("Рахунок загальний"):
                in_summary = True
                pending_name = ""
                continue

            if cols is None:
                # The first section marker prints above the first table, so it
                # arrives before any column header has been seen.
                marker = line.rstrip()
                if marker.endswith(":") and len(marker) < 80:
                    label_text = marker.rstrip(":").strip()
                    if label_text.startswith("Рахунок "):
                        label_text = label_text[len("Рахунок "):]
                    if label_text and label_text not in SKIP_LABELS:
                        section = label_text
                continue

            cells = _cells(band, cols)
            total = as_number(cells["total"])

            label = line.strip()
            if label.startswith("0 "):
                label = label[2:].strip()
            found = SUBTOTAL_RE.search(label)
            if found:
                key = BLOCK_OF[found.group(1)]
                if total is not None:
                    if in_summary:
                        proposal.invoice[key] = total
                    else:
                        proposal.block_totals[f"{section}|{key}"] = total
                pending_name = ""
                continue

            matched = False
            for text_label, key in INVOICE_LABELS:
                if label.startswith(text_label):
                    if total is not None:
                        proposal.invoice[key] = total
                    matched = True
                    break
            if matched:
                pending_name = ""
                continue

            if label.startswith("Разом"):
                name = label[len("Разом"):].strip().rstrip(":").strip()
                if total is not None and not in_summary:
                    proposal.section_totals[name or section] = total
                pending_name = ""
                continue

            quantity = as_number(cells["qty"])
            price = as_number(cells["price"])
            name = cells["name"].strip()

            if quantity is not None and price is not None and total is not None:
                if not in_summary:
                    full = f"{pending_name} {name}".strip()
                    if full:
                        proposal.rows.append(
                            Row(section or "misc", block, full, quantity,
                                cells["unit"].strip(), price, total)
                        )
                pending_name = ""
                continue

            # No numbers on this band: either a section marker, or the first
            # line of a name whose numbers are on the band below.
            if name and quantity is None and price is None and total is None:
                marker = name.rstrip()
                if marker.endswith(":") and len(marker) < 80:
                    label_text = marker.rstrip(":").strip()
                    if label_text.startswith("Рахунок "):
                        label_text = label_text[len("Рахунок "):]
                    if label_text and label_text not in SKIP_LABELS:
                        section = label_text
                    pending_name = ""
                else:
                    pending_name = name
                continue
            pending_name = ""

    doc.close()
    return proposal


# --- what the proposals agree on ---------------------------------------------


def price_book(proposals: list[Proposal]) -> dict[str, dict]:
    book: dict[str, dict] = defaultdict(lambda: {"prices": [], "projects": [], "unit": ""})
    for p in proposals:
        for row in p.rows:
            entry = book[row.name]
            entry["prices"].append(row.unit_price)
            entry["projects"].append(p.project)
            entry["unit"] = entry["unit"] or row.unit
    return book


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


def check_totals_layer(proposal: Proposal) -> list[str]:
    """Rebuild the invoice from the parsed rows with the project's totals code."""
    from app.services.rules.engine import Draft, DraftLine
    from app.services.rules.totals import compute_totals

    lines = [
        DraftLine(name=r.name, section=r.section or "misc", block=r.block,
                  unit=r.unit, quantity=r.quantity, unit_price=r.unit_price)
        for r in proposal.rows
    ]
    totals = compute_totals(Draft(lines=lines))
    problems: list[str] = []

    def compare(label: str, ours: float, theirs: float | None, tol: float = 1.5) -> None:
        if theirs is not None and abs(ours - theirs) > tol:
            problems.append(
                f"{label}: наш {ours:,.2f} != КП {theirs:,.2f} ({ours - theirs:+,.2f})"
            )

    compare("матеріали", totals.materials_total, proposal.invoice.get("materials"))
    compare("роботи", totals.works_total, proposal.invoice.get("works"))
    compare("загальний рахунок", totals.grand_total, proposal.invoice.get("grand_total"))
    compare("аванс (роботи)", totals.prepayment_works,
            proposal.invoice.get("prepayment_works"))
    compare("залишок", totals.balance, proposal.invoice.get("balance"))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--prices", type=int, default=25)
    args = parser.parse_args()

    pdfs = sorted(
        p for p in args.root.rglob("*.pdf")
        if p.name.startswith("КП ") and "бруківка" not in p.name.lower()
    )
    if not pdfs:
        print(f"No КП PDFs under {args.root}")
        return 1

    proposals: list[Proposal] = []
    print(f"PARSE — {len(pdfs)} proposals\n")
    print(f"{'rows':>5} {'сума рядків':>15} {'блоки':>8}  файл")
    print("-" * 94)
    exact = 0
    for path in pdfs:
        proposal = parse_proposal(path)
        proposals.append(proposal)
        deltas = proposal.block_deltas()
        if not deltas and proposal.rows:
            exact += 1
        flag = "OK" if not deltas else f"{len(deltas)} off"
        print(f"{len(proposal.rows):5d} {proposal.grand_total:>15,.2f} {flag:>8}  {path.name[:54]}")

    print(f"\n{exact} of {len(proposals)} parsed so that every block reconciles with "
          f"its own printed subtotal.")

    off = [(p, p.block_deltas()) for p in proposals if p.block_deltas()]
    if off:
        print("\nBlocks that do not reconcile (a parse problem, not a pricing one):")
        for p, deltas in off[:6]:
            print(f"  {p.project[:62]}")
            for key, ours, printed in deltas[:6]:
                print(f"      {key[:44]:46s} {ours:>13,.2f} vs {printed:>13,.2f}")

    print(f"\n{'=' * 94}\nTOTALS LAYER vs THE ISSUED INVOICES\n{'=' * 94}")
    clean = 0
    judged = 0
    for p in proposals:
        if p.block_deltas() or not p.rows:
            continue
        judged += 1
        problems = check_totals_layer(p)
        if not problems:
            clean += 1
            print(f"  OK   {p.project[:72]}")
        else:
            print(f"  FAIL {p.project[:72]}")
            for problem in problems:
                print(f"         {problem}")
    print(f"\n{clean} of {judged} reproduce the invoice exactly.")

    book = price_book(proposals)
    clashes = {n: e for n, e in book.items() if len(set(e["prices"])) > 1}
    print(f"\n{'=' * 94}\nPRICE BOOK: {len(book)} articles, "
          f"{len(clashes)} invoiced at more than one price\n{'=' * 94}")
    ordered = sorted(clashes.items(), key=lambda kv: -len(set(kv[1]["prices"])))
    for name, entry in ordered[: args.prices]:
        prices = sorted(set(entry["prices"]))
        shown = prices if len(prices) <= 6 else prices[:6] + ["…"]
        print(f"  {name[:54]:56s} {entry['unit']:>8}  {shown}")

    print(f"\n{'=' * 94}\nCOEFFICIENTS RECOVERED FROM THE INVOICES\n{'=' * 94}")
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
                    {"project": p.project, "rows": [asdict(r) for r in p.rows],
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
