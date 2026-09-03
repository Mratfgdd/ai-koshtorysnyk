"""Clarifications: a dictated sentence becomes edits, and the estimate follows.

The OpenAI call is injected, not mocked at the network layer: what needs
testing is what the application does with a plan, not that the SDK works.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import SessionLocal, init_db
from app.main import app
from app.models import ObjectAnalysis, Project, Question
from app.services.ai.openai_speech import (
    ComponentUpdate,
    FactUpdate,
    OpenAIClient,
    ResolutionPlan,
)
from app.services.resolution import ConflictNotFound, resolve_conflict


class FakeClient:
    """Stands in for OpenAI: returns a fixed plan, records what it was asked."""

    def __init__(self, plan: ResolutionPlan) -> None:
        self.plan = plan
        self.seen: dict | None = None

    def interpret(self, comment, *, conflict, facts, components):
        self.seen = {
            "comment": comment,
            "conflict": conflict,
            "facts": facts,
            "components": components,
        }
        return self.plan


@pytest.fixture
def project_with_conflict():
    init_db()
    session = SessionLocal()
    project = Project(name="Уточнення — тест", client_name="QA")
    session.add(project)
    session.commit()
    session.refresh(project)

    analysis = ObjectAnalysis(
        project_id=project.id,
        version=1,
        status="draft",
        object_type="ділянка",
        summary="",
        facts=[
            {
                "key": "green_area",
                "label": "Площа озеленення",
                "value": "112",
                "unit": "м2",
                "status": "needs_user_input",
                "confidence": "low",
                "source_type": "uploaded_pdf",
                "source_ref": "Генплан, с. 3",
                "note": "",
            }
        ],
        systems=[],
        components=[{"name": "Бруківка", "quantity": 40.0, "unit": "м2", "note": ""}],
        plants=[],
        assumptions=[],
        unknowns=[],
        risks=[],
        conflicts=[
            {
                "topic": "Площа озеленення",
                "values": ["112 м² (генплан)", "121 м² (ТЕП)"],
                "impact": "Кількість ґрунту, газону та робіт",
                "question": "Яка площа озеленення правильна?",
            }
        ],
    )
    session.add(analysis)
    session.commit()

    yield session, project

    session.query(ObjectAnalysis).filter_by(project_id=project.id).delete()
    session.query(Question).filter_by(project_id=project.id).delete()
    session.query(Project).filter_by(id=project.id).delete()
    session.commit()
    session.close()


def test_a_clarification_updates_the_fact_and_marks_the_conflict_resolved(
    project_with_conflict,
) -> None:
    session, project = project_with_conflict
    plan = ResolutionPlan(
        understood="Площа озеленення дорівнює 121 м².",
        fact_updates=[
            FactUpdate(label="Площа озеленення", value="121", unit="м2", action="set")
        ],
        component_updates=[],
        unresolved="",
    )
    fake = FakeClient(plan)

    result = resolve_conflict(
        session, project, 0, "Правильна площа озеленення 121 квадратний метр",
        client=fake, rebuild=False,
    )

    assert result["status"] == "ok"
    assert result["resolved"] is True
    assert any("112" in c and "121" in c for c in result["changes"]), result["changes"]

    session.expire_all()
    analysis = session.query(ObjectAnalysis).filter_by(project_id=project.id).one()

    fact = analysis.facts[0]
    assert fact["value"] == "121"
    # A clarification is a decision, so the value now counts towards the estimate.
    assert fact["status"] == "confirmed"
    assert fact["source_type"] == "user_input"

    conflict = analysis.conflicts[0]
    assert conflict["resolved"] is True
    assert "121" in conflict["resolution"]
    assert conflict["resolution_understood"] == plan.understood
    assert conflict["resolved_at"]

    # The model was shown the conflict and the current figures, not just the text.
    assert fake.seen is not None
    assert fake.seen["conflict"]["topic"] == "Площа озеленення"
    assert fake.seen["facts"][0]["label"] == "Площа озеленення"


def test_a_clarification_can_take_a_figure_out_of_the_estimate(
    project_with_conflict,
) -> None:
    session, project = project_with_conflict
    fake = FakeClient(
        ResolutionPlan(
            understood="Озеленення не входить до цього КП.",
            fact_updates=[
                FactUpdate(label="Площа озеленення", value="", unit="", action="exclude")
            ],
            component_updates=[],
            unresolved="",
        )
    )

    resolve_conflict(session, project, 0, "Озеленення не рахуємо", client=fake, rebuild=False)

    session.expire_all()
    analysis = session.query(ObjectAnalysis).filter_by(project_id=project.id).one()
    assert analysis.facts[0]["status"] == "excluded"


def test_a_clarification_can_add_and_remove_coverage_rows(project_with_conflict) -> None:
    session, project = project_with_conflict
    fake = FakeClient(
        ResolutionPlan(
            understood="Бруківки немає, натомість газон 80 м².",
            fact_updates=[],
            component_updates=[
                ComponentUpdate(name="Бруківка", quantity=None, unit="", action="remove"),
                ComponentUpdate(name="Газон рулонний", quantity=80.0, unit="м2", action="set"),
            ],
            unresolved="",
        )
    )

    result = resolve_conflict(session, project, 0, "Бруківки не буде, газон 80",
                              client=fake, rebuild=False)
    assert len(result["changes"]) == 2

    session.expire_all()
    analysis = session.query(ObjectAnalysis).filter_by(project_id=project.id).one()
    names = {c["name"] for c in analysis.components}
    assert "Бруківка" not in names
    assert "Газон рулонний" in names


def test_an_empty_plan_does_not_burn_a_rebuild(project_with_conflict) -> None:
    """Nothing actionable means nothing to recalculate."""
    session, project = project_with_conflict
    fake = FakeClient(
        ResolutionPlan(
            understood="Не вдалося зрозуміти, якого показника це стосується.",
            fact_updates=[],
            component_updates=[],
            unresolved="Репліка не називає жодного показника.",
        )
    )

    result = resolve_conflict(session, project, 0, "та щось там", client=fake, rebuild=True)

    assert result["changes"] == []
    assert result["recalculated"] is False
    assert result["estimate_id"] is None
    # The conflict is still closed: the estimator said their piece.
    session.expire_all()
    analysis = session.query(ObjectAnalysis).filter_by(project_id=project.id).one()
    assert analysis.conflicts[0]["resolved"] is True


def test_resolving_a_conflict_answers_its_question(project_with_conflict) -> None:
    """A conflict exists twice: as a card here and as an open question there."""
    from app.services.rules.engine import normalize_name

    session, project = project_with_conflict
    code = f"conflict:{normalize_name('Площа озеленення')[:80]}"
    session.add(
        Question(
            project_id=project.id,
            group="conflicts",
            code=code,
            text="Яка площа озеленення правильна?",
            why="Джерела дають різні значення",
            kind="choice",
            choices=["112", "121"],
            affects=[],
        )
    )
    session.commit()

    fake = FakeClient(
        ResolutionPlan(
            understood="121 м².",
            fact_updates=[FactUpdate(label="Площа озеленення", value="121", unit="м2",
                                     action="set")],
            component_updates=[],
            unresolved="",
        )
    )
    resolve_conflict(session, project, 0, "121 м²", client=fake, rebuild=False)

    session.expire_all()
    question = session.query(Question).filter_by(project_id=project.id, code=code).one()
    assert question.status == "answered"
    assert "121" in (question.answer or "")


def test_an_unknown_conflict_index_is_a_404_not_a_crash(project_with_conflict) -> None:
    session, project = project_with_conflict
    with pytest.raises(ConflictNotFound):
        resolve_conflict(session, project, 99, "будь-що", client=FakeClient(
            ResolutionPlan(understood="", fact_updates=[], component_updates=[], unresolved="")
        ), rebuild=False)


# --- wiring -------------------------------------------------------------------


def test_voice_is_switched_off_without_a_key() -> None:
    """No key must mean a hidden microphone, not a button that always fails."""
    assert OpenAIClient(Settings(openai_api_key="")).available is False
    assert OpenAIClient(Settings(openai_api_key="sk-test")).available is True


def test_the_endpoints_are_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/api/audio/transcribe" in paths
    assert "post" in paths["/api/audio/transcribe"]

    resolve = "/api/projects/{project_id}/issues/{issue_id}/resolve"
    assert resolve in paths
    assert "post" in paths[resolve]


def test_an_empty_clarification_is_rejected_before_any_api_call() -> None:
    with TestClient(app) as client:
        response = client.post("/api/projects/1/issues/0/resolve", json={"comment": "   "})
    # 422 from the schema's min_length, or 400 from the handler; either way it
    # must not reach OpenAI.
    assert response.status_code in (400, 404, 422)
