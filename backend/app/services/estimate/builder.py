"""Build an estimate from an object model.

The flow mirrors how the client fills their own workbook:

1. Open the sections the object actually needs.
2. Instantiate every template row of those sections (quantity 0), so the rules
   have the operands they reference.
3. Fill the *driver* rows and the rows whose quantity is stated in the drawings
   (plants, slabs, edging, coverage areas).
4. Let the rule engine derive everything else -- aggregates, sand and gravel
   volumes, fabric with its 20% overlap, fixings, deliveries, labour.
5. Price every line from the catalog by exact name, exactly like their VLOOKUP,
   then take the price the article was **last sold** at where the issued
   proposals carry one. That order is the client's rule and their data's:
   the base quotes no price at all for its 202 plants, and where an article has
   been invoiced since the base was written, the invoice is the newer figure.
   See :mod:`app.services.history.prices`.

Sections with no positive quantity disappear from the output, which is why
their five sample proposals each show a different set of sections.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.orm import Session

from ...config import get_settings
from ..catalog.search import CatalogSearch, MatchResult
from ..history.prices import InvoicedPrices, LastPrice
from ..rules.engine import Draft, DraftLine, RuleEngine, RuleOutcome, normalize_name
from ..rules.totals import EstimateTotals, compute_totals

log = logging.getLogger(__name__)



@dataclass
class BuildResult:
    draft: Draft
    totals: EstimateTotals
    outcomes: list[RuleOutcome] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    # Lines whose price came from an issued proposal rather than the price base.
    repriced: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class TemplateLayout:
    """The compiled template: sections, rows and their quantity rules."""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.sections: dict[str, dict[str, Any]] = {s["key"]: s for s in data.get("sections", [])}
        self.order: list[str] = [s["key"] for s in data.get("sections", [])]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "TemplateLayout":
        settings = get_settings()
        p = Path(path) if path else settings.rules_dir / settings.template_layout_file
        if not p.exists():
            raise FileNotFoundError(
                f"Не знайдено скомпільований шаблон {p}. "
                "Запустіть: python scripts/compile_template.py <Шаблон для ШІ.xlsx>"
            )
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def title(self, key: str) -> str:
        return self.sections.get(key, {}).get("title", key)

    def titles(self) -> dict[str, str]:
        return {k: v.get("title", k) for k, v in self.sections.items()}

    def lines(self, key: str) -> list[dict[str, Any]]:
        return self.sections.get(key, {}).get("lines", [])

    def block_of(self, key: str, name: str) -> str:
        target = normalize_name(name)
        for line in self.lines(key):
            if normalize_name(line["name"]) == target:
                return line["block"]
        return "materials"

    def drivers(self, key: str) -> list[str]:
        """Input rows of a section that other rows' formulas read.

        These are the template's own quantity-only rows -- "Довжина траншей (м)",
        "Загальна площа бруківка", "Площа газону (рулонного)" -- which the
        estimator fills in and everything else is computed from.
        """
        return [l["name"] for l in self.lines(key) if l["block"] == "driver"]

    def option_rows(self, key: str) -> list[str]:
        return [l["name"] for l in self.lines(key) if l.get("option_row")]


class EstimateBuilder:
    def __init__(
        self,
        session: Session,
        layout: TemplateLayout | None = None,
        *,
        use_invoiced_prices: bool = True,
        prices: InvoicedPrices | None = None,
    ):
        self.session = session
        self.layout = layout or TemplateLayout.load()
        self.catalog = CatalogSearch(session)
        self.engine = RuleEngine(self.layout.data)
        # Off leaves the catalogue price standing, which is what a machine with
        # no imported proposals gets anyway. Kept as a switch so the effect of
        # the rule can be measured against the same project both ways.
        self.use_invoiced_prices = use_invoiced_prices
        # A caller may supply the history instead of the stored one -- measuring
        # a project against its own signed proposal has to exclude that proposal
        # from the prices, or the answer is circular.
        self._prices = prices

    @property
    def prices(self) -> InvoicedPrices:
        """The last sold price of every article, loaded once per builder."""
        if self._prices is None:
            self._prices = InvoicedPrices.from_db(self.session)
        return self._prices

    # -- construction ---------------------------------------------------------
    def build(
        self,
        *,
        sections: Iterable[str],
        quantities: dict[str, dict[str, float]] | None = None,
        plants: list[dict[str, Any]] | None = None,
        options: dict[str, bool] | None = None,
        settings: dict[str, Any] | None = None,
        source_refs: dict[str, list[dict[str, Any]]] | None = None,
        reasons: dict[str, list[str]] | None = None,
    ) -> BuildResult:
        """Assemble, price and compute an estimate.

        ``quantities`` maps ``section -> {line name: quantity}`` and only needs
        the values that come from the documents; everything derivable is left
        to the rules.
        """
        quantities = quantities or {}
        plants = plants or []
        options = {normalize_name(k): v for k, v in (options or {}).items()}
        source_refs = source_refs or {}
        reasons = reasons or {}

        result = BuildResult(draft=Draft(options=options, settings=settings or {}),
                             totals=EstimateTotals())
        draft = result.draft
        active = [s for s in self.layout.order if s in set(sections) and s != "summary"]

        for key in active:
            for tpl in self.layout.lines(key):
                line = DraftLine(
                    name=tpl["name"],
                    section=key,
                    block=tpl["block"],
                    qty_source="template_default",
                )
                if tpl.get("option_row"):
                    line.option = options.get(normalize_name(tpl["name"]), False)
                if tpl["block"] == "driver":
                    # An input quantity, not a billable line: no catalog lookup,
                    # no price, and it never reaches the totals.
                    line.match_status = "matched"
                    line.confidence = "high"
                    line.unit = _driver_unit(tpl["name"])
                else:
                    self._price(line, result)
                draft.lines.append(line)

            # Plants are not in the template as fixed rows: the operator picks
            # them per project, so they come from the drawings.
            if key == "planting":
                self._add_plants(draft, plants, result)

        # Apply stated quantities.
        for key, values in quantities.items():
            for name, qty in values.items():
                line = draft.by_name(name, key)
                if line is None or line.section != key:
                    line = self._add_free_line(draft, key, name, result)
                    if line is None:
                        continue
                line.quantity = float(qty)
                line.qty_source = "document"
                line.locked = True  # a stated number is not the rules' to change
                # Added to, never replaced: where the quantity came from is a
                # different question from where the price came from, and the
                # line already carries the answer to the second one.
                ref_key = f"{key}|{normalize_name(name)}"
                line.source_refs += source_refs.get(
                    ref_key, source_refs.get(normalize_name(name), [])
                )
                line.reasons += reasons.get(
                    ref_key, reasons.get(normalize_name(name), [])
                )

        result.outcomes = self.engine.apply(draft)

        # Derived lines are unlocked again so later edits recompute normally.
        for line in draft.lines:
            if line.qty_source == "document":
                line.locked = False

        result.totals = compute_totals(draft, self.layout.titles(), settings)
        return result

    # -- pricing --------------------------------------------------------------
    def _price(self, line: DraftLine, result: BuildResult) -> MatchResult:
        """Resolve a line against the catalog and copy price, unit, attributes."""
        match = self.catalog.match(line.name)
        line.match_status = match.status
        if match.best is not None and match.status == "matched":
            item = match.best.item
            line.catalog_id = item.id
            line.unit = item.unit
            line.unit_price = item.unit_price
            line.unit_cost = item.unit_cost
            line.attributes = dict(item.attributes or {})
            line.confidence = match.confidence
            line.source_refs.append(
                {
                    "source_type": "product_catalog",
                    "source_ref": f"{item.category} / {item.name}",
                    "detail": match.reason,
                }
            )
        elif match.status == "ambiguous":
            line.confidence = "low"
            if match.best is not None:
                item = match.best.item
                line.unit = item.unit
                line.unit_price = item.unit_price
                line.unit_cost = item.unit_cost
            result.ambiguous.append(
                {
                    "section": line.section,
                    "name": line.name,
                    "reason": match.reason,
                    "candidates": [c.to_dict() for c in match.candidates[:5]],
                }
            )
        else:
            line.confidence = "low"
            result.unmatched.append(
                {"section": line.section, "name": line.name, "reason": match.reason}
            )

        self._apply_last_sold(line, match, result)
        return match

    def _apply_last_sold(
        self, line: DraftLine, match: MatchResult, result: BuildResult
    ) -> LastPrice | None:
        """Overwrite the list price with what the article was last sold for.

        Looked up under the catalogue's own name when there is a match, because
        the invoices and the base agree on wording far more often than a
        drawing and the base do. Only the price moves: unit cost stays with the
        catalogue, since an invoice records what was charged, not what it cost.
        """
        if not self.use_invoiced_prices:
            return None
        item = match.best.item if match.best is not None else None
        name = item.name if item is not None and match.status == "matched" else line.name

        last = self.prices.lookup(name, unit=line.unit)
        if last is None or last.unit_price <= 0:
            return None

        was = line.unit_price
        line.unit_price = last.unit_price
        if not line.unit:
            line.unit = last.unit
        line.source_refs.append(
            {
                "source_type": "historical_estimate",
                "source_ref": last.project,
                "detail": last.trace(),
            }
        )
        if was > 0 and abs(was - last.unit_price) >= 0.005:
            line.reasons.append(
                f"Ціна за останнім виданим КП: {last.unit_price:g} "
                f"(у базі {was:g})."
            )
            result.repriced.append(
                {
                    "section": line.section,
                    "name": line.name,
                    "catalog_price": was,
                    "invoiced_price": last.unit_price,
                    **last.to_dict(),
                }
            )
        elif was <= 0:
            line.confidence = match.confidence if match.status == "matched" else "medium"
            line.reasons.append(
                f"У базі ціни немає; взято останню продану: {last.unit_price:g}."
            )
            result.repriced.append(
                {
                    "section": line.section,
                    "name": line.name,
                    "catalog_price": 0.0,
                    "invoiced_price": last.unit_price,
                    **last.to_dict(),
                }
            )
        return last

    def _add_plants(
        self, draft: Draft, plants: list[dict[str, Any]], result: BuildResult
    ) -> None:
        """Add the plant schedule as lines in the planting section's plant block."""
        for entry in plants:
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            if entry.get("is_existing"):
                continue  # marked "існуючі" on the schedule -- already on site
            qty = entry.get("quantity")
            line = DraftLine(
                name=name,
                section="planting",
                block="plants",
                quantity=float(qty) if qty else 0.0,
                qty_source="document" if qty else "unknown",
                locked=bool(qty),
            )
            line.reasons = [r for r in [entry.get("reason")] if r]
            line.source_refs = list(entry.get("source_refs") or [])
            self._price(line, result)
            if line.match_status != "matched" and line.unit_price <= 0:
                line.reasons.append(
                    "Позиції немає ні в каталозі, ні серед проданих — "
                    "потрібно обрати аналог або внести ціну."
                )
            elif line.unit_price <= 0:
                # The client's assortment sheet lists plants without prices --
                # nursery quotes are set per project -- and this one has never
                # been invoiced either. Flag, never guess.
                line.confidence = "low"
                line.reasons.append(
                    "У базі рослин ціни не ведуться, і в історії КП ця рослина не "
                    "зустрічається — потрібно внести ціну постачальника."
                )
            draft.lines.append(line)

    def _add_free_line(
        self, draft: Draft, section: str, name: str, result: BuildResult
    ) -> DraftLine | None:
        """Add a line the template does not contain (an ad-hoc addition)."""
        if section not in self.layout.sections:
            result.notes.append(f"Невідома секція «{section}» — позицію «{name}» пропущено.")
            return None
        line = DraftLine(
            name=name,
            section=section,
            block=self.layout.block_of(section, name),
            qty_source="user_input",
        )
        self._price(line, result)
        if line.match_status == "matched" and line.catalog_id:
            item = self.catalog.get_exact(name)
            if item is not None:
                line.block = "plants" if item.kind == "plant" else (
                    "works" if item.kind == "work" else "materials"
                )
        draft.lines.append(line)
        return line

    # -- recalculation --------------------------------------------------------
    def recalculate(
        self,
        draft: Draft,
        settings: dict[str, Any] | None = None,
    ) -> BuildResult:
        """Re-run rules and totals over an edited draft.

        Lines the operator changed carry ``locked``, so their numbers survive
        and everything downstream of them updates.
        """
        result = BuildResult(draft=draft, totals=EstimateTotals())
        result.outcomes = self.engine.apply(draft)
        result.totals = compute_totals(draft, self.layout.titles(), settings or draft.settings)
        return result


def visible_lines(draft: Draft) -> list[DraftLine]:
    """Lines that appear in the proposal.

    The template hides zero-quantity rows, and driver rows are inputs rather
    than billable items, so neither reaches the customer's copy.
    """
    return [l for l in draft.lines if l.quantity > 0 and l.block != "driver"]


def _driver_unit(name: str) -> str:
    """Best-effort unit for an input row, taken from its own wording."""
    low = normalize_name(name)
    if "площа" in low:
        return "м²"
    if "довжина" in low or "(м)" in low:
        return "м.п"
    if "обєм" in low or "об'єм" in low:
        return "л"
    if "к-ть" in low or "кількість" in low:
        return "шт"
    return ""
