"""The pricing rule the client set: an article costs what it last sold for.

Everything here runs on proposals built in the test, not on the reference PDFs,
so the rule is pinned down independently of which folder a machine happens to
have. The reader that turns a real КП into these rows is covered by
``test_reference_reader.py``.
"""

from __future__ import annotations

import pytest

from app.services.history.prices import InvoicedPrices
from app.services.history.proposals import Proposal, Row, issued_on


def proposal(name: str, *rows: tuple[str, float, float]) -> Proposal:
    """A proposal named the way the real files are, so it dates itself."""
    p = Proposal(project=name, path=f"{name}.pdf")
    for article, price, quantity in rows:
        p.rows.append(
            Row(section="Бруківка", block="works", name=article,
                quantity=quantity, unit="м²", unit_price=price,
                total=round(price * quantity, 2))
        )
    return p


# --- dating -------------------------------------------------------------------


def test_a_quarter_in_the_filename_dates_the_proposal() -> None:
    when = issued_on("КП 2026 (1 Квартал) Максим Скленар ПМ - КП Галина Будьків")
    assert when is not None
    assert when.precision == "quarter"
    assert (when.date.year, when.date.month) == (2026, 3)
    assert str(when) == "1 кв. 2026"


def test_an_explicit_date_beats_the_quarter_it_sits_in() -> None:
    when = issued_on("КП 2026 (1 Квартал) … КП Оксана Басівка 15.06.2026")
    assert when is not None
    assert when.precision == "day"
    assert str(when) == "15.06.2026"


def test_a_filename_with_no_date_yields_none() -> None:
    assert issued_on("КП Богдан Вислобоки") is None


# --- picking the price --------------------------------------------------------


def test_the_newest_proposal_sets_the_price() -> None:
    prices = InvoicedPrices.from_proposals([
        proposal("КП 2026 (1 квартал) Ярослав ПМ", ("Монтаж бруківки 6см", 700.0, 10)),
        proposal("КП 2026 (2 квартал) Максим ПМ", ("Монтаж бруківки 6см", 450.0, 10)),
    ])
    last = prices.lookup("Монтаж бруківки 6см")
    assert last is not None
    assert last.unit_price == 450.0
    assert last.alternatives == [700.0]
    assert "2 кв. 2026" in last.trace()


def test_a_dated_proposal_outranks_an_undated_one() -> None:
    """An undated file cannot claim to be the most recent one."""
    prices = InvoicedPrices.from_proposals([
        proposal("КП Богдан Вислобоки", ("Зарізка поребриків", 320.0, 4)),
        proposal("КП 2026 (1 квартал) Василь ПМ", ("Зарізка поребриків", 260.0, 4)),
    ])
    last = prices.lookup("Зарізка поребриків")
    assert last is not None and last.unit_price == 260.0


def test_two_proposals_from_one_quarter_go_to_the_price_more_of_them_used() -> None:
    """Quarter precision leaves ties, and an average is a price nobody charged."""
    prices = InvoicedPrices.from_proposals([
        proposal("КП 2026 (1 квартал) А ПМ", ("Укладання геотекстилю", 60.0, 100)),
        proposal("КП 2026 (1 квартал) Б ПМ", ("Укладання геотекстилю", 60.0, 100)),
        proposal("КП 2026 (1 квартал) В ПМ", ("Укладання геотекстилю", 30.0, 100)),
    ])
    last = prices.lookup("Укладання геотекстилю")
    assert last is not None
    assert last.unit_price == 60.0
    assert last.alternatives == [30.0]
    assert last.sales == 3


def test_an_article_sold_once_carries_no_alternatives() -> None:
    prices = InvoicedPrices.from_proposals([
        proposal("КП 2026 (1 квартал) А ПМ", ("Заповнення швів піском", 20.0, 50)),
    ])
    last = prices.lookup("Заповнення швів піском")
    assert last is not None and last.alternatives == []
    assert prices.contested() == {}


def test_articles_sold_at_one_price_are_not_contested() -> None:
    prices = InvoicedPrices.from_proposals([
        proposal("КП 2026 (1 квартал) А ПМ",
                 ("Зарізка бордюрів", 300.0, 10), ("Установка бордюрів", 400.0, 10)),
        proposal("КП 2026 (2 квартал) Б ПМ",
                 ("Зарізка бордюрів", 300.0, 10), ("Установка бордюрів", 440.0, 10)),
    ])
    contested = prices.contested()
    assert set(contested) == {"установка бордюрів"}
    assert contested["установка бордюрів"].unit_price == 440.0


# --- lookup -------------------------------------------------------------------


def test_case_and_spacing_do_not_hide_a_price() -> None:
    prices = InvoicedPrices.from_proposals([
        proposal("КП 2026 (1 квартал) А ПМ", ("Транспортні витрати", 2500.0, 1)),
    ])
    assert prices.lookup("транспортні   ВИТРАТИ") is not None


def test_a_different_size_is_a_different_article() -> None:
    """275 грн for a 25cm kerb must never become the price of a 15cm one."""
    prices = InvoicedPrices.from_proposals([
        proposal("КП 2026 (1 квартал) А ПМ", ("Установка поребриків 25см", 290.0, 40)),
    ])
    assert prices.lookup("Установка поребриків 25см") is not None
    assert prices.lookup("Установка поребриків 15см") is None


def test_an_article_never_invoiced_has_no_price() -> None:
    prices = InvoicedPrices.from_proposals([
        proposal("КП 2026 (1 квартал) А ПМ", ("Монтаж бруківки 6см", 450.0, 10)),
    ])
    assert prices.lookup("Влаштування підпірної стінки з габіонів") is None


def test_an_empty_history_answers_nothing_rather_than_failing() -> None:
    prices = InvoicedPrices.from_proposals([])
    assert len(prices) == 0
    assert prices.lookup("Монтаж бруківки 6см") is None


# --- the builder applies it ---------------------------------------------------


class _FakeCandidate:
    def __init__(self, item):
        self.item = item


class _FakeMatch:
    def __init__(self, item, status="matched"):
        self.best = _FakeCandidate(item) if item is not None else None
        self.status = status
        self.confidence = "high"
        self.reason = "точний збіг"
        self.candidates = []


class _FakeItem:
    def __init__(self, name, price, unit="м²", cost=0.0):
        self.id = 1
        self.name = name
        self.category = "Роботи"
        self.unit = unit
        self.unit_price = price
        self.unit_cost = cost
        self.attributes = {}


@pytest.fixture()
def priced_builder():
    """A builder with the catalogue stubbed out, so only pricing is under test."""
    from app.services.estimate.builder import EstimateBuilder

    builder = EstimateBuilder.__new__(EstimateBuilder)
    builder.use_invoiced_prices = True
    builder._prices = InvoicedPrices.from_proposals([
        proposal("КП 2026 (1 квартал) А ПМ", ("Монтаж бруківки 6см", 700.0, 10)),
        proposal("КП 2026 (2 квартал) Б ПМ", ("Монтаж бруківки 6см", 450.0, 10)),
        proposal("КП 2026 (2 квартал) Б ПМ2", ("Ірга Ламарка 2м", 12800.0, 1)),
    ])
    return builder


def _line(name, price=0.0, block="works"):
    from app.services.rules.engine import DraftLine

    return DraftLine(name=name, section="paving", block=block, unit="м²",
                     quantity=1.0, unit_price=price)


def test_the_last_sold_price_replaces_the_list_price(priced_builder) -> None:
    from app.services.estimate.builder import BuildResult
    from app.services.rules.engine import Draft
    from app.services.rules.totals import EstimateTotals

    result = BuildResult(draft=Draft(), totals=EstimateTotals())
    line = _line("Монтаж бруківки 6см", price=550.0)
    item = _FakeItem("Монтаж бруківки 6см", 550.0)

    priced_builder._apply_last_sold(line, _FakeMatch(item), result)

    assert line.unit_price == 450.0
    assert result.repriced and result.repriced[0]["catalog_price"] == 550.0
    assert result.repriced[0]["invoiced_price"] == 450.0
    assert any(r["source_type"] == "historical_estimate" for r in line.source_refs)
    assert any("останнім виданим КП" in r for r in line.reasons)


def test_a_price_the_base_lacks_is_filled_from_the_history(priced_builder) -> None:
    """Every plant in the base is price-free; the invoices are the only source."""
    from app.services.estimate.builder import BuildResult
    from app.services.rules.engine import Draft
    from app.services.rules.totals import EstimateTotals

    result = BuildResult(draft=Draft(), totals=EstimateTotals())
    line = _line("Ірга Ламарка 2м", price=0.0, block="plants")
    item = _FakeItem("Ірга Ламарка 2м", 0.0, unit="шт")

    priced_builder._apply_last_sold(line, _FakeMatch(item), result)

    assert line.unit_price == 12800.0
    assert result.repriced[0]["catalog_price"] == 0.0
    assert any("останню продану" in r for r in line.reasons)


def test_an_article_with_no_sale_keeps_the_base_price(priced_builder) -> None:
    from app.services.estimate.builder import BuildResult
    from app.services.rules.engine import Draft
    from app.services.rules.totals import EstimateTotals

    result = BuildResult(draft=Draft(), totals=EstimateTotals())
    line = _line("Гідрофобізатор для бетонних плит, 5л", price=1200.0)
    item = _FakeItem("Гідрофобізатор для бетонних плит, 5л", 1200.0)

    priced_builder._apply_last_sold(line, _FakeMatch(item), result)

    assert line.unit_price == 1200.0
    assert result.repriced == []
    assert line.reasons == []


def test_the_rule_can_be_switched_off(priced_builder) -> None:
    from app.services.estimate.builder import BuildResult
    from app.services.rules.engine import Draft
    from app.services.rules.totals import EstimateTotals

    priced_builder.use_invoiced_prices = False
    result = BuildResult(draft=Draft(), totals=EstimateTotals())
    line = _line("Монтаж бруківки 6см", price=550.0)

    priced_builder._apply_last_sold(
        line, _FakeMatch(_FakeItem("Монтаж бруківки 6см", 550.0)), result
    )

    assert line.unit_price == 550.0
    assert result.repriced == []


def test_a_plant_nobody_ever_sold_is_still_a_question() -> None:
    """The client's rule: no price in the base and none in the history means
    ask, never guess."""
    from app.services.rules.engine import Draft, DraftLine
    from app.services.rules.totals import compute_totals
    from app.services.validation.validators import NEEDS_USER_INPUT, validate

    line = DraftLine(name="Магнолія Суланжа 2м", section="planting", block="plants",
                     unit="шт", quantity=3.0, unit_price=0.0)
    line.match_status = "matched"

    draft = Draft(lines=[line])
    report = validate(draft, compute_totals(draft))
    questions = [f for f in report.findings if f.code == "plant_price_required"]
    assert questions and all(f.severity == NEEDS_USER_INPUT for f in questions)
    assert not report.exportable


def test_the_price_is_looked_up_under_the_catalogue_name(priced_builder) -> None:
    """The drawings name an article loosely; the base and the invoices agree."""
    from app.services.estimate.builder import BuildResult
    from app.services.rules.engine import Draft
    from app.services.rules.totals import EstimateTotals

    result = BuildResult(draft=Draft(), totals=EstimateTotals())
    line = _line("бруківка 6 см монтаж", price=550.0)
    item = _FakeItem("Монтаж бруківки 6см", 550.0)

    priced_builder._apply_last_sold(line, _FakeMatch(item), result)

    assert line.unit_price == 450.0
