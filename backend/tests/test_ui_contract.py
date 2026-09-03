"""The UI contract, asserted against the frontend sources.

There is no JS test runner in this project, and adding one to check that a
button exists would be a poor trade. What actually regresses here is the
arrangement the estimator was promised: an upload action in the projects table,
one «Аналіз» button per document row instead of two in the page header, the
«Не враховувати в КП» status, and a PDF button beside the XLSX one. Those are
all visible in the source, so they are checked there.

These tests are about placement and wiring. Behaviour that can be tested for
real — an excluded fact staying out of the estimate, the PDF carrying every
figure — is covered in test_end_to_end.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def read(*parts: str) -> str:
    path = SRC.joinpath(*parts)
    if not path.exists():  # pragma: no cover - the frontend is always shipped
        pytest.skip(f"{path} not present")
    return path.read_text(encoding="utf-8")


def header_order(source: str) -> list[str]:
    """Column captions of the first table header, in document order."""
    import re

    start = source.index("<thead>")
    end = source.index("</thead>", start)
    return re.findall(r"<th[^>]*>([^<]+)</th>", source[start:end])


# --- 1. projects table --------------------------------------------------------


def test_projects_table_has_an_upload_action_between_status_and_count() -> None:
    source = read("pages", "Projects.tsx")

    assert "Завантажити проєкт" in source, "the upload button is missing from the row"

    columns = header_order(source)
    assert "Статус" in columns and "Документи" in columns and "Док." in columns, columns
    assert columns.index("Статус") < columns.index("Документи") < columns.index("Док."), (
        f"«Документи» must sit between «Статус» and «Док.», got {columns}"
    )


def test_projects_upload_posts_to_the_selected_project() -> None:
    source = read("pages", "Projects.tsx")
    # The dialog must upload to the row it was opened from, not to a global id.
    assert "api.uploadDocument(project.id" in source
    assert 'accept="application/pdf"' in source
    assert "setUploadTo" in source, "no dialog state for the chosen project"


# --- 2. analysis moved into the document row ----------------------------------


def test_document_row_carries_the_single_analyse_button() -> None:
    source = read("pages", "ProjectView.tsx")

    columns = header_order(source)
    assert "Аналіз" in columns, f"no «Аналіз» column in the documents table: {columns}"
    assert "Статус" in columns and columns.index("Статус") < columns.index("Аналіз")

    assert "onClick={() => analyse(d)}" in source, "the button is not wired to the row"


def test_the_old_header_analysis_buttons_are_gone() -> None:
    source = read("pages", "ProjectView.tsx")
    assert "Аналізувати документи" not in source, (
        "«Аналізувати документи» must no longer sit in the page header"
    )
    assert ">Аналіз об'єкта</Link>" not in source, (
        "«Аналіз об'єкта» must no longer be a header button — the sidebar has it"
    )


def test_analysis_runs_document_then_object_and_reports_why_it_failed() -> None:
    source = read("pages", "ProjectView.tsx")
    # Both steps, in order, from one click.
    assert "Крок 1 з 2" in source and "Крок 2 з 2" in source
    # And the failure path shows the cause and what to do, not a bare message.
    assert "diagnosis.reason" in source
    assert "diagnosis.recommendations" in source
    assert "Що зробити:" in source


# --- 3. object analysis: exclusion and editable blocks ------------------------


def test_excluded_status_is_offered_in_the_ui() -> None:
    source = read("pages", "AnalysisView.tsx")
    assert "Не враховувати в КП" in source
    assert 'excluded: "Не враховувати в КП"' in source, (
        "the label must be bound to the `excluded` status key the backend reads"
    )
    # And the estimator is told what the status does.
    assert "не бере участі в розрахунку кошторису" in source


def test_systems_and_coverage_are_editable() -> None:
    source = read("pages", "AnalysisView.tsx")
    for symbol in (
        "addSystem",
        "removeSystem",
        "editSystem",
        "addComponent",
        "removeComponent",
        "editComponent",
    ):
        assert symbol in source, f"«{symbol}» missing — the block is not interactive"

    assert "Додати систему" in source
    assert "Додати позицію" in source
    # Edits to all three collections have to reach the backend, not just facts.
    assert "facts: data.facts" in source
    assert "systems: data.systems" in source
    assert "components: data.components" in source


def test_fact_status_type_includes_excluded() -> None:
    source = read("api.ts")
    assert '| "excluded"' in source, "FactStatus must allow the excluded status"


# --- 4. PDF export ------------------------------------------------------------


def test_pdf_button_sits_beside_the_xlsx_one() -> None:
    source = read("pages", "EstimateView.tsx")
    assert "Завантажити PDF" in source
    assert "api.exportPdfUrl(id)" in source

    for label in ("Перерахувати", "Затвердити", "Експорт у XLSX"):
        assert label in source, f"«{label}» disappeared from the estimate header"

    # Same action row as the existing buttons.
    assert source.index("Завантажити PDF") > source.index("Затвердити")


def test_api_client_exposes_the_pdf_endpoint() -> None:
    source = read("api.ts")
    assert "exportPdfUrl" in source
    assert "/export/pdf" in source


# --- 5. clarification box under a conflict ------------------------------------


def test_conflict_cards_carry_a_clarification_box() -> None:
    source = read("pages", "AnalysisView.tsx")

    assert "Введіть уточнення або донесіть відсутні дані…" in source, (
        "the textarea placeholder the estimator was promised is missing"
    )
    assert "Оновити кошторис" in source, "no apply button on the conflict card"
    assert "<textarea" in source
    assert "api.resolveIssue(projectId, index" in source, (
        "the apply button must post to the conflict it sits under"
    )


def test_the_microphone_uses_mediarecorder_and_degrades_honestly() -> None:
    source = read("pages", "AnalysisView.tsx")

    assert "MediaRecorder" in source and "getUserMedia" in source
    assert "api.transcribe(" in source, "recorded audio must reach the backend"
    assert "setComment(" in source, "the transcript must land in the textarea"

    # getUserMedia only exists in a secure context; the app is served over
    # plain HTTP, so the button must be hidden with an explanation rather than
    # offered and then throwing.
    assert "secureContextOk" in source
    assert "лише через HTTPS" in source
    # And hidden entirely when the server has no key for Whisper.
    assert "voiceAvailable" in source


def test_api_client_exposes_transcribe_and_resolve() -> None:
    source = read("api.ts")
    assert "/audio/transcribe" in source
    assert "/issues/${issueId}/resolve" in source
    assert "resolveIssue" in source and "transcribe:" in source
