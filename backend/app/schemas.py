"""Request and response shapes for the HTTP layer."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    client_name: str = ""
    address: str = ""
    manager: str = ""
    brief: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    client_name: str | None = None
    address: str | None = None
    manager: str | None = None
    brief: str | None = None
    status: str | None = None
    settings: dict[str, Any] | None = None


class FactUpdate(BaseModel):
    """Editing one fact of the object analysis re-drives the estimate."""

    key: str
    value: str
    status: Literal["confirmed", "assumption", "unknown", "needs_user_input"] = "confirmed"
    note: str = ""


class AnalysisUpdate(BaseModel):
    object_type: str | None = None
    summary: str | None = None
    facts: list[dict[str, Any]] | None = None
    systems: list[dict[str, Any]] | None = None
    plants: list[dict[str, Any]] | None = None
    components: list[dict[str, Any]] | None = None
    assumptions: list[str] | None = None
    unknowns: list[str] | None = None
    risks: list[str] | None = None
    status: str | None = None


class IssueResolve(BaseModel):
    """A clarification typed or dictated under a conflict card."""

    comment: str = Field(min_length=1, max_length=4000)
    # Off only for testing the interpretation without paying for a plan call.
    recalculate: bool = True


class EstimateCreate(BaseModel):
    sections: list[str] = Field(default_factory=list)
    quantities: dict[str, dict[str, float]] = Field(default_factory=dict)
    plants: list[dict[str, Any]] = Field(default_factory=list)
    options: dict[str, bool] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    use_ai_plan: bool = True


class LineUpdate(BaseModel):
    quantity: float | None = None
    unit_price: float | None = None
    unit: str | None = None
    name: str | None = None
    catalog_id: int | None = None
    section: str | None = None
    block: Literal["materials", "works", "plants"] | None = None
    comment: str | None = None
    locked: bool | None = None


class LineCreate(BaseModel):
    section: str
    block: Literal["materials", "works", "plants"] = "materials"
    name: str
    quantity: float = 0.0
    unit_price: float | None = None
    unit: str | None = None
    catalog_id: int | None = None
    comment: str = ""


class OptionsUpdate(BaseModel):
    options: dict[str, bool] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class AnswerSubmit(BaseModel):
    answer: str


class BulkAnswers(BaseModel):
    """Answer many questions at once.

    An estimate can raise 80+ questions, most of them the same question asked
    about different rows ("what does this plant cost?"). Answering them one by
    one is the single biggest drag on the workflow, so the UI batches them.
    """

    answers: dict[int, str] = Field(default_factory=dict)


class BulkSameAnswer(BaseModel):
    """Apply one value to a whole set of questions."""

    question_ids: list[int] = Field(min_length=1)
    answer: str


class BulkDismiss(BaseModel):
    question_ids: list[int] = Field(min_length=1)
    reason: str = ""


class CatalogQuery(BaseModel):
    q: str = ""
    kind: str | None = None
    category: str | None = None
    limit: int = 25
    offset: int = 0
