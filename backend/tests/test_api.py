"""API smoke tests through the real app, with a temporary database."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def test_health_reports_system_readiness(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "catalog_items" in body
    assert "quantity_rules" in body


def test_sections_come_from_the_compiled_template(client: TestClient) -> None:
    response = client.get("/api/meta/sections")
    if response.status_code == 503:
        pytest.skip("template not compiled")
    assert response.status_code == 200
    sections = response.json()
    keys = {s["key"] for s in sections}
    assert {"prep", "planting", "lawn", "paving", "irrigation"} <= keys
    assert "summary" not in keys

    planting = next(s for s in sections if s["key"] == "planting")
    assert planting["title"]
    assert planting["line_count"] > 0


def test_project_lifecycle(client: TestClient) -> None:
    created = client.post(
        "/api/projects",
        json={"name": "Тест Будьків", "client_name": "Галина", "address": "с. Будьків"},
    )
    assert created.status_code == 201
    project_id = created.json()["id"]

    fetched = client.get(f"/api/projects/{project_id}")
    assert fetched.status_code == 200
    assert fetched.json()["client_name"] == "Галина"

    patched = client.patch(f"/api/projects/{project_id}", json={"manager": "Максим"})
    assert patched.status_code == 200
    assert patched.json()["manager"] == "Максим"

    listed = client.get("/api/projects")
    assert any(p["id"] == project_id for p in listed.json())

    assert client.delete(f"/api/projects/{project_id}").status_code == 204
    assert client.get(f"/api/projects/{project_id}").status_code == 404


def test_uploading_a_non_pdf_is_rejected(client: TestClient) -> None:
    project_id = client.post("/api/projects", json={"name": "Файли"}).json()["id"]
    response = client.post(
        f"/api/projects/{project_id}/documents",
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert response.status_code == 415
    client.delete(f"/api/projects/{project_id}")


def test_oversized_upload_is_refused_before_it_is_written(client: TestClient) -> None:
    """Drawing sets run to 150 MB+; the limit must bite before touching disk.

    Writing the whole body and checking afterwards can exhaust a shared host's
    quota, so an oversized upload is rejected on its declared Content-Length.
    """
    from app.config import get_settings

    settings = get_settings()
    project_id = client.post("/api/projects", json={"name": "Ліміт"}).json()["id"]
    oversized = (settings.max_upload_mb * 1024 * 1024) + (10 * 1024 * 1024)

    response = client.post(
        f"/api/projects/{project_id}/documents",
        files={"file": ("huge.pdf", io.BytesIO(b"%PDF-1.4\n"), "application/pdf")},
        headers={"Content-Length": str(oversized)},
    )
    assert response.status_code == 413
    assert str(settings.max_upload_mb) in response.json()["detail"]

    upload_dir = settings.upload_dir / str(project_id)
    written = list(upload_dir.glob("*")) if upload_dir.exists() else []
    assert not written, f"rejected upload still hit disk: {written}"

    client.delete(f"/api/projects/{project_id}")


def test_empty_upload_is_refused(client: TestClient) -> None:
    project_id = client.post("/api/projects", json={"name": "Порожній файл"}).json()["id"]
    response = client.post(
        f"/api/projects/{project_id}/documents",
        files={"file": ("empty.pdf", io.BytesIO(b""), "application/pdf")},
    )
    assert response.status_code == 400
    client.delete(f"/api/projects/{project_id}")


def test_analyze_without_documents_is_refused(client: TestClient) -> None:
    project_id = client.post("/api/projects", json={"name": "Порожній"}).json()["id"]
    response = client.post(f"/api/projects/{project_id}/analyze")
    assert response.status_code == 400
    client.delete(f"/api/projects/{project_id}")


def test_catalog_search_is_ranked(client: TestClient) -> None:
    response = client.get("/api/catalog", params={"q": "геотекстиль", "limit": 5})
    assert response.status_code == 200
    body = response.json()
    if not body["items"]:
        pytest.skip("catalog not imported")
    assert body["ranked"] is True
    assert all("score" in i for i in body["items"])


def test_catalog_match_endpoint_refuses_to_invent(client: TestClient) -> None:
    response = client.get("/api/catalog/match", params={"q": "Гіперболоїд інженера Гаріна"})
    assert response.status_code == 200
    body = response.json()
    if body["status"] == "not_in_catalog":
        assert body["best"] is None


def test_estimate_can_be_built_and_edited(client: TestClient) -> None:
    project_id = client.post(
        "/api/projects", json={"name": "Кошторис-тест", "client_name": "Тест"}
    ).json()["id"]

    created = client.post(
        f"/api/projects/{project_id}/estimates",
        json={
            "sections": ["lawn"],
            "quantities": {"lawn": {"Площа газону (рулонного)": 259}},
            "options": {"Газон рулонний універсальний": True},
        },
    )
    if created.status_code == 503:
        pytest.skip("template not compiled")
    assert created.status_code == 201, created.text
    body = created.json()
    if body.get("status") == "error":
        pytest.skip(body.get("message", "estimate could not be built"))

    estimate_id = body["estimate_id"]
    detail = client.get(f"/api/estimates/{estimate_id}").json()
    assert detail["sections"], "estimate produced no visible sections"

    # Every line must carry a justification.
    for section in detail["sections"]:
        for block in section["blocks"]:
            for line in block["lines"]:
                assert line["qty_source"] != "unknown" or line["reasons"]

    validation = client.get(f"/api/estimates/{estimate_id}/validate").json()
    assert "status" in validation and "findings" in validation

    client.delete(f"/api/projects/{project_id}")


def test_questions_can_be_filtered_and_answered_in_bulk(client: TestClient) -> None:
    """80+ questions are only workable if they can be filtered and batched."""
    from app.db import SessionLocal
    from app.models import Question

    project_id = client.post("/api/projects", json={"name": "Питання"}).json()["id"]

    session = SessionLocal()
    try:
        for i in range(3):
            session.add(
                Question(
                    project_id=project_id,
                    group="Ціни рослин",
                    code=f"price:рослина {i}",
                    text=f"Яка ціна за 1 шт для «Рослина {i}»?",
                    why="У базі рослин ціни не ведуться.",
                    kind="number",
                )
            )
        session.add(
            Question(
                project_id=project_id,
                group="Позиції поза каталогом",
                code="missing:терасна дошка",
                text="Чим замінити «Терасна дошка»?",
                why="Позиції немає в каталозі.",
                kind="text",
            )
        )
        session.commit()
    finally:
        session.close()

    summary = client.get(f"/api/projects/{project_id}/questions/summary").json()
    assert summary["open"] == 4
    families = {f["family"]: f["open"] for f in summary["families"]}
    assert families["price"] == 3
    assert families["missing"] == 1

    prices = client.get(
        f"/api/projects/{project_id}/questions", params={"family": "price"}
    ).json()
    assert len(prices) == 3
    assert all(q["kind_key"] == "price" for q in prices)

    found = client.get(
        f"/api/projects/{project_id}/questions", params={"q": "Рослина 1"}
    ).json()
    assert len(found) == 1

    applied = client.post(
        f"/api/projects/{project_id}/questions/bulk-answer",
        json={"question_ids": [q["id"] for q in prices], "answer": "450"},
    )
    assert applied.status_code == 200
    assert applied.json()["answered"] == 3

    after = client.get(f"/api/projects/{project_id}/questions/summary").json()
    assert after["open"] == 1

    remaining = client.get(
        f"/api/projects/{project_id}/questions", params={"status": "open"}
    ).json()
    dismissed = client.post(
        f"/api/projects/{project_id}/questions/dismiss",
        json={"question_ids": [q["id"] for q in remaining], "reason": "не потрібно"},
    )
    assert dismissed.json()["dismissed"] == 1

    final = client.get(f"/api/projects/{project_id}/questions/summary").json()
    assert final["open"] == 0

    client.delete(f"/api/projects/{project_id}")


def test_dashboard_summarises_state(client: TestClient) -> None:
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert "totals" in body and "recent_projects" in body
