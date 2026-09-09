"""An article that lives in several sections belongs to the one asked for.

The template puts the overheads in every section by design: "Транспортні
витрати" is a row of thirteen of them, "Пісок" of ten, agrofabric of three.
Resolving such a name by walking the template in order handed every one of them
to whichever section comes first, so a drawing's planting fabric was billed
under Доріжка — and the planting section's own input row stayed at zero, which
is what the rest of that section is derived from.
"""

from __future__ import annotations

import pytest

from app.services.estimate.builder import TemplateLayout
from app.services.pipeline import _section_of_article
from app.services.rules.engine import normalize_name


@pytest.fixture(scope="module")
def layout() -> TemplateLayout:
    try:
        return TemplateLayout.load()
    except FileNotFoundError:
        pytest.skip("template not compiled; run scripts/compile_template.py")


def sections_carrying(layout, article: str) -> list[str]:
    key = normalize_name(article)
    return [
        section for section in layout.order
        if section != "summary" and any(
            line["block"] != "driver" and normalize_name(line["name"]) == key
            for line in layout.lines(section)
        )
    ]


def a_shared_article(layout, minimum: int = 3) -> tuple[str, list[str]]:
    """Any article the template repeats across sections, chosen from the data."""
    for section in layout.order:
        for line in layout.lines(section):
            if line["block"] == "driver":
                continue
            homes = sections_carrying(layout, line["name"])
            if len(homes) >= minimum:
                return line["name"], homes
    pytest.skip("this template shares no article across sections")


def test_the_requested_section_wins_when_it_carries_the_article(layout) -> None:
    article, homes = a_shared_article(layout)
    for home in homes:
        assert _section_of_article(article, layout, prefer=home) == home, (
            f"«{article}» asked for in {home} resolved elsewhere"
        )


def test_without_a_preference_the_answer_is_unchanged(layout) -> None:
    """The old behaviour is the fallback, not the rule."""
    article, homes = a_shared_article(layout)
    assert _section_of_article(article, layout) == homes[0]


def test_a_section_that_does_not_carry_it_is_not_forced(layout) -> None:
    """Preference is a tie-break among the sections that have the row, never an
    instruction to put the article somewhere it does not exist."""
    article, homes = a_shared_article(layout)
    outsider = next((s for s in layout.order
                     if s != "summary" and s not in homes), None)
    if outsider is None:
        pytest.skip("every section carries this article")
    assert _section_of_article(article, layout, prefer=outsider) == homes[0]


def test_an_unknown_article_stays_unknown(layout) -> None:
    assert _section_of_article("Позиція якої немає QWXZ", layout,
                               prefer="planting") is None


def test_an_unknown_preference_is_ignored(layout) -> None:
    article, homes = a_shared_article(layout)
    assert _section_of_article(article, layout, prefer="не існує") == homes[0]
    assert _section_of_article(article, layout, prefer="summary") == homes[0]
