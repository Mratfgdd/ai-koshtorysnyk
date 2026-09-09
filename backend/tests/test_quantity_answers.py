"""An answered quantity reaches the formulas that read it.

The estimate could ask "Вкажіть кількості, відсутні у відомостях" and had
nowhere to put the reply: apply_answer acted on a price and on a catalogue
choice and recorded everything else as prose. On Майорівка that left the paving
area unknown and the section at 1 325 грн against an invoiced 502 352, because
the bedding, the gravel, the levelling and the laying are all derived from that
one figure.
"""

from __future__ import annotations

import pytest

from app.models import ObjectAnalysis, Project, Question
from app.services.pipeline import (
    QUANTITY_PREFIX,
    _apply_quantity,
    _quantity_targets,
)
from app.services.rules.engine import normalize_name


def analysis(**over) -> ObjectAnalysis:
    return ObjectAnalysis(
        project_id=over.pop("project_id", 1),
        facts=over.pop("facts", []),
        components=over.pop("components", []),
        plants=over.pop("plants", []),
        systems=[],
    )


# --- what gets asked about ----------------------------------------------------


def test_a_fact_with_no_figure_is_asked_about() -> None:
    a = analysis(facts=[{"label": "Бруківка (площа)", "value": "", "unit": "м²",
                         "status": "needs_user_input", "section": "paving"}])
    targets = _quantity_targets(a)
    assert [t["name"] for t in targets] == ["Бруківка (площа)"]
    assert targets[0]["kind"] == "fact"
    assert targets[0]["section"] == "paving"


def test_a_figure_held_for_review_is_asked_about() -> None:
    """It has a number, and a status that keeps it out of the estimate."""
    a = analysis(facts=[{"label": "Газон рулонний", "value": 68, "unit": "м²",
                         "status": "needs_user_input", "section": "lawn"}])
    targets = _quantity_targets(a)
    assert len(targets) == 1
    assert targets[0]["seen"] == "68", "the reading is offered as the default"


def test_a_confirmed_figure_is_not_asked_about() -> None:
    a = analysis(facts=[{"label": "Площа газону", "value": 68, "unit": "м²",
                         "status": "confirmed", "section": "lawn"}])
    assert _quantity_targets(a) == []


def test_a_note_about_the_site_is_not_asked_about() -> None:
    """Neither the drawing scale nor the ±0,00 level is a quantity. The scale
    carries no unit at all; the level carries "м", which the company does not
    bill in — its rows are м.п, м² and м³."""
    a = analysis(facts=[
        {"label": "Масштаб основних креслень", "value": "1:130", "unit": "",
         "status": "confirmed"},
        {"label": "Рівень ±0,00", "value": "367,30", "unit": "м",
         "status": "confirmed", "section": "prep"},
    ])
    billing = {"шт", "м²", "мп", "м³", "кг", "послуга"}
    assert _quantity_targets(a, billing) == []


def test_a_real_unit_still_gets_its_question() -> None:
    a = analysis(facts=[{"label": "Бруківка (площа)", "value": "", "unit": "м²",
                         "status": "needs_user_input", "section": "paving"}])
    assert len(_quantity_targets(a, {"шт", "м²", "мп"})) == 1


def test_a_component_without_a_quantity_is_asked_about() -> None:
    a = analysis(components=[{"name": "Бруківка", "quantity": None, "unit": "м²",
                              "section": "paving"}])
    targets = _quantity_targets(a)
    assert targets[0]["kind"] == "component"


def test_a_plant_without_a_count_is_asked_about() -> None:
    a = analysis(plants=[{"name": "Сосна гірська", "quantity": None}])
    targets = _quantity_targets(a)
    assert targets[0]["kind"] == "plant"
    assert targets[0]["section"] == "planting"


def test_a_plant_already_on_site_is_not_asked_about() -> None:
    a = analysis(plants=[{"name": "Існуючі дерева", "quantity": None,
                          "is_existing": True}])
    assert _quantity_targets(a) == []


# --- and what an answer does --------------------------------------------------


@pytest.fixture()
def stored(tmp_path):
    """A project and an analysis in a database of their own."""
    from app.db import SessionLocal, init_db

    init_db()
    session = SessionLocal()
    project = Project(name="quantity-answer test")
    session.add(project)
    session.commit()
    yield session, project
    session.query(ObjectAnalysis).filter_by(project_id=project.id).delete()
    session.delete(project)
    session.commit()
    session.close()


def question_for(kind: str, name: str, answer: str) -> Question:
    return Question(
        project_id=0,
        code=f"{QUANTITY_PREFIX}{kind}:{normalize_name(name)[:80]}",
        text="", kind="number", answer=answer,
    )


def test_an_answer_lands_on_the_fact_and_confirms_it(stored) -> None:
    session, project = stored
    a = analysis(project_id=project.id, facts=[
        {"label": "Бруківка (площа)", "value": "", "unit": "м²",
         "status": "needs_user_input", "section": "paving"}
    ])
    session.add(a)
    session.commit()

    assert _apply_quantity(session, project.id,
                           question_for("fact", "Бруківка (площа)", "190"))
    session.refresh(a)
    fact = a.facts[0]
    assert fact["value"] == 190
    assert fact["status"] == "confirmed", "otherwise the rules still skip it"
    assert fact["source_type"] == "user_input"


def test_an_answer_lands_on_a_component(stored) -> None:
    session, project = stored
    a = analysis(project_id=project.id,
                 components=[{"name": "Бруківка", "quantity": None, "unit": "м²"}])
    session.add(a)
    session.commit()
    assert _apply_quantity(session, project.id,
                           question_for("component", "Бруківка", "190"))
    session.refresh(a)
    assert a.components[0]["quantity"] == 190


def test_an_answer_lands_on_a_plant(stored) -> None:
    session, project = stored
    a = analysis(project_id=project.id,
                 plants=[{"name": "Сосна гірська", "quantity": None}])
    session.add(a)
    session.commit()
    assert _apply_quantity(session, project.id,
                           question_for("plant", "Сосна гірська", "7"))
    session.refresh(a)
    assert a.plants[0]["quantity"] == 7


def test_an_answer_that_is_not_a_number_changes_nothing(stored) -> None:
    session, project = stored
    a = analysis(project_id=project.id, facts=[
        {"label": "Бруківка (площа)", "value": "", "unit": "м²",
         "status": "needs_user_input"}
    ])
    session.add(a)
    session.commit()
    assert not _apply_quantity(
        session, project.id,
        question_for("fact", "Бруківка (площа)", "приблизно як на плані"))
    session.refresh(a)
    assert a.facts[0]["status"] == "needs_user_input"


def test_an_answer_for_something_absent_changes_nothing(stored) -> None:
    session, project = stored
    a = analysis(project_id=project.id, facts=[
        {"label": "Бруківка (площа)", "value": "", "unit": "м²",
         "status": "needs_user_input"}
    ])
    session.add(a)
    session.commit()
    assert not _apply_quantity(session, project.id,
                              question_for("fact", "Чогось такого немає", "190"))
