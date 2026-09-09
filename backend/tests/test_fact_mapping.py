"""Getting a figure off a drawing into the right row of the estimate.

Every case here is one the Катерина Сокільники set produced. The proposal came
out at 139 441,50 against an issued 1 274 932,40, and the reason was not the
arithmetic: 65 of the 91 facts the analysis had read never reached the estimate
and never became a question either.
"""

from __future__ import annotations

import pytest

from app.services.estimate.builder import TemplateLayout
from app.services.pipeline import _identifying_overlap, _resolve_coverage_target


@pytest.fixture(scope="module")
def layout() -> TemplateLayout:
    try:
        return TemplateLayout.load()
    except FileNotFoundError:
        pytest.skip("template not compiled; run scripts/compile_template.py")


@pytest.fixture(scope="module")
def search(layout):
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


def resolve(name, unit, section, layout, search):
    return _resolve_coverage_target(name, unit, section, layout, search)


# --- what the drawing calls a template input row ------------------------------


def test_a_trench_length_reaches_its_input_row(layout, search) -> None:
    """"Мережі поливу, L траншей" against "Довжина траншей (м)".

    Neither string contains the other, so the substring test that used to stand
    here matched nothing and 66 m of trench was dropped without a word.
    """
    target, _kind, reason = resolve("Мережі поливу, L траншей", "м.п", "irrigation",
                                    layout, search)
    assert target == "Довжина траншей (м)", reason


def test_the_same_row_in_another_section_takes_its_own_trench(layout, search) -> None:
    target, _kind, _r = resolve("Мережі водопостачання, L траншей", "м.п",
                                "water_supply", layout, search)
    assert target == "Довжина траншей (м)"


# --- and the four ways that nearly went wrong ---------------------------------


def test_a_planted_area_is_not_a_fabric_area(layout, search) -> None:
    """They share only the word "площа", and the figures differ: Будьків
    invoiced 175 m² of fabric against 406 m² of planting."""
    target, _kind, _r = resolve("Площа озеленення", "м²", "planting", layout, search)
    assert target != "Агрополотно площа (клумб)"


def test_a_count_does_not_become_an_area(layout, search) -> None:
    """"Плити 400х400 = 3 шт" must not land in "Загальна площа плит" as 3 m²."""
    target, _kind, _r = resolve("Плити 400х400", "шт", "pathway", layout, search)
    assert target != "Загальна площа плит"


def test_a_flow_rate_is_not_a_quantity(layout, search) -> None:
    """"Загальна витрата форсунок 25,72 л/хв" shares "форсунок" with an input
    row and is a flow rate. л/хв is not a unit this company bills in."""
    target, _kind, _r = resolve("Загальна витрата форсунок", "л/хв", "irrigation",
                                layout, search)
    assert target != "Обвʼязка Форсунки"


def test_a_confident_article_beats_a_half_matched_row(layout, search) -> None:
    """92 m.п of drip tube is an article, not 92 outlets: the catalogue has the
    tube, and "Виводи під капельну трубу" is a count of outlets."""
    target, kind, _r = resolve("Крапельна трубка", "м.п", "irrigation", layout, search)
    assert target != "Виводи під капельну трубу"
    assert "каталог" in kind


def test_ties_between_input_rows_follow_the_workbook_order(layout, search) -> None:
    """Both paving rows contain "бруківка"; the workbook lists the area first.
    Sorting on the tuple broke the tie alphabetically instead."""
    target, _kind, _r = resolve("Бруківка", "м²", "paving", layout, search)
    assert target == "Загальна площа бруківка"


# --- units --------------------------------------------------------------------


def test_a_trailing_dot_is_not_a_different_unit() -> None:
    """The base writes "м.п" 75 times and "м.п." once; the drawings use both."""
    from app.services.catalog.search import normalize_unit

    assert normalize_unit("м.п.") == normalize_unit("м.п")
    assert normalize_unit("шт.") == normalize_unit("шт")
    assert normalize_unit(" ШТ ") == normalize_unit("шт")
    # Collapsing punctuation must not collapse genuinely different units.
    assert normalize_unit("м²") != normalize_unit("м³")
    assert normalize_unit("м.п") != normalize_unit("м²")


def test_a_real_unit_mismatch_is_still_refused(layout, search) -> None:
    """70 м.п of edging against an article the base sells by the piece needs a
    conversion, and the system does not invent one."""
    target, _kind, reason = resolve("Садовий бордюр", "м.п.", "paving", layout, search)
    assert target is None
    assert "одиниці не збігаються" in reason


# --- the identifying word -----------------------------------------------------


@pytest.mark.parametrize(
    "driver, name, agree",
    [
        ("Довжина траншей (м)", "Мережі поливу, L траншей", True),
        ("Загальна площа бруківка", "Бруківка (проєктна)", True),
        # Only the measurement word is shared, which is no agreement at all.
        ("Агрополотно площа (клумб)", "Площа озеленення", False),
        ("Загальна площа плит", "Загальна площа газону", False),
    ],
)
def test_two_rows_must_agree_on_what_is_measured(driver, name, agree) -> None:
    assert _identifying_overlap(driver, name) is agree


# --- nothing may vanish -------------------------------------------------------


def test_a_figure_that_cannot_be_placed_is_reported(layout, search) -> None:
    """Something the base does not carry must come back with a reason, never as
    a silent None-and-shrug.

    The subject is deliberately invented rather than borrowed from a real
    project: this once used GOLDLUX 32652, which the catalogue has since been
    given, so the test started failing for the reason the work succeeded.
    """
    target, _kind, reason = resolve(
        "Ліхтар підвісний QWXZ-0000 (вигадана позиція)", "шт", "lighting",
        layout, search,
    )
    assert target is None
    assert reason, "a refusal without a reason is a silent drop"
