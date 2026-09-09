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

# A manufacturer's article number: "Ideal Lux 306872", "EGLO 79024". Five digits
# is where this stops being ambiguous and the catalogue says so — of the codes
# it carries, all thirteen with five or more digits are unique to one article,
# while every four-digit one (1000, 1200, 1500, 2000, 3000) is a length, a
# volume or a nozzle range shared by several. So four digits is a dimension and
# five is an identity.
# Only a digit or a dimension separator disqualifies a neighbour: a comma or a
# hyphen is punctuation, and requiring clear space either side missed both
# "NOWODVORSKI 30865," and "IDEAL LUX 81069-".
SKU_RE = re.compile(r"(?<![\dxX×хХ])\d{5,}(?![\dxX×хХ])")

# A model designation: "Plurijet 4/100", "X-CORE-401-E", "MP1000". Either it
# begins with a letter, or it is digits joined by a slash. Digits joined by a
# hyphen are excluded on purpose — "фракція 0-5", "щебінь 20-40" are size
# ranges, and treating one as an identity would price gravel as a pipe.
MODEL_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9]+)+|[A-Za-z]{1,6}\d{2,}|\d+/\d+"
)

# Sales notes the price base carries inside the article name. They say nothing
# about which product it is and they wreck a name comparison: the catalogue
# writes "Світильник вуличний бра IDEAL LUX 81069- 15% знижки від вартсоті
# світильника" where a drawing writes "Світильник фасадний Ф-1, IDEAL LUX 81069".
MARKETING_RE = re.compile(
    r"\s*[-—]?\s*\d+\s*%\s*знижк\w*.*$|\s*[-—]\s*акці\w*.*$", re.IGNORECASE
)


def strip_marketing(name: str) -> str:
    """Drop a discount note from an article name."""
    return MARKETING_RE.sub("", name or "").strip(" -—,")


def sku_codes(name: str) -> set[str]:
    """Manufacturer article numbers written in a name."""
    return set(SKU_RE.findall(name or ""))


def model_codes(name: str) -> set[str]:
    """Model designations written in a name, lower-cased."""
    found = set()
    for token in MODEL_RE.findall(name or ""):
        low = token.lower().strip("-/")
        if any(c.isdigit() for c in low) and len(low) >= 3:
            found.add(low)
    return found


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


# A run of dimensions with an optional unit: "1200х400", "120х40х6см",
# "100х50х50 см", "50х25х4,4см".
DIMENSION_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[x×хХ*]\s*(\d+(?:[.,]\d+)?)"
    r"(?:\s*[x×хХ*]\s*(\d+(?:[.,]\d+)?))?"
    r"\s*(мм|см|м)?\b",
    re.IGNORECASE,
)

_TO_MM = {"мм": 1.0, "см": 10.0, "м": 1000.0}


def dimension_forms(name: str) -> set[str]:
    """Every dimension in a name, as pairs of millimetres.

    Two names describe the same object in different units all the time: a
    drawing writes "Плити 1200х400" and the price base writes "Плита ходова
    бетонна 120х40х6см". Compared as written those share nothing, and because
    each carries a size token that the other lacks, the scorer used to subtract
    25 for a size disagreement -- pushing apart two names for one product.

    So each run of numbers is converted to millimetres. Where no unit is
    written the reading is genuinely ambiguous, and both are emitted rather
    than assumed: 1200х400 is either 1200 mm or 1200 cm and only the other name
    can say which.

    Emitted pairwise rather than as one token so that a name giving two
    dimensions meets a name giving three. The base lists a thickness the
    drawing does not, and "120х40х6см" must still meet "1200х400".
    """
    out: set[str] = set()
    for found in DIMENSION_RE.finditer(name or ""):
        numbers = [
            float(g.replace(",", "."))
            for g in (found.group(1), found.group(2), found.group(3))
            if g
        ]
        unit = (found.group(4) or "").lower()
        scales = [_TO_MM[unit]] if unit in _TO_MM else [1.0, 10.0]
        for scale in scales:
            millimetres = sorted(round(n * scale) for n in numbers)
            for i, first in enumerate(millimetres):
                for second in millimetres[i + 1:]:
                    out.add(f"{first}x{second}мм")
    return out


def specs(name: str) -> set[str]:
    """Extract the size/spec tokens that must agree between two product names."""
    written = {
        re.sub(r"\s+", "", m.group(0).lower().replace(",", ".").replace("х", "x").replace("×", "x"))
        for m in SPEC_RE.finditer(name or "")
    }
    return written | dimension_forms(name)


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
        self._by_sku: dict[str, list[CatalogItem]] = {}
        self._by_model: dict[str, list[CatalogItem]] = {}
        self._by_dimension: dict[str, list[CatalogItem]] = {}

    # -- data -----------------------------------------------------------------
    def index(self, items: list[CatalogItem]) -> None:
        """Build the lookups the matcher reads: by name, number, model, size."""
        self._items = items
        self._by_norm.clear()
        self._by_sku.clear()
        self._by_model.clear()
        self._by_dimension.clear()
        for it in items:
            # First occurrence wins, matching the workbook's VLOOKUP semantics.
            self._by_norm.setdefault(it.name_norm, it)
            self._by_norm.setdefault(normalize_name(strip_marketing(it.name)), it)
            for code in sku_codes(it.name):
                self._by_sku.setdefault(code, []).append(it)
            for code in model_codes(it.name):
                self._by_model.setdefault(code, []).append(it)
            for form in dimension_forms(it.name):
                self._by_dimension.setdefault(form, []).append(it)

    @property
    def items(self) -> list[CatalogItem]:
        if self._items is None:
            self.index(list(
                self.session.scalars(
                    select(CatalogItem).where(CatalogItem.active.is_(True))
                ).all()
            ))
        return self._items

    def by_article_number(self, query: str) -> CatalogItem | None:
        """The one article whose manufacturer's number the query also carries.

        ``None`` when the query names no such number, when the catalogue carries
        none of them, or when more than one article does — a code shared by two
        products identifies neither, and that is a question, not a match.
        """
        self.items  # ensure loaded
        for code in sku_codes(query):
            hits = self._by_sku.get(code, [])
            if len(hits) == 1:
                return hits[0]
        return None

    def by_dimensions(self, query: str) -> CatalogItem | None:
        """The one article of its kind carrying the size the query gives.

        A size identifies a product about as well as an article number does,
        once it is read in the same units: "Плити 1200х400" and "Плита ходова
        бетонна 120х40х6см" are one slab, and nothing else in the catalogue is
        1200 by 400. As prose they score 49 out of 100 and lose to "Підготовка
        подушки під плити", which shares the word and none of the object.

        Two conditions, and both are needed. The size must belong to exactly
        one article, and the two names must agree on at least one word that is
        not the size — otherwise a 1200x400 slab would answer for a 1200x400
        sheet of anything else that happened to be alone at that size.
        """
        self.items
        for form in dimension_forms(query):
            hits = self._by_dimension.get(form, [])
            if len(hits) != 1:
                continue
            item = hits[0]
            if stems(query) & stems(item.name):
                return item
        return None

    def by_model(self, query: str) -> CatalogItem | None:
        """The one article sharing a model designation with the query.

        "Насосна станція Pedrollo Plurijet 4/100" and "Насос Pedrollo Plurijet
        4/100 (100л/хв)" agree on 4/100, and the 4/200 in the next row of the
        catalogue is a different pump at two and a half times the price. Where
        two articles share the designation this returns nothing: "X-CORE-401-E"
        against an X-CORE in eight zones and an X2 in four is a real choice
        between two products and belongs to the estimator.
        """
        self.items
        for code in model_codes(query):
            hits = self._by_model.get(code, [])
            if len(hits) == 1:
                return hits[0]
        return None

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
        # Compared without the discount notes: "- 15% знижки від вартсоті
        # світильника" is eleven tokens of sales copy on the end of an article
        # name, and token_set_ratio counts every one of them against the match.
        scored = process.extract(
            qnorm,
            {i.id: normalize_name(strip_marketing(i.name)) for i in pool},
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
        confident_at: float = 80.0,
        confident_margin: float = 25.0,
    ) -> MatchResult:
        """Resolve a requested item to a catalog row, or refuse to.

        Thresholds are conservative on purpose: an "almost right" article at the
        wrong diameter is worse than an explicit question.

        A manufacturer's article number outranks all of it. A drawing writes
        "Прожектор Пр-2, NOWODVORSKI 30865, 7 Вт" and the catalogue writes
        "Світильник вуличний прожектор NOWODVORSKI 30865- 15% знижки від
        вартсоті світильника": as prose they share almost nothing and the fuzzy
        score put the right article at 46 out of 100, below three unrelated
        rows. As identities they are the same object and 30865 says so.
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

        for finder, label, why in (
            (self.by_article_number, "артикул виробника",
             "Збіг за артикулом виробника — це та сама позиція, як би не"
             " відрізнялися назви."),
            (self.by_model, "модель",
             "Збіг за позначенням моделі, унікальним у каталозі."),
            (self.by_dimensions, "габарити",
             "Збіг за габаритами, унікальними в каталозі, і спільним словом у назві."),
        ):
            item = finder(query)
            if item is None:
                continue
            cand = Candidate(item=item, score=100.0,
                             reasons=[f"{label}: {item.name}"])
            return MatchResult(
                status="matched", best=cand, candidates=[cand],
                confidence="high", reason=why,
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
        margin = best.score - runner_up

        # Two ways to be sure, and a candidate needs one of them.
        #
        # A high score with the field close behind is the original test: the
        # name is nearly the base's own wording.
        #
        # A lower score with the field far behind is the other. A drawing that
        # writes "Кабель ВВГ-нг 3х2,5 — група 01" scores 85 against the base's
        # "Електричний кабель 3х2.5 ВВГ НГ LS" — too little prose in common for
        # 92 — while the next candidate, a cable of a different cross-section,
        # scores 42. Forty-three points of daylight is not a close call, and
        # refusing it made the estimator answer a question whose answer was the
        # only thing on the list. The margin is measured the same way for every
        # article; nothing here knows what a cable is.
        decisive = best.score >= accept_at and margin >= ambiguous_gap
        clear = best.score >= confident_at and margin >= confident_margin
        if decisive or clear:
            return MatchResult(
                status="matched",
                best=best,
                candidates=candidates,
                confidence="high" if best.score >= 97 or clear else "medium",
                reason=(
                    f"Найкращий кандидат з відривом ({best.score:.0f} проти "
                    f"{runner_up:.0f}, відрив {margin:.0f})."
                ),
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
