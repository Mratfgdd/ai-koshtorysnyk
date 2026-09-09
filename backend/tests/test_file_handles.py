"""Files must be released, and a filesystem refusal must be legible.

Uploading "Львів_Майорівка,2а_Тарас,Наталя_проект.pdf" failed with a bare
`PermissionError: [Errno 13] Permission denied`. The cause was not a leaked
handle — the upload path has always streamed through a context manager, and it
fails on the very first byte, before any PDF library is involved. Two
directories under DATA_DIR had come to be owned by root, so the service user
could not write into them, and the error said nothing about that.
"""

from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


# --- the document is released even when the parse fails -----------------------


def test_a_failed_parse_still_closes_the_pdf(tmp_path, monkeypatch) -> None:
    """`parse_proposal` closed its document with a plain call at the end, which
    any exception in the 150-line loop above would skip. On Windows that leaves
    the file locked against being replaced or deleted."""
    import pymupdf

    from app.services.history import proposals

    closed: list[bool] = []
    real_open = pymupdf.open

    class Watched:
        def __init__(self, doc):
            self._doc = doc

        def __enter__(self):
            self._doc.__enter__()
            return self

        def __exit__(self, *exc):
            closed.append(True)
            return self._doc.__exit__(*exc)

        def close(self):
            closed.append(True)
            self._doc.close()

        def __iter__(self):
            raise RuntimeError("a page failed to render")

    pdf = tmp_path / "kp.pdf"
    doc = real_open()
    doc.new_page()
    doc.save(pdf)
    doc.close()

    monkeypatch.setattr(proposals.pymupdf, "open",
                        lambda *a, **k: Watched(real_open(*a, **k)))

    with pytest.raises(RuntimeError):
        proposals.parse_proposal(pdf)
    assert closed, "the document was left open when the parse raised"


def test_a_successful_parse_closes_the_pdf(tmp_path) -> None:
    """And the file is free afterwards: renaming it must not be refused."""
    import pymupdf

    from app.services.history.proposals import parse_proposal

    pdf = tmp_path / "kp.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(pdf)
    doc.close()

    parse_proposal(pdf)
    pdf.replace(tmp_path / "moved.pdf")  # PermissionError here if still open


# --- a refusal the operator can act on ----------------------------------------


def test_an_unwritable_upload_directory_is_explained(client, monkeypatch) -> None:
    """Not a stack trace: the directory, and the command that fixes it."""
    from pathlib import Path

    project_id = client.post("/api/projects", json={"name": "Права"}).json()["id"]

    real_open = Path.open

    def refuse(self, *args, **kwargs):
        if self.suffix == ".pdf":
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse)
    response = client.post(
        f"/api/projects/{project_id}/documents",
        files={"file": ("plan.pdf", io.BytesIO(b"%PDF-1.4\n%%EOF\n"), "application/pdf")},
    )
    monkeypatch.undo()

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "Немає прав на запис" in detail
    assert "chown" in detail, "the message must say how to fix it"
    client.delete(f"/api/projects/{project_id}")


def test_health_names_a_directory_it_cannot_write_to(client, monkeypatch) -> None:
    from app.api import routes

    assert client.get("/api/health").json()["storage_unwritable"] == []

    monkeypatch.setattr(routes.os, "access", lambda p, mode: False)
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert body["storage_unwritable"], "an unwritable data directory must be named"


def test_a_disk_full_refusal_says_so() -> None:
    """ENOSPC and EACCES need different actions, so they read differently."""
    import errno
    from pathlib import Path

    from app.api.routes import _storage_problem

    full = OSError(errno.ENOSPC, "No space left on device")
    assert "немає місця" in _storage_problem(Path("/tmp/x"), full)

    denied = PermissionError(errno.EACCES, "Permission denied")
    assert "Немає прав" in _storage_problem(Path("/tmp/x"), denied)


# --- every PDF opened in the codebase is closed --------------------------------


def test_no_module_opens_a_pdf_without_releasing_it() -> None:
    """A guard against the next one: every `pymupdf.open` must sit in a `with`,
    or have a `close()` reachable through `finally`."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^(\s*)(\w+\s*=\s*)?pymupdf\.open\(", source, re.M):
            line_no = source[: match.start()].count("\n") + 1
            line = source.splitlines()[line_no - 1]
            if "with pymupdf.open(" in line:
                continue
            tail = "\n".join(source.splitlines()[line_no: line_no + 60])
            if re.search(r"^\s*finally:", tail, re.M) and ".close()" in tail:
                continue
            offenders.append(f"{path.name}:{line_no}")
    assert not offenders, f"pymupdf.open without a guaranteed close: {offenders}"
