"""Totals are checked against a real, signed proposal.

Source: "КП 2026 (1 Квартал) Максим Скленар ПМ - КП Галина Будьків.pdf".
If these numbers ever stop matching, the calculation layer has drifted from
what the client actually invoices.
"""

from __future__ import annotations

from app.services.rules.engine import Draft, DraftLine
from app.services.rules.totals import compute_totals

# Section subtotals as printed on the customer's proposal.
BUDKIV = {
    "prep": {"materials": 89_720.0, "works": 50_070.0},
    "pathway": {"materials": 41_594.0, "works": 43_970.0},
    "planting": {"materials": 100_124.0, "works": 112_366.0, "plants": 116_760.0},
    "lawn": {"materials": 108_771.0, "works": 78_185.0},
}
EXPECTED_MATERIALS = 456_969.0
EXPECTED_WORKS = 284_591.0
EXPECTED_GRAND = 741_560.0
EXPECTED_PREPAY_WORKS = 85_400.0
EXPECTED_BALANCE = 199_191.0


def _draft() -> Draft:
    lines: list[DraftLine] = []
    for section, blocks in BUDKIV.items():
        for block, amount in blocks.items():
            # One synthetic line per block carrying that block's total; the
            # totals layer only cares about section/block attribution.
            lines.append(
                DraftLine(
                    name=f"{section}:{block}",
                    section=section,
                    block=block,
                    unit="шт",
                    quantity=1.0,
                    unit_price=amount,
                )
            )
    return Draft(lines=lines)


def test_section_totals_match_the_proposal() -> None:
    totals = compute_totals(_draft())
    by_key = {s.key: s for s in totals.sections}

    assert by_key["prep"].total == 139_790.0
    assert by_key["pathway"].total == 85_564.0
    assert by_key["planting"].total == 329_250.0
    assert by_key["lawn"].total == 186_956.0


def test_invoice_totals_match_the_proposal() -> None:
    totals = compute_totals(_draft())

    assert totals.materials_total == EXPECTED_MATERIALS
    assert totals.works_total == EXPECTED_WORKS
    assert totals.grand_total == EXPECTED_GRAND
    assert totals.prepayment_materials == EXPECTED_MATERIALS
    assert totals.prepayment_works == EXPECTED_PREPAY_WORKS
    assert totals.balance == EXPECTED_BALANCE


def test_work_prepayment_rounds_up_to_the_nearest_hundred() -> None:
    # 284 591 * 0.3 = 85 377.3 -> the template's CEILING.MATH(.., 100).
    totals = compute_totals(_draft())
    assert totals.prepayment_works == 85_400.0
    assert totals.prepayment_works % 100 == 0


def test_cashless_applies_the_six_percent_surcharge() -> None:
    draft = _draft()
    draft.settings = {"cashless": True}
    totals = compute_totals(draft)

    assert totals.subtotal == EXPECTED_GRAND
    assert totals.surcharge == round(EXPECTED_GRAND * 0.06, 2)
    assert totals.grand_total == round(EXPECTED_GRAND * 1.06, 2)


def test_sections_without_quantities_are_omitted() -> None:
    draft = _draft()
    draft.lines.append(
        DraftLine(name="irrigation:materials", section="irrigation", block="materials",
                  quantity=0.0, unit_price=50_000.0)
    )
    totals = compute_totals(draft)

    assert "irrigation" not in {s.key for s in totals.sections}
    assert totals.grand_total == EXPECTED_GRAND
