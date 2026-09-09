"""What an article was last sold for.

The rule the client set: **the canonical price of an article is the one on the
most recently issued proposal that carried it.** Two facts from their own data
make that the only workable rule:

* the price base quotes no price at all for any of its 202 plants — nursery
  quotes are per project — so for planting the invoices are the only source;
* of the articles the base does price, 67 were invoiced at a different rate at
  least once, and in 55 of those the base figure is one of the invoiced ones —
  usually the lowest. The base reads as a floor, and the sale price drifts
  above it.

Dating. The printed КП leaves "Дата рахунку" blank and the PDFs carry no
metadata, so a proposal is dated from its filename: an explicit ``15.06.2026``
where one is written, otherwise the quarter (``КП 2026 (1 Квартал) …``). That
gives quarter precision for most of them, so several proposals routinely share
a date. Where they do and their prices disagree, the tie goes to the price the
most proposals in that group used — the rate the managers converged on — and
the alternatives are kept on the entry so the operator can see what was passed
over. Nothing here silently averages: an average is a price nobody ever
charged.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from rapidfuzz import fuzz, process

from ..catalog.search import normalize_unit, specs, stem_overlap, stems
from ..rules.engine import normalize_name
from .proposals import IssuedOn, Proposal, issued_on

# A price is accepted for a name that is not identical only when the match is
# as strong as the catalogue demands of its own lookups.
ACCEPT_AT = 92.0
AMBIGUOUS_GAP = 6.0


@dataclass
class Sale:
    """One article, on one proposal, at one price."""

    name: str
    unit: str
    unit_price: float
    quantity: float
    block: str
    project: str
    issued: IssuedOn | None

    @property
    def sort_key(self) -> tuple[dt.date, int]:
        """Newest first. An undated proposal sorts oldest — it cannot outrank
        one that says when it was issued."""
        if self.issued is None:
            return (dt.date.min, 0)
        # A proposal dated to the day is more precisely known than one dated to
        # its quarter; on the same date, prefer the precise one.
        return (self.issued.date, 1 if self.issued.precision == "day" else 0)


@dataclass
class LastPrice:
    """The canonical price of one article, and where it came from."""

    name: str
    unit: str
    unit_price: float
    block: str
    project: str
    issued: IssuedOn | None
    sales: int = 0
    alternatives: list[float] = field(default_factory=list)
    matched_name: str = ""  # the invoiced name, when it is not the one asked for
    score: float = 100.0

    @property
    def exact(self) -> bool:
        return not self.matched_name

    def trace(self) -> str:
        when = f" від {self.issued}" if self.issued else ""
        via = f" (як «{self.name}»)" if self.matched_name else ""
        note = f"; інші ціни в історії: {self._other}" if self.alternatives else ""
        return f"остання продана ціна — КП «{self.project}»{when}{via}{note}"

    @property
    def _other(self) -> str:
        return ", ".join(f"{p:g}" for p in self.alternatives)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "unit": self.unit,
            "unit_price": self.unit_price,
            "block": self.block,
            "project": self.project,
            "issued": str(self.issued) if self.issued else "",
            "sales": self.sales,
            "alternatives": self.alternatives,
            "matched_name": self.matched_name,
            "score": round(self.score, 1),
        }


def _canonical(sales: Sequence[Sale]) -> LastPrice:
    """The price from the newest proposal, ties broken by how many used it."""
    newest = max(s.sort_key for s in sales)
    latest = [s for s in sales if s.sort_key == newest]

    counts = Counter(s.unit_price for s in latest)
    # Most used within the newest group. On a dead heat, the higher figure: the
    # estimator sees both on the line and can lower it, whereas an estimate that
    # comes in under what the work costs is only found out afterwards.
    price = max(counts, key=lambda p: (counts[p], p))
    chosen = next(s for s in latest if s.unit_price == price)

    others = sorted({s.unit_price for s in sales} - {price})
    return LastPrice(
        name=chosen.name,
        unit=chosen.unit or next((s.unit for s in sales if s.unit), ""),
        unit_price=price,
        block=chosen.block,
        project=chosen.project,
        issued=chosen.issued,
        sales=len(sales),
        alternatives=others,
    )


class InvoicedPrices:
    """The price history, keyed by article name.

    Built from parsed proposals or from the ``historical_lines`` table; both
    routes produce the same :class:`Sale` list, so the lookup does not care
    which was used.
    """

    def __init__(self, sales: Iterable[Sale]):
        self._by_name: dict[str, list[Sale]] = defaultdict(list)
        for sale in sales:
            self._by_name[normalize_name(sale.name)].append(sale)
        self._cache: dict[str, LastPrice | None] = {}

    # -- construction ---------------------------------------------------------
    @classmethod
    def from_proposals(cls, proposals: Iterable[Proposal]) -> "InvoicedPrices":
        sales: list[Sale] = []
        for proposal in proposals:
            when = proposal.issued
            for row in proposal.rows:
                sales.append(
                    Sale(
                        name=row.name,
                        unit=row.unit,
                        unit_price=row.unit_price,
                        quantity=row.quantity,
                        block=row.block,
                        project=proposal.project,
                        issued=when,
                    )
                )
        return cls(sales)

    @classmethod
    def from_db(cls, session, *, exclude: Iterable[str] = ()) -> "InvoicedPrices":
        """The stored history, optionally without some proposals.

        ``exclude`` names proposals to leave out, matched as a substring of the
        project name. Measuring a project against its own signed proposal has
        to drop that proposal, or the prices come from the answer sheet.
        """
        from sqlalchemy import select

        from ...models import HistoricalEstimate, HistoricalLine

        skip = [s for s in exclude if s]
        dated = {
            e.id: (e.source_file, issued_on(e.dated) or issued_on(e.source_file))
            for e in session.scalars(select(HistoricalEstimate)).all()
            if not any(s.lower() in e.source_file.lower() for s in skip)
        }
        sales = [
            Sale(
                name=line.name,
                unit=line.unit,
                unit_price=line.unit_price,
                quantity=line.quantity,
                block=line.block,
                project=dated.get(line.estimate_id, ("", None))[0],
                issued=dated.get(line.estimate_id, ("", None))[1],
            )
            for line in session.scalars(select(HistoricalLine)).all()
            if line.estimate_id in dated
        ]
        return cls(sales)

    # -- data -----------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._by_name)

    @property
    def names(self) -> list[str]:
        return list(self._by_name)

    def sales_of(self, name: str) -> list[Sale]:
        return list(self._by_name.get(normalize_name(name), []))

    def contested(self) -> dict[str, LastPrice]:
        """Articles invoiced at more than one price, with the winner of each."""
        out: dict[str, LastPrice] = {}
        for key, sales in self._by_name.items():
            if len({s.unit_price for s in sales}) > 1:
                out[key] = _canonical(sales)
        return out

    def variants(self, name: str, *, limit: int = 8) -> list[LastPrice]:
        """Every article the invoices carry under this name, size by size.

        "Сосна гірська" was sold four times over, as «Мопс» d30-40, «Mops»
        d=30-60, d20-30см and d40см — four sizes at four prices. :meth:`lookup`
        refuses that on purpose, because the size *is* the price and guessing
        one would invent money. This is what it refused: the choice, priced,
        so the estimator can answer it in one click instead of ringing a
        nursery.

        Matched on the words of the request being contained in the article's:
        the request is the short name, the article adds the size to it.
        """
        wanted = set(stems(name))
        if not wanted:
            return []
        out: list[LastPrice] = []
        for key, sales in self._by_name.items():
            if wanted and wanted <= set(stems(key)):
                out.append(_canonical(sales))
        # Cheapest first: a size list reads as a price list.
        out.sort(key=lambda p: p.unit_price)
        return out[:limit]

    # -- lookup ---------------------------------------------------------------
    def exact(self, name: str) -> LastPrice | None:
        sales = self._by_name.get(normalize_name(name))
        return _canonical(sales) if sales else None

    def lookup(self, name: str, *, unit: str = "") -> LastPrice | None:
        """The last sold price for ``name``, exactly or by a confident match.

        The invoices name the same article in more ways than the base does, so
        an exact miss is common and worth a second attempt. It is the same test
        the catalogue applies to itself: the sizes in the name must agree, and
        the best candidate must be both strong and clear of the runner-up. An
        "almost right" article at the wrong diameter is worse than no price.
        """
        key = normalize_name(name)
        if key in self._cache:
            return self._cache[key]

        hit = self.exact(name)
        if hit is None:
            hit = self._fuzzy(name, unit=unit)
        self._cache[key] = hit
        return hit

    def _fuzzy(self, name: str, *, unit: str = "") -> LastPrice | None:
        if not self._by_name:
            return None
        query = normalize_name(name)
        qspecs = specs(name)

        scored = process.extract(
            query, {k: k for k in self._by_name}, scorer=fuzz.token_set_ratio, limit=None
        )
        ranked: list[tuple[float, str]] = []
        for _text, base, key in scored:
            score = float(base)
            ispecs = specs(key)
            if qspecs:
                overlap = qspecs & ispecs
                if overlap:
                    score += 12 * len(overlap)
                elif ispecs:
                    # The request named a size and this article names another.
                    score -= 25
            score += 18 * stem_overlap(name, key)
            if unit:
                units = {normalize_unit(s.unit) for s in self._by_name[key]}
                if normalize_unit(unit) in units:
                    score += 6
            ranked.append((score, key))

        ranked.sort(reverse=True)
        best_score, best_key = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        if best_score < ACCEPT_AT or (best_score - runner_up) < AMBIGUOUS_GAP:
            return None

        price = _canonical(self._by_name[best_key])
        price.matched_name = price.name
        price.score = best_score
        return price


__all__ = ["InvoicedPrices", "LastPrice", "Sale"]
