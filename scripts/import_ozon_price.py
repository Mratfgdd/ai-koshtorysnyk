"""Fill the paving prices in «2026 База 1» from the manufacturer's price list.

    python scripts/import_ozon_price.py price-ozon_02.26.pdf
    python scripts/import_ozon_price.py price-ozon_02.26.pdf --apply

Every OZON paving row of the price base carries no price -- all twenty-one of
them sit at 0,00, the same way the plants do -- so a drawing that names a paving
series produces a line the estimate cannot cost, and the whole section with it.
The prices are in the manufacturer's own PDF, reissued every few months.

Read by geometry, like the issued proposals. Each product block prints its name
at the left margin, then two rows naming the colour groups, then a row reading
"Ціна, грн. за 1 м2" followed by one figure per colour group, then a table whose
first column is the thickness in centimetres. Exclusive series price by
thickness and print a figure per thickness instead; both shapes are handled.

The catalogue keeps one row per series, surface and thickness -- "Бруківка Озон
«Романо Соло» основний колір 8см" -- while the price list gives four prices
across the основна-поверхня group and one for колор-мікс. So a row named
"основний колір" is filled from сірий, the base colour of that group, and the
report prints what the other colours of the group cost so the difference is
visible rather than buried.

Nothing is invented: a row whose series, surface or thickness the price list
does not carry is left at zero and named in the report.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import pymupdf  # noqa: E402

from app.db import init_db, rebuild_fts, session_scope  # noqa: E402
from app.models import CatalogItem  # noqa: E402
from app.services.rules.engine import normalize_name  # noqa: E402

BAND = 4.0
PRICE_RE = re.compile(r"^\d{2,4},\d{2}$")
MONEY_COLUMNS = 7  # what the sheet prints per product: 5 main + 2 washed

# The colour groups, in the order their prices are printed. Taken from the
# header the sheet repeats above every block.
COLUMNS = [
    "сірий",
    "червоний, коричневий, теракотовий, чорний, гірчичний",
    "білий, зелений, жовтий",
    "бежевий",
    "колор-мікс (дюна, азія, каштан, бруней)",
    "мита поверхня: жовтий, чорний, коричневий, сірий, бежевий",
    "мита поверхня: білий, малахіт",
]
GREY, MIX = 0, 4


@dataclass
class Block:
    series: str
    page: int
    # thickness in cm -> list of prices by colour column
    prices: dict[float, list[float | None]]

    def price(self, thickness: float, column: int) -> float | None:
        row = self.prices.get(thickness)
        if row is None or column >= len(row):
            return None
        return row[column]


def _bands(page) -> list[list[tuple]]:
    words = sorted(page.get_text("words"), key=lambda w: (round(w[1], 1), w[0]))
    out: list[list[tuple]] = []
    for w in words:
        if out and abs(w[1] - out[-1][0][1]) <= BAND:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(b, key=lambda w: w[0]) for b in out]


def _money(band: list[tuple]) -> list[float | None]:
    """The price figures of a band, in printed order. "—" reads as no price."""
    out: list[float | None] = []
    for w in band:
        token = w[4].strip()
        if PRICE_RE.match(token):
            out.append(float(token.replace(",", ".")))
        elif token in ("—", "-", "–"):
            out.append(None)
    return out


def _thickness(band: list[tuple]) -> float | None:
    """"6 см" / "4,5 см" at the head of a price row."""
    text = " ".join(w[4] for w in band)
    found = re.search(r"(\d+(?:,\d+)?)\s*см", text)
    return float(found.group(1).replace(",", ".")) if found else None


def read_price_list(path: Path) -> list[Block]:
    """Every product block the sheet prints, with its prices."""
    blocks: list[Block] = []
    with pymupdf.open(path) as doc:
        for pno, page in enumerate(doc):
            current: Block | None = None
            pending_thickness: float | None = None
            for band in _bands(page):
                line = " ".join(w[4] for w in band).strip()
                left = band[0]

                # A product name is the only thing printed at the far left of a
                # block, on the same row as "ОснОвна пОверхня".
                if left[0] < 60 and "поверхня" in line.lower():
                    name = " ".join(
                        w[4] for w in band if w[0] < 200
                    ).strip()
                    if name:
                        current = Block(series=name, page=pno, prices={})
                        blocks.append(current)
                    continue

                if current is None:
                    continue

                money = _money(band)
                if len(money) < MONEY_COLUMNS - 2:
                    continue
                # Either "Ціна, грн. за 1 м2 …" (one thickness for the series)
                # or "6 см …" inside a per-thickness block.
                thickness = _thickness(band)
                if thickness is None and "ціна" in line.lower():
                    thickness = pending_thickness
                if thickness is None:
                    thickness = 0.0  # filled in from the size table below
                current.prices[thickness] = money
    return blocks


# The catalogue names a row "Бруківка Озон «Романо Соло» основний колір 8см …".
CATALOGUE_RE = re.compile(
    r"Бруківка\s+Озон\s+[\"«](?P<series>[^\"»]+)[\"»]\s+"
    r"(?P<surface>основний колір|колір-мікс)\s+"
    r"(?P<thickness>\d+(?:[.,]\d+)?)\s*см",
    re.IGNORECASE,
)


def series_key(name: str) -> str:
    """"Новатор 8L" and "Новатор8L" and "новатор 8 l" are one series."""
    return re.sub(r"[^a-zа-яіїєґ0-9]", "", normalize_name(name))


def candidates_for(
    series: str, thickness: float, by_series: dict[str, list[Block]]
) -> list[Block]:
    """The price-list blocks that could be this catalogue row's series.

    The manufacturer splits a family by suffix where the catalogue keeps one
    name: "Новатор" in the base is "Новатор 6", "Новатор 8", "Новатор 8L" and
    "Новатор Лонг" on the sheet, and "Променада А/В/С" is three blocks. So an
    exact key is tried first, then any block whose key begins with it — and
    among those, one whose suffix names the thickness being asked for wins,
    which is what tells "Новатор 6" from "Новатор 8".
    """
    key = series_key(series)
    if key in by_series:
        return by_series[key]

    # "променадаавс" -> "променада"; the sheet prints А, В and С separately.
    stem = re.sub(r"(?:[авсabc]){2,}$", "", key)
    prefixed = [
        (other, blocks) for other, blocks in by_series.items()
        if other.startswith(key) or (stem and other.startswith(stem))
    ]
    if not prefixed:
        return []
    thick = f"{thickness:g}".replace(".", "")
    exact = [b for other, blocks in prefixed if other.endswith(thick) for b in blocks]
    if exact:
        return exact
    return [b for _other, blocks in prefixed for b in blocks]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    blocks = read_price_list(args.pdf)
    print(f"прочитано блоків прайсу: {len(blocks)}")
    for b in blocks:
        thicknesses = ", ".join(f"{t:g}см" for t in sorted(b.prices) if t)
        print(f"  с.{b.page:2d}  {b.series[:38]:40s} {thicknesses or '—'}")

    by_series: dict[str, list[Block]] = {}
    for b in blocks:
        by_series.setdefault(series_key(b.series), []).append(b)

    init_db()
    with session_scope() as session:
        rows = [
            i for i in session.query(CatalogItem).all()
            if CATALOGUE_RE.search(i.name)
        ]
        print(f"\nрядків бруківки Озон у каталозі: {len(rows)}")

        filled, missed = [], []
        for item in rows:
            found = CATALOGUE_RE.search(item.name)
            series = found.group("series")
            surface = found.group("surface").lower()
            thickness = float(found.group("thickness").replace(",", "."))
            column = MIX if "мікс" in surface else GREY

            candidates = candidates_for(series, thickness, by_series)
            price = None
            for block in candidates:
                price = block.price(thickness, column)
                if price is not None:
                    break
                # A series printed at one thickness only carries it in the size
                # table, not beside the price; accept its single price row.
                if len(block.prices) == 1:
                    only = next(iter(block.prices.values()))
                    if column < len(only) and only[column] is not None:
                        price = only[column]
                        break
            if price is None:
                missed.append((item, series, thickness, surface))
                continue
            others = None
            for block in candidates:
                row = block.prices.get(thickness) or next(iter(block.prices.values()), None)
                if row:
                    others = row
                    break
            filled.append((item, price, others))

        print(f"\n{'позиція каталогу':<58} {'було':>8} {'стане':>10}")
        print("-" * 96)
        for item, price, _ in sorted(filled, key=lambda f: f[0].name):
            print(f"{item.name[:56]:<58} {item.unit_price:>8,.0f} {price:>10,.2f}")

        if missed:
            print(f"\nбез ціни у прайсі ({len(missed)}) — лишаються 0,00:")
            for item, series, thickness, surface in missed:
                print(f"  {item.name[:56]:58s}  серія «{series}» {thickness:g}см {surface}")

        print("\nінші кольори тієї ж поверхні (для довідки):")
        for item, price, others in sorted(filled, key=lambda f: f[0].name)[:6]:
            if not others:
                continue
            shown = ", ".join(
                f"{COLUMNS[i].split(',')[0]}: {p:,.0f}"
                for i, p in enumerate(others[:5]) if p
            )
            print(f"  {item.name[:44]:46s} {shown}")

        if not args.apply:
            print("\nDry run. Записати ціни: --apply")
            return 0

        for item, price, _ in filled:
            item.unit_price = price
            item.source_file = f"OZON:{args.pdf.name}"
        session.commit()
        rebuild_fts(session)
        print(f"\nоновлено цін: {len(filled)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
