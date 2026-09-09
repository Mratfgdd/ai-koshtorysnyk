"""Three rules that let a reading reach the estimate, and their limits.

Each is general arithmetic over whatever the drawing and the catalogue happen
to say. None of them knows what a cable or a paving slab is, and the tests below
are written to fail if one ever starts to.
"""

from __future__ import annotations

import pytest

from app.services.catalog.search import dimension_forms, specs


# --- dimensions in different units are the same dimensions ---------------------


def test_millimetres_and_centimetres_meet() -> None:
    """A drawing writes "Плити 1200х400"; the base writes "120х40х6см"."""
    assert dimension_forms("Плити 1200х400") & dimension_forms(
        "Плита ходова бетонна 120х40х6см"
    )


def test_a_thickness_the_drawing_omits_does_not_break_the_match() -> None:
    """Two dimensions must still meet three, which is why they are emitted
    pairwise."""
    drawing = dimension_forms("Плити 1600х300")
    base = dimension_forms("Плита ходова бетонна 160х30х6см")
    assert drawing & base


def test_the_same_size_written_in_metres_also_meets() -> None:
    assert dimension_forms("Труба 1.2х0.4 м") & dimension_forms("Щось 120х40см")


def test_an_explicit_unit_is_taken_at_its_word() -> None:
    """With "см" written there is nothing to infer, so only that reading is
    emitted — 120х40см is 1200x400 mm and never 120x400."""
    forms = dimension_forms("Плита 120х40см")
    assert "400x1200мм" in forms
    assert "40x120мм" not in forms


def test_a_size_written_without_a_unit_keeps_both_readings() -> None:
    """1200х400 is either millimetres or centimetres and only the other name
    can say which, so neither is assumed."""
    forms = dimension_forms("Плити 1200х400")
    assert "400x1200мм" in forms       # read as millimetres
    assert "4000x12000мм" in forms     # read as centimetres


def test_different_sizes_do_not_meet() -> None:
    assert not (dimension_forms("Плита 120х40х6см") & dimension_forms("Плита 60х60х6см"))
    assert not (dimension_forms("Кашпо 100х50х50") & dimension_forms("Кашпо 80х40х40"))


def test_specs_still_carries_what_it_always_did() -> None:
    """The normalised forms are added to the written ones, not swapped in."""
    assert "d-32" in {s.replace(" ", "") for s in specs("Труба поліетиленова D-32")}


def test_a_name_with_no_dimensions_yields_none() -> None:
    assert dimension_forms("Транспортні витрати") == set()
    assert dimension_forms("") == set()


# --- a clear leader is a match, a crowded field is a question ------------------


class _Item:
    def __init__(self, name, price=100.0, unit="шт"):
        self.id = abs(hash(name)) % 10**6
        self.name = name
        self.name_norm = name.lower()
        self.category = ""
        self.unit = unit
        self.unit_price = price
        self.unit_cost = 0.0
        self.kind = "material"
        self.attributes = {}


def _search_over(names):
    from app.services.catalog.search import CatalogSearch

    search = CatalogSearch(session=None)
    search.index([_Item(n) for n in names])
    return search


def test_a_leader_far_clear_of_the_field_is_accepted() -> None:
    """The old rule is switched off with an unreachable threshold, so only the
    margin rule can produce this match."""
    search = _search_over([
        "Електричний кабель 3х2.5 ВВГ НГ LS",
        "Мішок будівельний білий",
        "Транспортні витрати",
    ])
    result = search.match("Кабель ВВГ-нг 3х2,5 — група 01", accept_at=1000.0)
    assert result.status == "matched"
    assert result.best.item.name.startswith("Електричний кабель 3х2.5")
    assert result.confidence == "high"


def test_a_leader_with_a_rival_on_its_heels_is_still_a_question() -> None:
    """The margin rule must not wave through a real choice between two
    products, however high both score."""
    search = _search_over([
        "Гребінка на 3 клапани",
        "Гребінка на 4 клапани",
    ])
    result = search.match("Клапанна гребінка")
    assert result.status == "ambiguous"


def test_a_thin_margin_is_refused_even_at_a_good_score() -> None:
    """25 points is the bar and it is a bar, not a suggestion."""
    search = _search_over([
        "Електричний кабель 3х2.5 ВВГ НГ LS",
        "Електричний кабель 3х2.5 ВВГ НГ LS у гофрі",
    ])
    result = search.match("Кабель ВВГ-нг 3х2,5 — група 01", accept_at=1000.0)
    assert result.status == "ambiguous"


def test_the_margin_is_measured_the_same_way_for_everything() -> None:
    """No article is special-cased: an invented product with the same shape of
    evidence is accepted on the same arithmetic."""
    search = _search_over([
        "Штуковина універсальна QQ-17 для монтажу",
        "Мішок будівельний білий",
    ])
    result = search.match("Штуковина QQ-17", accept_at=1000.0)
    assert result.status == "matched"
    assert result.best.score >= 80.0
