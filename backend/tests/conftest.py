"""Fixtures shared across the suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# The issued proposals are the client's own files and are not in the repository.
# Point REFERENCE_ROOT at the folder holding the project sub-folders to run the
# checks that need them; without it those tests skip rather than fail.
DEFAULT_REFERENCE_ROOTS = (
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
