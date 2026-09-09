"""Read an issued proposal (КП) back into structured rows.

The КП is ground truth: it is what the company invoiced and what the customer
signed. Reading it turns a folder of PDFs into a price history — every article,
the quantity billed, the rate charged and the proposal it came from — which is
what the pricing rule in :mod:`.prices` runs on.

Read by geometry, not by token order. The proposals are printed from one Excel
template, so the columns sit at the same x on every page, and the column edges
are taken from each table's own header row — which is why proposals written by
different project managers all parse. Two things defeat a token-stream reader
and are handled naturally here: the accounting format emits a bare "-" of its
own between price and sum, and a long article name wraps onto a second line
inside its cell.

The parse checks itself: :meth:`Proposal.block_deltas` compares the rows of
each block against the subtotal printed above them. Nothing may be concluded
from a reading that does not reconcile.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

SUBTOTAL_RE = re.compile(r"Разом за (матеріали|роботу|рослини)")
BLOCK_OF = {"матеріали": "materials", "роботу": "works", "рослини": "plants"}

# A section is not always one table per block. "Автоматичний полив" carries a
# second pair of tables for the pump station, titled "Насосна станція
# (матеріали)" and "Насосна станція (робота)", and a table split by a page break
# repeats its header. So the block is read from whatever the title says about
# it, not from an exact title match.
BLOCK_MARKERS = (("матеріал", "materials"), ("робот", "works"), ("рослин", "plants"))

# Roll-ups of such a sub-table. They print like a section total ("Разом за
# насосну станцію") but close a group inside a section, so they must not be
# recorded as one.
SUBGROUP_TOTALS = ("насосну станцію", "насосна станція")

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


def block_of_title(title: str) -> str | None:
    low = title.lower()
    for marker, block in BLOCK_MARKERS:
        if marker in low:
            return block
    return None


def as_number(text: str) -> float | None:
    """"29 000,0-" -> 29000.0 ; "-" -> None."""
    # The thousands separator is a space, and the sheet uses several of
    # them: ordinary, non-breaking (U+00A0), figure (U+2007) and thin (U+2009).
    cleaned = re.sub(r"[\s\u00a0\u2007\u2009]", "", text)
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


# --- when a proposal was issued ----------------------------------------------

# The printed КП leaves "Дата рахунку" blank and the PDFs carry no metadata, so
# the only date these documents hold is the one in their own filename.
EXPLICIT_DATE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](20\d{2})")
QUARTER_RE = re.compile(r"\((\d)\s*кв[а-яі]*\)", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(20\d{2})\b")

# The last day of each quarter: a proposal is dated no later than this, and two
# proposals from the same quarter get the same key, which is the truth here.
QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


@dataclass(frozen=True)
class IssuedOn:
    """When a proposal was issued, and how precisely that is known."""

    date: dt.date
    precision: str  # day | quarter

    def __str__(self) -> str:
        if self.precision == "quarter":
            return f"{(self.date.month - 1) // 3 + 1} кв. {self.date.year}"
        return self.date.strftime("%d.%m.%Y")


def issued_on(name: str) -> IssuedOn | None:
    """Date a proposal from its filename. ``None`` when it carries no date."""
    found = EXPLICIT_DATE_RE.search(name)
    if found:
        day, month, year = (int(g) for g in found.groups())
        try:
            return IssuedOn(dt.date(year, month, day), "day")
        except ValueError:
            pass
    quarter = QUARTER_RE.search(name)
    year_found = YEAR_RE.search(name)
    if quarter and year_found:
        q = int(quarter.group(1))
        if q in QUARTER_END:
            month, day = QUARTER_END[q]
            return IssuedOn(dt.date(int(year_found.group(1)), month, day), "quarter")
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
    def issued(self) -> IssuedOn | None:
        return issued_on(self.project)

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

    @property
    def reconciles(self) -> bool:
        return bool(self.rows) and not self.block_deltas()


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
        # The row-number column, read off the "#" of this table's own header.
        # It was pinned at x < 36, which is where it sits on most of these
        # proposals; Катерина's is printed on a wider sheet with "#" at 108, so
        # every row number was read as the first word of the article and 242 of
        # its 248 rows came out as "35 Доставка сипучих матеріалів".
        "index": x_of.get("#", 24.0) + 12,
        "qty": x_of["К-сть"] - 12,
        "unit": x_of.get("Од.", x_of["К-сть"] + 40) - 8,
        "price": x_of["Ціна"] - 14,
        "total": x_of["Сума"] - 22,
    }


def _cells(band: list[tuple], cols: dict[str, float]) -> dict[str, str]:
    out: dict[str, list[str]] = {
        "index": [], "name": [], "qty": [], "unit": [], "price": [], "total": [],
        "label": [],
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
        elif x < cols["index"] and text.isdigit():
            # The row number. Anything else this far left is a section marker,
            # which belongs to the name so it can be recognised as one.
            key = "index"
        else:
            key = "name"
        out[key].append(text)
        if x < cols["price"]:
            # Everything the row says before its figures. A subtotal line prints
            # its wording right-aligned against the quantity column, so it lands
            # in "qty" and "unit" rather than in "name" — but never as far right
            # as the price.
            out["label"].append(text)
    return {k: " ".join(v).strip() for k, v in out.items()}


def _section_marker(text: str) -> str | None:
    """"Рахунок Освітлення:" -> "Освітлення". ``None`` when it is not one."""
    marker = text.rstrip()
    if not marker.endswith(":") or len(marker) >= 80:
        return None
    label = marker.rstrip(":").strip()
    if label.startswith("Рахунок "):
        label = label[len("Рахунок "):]
    if not label or label in SKIP_LABELS:
        return None
    return label


def parse_proposal(path: Path) -> Proposal:
    """Read one issued proposal into rows, subtotals and invoice figures.

    The document is held open for the length of the parse and closed by the
    context manager. It used to be closed by a plain call at the end, which any
    exception in the loop below would skip -- and a PDF left open is a file that
    cannot be replaced or deleted on Windows.
    """
    proposal = Proposal(project=path.stem, path=str(path))
    with pymupdf.open(path) as doc:
        section = ""
        block = "materials"
        cols: dict[str, float] | None = None
        in_summary = False
        pending_name = ""
        # The row a trailing wrapped line would belong to, and where that row sits.
        last_row: Row | None = None
        last_row_y = 0.0

        for page in doc:
            bands = _bands(page)
            last_row = None
            for index, band in enumerate(bands):
                line = " ".join(w[4] for w in band).strip()
                if not line:
                    continue
                y = band[0][1]
                next_y = bands[index + 1][0][1] if index + 1 < len(bands) else None

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
                        block = block_of_title(title) or block
                    pending_name = ""
                    last_row = None
                    continue

                if band[0][4] == "#":
                    title = " ".join(w[4] for w in band[1:]).strip()
                    if title.startswith(("Матеріали за видами", "Робота за видами")):
                        in_summary = True
                    else:
                        block = block_of_title(title) or block
                    pending_name = ""
                    last_row = None
                    continue

                if line.startswith("Рахунок загальний"):
                    in_summary = True
                    pending_name = ""
                    last_row = None
                    continue

                if cols is None:
                    # The first section marker prints above the first table, so it
                    # arrives before any column header has been seen.
                    section = _section_marker(line) or section
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
                            # A section can print this line more than once — once per
                            # sub-table (the pump station) and once per page a long
                            # table spans. Each closes a part of the same block, so
                            # they add up; taking the last would keep only the tail.
                            acc = f"{section}|{key}"
                            proposal.block_totals[acc] = (
                                proposal.block_totals.get(acc, 0.0) + total
                            )
                    pending_name = ""
                    last_row = None
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
                    last_row = None
                    continue

                if label.startswith("Разом"):
                    # From the wording alone, not the whole line: the line carries
                    # its figure too and it would end up inside the key.
                    name = cells["label"].strip()
                    if name.startswith("0 "):
                        name = name[2:].strip()
                    name = name[len("Разом"):].strip().rstrip(":").strip()
                    bare = name[len("за "):].strip() if name.startswith("за ") else name
                    if (
                        total is not None
                        and not in_summary
                        and bare.lower() not in SUBGROUP_TOTALS
                    ):
                        proposal.section_totals[bare or section] = total
                    pending_name = ""
                    last_row = None
                    continue

                quantity = as_number(cells["qty"])
                price = as_number(cells["price"])
                name = cells["name"].strip()

                if quantity is not None and price is not None and total is not None:
                    if not in_summary:
                        full = f"{pending_name} {name}".strip()
                        if full:
                            row = Row(section or "misc", block, full, quantity,
                                      cells["unit"].strip(), price, total)
                            proposal.rows.append(row)
                            last_row, last_row_y = row, y
                    pending_name = ""
                    continue

                # No numbers on this band: a section marker, or a line of a name too
                # long for its cell. A wrapped name can spill either way — above the
                # numbers or below them — so it goes to whichever row it sits closer
                # to, which is where Excel drew it.
                if name and quantity is None and price is None and total is None:
                    marker = _section_marker(name)
                    if marker is not None:
                        section = marker
                        pending_name = ""
                        last_row = None
                    elif name.rstrip().endswith(":") and len(name.rstrip()) < 80:
                        pending_name = ""
                        last_row = None
                    elif (
                        last_row is not None
                        and next_y is not None
                        and (y - last_row_y) < (next_y - y)
                    ):
                        last_row.name = f"{last_row.name} {name}".strip()
                    else:
                        pending_name = name
                    continue
                pending_name = ""

    return proposal


def reference_proposals(root: Path) -> list[Path]:
    """The issued proposals under ``root``, and only those.

    Each reference lives in its own project folder next to the drawings it was
    priced from. Two things share the tree with them and are not ground truth:
    the paving-only variant of a proposal (``… - бруківка``), which restates
    rows already counted in the main one, and whatever this project has itself
    exported into the download folder — those sit loose at the top level.
    A folder downloaded twice yields the same proposal twice, so keep one copy
    of each filename.

    The name may separate "КП" from the rest with a space or an underscore —
    Drive substitutes one for the other on download, and a proposal saved as
    "КП_2026_2_квартал_…" was silently skipped by a check for "КП ".
    """
    found: dict[str, Path] = {}
    for path in sorted(root.rglob("*.pdf")):
        if not re.match(r"КП[ _]", path.name) or "бруківка" in path.name.lower():
            continue
        if path.parent == root:
            continue
        found.setdefault(path.name, path)
    return sorted(found.values())


__all__ = [
    "IssuedOn",
    "Proposal",
    "Row",
    "as_number",
    "block_of_title",
    "issued_on",
    "parse_proposal",
    "reference_proposals",
]
