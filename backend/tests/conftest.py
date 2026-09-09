"""Fixtures shared across the suite.

The first thing this file does is move the directories the tests write into out
of whatever DATA_DIR is configured, and it has to happen here, at import: pytest
loads conftest before any test module, and ``app.api.routes`` binds its settings
the moment it is imported.

This is not tidiness. The API tests upload through the real endpoint, which
creates ``uploads/<project id>/``, and they delete the project afterwards —
which used to remove the row and leave the directory. Run against a deployment,
that seeded the live upload tree with directories owned by whoever ran pytest.
Project ids are handed out in sequence, so a real project later took one of
those numbers, found a directory it could not write to, and every upload to it
failed with `PermissionError: [Errno 13]` on the first byte. Two projects on the
server were dead this way before it was traced.

The database is deliberately left alone: the catalogue and the price history
live in it, and the tests that matter most would skip without them. The API
tests still create and delete real project rows, so run the suite against a
deployment only when you mean to.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_SANDBOX = Path(tempfile.mkdtemp(prefix="estimator-tests-"))
for _name in ("uploads", "cache", "pages"):
    (_SANDBOX / _name).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("UPLOAD_DIR", str(_SANDBOX / "uploads"))
os.environ.setdefault("CACHE_DIR", str(_SANDBOX / "cache"))
os.environ.setdefault("PAGE_IMAGE_DIR", str(_SANDBOX / "pages"))


@pytest.fixture(scope="session", autouse=True)
def _sandbox_is_used() -> None:
    """Fail loudly rather than write a stray directory into a deployment."""
    from app.config import get_settings

    settings = get_settings()
    assert settings.upload_dir.is_relative_to(_SANDBOX), (
        f"tests would upload into {settings.upload_dir}; expected {_SANDBOX}"
    )


# The issued proposals are the client's own files and are not in the repository.
# Point REFERENCE_ROOT at the folder holding the project sub-folders to run the
# checks that need them; without it those tests skip rather than fail.
DEFAULT_REFERENCE_ROOTS = (
    # Where a deployment keeps them — see docs/VPS.md.
    Path("/var/lib/estimator/references"),
    Path("D:/Chrome download"),
    Path.home() / "Downloads",
)


@pytest.fixture(scope="session")
def reference_root() -> Path:
    """The folder holding the reference КП, or a skip."""
    from app.services.history.proposals import reference_proposals

    configured = os.getenv("REFERENCE_ROOT")
    candidates = [Path(configured)] if configured else list(DEFAULT_REFERENCE_ROOTS)
    for root in candidates:
        if root.exists() and reference_proposals(root):
            return root
    pytest.skip(
        "reference proposals not on this machine; set REFERENCE_ROOT to the "
        "folder holding the КП project folders"
    )
