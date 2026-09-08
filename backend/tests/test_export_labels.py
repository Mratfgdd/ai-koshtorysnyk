"""How a section is named in the документ the customer receives.

The template stores a heading as the workbook writes it — "Рахунок Газон:" —
where "Рахунок" is the marker word that opens the block. The issued proposals
close the same block with "Разом Газон:", never "Разом Рахунок Газон:".
"""

from __future__ import annotations

import pytest

from app.services.export.labels import bare_name, marker_label


@pytest.mark.parametrize(
    "title, bare, marked",
    [
        ("Рахунок Газон", "Газон", "Рахунок Газон"),
        ("Рахунок Автоматичний полив", "Автоматичний полив", "Рахунок Автоматичний полив"),
        ("Рахунок озеленення", "озеленення", "Рахунок озеленення"),
        ("Рахунок Бетонні Роботи", "Бетонні Роботи", "Рахунок Бетонні Роботи"),
        # Already bare: the marker is added for the heading, not doubled.
        ("Газон", "Газон", "Рахунок Газон"),
        # The one section the workbook writes without the marker.
        ("Підготовчий етап", "Підготовчий етап", "Підготовчий етап"),
        # A trailing colon belongs to the cell, not to the name.
        ("Рахунок Доріжка:", "Доріжка", "Рахунок Доріжка"),
    ],
)
def test_a_section_is_named_both_ways(title, bare, marked) -> None:
    assert bare_name(title) == bare
    assert marker_label(title) == marked


def test_the_marker_alone_is_left_alone() -> None:
    """Nothing is left to name if the marker is stripped from "Рахунок"."""
    assert bare_name("Рахунок") == "Рахунок"


def test_both_forms_are_stable_under_repetition() -> None:
    """Rendering twice must not add or remove a second marker."""
    for title in ("Рахунок Газон", "Газон", "Підготовчий етап"):
        assert marker_label(marker_label(title)) == marker_label(title)
        assert bare_name(bare_name(title)) == bare_name(title)
