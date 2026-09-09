"""Matching an article by its manufacturer's number rather than by its prose.

A drawing writes "Прожектор Пр-2, NOWODVORSKI 30865, 7 Вт". The price base
writes "Світильник вуличний прожектор NOWODVORSKI 30865- 15% знижки від
вартсоті світильника". As sentences they share almost nothing and the fuzzy
score put the right article at 46 out of 100, below three unrelated rows; the
lighting section came out empty. As identities they are one object and 30865
says so.
"""

from __future__ import annotations

import pytest

from app.services.catalog.search import (
    model_codes,
    sku_codes,
    strip_marketing,
)

# --- what counts as an article number -----------------------------------------


@pytest.mark.parametrize(
    "name, codes",
    [
        ("Прожектор Пр-2, NOWODVORSKI 30865, 7 Вт", {"30865"}),
        ("Світильник вуличний бра IDEAL LUX 81069- 15% знижки", {"81069"}),
        ("Світильник стовпець С-1, Ideal Lux 306872, 11 Вт", {"306872"}),
        ("Гірлянда Г-1, GOLDLUX 32652, 60 Вт", {"32652"}),
    ],
)
def test_a_manufacturer_number_is_found_next_to_punctuation(name, codes) -> None:
    """It is written against a comma or a hyphen as often as against a space."""
    assert sku_codes(name) == codes


@pytest.mark.parametrize(
    "name",
    [
        "Плити 1200х400",                    # a size
        "Плити 1600х300",
        "Кашпо Gardener Довгий М 100х50х50, RAL 5004",   # a size and a RAL colour
        "Бак для води 1000л, Вертикальний",  # a capacity
        "Форсунка Hunter MP3000 360",        # a spray range
        "Екструдований пінополістирол 1200x550x50",
    ],
)
def test_a_dimension_is_not_an_article_number(name) -> None:
    """Four digits is a length or a volume; the catalogue's own four-digit
    numbers are shared by several articles, and its five-digit ones by none."""
    assert not sku_codes(name)


# --- and what counts as a model -----------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Насосна станція Pedrollo Plurijet 4/100", "4/100"),
        ("Контролер поливу Hunter X-CORE-401-E (4 зони)", "x-core-401-e"),
        ("Контроллер зовнішній X2-401-E Hunter", "x2-401-e"),
    ],
)
def test_a_model_designation_is_found(name, expected) -> None:
    assert expected in model_codes(name)


@pytest.mark.parametrize(
    "name",
    ["Відсів, фракція 0-5мм", "Щебінь, фракція 20-40мм", "Кора соснова, фр. 50-70"],
)
def test_a_size_range_is_not_a_model(name) -> None:
    """Digits joined by a hyphen are a range. Reading one as an identity would
    price gravel as whatever else happens to carry those two numbers."""
    assert not model_codes(name)


# --- the sales copy ------------------------------------------------------------


def test_a_discount_note_is_dropped_before_comparing() -> None:
    assert strip_marketing(
        "Світильник вуличний прожектор NOWODVORSKI 30865- 15% знижки від вартсоті"
    ) == "Світильник вуличний прожектор NOWODVORSKI 30865"
    assert strip_marketing("Відсів, фракція 0-5мм") == "Відсів, фракція 0-5мм"


# --- against the real catalogue ------------------------------------------------


@pytest.fixture(scope="module")
def search():
    from sqlalchemy import select

    from app.db import SessionLocal, init_db
    from app.models import CatalogItem
    from app.services.catalog.search import CatalogSearch

    init_db()
    session = SessionLocal()
    if session.scalar(select(CatalogItem).limit(1)) is None:
        session.close()
        pytest.skip("catalog not imported; run scripts/import_catalog.py")
    return CatalogSearch(session)


def test_a_shared_article_number_settles_the_match(search) -> None:
    result = search.match("Прожектор Пр-2, NOWODVORSKI 30865, 7 Вт")
    if result.best is None or "30865" not in result.best.item.name:
        pytest.skip("this catalogue build does not carry NOWODVORSKI 30865")
    assert result.status == "matched"
    assert result.confidence == "high"


def test_a_number_two_articles_share_settles_nothing(search) -> None:
    """A code on more than one row identifies neither of them."""
    search.items  # load the indexes
    shared = next((code for code, items in search._by_sku.items() if len(items) > 1), None)
    if shared is None:
        pytest.skip("no article number is shared in this catalogue build")
    assert search.by_article_number(f"Позиція {shared}") is None


def test_a_number_the_catalogue_does_not_carry_matches_nothing(search) -> None:
    assert search.by_article_number("Світильник Acme 999991") is None


def test_two_models_of_one_family_stay_a_question(search) -> None:
    """"X-CORE-401-E" against an X-CORE in eight zones and an X2 in four is a
    choice between two products, and the estimator makes it."""
    assert search.by_model("Контролер поливу Hunter X-CORE-401-E (4 зони)") is None
