"""Product matching against the client's real catalog.

These tests need the catalog imported (``scripts/import_catalog.py``); they skip
themselves otherwise so the suite still runs on a clean checkout.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import CatalogItem
from app.services.catalog.search import CatalogSearch, specs


@pytest.fixture(scope="module")
def search() -> CatalogSearch:
    init_db()
    session = SessionLocal()
    count = session.scalar(select(CatalogItem).where(CatalogItem.active.is_(True)).limit(1))
    if count is None:
        session.close()
        pytest.skip("catalog not imported; run scripts/import_catalog.py")
    return CatalogSearch(session)


def test_exact_name_matches_like_vlookup(search: CatalogSearch) -> None:
    result = search.match("Гачок Металевий (для Сітки та Агрополотна)")
    assert result.status == "matched"
    assert result.confidence == "high"
    assert result.best is not None
    assert result.best.item.unit_price > 0


def test_normalisation_absorbs_whitespace_and_case(search: CatalogSearch) -> None:
    # The template itself contains double spaces and trailing blanks.
    result = search.match("  щебінь,   ФРАКЦІЯ 20-40мм ")
    assert result.status == "matched"
    assert result.best is not None
    assert "20-40" in result.best.item.name


def test_a_different_diameter_is_not_a_match(search: CatalogSearch) -> None:
    """A near-identical name at the wrong size must not be accepted silently."""
    d32 = search.get_exact("Труба поліетиленова, D-32 PN10*2,4")
    d40 = search.get_exact("Труба поліетиленова, D-40 PN10*2,4")
    if d32 is None or d40 is None:
        pytest.skip("pipe articles absent from this catalog build")

    result = search.match("Труба поліетиленова, D-40 PN10*2,4")
    assert result.best is not None
    assert result.best.item.id == d40.id


def test_unknown_product_is_refused_not_invented(search: CatalogSearch) -> None:
    result = search.match("Квантовий генератор поля Тесла 9000")
    assert result.status == "not_in_catalog"
    assert result.best is None


def test_spec_tokens_are_extracted() -> None:
    assert "d-32" in {s.replace(" ", "") for s in specs("Труба поліетиленова, D-32 PN10*2,4")}
    assert specs("Геотекстиль 200г/м²,білий, 2м.")
    assert not specs("Витратні матеріали")


def test_search_returns_ranked_candidates_with_reasons(search: CatalogSearch) -> None:
    candidates = search.search("геотекстиль білий 200", limit=5)
    assert candidates
    assert all(c.reasons for c in candidates)
    scores = [c.score for c in candidates]
    assert scores == sorted(scores, reverse=True)


def test_kind_filter_separates_works_from_materials(search: CatalogSearch) -> None:
    works = search.search("монтаж бруківки", kind="work", limit=5)
    assert works
    assert all(c.item.kind == "work" for c in works)
