"""Estimate totals.

Every formula here is read off the client's template and cross-checked against
their signed proposals; none of it is invented. See ``docs/RULES.md`` for the
evidence trail.

From ``Шаблон основний!!!! 1``::

    Разом за матеріали  = SUM(materials block)
    Разом за роботу     = SUM(works block)
    Разом <секція>      = materials + works
    Загальний рахунок   = IF(cashless, (M + W) * (1 + surcharge), M + W)
    Аванс (матеріали)   = Разом за матеріали
    Аванс (роботи)      = CEILING.MATH(Разом за роботу * 0.3, 100)
    Залишок             = Загальний - Аванс_м - Аванс_р

Verified on "КП Галина Будьків": materials 456 969 + works 284 591 = 741 560;
work prepayment CEILING(284 591 * 0.3, 100) = 85 400; balance 199 191.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .engine import Draft, DraftLine

# Defaults lifted from the template. They are settings, not constants: the
# client can change them per project without touching code.
DEFAULT_SETTINGS: dict[str, Any] = {
    # G1028 on the template: the cashless-payment surcharge.
    "cashless": False,
    "cashless_surcharge": 0.06,
    # G1033 / G1034: prepayment policy.
    "material_prepayment_share": 1.0,
    "work_prepayment_share": 0.3,
    "work_prepayment_rounding": 100,
    # C1/C2 on the catalog sheet: internal bonus model, reported not charged.
    "brigade_share_of_works": 0.5,
    "pm_share_of_works": 0.1,
    # A4 on the catalog sheet.
    "usd_rate": 43.0,
}


@dataclass
class BlockTotal:
    block_id: str
    kind: str
    label: str
    total: float
    cost_total: float
    line_count: int


@dataclass
class SectionTotal:
    key: str
    title: str
    materials: float = 0.0
    works: float = 0.0
    plants: float = 0.0
    cost_materials: float = 0.0
    cost_works: float = 0.0
    blocks: list[BlockTotal] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(self.materials + self.works + self.plants, 2)

    @property
    def cost_total(self) -> float:
        return round(self.cost_materials + self.cost_works, 2)


@dataclass
class EstimateTotals:
    sections: list[SectionTotal] = field(default_factory=list)
    materials_total: float = 0.0
    works_total: float = 0.0
    subtotal: float = 0.0
    surcharge: float = 0.0
    grand_total: float = 0.0
    prepayment_materials: float = 0.0
    prepayment_works: float = 0.0
    balance: float = 0.0
    cost_total: float = 0.0
    margin: float = 0.0
    margin_pct: float = 0.0
    formulas: dict[str, str] = field(default_factory=dict)


def _r(v: float) -> float:
    return round(v + 1e-9, 2)


def compute_totals(
    draft: Draft,
    section_titles: dict[str, str] | None = None,
    settings: dict[str, Any] | None = None,
) -> EstimateTotals:
    """Roll a draft up into section subtotals and the final invoice figures.

    Sections with no positive quantities are omitted, mirroring the template's
    ``IF(sum(D..)>0, 1, 0)`` visibility flags -- which is why the five sample
    proposals each show a different set of sections.
    """
    cfg = {**DEFAULT_SETTINGS, **(draft.settings or {}), **(settings or {})}
    titles = section_titles or {}

    order: list[str] = []
    buckets: dict[str, SectionTotal] = {}
    blocks: dict[str, BlockTotal] = {}

    for line in draft.lines:
        # Driver rows are the estimator's input quantities (trench length, paved
        # area, lawn area). The rules read them; they are never billed.
        if line.quantity <= 0 or line.block == "driver":
            continue
        sec = buckets.get(line.section)
        if sec is None:
            sec = SectionTotal(key=line.section, title=titles.get(line.section, line.section))
            buckets[line.section] = sec
            order.append(line.section)

        if line.block == "works":
            sec.works += line.total
            sec.cost_works += line.cost_total
        elif line.block == "plants":
            sec.plants += line.total
            sec.cost_materials += line.cost_total
        else:
            sec.materials += line.total
            sec.cost_materials += line.cost_total

        b = blocks.get(line.block_id)
        if b is None:
            b = BlockTotal(
                block_id=line.block_id,
                kind=line.block,
                label=line.block_id,
                total=0.0,
                cost_total=0.0,
                line_count=0,
            )
            blocks[line.block_id] = b
            sec.blocks.append(b)
        b.total += line.total
        b.cost_total += line.cost_total
        b.line_count += 1

    for sec in buckets.values():
        sec.materials = _r(sec.materials)
        sec.works = _r(sec.works)
        sec.plants = _r(sec.plants)
        sec.cost_materials = _r(sec.cost_materials)
        sec.cost_works = _r(sec.cost_works)
        for b in sec.blocks:
            b.total = _r(b.total)
            b.cost_total = _r(b.cost_total)

    totals = EstimateTotals(sections=[buckets[k] for k in order])
    totals.materials_total = _r(sum(s.materials + s.plants for s in totals.sections))
    totals.works_total = _r(sum(s.works for s in totals.sections))
    totals.subtotal = _r(totals.materials_total + totals.works_total)

    if cfg["cashless"]:
        totals.surcharge = _r(totals.subtotal * float(cfg["cashless_surcharge"]))
    totals.grand_total = _r(totals.subtotal + totals.surcharge)

    totals.prepayment_materials = _r(totals.materials_total * float(cfg["material_prepayment_share"]))
    step = float(cfg["work_prepayment_rounding"]) or 1.0
    totals.prepayment_works = _r(
        math.ceil(totals.works_total * float(cfg["work_prepayment_share"]) / step) * step
    )
    totals.balance = _r(totals.grand_total - totals.prepayment_materials - totals.prepayment_works)

    totals.cost_total = _r(sum(s.cost_total for s in totals.sections))
    totals.margin = _r(totals.subtotal - totals.cost_total)
    totals.margin_pct = round(totals.margin / totals.subtotal, 4) if totals.subtotal else 0.0

    totals.formulas = {
        "materials_total": "Σ (ціна × к-сть) по всіх блоках «Матеріали» та «Рослини»",
        "works_total": "Σ (ціна × к-сть) по всіх блоках «Робота»",
        "subtotal": "Разом за матеріали + Разом за роботу",
        "surcharge": (
            f"Безготівковий розрахунок: підсумок × {cfg['cashless_surcharge']:.0%}"
            if cfg["cashless"]
            else "Безготівковий розрахунок вимкнено — надбавка 0"
        ),
        "grand_total": "Загальний рахунок = підсумок + надбавка",
        "prepayment_materials": (
            f"Аванс на матеріали = Разом за матеріали × {cfg['material_prepayment_share']:.0%}"
        ),
        "prepayment_works": (
            f"Аванс на роботи = ОКРВГОРУ(Разом за роботу × "
            f"{cfg['work_prepayment_share']:.0%}; {int(step)})"
        ),
        "balance": "Залишок = Загальний рахунок − Аванс(матеріали) − Аванс(роботи)",
    }
    return totals


def line_explanation(line: DraftLine) -> dict[str, Any]:
    """Structured "why is this here, and why this many" for one line."""
    return {
        "name": line.name,
        "quantity": line.quantity,
        "unit": line.unit,
        "unit_price": line.unit_price,
        "total": line.total,
        "formula": f"{line.unit_price:g} × {line.quantity:g} = {line.total:.2f}",
        "quantity_source": line.qty_source,
        "quantity_rule": line.qty_expr,
        "quantity_trace": line.qty_trace,
        "match_status": line.match_status,
        "confidence": line.confidence,
        "reasons": line.reasons,
        "source_refs": line.source_refs,
        "locked": line.locked,
    }
