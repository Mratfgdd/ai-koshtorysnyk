"""Persist estimates: map between the in-memory :class:`Draft` and the ORM.

Keeping the engine's model separate from the database rows means the rule
engine stays pure and testable, and the database keeps the audit trail.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Iterable

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ...models import Estimate, EstimateLine, Issue
from ..rules.engine import Draft, DraftLine
from ..rules.totals import EstimateTotals, line_explanation
from ..validation.validators import ValidationReport


def draft_from_estimate(estimate: Estimate) -> Draft:
    """Rebuild the engine's view from stored rows."""
    draft = Draft(
        options={k: bool(v) for k, v in (estimate.options or {}).items()},
        settings=dict(estimate.settings or {}),
    )
    for row in estimate.lines:
        draft.lines.append(
            DraftLine(
                name=row.name,
                section=row.section,
                block=row.block,
                unit=row.unit,
                quantity=row.quantity,
                unit_price=row.unit_price,
                unit_cost=row.unit_cost,
                attributes=dict(row.attributes or {}),
                option=row.option_value,
                qty_source=row.qty_source,
                qty_expr=row.qty_expr,
                qty_trace=row.qty_trace,
                catalog_id=row.catalog_id,
                match_status=row.match_status,
                confidence=row.confidence,
                reasons=list(row.reasons or []),
                source_refs=list(row.source_refs or []),
                locked=row.locked,
            )
        )
    return draft


def save_draft(
    session: Session,
    estimate: Estimate,
    draft: Draft,
    totals: EstimateTotals,
    *,
    section_titles: dict[str, str] | None = None,
    candidates: dict[str, list[dict[str, Any]]] | None = None,
    report: ValidationReport | None = None,
) -> Estimate:
    """Replace the estimate's lines and issues with the current draft."""
    titles = section_titles or {}
    candidates = candidates or {}

    session.execute(delete(EstimateLine).where(EstimateLine.estimate_id == estimate.id))
    session.execute(delete(Issue).where(Issue.estimate_id == estimate.id))
    session.flush()

    severities = _severity_by_line(report)
    for position, line in enumerate(draft.lines):
        session.add(
            EstimateLine(
                estimate_id=estimate.id,
                position=position,
                section=line.section,
                section_title=titles.get(line.section, line.section),
                block=line.block,
                name=line.name,
                catalog_id=line.catalog_id,
                unit=line.unit,
                quantity=line.quantity,
                unit_price=line.unit_price,
                unit_cost=line.unit_cost,
                total=line.total,
                qty_source=line.qty_source,
                qty_expr=line.qty_expr,
                qty_trace=line.qty_trace,
                locked=line.locked,
                match_status=line.match_status,
                confidence=line.confidence,
                status=severities.get(line.name, "OK"),
                reasons=list(line.reasons),
                source_refs=list(line.source_refs),
                candidates=candidates.get(line.name, []),
                attributes=dict(line.attributes),
                option_value=line.option,
            )
        )

    if report is not None:
        by_name = {l.name: l for l in draft.lines}
        for finding in report.sorted():
            session.add(
                Issue(
                    estimate_id=estimate.id,
                    code=finding.code,
                    severity=finding.severity,
                    title=finding.title,
                    detail=finding.detail,
                    fix_hint=finding.fix_hint,
                    impact=finding.impact,
                )
            )
        _ = by_name  # line linkage is resolved on read; ids are not stable here

    estimate.totals = totals_to_dict(totals)
    estimate.options = dict(draft.options)
    estimate.settings = dict(draft.settings)
    session.commit()
    session.refresh(estimate)
    return estimate


def totals_to_dict(totals: EstimateTotals) -> dict[str, Any]:
    data = asdict(totals)
    data["sections"] = [
        {
            **{k: v for k, v in asdict(s).items() if k != "blocks"},
            "total": s.total,
            "cost_total": s.cost_total,
            "blocks": [asdict(b) for b in s.blocks],
        }
        for s in totals.sections
    ]
    return data


def line_to_dict(row: EstimateLine) -> dict[str, Any]:
    return {
        "id": row.id,
        "position": row.position,
        "section": row.section,
        "section_title": row.section_title,
        "block": row.block,
        "name": row.name,
        "catalog_id": row.catalog_id,
        "article": row.article,
        "description": row.description,
        "unit": row.unit,
        "quantity": row.quantity,
        "unit_price": row.unit_price,
        "unit_cost": row.unit_cost,
        "total": row.total,
        "margin": round((row.unit_price - row.unit_cost) * row.quantity, 2),
        "qty_source": row.qty_source,
        "qty_expr": row.qty_expr,
        "qty_trace": row.qty_trace,
        "locked": row.locked,
        "match_status": row.match_status,
        "confidence": row.confidence,
        "status": row.status,
        "reasons": row.reasons or [],
        "source_refs": row.source_refs or [],
        "candidates": row.candidates or [],
        "comment": row.comment,
        "option_value": row.option_value,
        "formula": f"{row.unit_price:g} × {row.quantity:g} = {row.total:.2f}",
    }


def explain(draft: Draft, name: str) -> dict[str, Any] | None:
    line = draft.by_name(name)
    return line_explanation(line) if line else None


def _severity_by_line(report: ValidationReport | None) -> dict[str, str]:
    if not report:
        return {}
    order = {"ERROR": 0, "NEEDS_USER_INPUT": 1, "WARNING": 2, "OK": 3}
    out: dict[str, str] = {}
    for finding in report.findings:
        if not finding.line_name:
            continue
        current = out.get(finding.line_name)
        if current is None or order[finding.severity] < order[current]:
            out[finding.line_name] = finding.severity
    return out


def group_by_section(rows: Iterable[EstimateLine]) -> list[dict[str, Any]]:
    """Shape the lines the way the UI renders them: section -> block -> lines."""
    sections: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.quantity <= 0 or row.block == "driver":
            continue
        section = sections.setdefault(
            row.section,
            {
                "key": row.section,
                "title": row.section_title or row.section,
                "blocks": {},
                "materials_total": 0.0,
                "works_total": 0.0,
                "total": 0.0,
            },
        )
        block = section["blocks"].setdefault(row.block, {"kind": row.block, "lines": [], "total": 0.0})
        block["lines"].append(line_to_dict(row))
        block["total"] = round(block["total"] + row.total, 2)
        if row.block == "works":
            section["works_total"] = round(section["works_total"] + row.total, 2)
        else:
            section["materials_total"] = round(section["materials_total"] + row.total, 2)
        section["total"] = round(section["total"] + row.total, 2)

    order = {"plants": 0, "materials": 1, "works": 2}
    out = []
    for section in sections.values():
        section["blocks"] = sorted(
            section["blocks"].values(), key=lambda b: order.get(b["kind"], 9)
        )
        out.append(section)
    return out
