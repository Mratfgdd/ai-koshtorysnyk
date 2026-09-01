"""Database schema.

Design notes:

* Everything the AI decides carries a ``source_type`` / ``source_ref`` pair so
  any number in a final proposal can be traced back to a page of a drawing, a
  catalog row, a historical estimate or a human answer.
* ``CatalogItem`` mirrors the client's ``2026 База 1`` sheet, including cost and
  margin, and keeps an ``external_id`` free for a future CRM sync -- no CRM code
  is present, only the seam for it.
* Processing is resumable: ``DocumentPage`` rows record per-page status and a
  content hash, so a re-run skips pages that are already analysed.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# --- Catalog -----------------------------------------------------------------


class CatalogItem(Base, TimestampMixin):
    """One row of the client's price base (``2026 База 1``)."""

    __tablename__ = "catalog_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    name_norm: Mapped[str] = mapped_column(String(500), index=True)
    category: Mapped[str] = mapped_column(String(120), index=True, default="")
    unit: Mapped[str] = mapped_column(String(40), default="")

    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)  # Собівартість 1од(грн)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)  # Ціна реалізації 1од(грн)
    margin_pct: Mapped[float] = mapped_column(Float, default=0.0)
    margin_uah: Mapped[float] = mapped_column(Float, default=0.0)
    unit_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    kind: Mapped[str] = mapped_column(String(20), default="material")  # material | work | plant
    owner: Mapped[str] = mapped_column(String(120), default="")  # Відповідальний
    price_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # Per-item attributes used by template rules (planter volume, fabric areas).
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Seam for a later CRM sync; unused by the current pipeline.
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    source_file: Mapped[str] = mapped_column(String(400), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (Index("ix_catalog_kind_category", "kind", "category"),)


# --- Projects and documents --------------------------------------------------


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    client_name: Mapped[str] = mapped_column(String(300), default="")
    address: Mapped[str] = mapped_column(String(400), default="")
    manager: Mapped[str] = mapped_column(String(200), default="")
    brief: Mapped[str] = mapped_column(Text, default="")  # free-text technical brief
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    documents: Mapped[list["Document"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    estimates: Mapped[list["Estimate"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    analyses: Mapped[list["ObjectAnalysis"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(400))
    stored_path: Mapped[str] = mapped_column(String(700))
    content_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    mime_type: Mapped[str] = mapped_column(String(120), default="application/pdf")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(40), default="unknown")  # drawings|concept|brief|estimate
    status: Mapped[str] = mapped_column(String(40), default="uploaded", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    project: Mapped[Project] = relationship(back_populates="documents")
    pages: Mapped[list["DocumentPage"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentPage(Base, TimestampMixin):
    """A page, its cheap extraction, and (if it earned one) its vision result."""

    __tablename__ = "document_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, index=True)
    page_hash: Mapped[str] = mapped_column(String(64), index=True, default="")

    text: Mapped[str] = mapped_column(Text, default="")
    text_length: Mapped[int] = mapped_column(Integer, default=0)
    image_count: Mapped[int] = mapped_column(Integer, default=0)
    tables: Mapped[list[Any]] = mapped_column(JSON, default=list)

    page_type: Mapped[str] = mapped_column(String(60), default="unknown", index=True)
    needs_vision: Mapped[bool] = mapped_column(Boolean, default=False)
    analysed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    image_path: Mapped[str | None] = mapped_column(String(700), nullable=True)

    findings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[str] = mapped_column(String(20), default="medium")

    document: Mapped[Document] = relationship(back_populates="pages")

    __table_args__ = (UniqueConstraint("document_id", "page_number", name="uq_doc_page"),)


# --- Object analysis ---------------------------------------------------------


class ObjectAnalysis(Base, TimestampMixin):
    """The structured understanding of the site, before any pricing happens."""

    __tablename__ = "object_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), default="draft")

    object_type: Mapped[str] = mapped_column(String(120), default="")
    summary: Mapped[str] = mapped_column(Text, default="")

    # Each entry: {key, label, value, unit, confidence, source_type, source_ref,
    #              status: confirmed|assumption|unknown|needs_user_input, note}
    facts: Mapped[list[Any]] = mapped_column(JSON, default=list)
    systems: Mapped[list[Any]] = mapped_column(JSON, default=list)
    components: Mapped[list[Any]] = mapped_column(JSON, default=list)
    plants: Mapped[list[Any]] = mapped_column(JSON, default=list)
    assumptions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    unknowns: Mapped[list[Any]] = mapped_column(JSON, default=list)
    risks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    conflicts: Mapped[list[Any]] = mapped_column(JSON, default=list)

    project: Mapped[Project] = relationship(back_populates="analyses")


# --- Estimates ---------------------------------------------------------------


class Estimate(Base, TimestampMixin):
    __tablename__ = "estimates"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    analysis_id: Mapped[int | None] = mapped_column(
        ForeignKey("object_analyses.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(300), default="Комерційна пропозиція")
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True)

    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    totals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    project: Mapped[Project] = relationship(back_populates="estimates")
    lines: Mapped[list["EstimateLine"]] = relationship(
        back_populates="estimate", cascade="all, delete-orphan", order_by="EstimateLine.position"
    )
    issues: Mapped[list["Issue"]] = relationship(
        back_populates="estimate", cascade="all, delete-orphan"
    )


class EstimateLine(Base, TimestampMixin):
    __tablename__ = "estimate_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    estimate_id: Mapped[int] = mapped_column(
        ForeignKey("estimates.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)

    section: Mapped[str] = mapped_column(String(60), index=True)
    section_title: Mapped[str] = mapped_column(String(200), default="")
    block: Mapped[str] = mapped_column(String(20), default="materials")  # materials|works|plants

    name: Mapped[str] = mapped_column(String(500))
    catalog_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_items.id", ondelete="SET NULL"), nullable=True
    )
    article: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(40), default="")

    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)

    qty_source: Mapped[str] = mapped_column(String(40), default="unknown")
    qty_expr: Mapped[str | None] = mapped_column(Text, nullable=True)
    qty_trace: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)

    match_status: Mapped[str] = mapped_column(String(30), default="unmatched", index=True)
    confidence: Mapped[str] = mapped_column(String(20), default="medium")
    status: Mapped[str] = mapped_column(String(30), default="OK", index=True)
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    source_refs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    comment: Mapped[str] = mapped_column(Text, default="")
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    option_value: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    estimate: Mapped[Estimate] = relationship(back_populates="lines")


class Issue(Base, TimestampMixin):
    """A validation finding: what is wrong, why, and what it costs."""

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    estimate_id: Mapped[int] = mapped_column(
        ForeignKey("estimates.id", ondelete="CASCADE"), index=True
    )
    line_id: Mapped[int | None] = mapped_column(
        ForeignKey("estimate_lines.id", ondelete="CASCADE"), nullable=True
    )
    code: Mapped[str] = mapped_column(String(80), index=True)
    severity: Mapped[str] = mapped_column(String(30), index=True)  # OK|WARNING|ERROR|NEEDS_USER_INPUT
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text, default="")
    fix_hint: Mapped[str] = mapped_column(Text, default="")
    impact: Mapped[str] = mapped_column(Text, default="")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    estimate: Mapped[Estimate] = relationship(back_populates="issues")


class Question(Base, TimestampMixin):
    """A blocking question for the operator. Never guess -- ask."""

    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    estimate_id: Mapped[int | None] = mapped_column(
        ForeignKey("estimates.id", ondelete="CASCADE"), nullable=True
    )
    group: Mapped[str] = mapped_column(String(80), default="general", index=True)
    code: Mapped[str] = mapped_column(String(120), index=True)
    text: Mapped[str] = mapped_column(Text)
    why: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(30), default="text")  # text|number|choice|boolean
    choices: Mapped[list[Any]] = mapped_column(JSON, default=list)
    default_value: Mapped[str] = mapped_column(String(300), default="")
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    affects: Mapped[list[Any]] = mapped_column(JSON, default=list)


# --- Historical knowledge base -----------------------------------------------


class HistoricalEstimate(Base, TimestampMixin):
    """A past proposal, parsed into structure so it can be retrieved and reused."""

    __tablename__ = "historical_estimates"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file: Mapped[str] = mapped_column(String(500), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    client_name: Mapped[str] = mapped_column(String(300), default="")
    address: Mapped[str] = mapped_column(String(400), default="")
    dated: Mapped[str] = mapped_column(String(60), default="")
    sections: Mapped[list[Any]] = mapped_column(JSON, default=list)
    totals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    line_count: Mapped[int] = mapped_column(Integer, default=0)

    lines: Mapped[list["HistoricalLine"]] = relationship(
        back_populates="estimate", cascade="all, delete-orphan"
    )


class HistoricalLine(Base):
    __tablename__ = "historical_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    estimate_id: Mapped[int] = mapped_column(
        ForeignKey("historical_estimates.id", ondelete="CASCADE"), index=True
    )
    section: Mapped[str] = mapped_column(String(60), index=True)
    block: Mapped[str] = mapped_column(String(20), default="materials")
    name: Mapped[str] = mapped_column(String(500), index=True)
    name_norm: Mapped[str] = mapped_column(String(500), index=True)
    unit: Mapped[str] = mapped_column(String(40), default="")
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)

    estimate: Mapped[HistoricalEstimate] = relationship(back_populates="lines")


class JobRun(Base, TimestampMixin):
    """Processing status for long operations, so a run can be resumed."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    step: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
