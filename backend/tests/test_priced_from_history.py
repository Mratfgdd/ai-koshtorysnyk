"""An article missing from «2026 База 1» but sold before.

"Клен Фрімана" is not in the price base and never will be until someone types
it in; it was invoiced at 12 500 грн on a proposal the customer signed. Blocking
the export over it says the price is unknown when it is documented. Letting it
through on anything less than that document would be exactly the invention the
block exists to prevent, so the bar is: a price above zero, carried by a
``historical_estimate`` reference that names the proposal.
"""

from __future__ import annotations

import pytest

from app.services.rules.engine import Draft, DraftLine
from app.services.rules.totals import compute_totals
from app.services.validation.validators import NEEDS_USER_INPUT, WARNING, validate


def line(**over) -> DraftLine:
    l = DraftLine(name=over.pop("name", "Клен Фрімана"), section="planting",
                  block="plants", unit="шт", quantity=1.0,
                  unit_price=over.pop("unit_price", 12500.0))
    l.match_status = over.pop("match_status", "not_in_catalog")
    l.source_refs = over.pop("source_refs", [{
        "source_type": "historical_estimate",
        "source_ref": "КП 2026 (2 квартал) … Катерина Сокільники",
        "detail": "остання продана ціна",
    }])
    return l


def report_for(l: DraftLine):
    draft = Draft(lines=[l])
    return validate(draft, compute_totals(draft))


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


def test_a_documented_sale_does_not_block_the_export() -> None:
    report = report_for(line())
    assert "not_in_catalog" not in codes(report)
    assert "priced_from_history" in codes(report)
    assert all(f.severity != NEEDS_USER_INPUT for f in report.findings
               if f.code == "priced_from_history")


def test_it_still_says_the_article_is_missing_from_the_base() -> None:
    """Downgraded, not silenced: the base has a gap and someone should fill it."""
    finding = next(f for f in report_for(line()).findings
                   if f.code == "priced_from_history")
    assert finding.severity == WARNING
    assert "2026 База 1" in finding.detail
    assert "Катерина" in finding.detail, "the proposal must be named"


# --- and every way it must NOT be waved through -------------------------------


def test_no_price_is_still_a_blocker() -> None:
    assert "not_in_catalog" in codes(report_for(line(unit_price=0.0)))


def test_a_price_with_no_source_is_still_a_blocker() -> None:
    """A number on a line nobody can account for is what the block is for."""
    assert "not_in_catalog" in codes(report_for(line(source_refs=[])))


def test_a_catalogue_reference_does_not_count_as_a_sale() -> None:
    """Only an invoice proves a price was charged."""
    refs = [{"source_type": "product_catalog", "source_ref": "Рослини / Клен"}]
    assert "not_in_catalog" in codes(report_for(line(source_refs=refs)))


def test_a_hand_typed_price_does_not_count_as_a_sale() -> None:
    refs = [{"source_type": "user_input", "source_ref": "Уточнення користувача"}]
    assert "not_in_catalog" in codes(report_for(line(source_refs=refs)))


def test_a_reference_that_names_no_proposal_does_not_count() -> None:
    refs = [{"source_type": "historical_estimate", "source_ref": "  "}]
    assert "not_in_catalog" in codes(report_for(line(source_refs=refs)))


def test_a_malformed_reference_does_not_crash_or_pass() -> None:
    assert "not_in_catalog" in codes(report_for(line(source_refs=["не словник"])))


def test_an_ambiguous_match_is_untouched() -> None:
    """This rule is about articles the base lacks, not about choosing between
    two it has."""
    assert "ambiguous_match" in codes(report_for(line(match_status="ambiguous")))
