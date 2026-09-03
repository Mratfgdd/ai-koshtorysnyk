"""Resolving a conflict from a free-text clarification, then rebuilding.

The estimator reads a conflict — "площа озеленення: 112 м² на генплані, 121 м²
у ТЕП" — types or dictates what is actually true, and the estimate is rebuilt
from that. The model's only job is to say *which показник* changes and to what;
every derived quantity still comes from the rule engine afterwards, so a
clarification cannot smuggle an arithmetic error into the proposal.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ObjectAnalysis, Project, Question
from ..schemas import EstimateCreate
from .ai.openai_speech import OpenAIClient, OpenAIUnavailable, ResolutionPlan
from .ai.schemas import FACT_EXCLUDED
from .pipeline import plan_and_build
from .rules.engine import normalize_name

log = logging.getLogger(__name__)


class ConflictNotFound(LookupError):
    pass


def latest_analysis(session: Session, project_id: int) -> ObjectAnalysis | None:
    return session.scalars(
        select(ObjectAnalysis)
        .where(ObjectAnalysis.project_id == project_id)
        .order_by(ObjectAnalysis.version.desc())
        .limit(1)
    ).first()


def _apply_fact_updates(analysis: ObjectAnalysis, plan: ResolutionPlan) -> list[str]:
    """Write the model's edits onto the analysis. Returns a human log."""
    facts: list[dict[str, Any]] = list(analysis.facts or [])
    changes: list[str] = []

    for update in plan.fact_updates:
        target = normalize_name(update.label)
        existing = None
        for fact in facts:
            label = normalize_name(str(fact.get("label") or fact.get("key") or ""))
            if label and (label == target or target in label or label in target):
                existing = fact
                break

        if update.action == "exclude":
            if existing is None:
                changes.append(f"«{update.label}»: показника немає, нічого виключати")
                continue
            existing["status"] = FACT_EXCLUDED
            changes.append(f"«{existing.get('label')}» — не враховувати в КП")
            continue

        if existing is None:
            # A clarification may add a figure the drawings never carried.
            facts.append(
                {
                    "key": target[:60] or "user_fact",
                    "label": update.label,
                    "value": update.value,
                    "unit": update.unit,
                    "status": "confirmed",
                    "confidence": "high",
                    "source_type": "user_input",
                    "source_ref": "Уточнення користувача",
                    "note": "Додано з уточнення",
                }
            )
            changes.append(f"«{update.label}» = {update.value} {update.unit}".rstrip())
            continue

        was = existing.get("value")
        existing["value"] = update.value
        if update.unit:
            existing["unit"] = update.unit
        existing["status"] = "confirmed"
        existing["confidence"] = "high"
        existing["source_type"] = "user_input"
        existing["source_ref"] = "Уточнення користувача"
        changes.append(
            f"«{existing.get('label')}»: {was} → {update.value} {existing.get('unit', '')}".rstrip()
        )

    analysis.facts = facts
    return changes


def _apply_component_updates(analysis: ObjectAnalysis, plan: ResolutionPlan) -> list[str]:
    components: list[dict[str, Any]] = list(analysis.components or [])
    changes: list[str] = []

    for update in plan.component_updates:
        target = normalize_name(update.name)
        index = next(
            (
                i
                for i, c in enumerate(components)
                if normalize_name(str(c.get("name", ""))) == target
            ),
            None,
        )

        if update.action == "remove":
            if index is None:
                continue
            changes.append(f"Прибрано з відомості: «{components[index].get('name')}»")
            components.pop(index)
            continue

        if index is None:
            components.append(
                {
                    "name": update.name,
                    "quantity": update.quantity,
                    "unit": update.unit,
                    "note": "Додано з уточнення",
                }
            )
            changes.append(f"Додано «{update.name}»: {update.quantity} {update.unit}".rstrip())
            continue

        was = components[index].get("quantity")
        components[index]["quantity"] = update.quantity
        if update.unit:
            components[index]["unit"] = update.unit
        changes.append(f"«{update.name}»: {was} → {update.quantity} {update.unit}".rstrip())

    analysis.components = components
    return changes


def _close_question(session: Session, project_id: int, topic: str, answer: str) -> None:
    """A conflict also exists as an open question; answering one answers both."""
    code = f"conflict:{normalize_name(topic)[:80]}"
    question = session.scalars(
        select(Question).where(Question.project_id == project_id, Question.code == code)
    ).first()
    if question is None:
        return
    question.answer = answer[:2000]
    question.status = "answered"


def resolve_conflict(
    session: Session,
    project: Project,
    issue_index: int,
    comment: str,
    *,
    client: OpenAIClient | None = None,
    rebuild: bool = True,
) -> dict[str, Any]:
    """Apply a clarification to one conflict and rebuild the estimate."""
    analysis = latest_analysis(session, project.id)
    if analysis is None:
        raise ConflictNotFound("Аналіз об'єкта ще не сформовано.")

    conflicts: list[dict[str, Any]] = list(analysis.conflicts or [])
    if not 0 <= issue_index < len(conflicts):
        raise ConflictNotFound(
            f"Зауваження №{issue_index} немає: у аналізі {len(conflicts)} суперечностей."
        )
    conflict = dict(conflicts[issue_index])

    client = client or OpenAIClient()
    plan = client.interpret(
        comment,
        conflict=conflict,
        facts=list(analysis.facts or []),
        components=list(analysis.components or []),
    )

    changes = _apply_fact_updates(analysis, plan)
    changes += _apply_component_updates(analysis, plan)

    conflict["resolved"] = True
    conflict["resolution"] = comment.strip()[:2000]
    conflict["resolution_understood"] = plan.understood
    conflict["resolved_at"] = dt.datetime.now().isoformat(timespec="seconds")
    conflicts[issue_index] = conflict
    analysis.conflicts = conflicts

    # SQLAlchemy does not see in-place edits to a JSON column.
    from sqlalchemy.orm.attributes import flag_modified

    for field in ("facts", "components", "conflicts"):
        flag_modified(analysis, field)

    _close_question(session, project.id, str(conflict.get("topic", "")), comment)
    session.commit()

    result: dict[str, Any] = {
        "status": "ok",
        "issue_id": issue_index,
        "resolved": True,
        "understood": plan.understood,
        "changes": changes,
        "unresolved": plan.unresolved,
        "analysis_id": analysis.id,
    }

    if not changes:
        # Nothing actionable came out of the sentence. The conflict is still
        # marked resolved — the estimator said their piece — but rebuilding
        # would only burn an AI plan call to produce the same estimate.
        result["estimate_id"] = None
        result["recalculated"] = False
        return result

    if not rebuild:
        result["recalculated"] = False
        return result

    built = plan_and_build(session, project, EstimateCreate())
    result["recalculated"] = built.get("status") == "ok"
    result["estimate_id"] = built.get("estimate_id")
    result["validation"] = built.get("validation")
    if built.get("status") != "ok":
        result["status"] = "partial"
        result["message"] = built.get("message", "Кошторис не перераховано.")
    return result


__all__ = [
    "ConflictNotFound",
    "OpenAIUnavailable",
    "resolve_conflict",
    "latest_analysis",
]
