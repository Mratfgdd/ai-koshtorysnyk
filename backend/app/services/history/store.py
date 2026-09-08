"""Persist the issued proposals so the engine can price from them.

Reading fourteen PDFs takes a few seconds; the estimate builder must not pay
that on every request. The parsed proposals go into ``historical_estimates`` /
``historical_lines`` once, and :meth:`InvoicedPrices.from_db` reads them back.

A proposal is keyed by its filename, so re-importing the same folder updates it
in place rather than doubling the price history — which would double-count the
votes that break a tie between two prices.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import HistoricalEstimate, HistoricalLine
from ..rules.engine import normalize_name
from .proposals import Proposal, parse_proposal, reference_proposals


@dataclass
class ImportReport:
    imported: int = 0
    updated: int = 0
    rejected: list[tuple[str, str]] = None  # type: ignore[assignment]
    lines: int = 0

    def __post_init__(self) -> None:
        self.rejected = self.rejected or []


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def store_proposal(session: Session, proposal: Proposal) -> HistoricalEstimate:
    """Upsert one parsed proposal and its rows."""
    record = session.scalars(
        select(HistoricalEstimate).where(HistoricalEstimate.source_file == proposal.project)
    ).first()
    if record is None:
        record = HistoricalEstimate(source_file=proposal.project)
        session.add(record)
        session.flush()
    else:
        for line in list(record.lines):
            session.delete(line)
        session.flush()

    path = Path(proposal.path)
    record.content_hash = _digest(path) if path.exists() else ""
    record.dated = str(proposal.issued) if proposal.issued else ""
    record.sections = sorted({r.section for r in proposal.rows})
    record.totals = {
        "materials": round(proposal.materials_total, 2),
        "works": round(proposal.works_total, 2),
        "grand_total": round(proposal.grand_total, 2),
        **{k: v for k, v in proposal.invoice.items()},
    }
    record.line_count = len(proposal.rows)

    for row in proposal.rows:
        session.add(
            HistoricalLine(
                estimate_id=record.id,
                section=row.section[:60],
                block=row.block,
                name=row.name[:500],
                name_norm=normalize_name(row.name)[:500],
                unit=row.unit[:40],
                quantity=row.quantity,
                unit_price=row.unit_price,
                total=row.total,
            )
        )
    return record


def import_references(
    session: Session, root: Path, *, require_reconciled: bool = True
) -> ImportReport:
    """Read every issued proposal under ``root`` into the price history.

    A proposal whose blocks do not add up to their own printed subtotals is
    rejected rather than imported. A misread row carries a price that was never
    charged, and one of those in the history is worse than a gap in it.
    """
    report = ImportReport()
    known = set(session.scalars(select(HistoricalEstimate.source_file)).all())
    for path in reference_proposals(root):
        proposal = parse_proposal(path)
        if require_reconciled and not proposal.reconciles:
            deltas = proposal.block_deltas()
            detail = (
                f"{len(deltas)} блоків не сходяться з надрукованими підсумками"
                if deltas
                else "не прочитано жодного рядка"
            )
            report.rejected.append((proposal.project, detail))
            continue
        if proposal.project in known:
            report.updated += 1
        else:
            report.imported += 1
        store_proposal(session, proposal)
        report.lines += len(proposal.rows)
    session.commit()
    return report


__all__ = ["ImportReport", "import_references", "store_proposal"]
