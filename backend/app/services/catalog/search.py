"""Catalog retrieval and intelligent product matching.

The client's workbook resolves prices with an exact ``VLOOKUP`` on the item
name, so an exact normalised hit is always preferred and always trusted. When
there is no exact hit we fall back to retrieval + ranking, and we never invent
a product: an unmatched request becomes ``not_in_catalog`` and, if it matters
to the price, a question for the operator.

Retrieval is deliberately local (SQLite FTS5 + rapidfuzz over ~850 rows). The
catalog is never shipped into a prompt; the model asks for a *concept* and gets
back a short candidate list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from rapidfuzz import fuzz, process
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ...models import CatalogItem
from ..rules.engine import normalize_name

# Tokens that carry the technical meaning of a product name: diameters, sizes,
# densities, thicknesses. Two items that differ only in these are different
# products, so a fuzzy score alone must never decide between them.
SPEC_RE = re.compile(
    r"(?:d-?\s*\d+|dn\s*\d+|\d+\s*[x×хХ]\s*\d+(?:\s*[x×хХ]\s*\d+)?"
    r"|\d+(?:[.,]\d+)?\s*(?:мм|см|м²|м3|м³|м\.п|мкм|г/м²|вт|w|л|кг|т|а|в|v))",
    re.IGNORECASE,
)

STOPWORDS = {"для", "та", "і", "в", "з", "на", "під", "до", "по", "від", "мм", "см"}


@dataclass
class Candidate:
    item: CatalogItem
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.item.id,
            "name": self.item.name,
            "category": self.item.category,
            "unit": self.item.unit,
            "unit_price": self.item.unit_price,
            "unit_cost": self.item.unit_cost,
            "kind": self.item.kind,
            "score": round(self.score, 1),
            "reasons": self.reasons,
        }


@dataclass
class MatchResult:
    status: str  # matched | ambiguous | not_in_catalog
    best: Candidate | None
    candidates: list[Candidate]
    confidence: str  # high | medium | low
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "confidence": self.confidence,
            "reason": self.reason,
            "best": self.best.to_dict() if self.best else None,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def normalize_unit(unit: str) -> str:
    """Collapse the ways one unit of measure is written.

    The price base is not consistent with itself: "м.п" on 75 rows and "м.п."
    on one, "шт" on 595 and "шт." on one, and the drawings use both spellings
    freely. Compared as plain strings those read as different units and a real
    match would be refused for a trailing dot.

    No case in the reference projects currently turns on this -- the mismatches
    there are genuine, м.п of edging against an article sold by the piece -- so
    this prevents a fault rather than fixing an observed one.
    """
    return normalize_name(unit).replace(".", "").replace(" ", "")


def specs(name: str) -> set[str]:
    """Extract the size/spec tokens that must agree between two product names."""
    return {
        re.sub(r"\s+", "", m.group(0).lower().replace(",", ".").replace("х", "x").replace("×", "x"))
        for m in SPEC_RE.finditer(name or "")
    }


def _keywords(name: str) -> list[str]:
    words = re.findall(r"[\w'’²³]+", normalize_name(name))
    return [w for w in words if len(w) > 2 and w not in STOPWORDS]


# Ukrainian is heavily inflected and the drawings and the price base disagree on
# case and number all the time: a schedule says "Газон", "Плити ходові бетонні",
# the base says "Площа газону", "Плита ходова бетонна". Truncating tokens to a
# short stem makes those line up without pulling in a morphology library.
STEM_LENGTH = 4


def stems(name: str) -> set[str]:
    return {w[:STEM_LENGTH] for w in _keywords(name) if len(w) >= STEM_LENGTH}


def stem_overlap(a: str, b: str) -> float:
    """Fraction of ``a``'s stems that also occur in ``b``. 0.0 when ``a`` is empty."""
    sa, sb = stems(a), stems(b)
    return len(sa & sb) / len(sa) if sa else 0.0


class CatalogSearch:
    """Loads the active catalog once and answers lookups against it."""

    def __init__(self, session: Session):
        self.session = session
        self._items: list[CatalogItem] | None = None
        self._by_norm: dict[str, CatalogItem] = {}

    # -- data -----------------------------------------------------------------
    @property
    def items(self) -> list[CatalogItem]:
        if self._items is None:
            self._items = list(
                self.session.scalars(select(CatalogItem).where(CatalogItem.active.is_(True))).all()
            )
            # First occurrence wins, matching the workbook's VLOOKUP semantics.
            for it in self._items:
                self._by_norm.setdefault(it.name_norm, it)
        return self._items

    def get_exact(self, name: str) -> CatalogItem | None:
        self.items  # ensure loaded
        return self._by_norm.get(normalize_name(name))

    # -- retrieval ------------------------------------------------------------
    def fts(self, query: str, limit: int = 200) -> list[CatalogItem]:
        """Full-text prefilter, matched on stems.

        Matching whole words here was actively harmful: ``плити*`` does not
        prefix-match ``Плита``, so an inflected query returned a small but
        *wrong* pool and the correct article was never scored at all. Stems
        (``плит*``) match across cases and numbers.
        """
        roots = sorted(stems(query))
        if not roots:
            return []
        expr = " OR ".join(f"{r}*" for r in roots[:8])
        try:
            rows = self.session.execute(
                text("SELECT item_id FROM catalog_fts WHERE catalog_fts MATCH :q LIMIT :n"),
                {"q": expr, "n": limit},
            ).all()
        except Exception:
            return []
        ids = {r[0] for r in rows}
        return [i for i in self.items if i.id in ids]

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        category: str | None = None,
        unit: str | None = None,
        limit: int = 8,
    ) -> list[Candidate]:
        """Rank catalog items against a free-text description."""
        # A narrow prefilter that misses the right row is worse than none, so a
        # thin FTS result falls back to the full catalog.
        prefiltered = self.fts(query)
        pool: Iterable[CatalogItem] = prefiltered if len(prefiltered) >= 25 else self.items
        if kind:
            pool = [i for i in pool if i.kind == kind]
        if category:
            cat = normalize_name(category)
            narrowed = [i for i in pool if normalize_name(i.category) == cat]
            pool = narrowed or list(pool)
        pool = list(pool)
        if not pool:
            return []

        qnorm = normalize_name(query)
        qspecs = specs(query)
        qwords = set(_keywords(query))

        # Score the whole pool, not a fuzzy top-N. The bonuses below (spec
        # tokens, unit, stems) routinely promote an item the raw fuzzy score
        # ranks poorly: "Плити ходові бетонні" sits outside the fuzzy top-25
        # yet is an exact stem match for "Плита ходова бетонна 120х40х6см".
        # The catalog is ~850 rows, so scoring all of it costs nothing.
        scored = process.extract(
            qnorm,
            {i.id: i.name_norm for i in pool},
            scorer=fuzz.token_set_ratio,
            limit=None,
        )
        by_id = {i.id: i for i in pool}

        out: list[Candidate] = []
        for _text, base_score, item_id in scored:
            item = by_id[item_id]
            score = float(base_score)
            reasons: list[str] = [f"схожість назви {base_score:.0f}%"]

            ispecs = specs(item.name)
            if qspecs:
                overlap = qspecs & ispecs
                if overlap:
                    score += 12 * len(overlap)
                    reasons.append("збіг характеристик: " + ", ".join(sorted(overlap)))
                elif ispecs:
                    # The request named a size and this item names a different
                    # one -- almost certainly the wrong article.
                    score -= 25
                    reasons.append("характеристики не збігаються")

            if unit and normalize_unit(unit) == normalize_unit(item.unit):
                score += 6
                reasons.append(f"одиниця виміру збігається ({item.unit})")

            iwords = set(_keywords(item.name))
            cover = len(qwords & iwords) / len(qwords) if qwords and iwords else 0.0
            if cover:
                score += 10 * cover
                if cover >= 0.6:
                    reasons.append(f"покриття ключових слів {cover:.0%}")

            # Case and number differ constantly between drawings and the price
            # base; a stem match recovers those without loosening the scorer.
            overlap = stem_overlap(query, item.name)
            if overlap:
                score += 18 * overlap
                if overlap >= 0.75 and cover < 0.6:
                    reasons.append(f"збіг основ слів {overlap:.0%} (різні відмінки)")

            if item.unit_price <= 0:
                score -= 8
                reasons.append("у базі немає ціни реалізації")

            out.append(Candidate(item=item, score=score, reasons=reasons))

        out.sort(key=lambda c: c.score, reverse=True)
        return out[:limit]

    # -- matching -------------------------------------------------------------
    def match(
        self,
        query: str,
        *,
        kind: str | None = None,
        category: str | None = None,
        unit: str | None = None,
        accept_at: float = 92.0,
        ambiguous_gap: float = 6.0,
    ) -> MatchResult:
        """Resolve a requested item to a catalog row, or refuse to.

        Thresholds are conservative on purpose: an "almost right" article at the
        wrong diameter is worse than an explicit question.
        """
        exact = self.get_exact(query)
        if exact is not None:
            cand = Candidate(item=exact, score=100.0, reasons=["точний збіг назви з базою"])
            return MatchResult(
                status="matched",
                best=cand,
                candidates=[cand],
                confidence="high",
                reason="Назва повністю збігається з позицією у базі (як VLOOKUP у шаблоні).",
            )

        candidates = self.search(query, kind=kind, category=category, unit=unit)
        if not candidates:
            return MatchResult(
                status="not_in_catalog",
                best=None,
                candidates=[],
                confidence="low",
                reason=f"У каталозі немає позиції, схожої на «{query}».",
            )

        best = candidates[0]
        runner_up = candidates[1].score if len(candidates) > 1 else 0.0

        if best.score >= accept_at and (best.score - runner_up) >= ambiguous_gap:
            return MatchResult(
                status="matched",
                best=best,
                candidates=candidates,
                confidence="high" if best.score >= 97 else "medium",
                reason=f"Найкращий кандидат з відривом ({best.score:.0f} проти {runner_up:.0f}).",
            )

        if best.score >= 70:
            return MatchResult(
                status="ambiguous",
                best=best,
                candidates=candidates,
                confidence="low",
                reason=(
                    f"Кілька позицій підходять однаково добре "
                    f"({best.score:.0f} проти {runner_up:.0f}) — потрібен вибір користувача."
                ),
            )

        return MatchResult(
            status="not_in_catalog",
            best=None,
            candidates=candidates,
            confidence="low",
            reason=f"Найкращий кандидат надто слабкий ({best.score:.0f}).",
        )

    def match_many(self, queries: Sequence[str], **kwargs: Any) -> dict[str, MatchResult]:
        return {q: self.match(q, **kwargs) for q in queries}
