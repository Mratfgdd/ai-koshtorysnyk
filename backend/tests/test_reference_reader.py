"""Reading an issued proposal back into rows.

The reader works off geometry, so these tests draw a proposal at fixed
coordinates rather than asserting against a real PDF that may or may not be on
the machine. Each one pins down a shape that actually appears in the client's
fourteen references and that a token-stream reader gets wrong.
"""

from __future__ import annotations

import pymupdf
import pytest

from app.services.history.proposals import (
    as_number,
    block_of_title,
    parse_proposal,
)

# Where the template prints each column. The reader takes the edges from the
# header row itself, so only the relative order has to be right.
X = {"index": 25, "name": 40, "qty": 356, "unit": 409, "price": 471, "total": 545}
HEADER = ((X["qty"], "К-сть"), (X["unit"], "Од."), (X["price"], "Ціна"),
          (X["total"], "Сума"))


def money(value: float) -> str:
    """The accounting format the template prints: "1 700,0-"."""
    whole = f"{value:,.1f}".replace(",", " ").replace(".", ",")
    return f"{whole}-"


class Sheet:
    """Draws a КП the way the client's Excel template prints one.

    Each cell goes into its own box at a known x, because the reader decides
    which column a word belongs to from where it sits on the page. Cells are
    written as HTML rather than with ``insert_text``: the base-14 fonts carry
    no Cyrillic, and a proposal in Latin would not exercise anything.
    """

    CSS = "* {font-family: sans-serif; font-size: 8px;}"

    def __init__(self, path):
        self.doc = pymupdf.open()
        self.page = self.doc.new_page()
        self.y = 60.0
        self.path = path

    def _write(self, x: float, text: str) -> None:
        self.page.insert_htmlbox(
            pymupdf.Rect(x, self.y, x + 300, self.y + 14), text, css=self.CSS
        )

    def _row(self, cells: list[tuple[float, str]]) -> None:
        for x, text in cells:
            self._write(x, text)
        self.y += 16.4

    def section(self, title: str) -> None:
        self._row([(20, f"Рахунок {title}:")])

    def table(self, title: str) -> None:
        self._row([(X["index"] - 1, "#"), (160, title), *HEADER])

    def line(self, n: int, name: str, qty: float, unit: str, price: float) -> None:
        self._row([
            (X["index"], str(n)), (X["name"], name),
            (X["qty"], f"{qty:g}"), (X["unit"], unit),
            (X["price"], f"{price:g}"), (X["total"], money(qty * price)),
        ])

    def subtotal(self, what: str, value: float) -> None:
        self._row([(383, f"Разом за {what}:"), (X["total"], money(value))])

    def section_total(self, what: str, value: float) -> None:
        self._row([(375, f"Разом {what}:"), (X["total"], money(value))])

    def save(self):
        self.doc.save(self.path)
        self.doc.close()
        return self.path


@pytest.fixture()
def pump_station(tmp_path):
    """"Автоматичний полив" as the client prints it: two pairs of tables.

    The pump station has its own materials and works tables inside the section,
    so "Разом за матеріали" and "Разом за роботу" are each printed twice.
    """
    sheet = Sheet(tmp_path / "kp.pdf")
    sheet.section("Автоматичний полив")
    sheet.table("Матеріали")
    sheet.line(1, "Труба поліетиленова, D-32 PN10", 65, "м.п", 27)
    sheet.subtotal("матеріали", 1755)
    sheet.table("Насосна станція (матеріали)")
    sheet.line(1, "Насос відцентровий", 1, "шт", 35920)
    sheet.subtotal("матеріали", 35920)
    sheet.table("Насосна станція (робота)")
    sheet.line(1, "Монтаж насосної станції", 1, "шт", 12500)
    sheet.subtotal("роботу", 12500)
    sheet.table("Робота")
    sheet.line(1, "Прокладання труби", 65, "м.п", 45)
    sheet.subtotal("роботу", 2925)
    sheet.section_total("Автополив", 53100)
    return parse_proposal(sheet.save())


# --- the shapes that used to break the reader ---------------------------------


def test_a_section_split_into_several_tables_reconciles(pump_station) -> None:
    """Its blocks add up to the subtotals printed above them, all four."""
    assert pump_station.reconciles, pump_station.block_deltas()


def test_repeated_subtotals_are_added_not_overwritten(pump_station) -> None:
    """Taking the last "Разом за матеріали" would keep only the pump station."""
    assert pump_station.block_totals["Автоматичний полив|materials"] == 1755 + 35920
    assert pump_station.block_totals["Автоматичний полив|works"] == 12500 + 2925


def test_a_pump_station_works_table_is_read_as_work(pump_station) -> None:
    """It is titled "Насосна станція (робота)", not "Робота"."""
    blocks = {r.name: r.block for r in pump_station.rows}
    assert blocks["Монтаж насосної станції"] == "works"
    assert blocks["Насос відцентровий"] == "materials"


def test_the_section_roll_up_is_not_taken_for_a_section(pump_station) -> None:
    """"Разом за насосну станцію" closes a group, not a section."""
    assert "насосну станцію" not in pump_station.section_totals


def test_a_section_total_keeps_its_name_free_of_its_figure(pump_station) -> None:
    assert pump_station.section_totals == {"Автополив": 53100}


def test_every_row_multiplies_out(pump_station) -> None:
    assert pump_station.rows
    assert all(row.consistent for row in pump_station.rows)


# --- block titles -------------------------------------------------------------


@pytest.mark.parametrize(
    "title, block",
    [
        ("Матеріали", "materials"),
        ("Робота", "works"),
        ("Рослини", "plants"),
        ("Насосна станція (матеріали)", "materials"),
        ("Насосна станція (робота)", "works"),
    ],
)
def test_a_table_title_says_which_block_it_is(title, block) -> None:
    assert block_of_title(title) == block


def test_a_title_that_says_nothing_about_the_block_leaves_it_alone() -> None:
    assert block_of_title("Додатково") is None


# --- the accounting number format ---------------------------------------------


@pytest.mark.parametrize(
    "text, value",
    [
        ("29 000,0-", 29000.0),
        ("- 89 720,0-", 89720.0),
        ("1 755,0-", 1755.0),
        ("741 560,00-", 741560.0),
        ("27", 27.0),
    ],
)
def test_the_accounting_format_reads_as_a_number(text, value) -> None:
    assert as_number(text) == value


@pytest.mark.parametrize("text", ["-", "", "   ", "м.п"])
def test_a_cell_with_no_number_reads_as_none(text) -> None:
    assert as_number(text) is None


# --- against the real references, when they are on this machine ---------------


def test_every_reference_proposal_reconciles(reference_root) -> None:
    """The reading must be self-consistent on all fourteen, or nothing about
    pricing can be concluded from them."""
    from app.services.history.proposals import parse_proposal, reference_proposals

    paths = reference_proposals(reference_root)
    assert paths, f"no КП PDFs under {reference_root}"

    failed = []
    for path in paths:
        proposal = parse_proposal(path)
        if not proposal.reconciles:
            failed.append((proposal.project, proposal.block_deltas()))
    assert not failed, failed
