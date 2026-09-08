"""How a section is named in the customer's copy.

The compiled template stores a section's heading exactly as the workbook cell
writes it — "Рахунок Автоматичний полив:". "Рахунок" there is the marker word
that opens the block, not part of the section's name, and the issued proposals
show the difference plainly: the block opens with

    Рахунок Освітлення:

and closes with

    Разом за освітлення

never "Разом Рахунок Освітлення". Both exporters need the name both ways, so
the two forms live here rather than as the same expression written twice.
"""

from __future__ import annotations

MARKER = "Рахунок"

# The one section whose own name begins the same way. Its heading is genuinely
# "Рахунок загальний" — the summary of the whole invoice, not a section of it.
_KEEPS_MARKER = ("підготовчий",)


def bare_name(title: str) -> str:
    """The section's name without the marker word: "Рахунок Газон" -> "Газон"."""
    name = title.strip().rstrip(":").strip()
    if name.lower().startswith(MARKER.lower() + " "):
        stripped = name[len(MARKER):].strip()
        if stripped:
            return stripped
    return name


def marker_label(title: str) -> str:
    """The heading that opens the block: "Газон" -> "Рахунок Газон"."""
    name = title.strip().rstrip(":").strip()
    low = name.lower()
    if low.startswith(MARKER.lower()) or low.startswith(_KEEPS_MARKER):
        return name
    return f"{MARKER} {name}"


__all__ = ["bare_name", "marker_label"]
