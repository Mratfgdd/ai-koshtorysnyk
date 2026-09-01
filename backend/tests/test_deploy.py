"""The deployment contract: env-driven config, CORS, and the bundled seed.

A broken deploy config fails silently — the app boots with an empty catalog and
a browser that cannot reach it. These pin the parts Render and Vercel rely on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import SEED_FILE
from app.main import app

ROOT = Path(__file__).resolve().parents[2]


# --- configuration -----------------------------------------------------------


def test_cors_allows_any_vercel_deployment() -> None:
    """Vercel gives every build its own hostname, so one pinned origin breaks."""
    s = Settings(cors_origins="")
    assert s.cors_origin_regex == r"https://.*\.vercel\.app"
    assert "http://localhost:5173" in s.cors_origin_list


def test_cors_accepts_extra_origins_and_deduplicates() -> None:
    s = Settings(cors_origins="https://kp.example.com/, https://kp.example.com")
    assert s.cors_origin_list.count("https://kp.example.com") == 1
    assert not any(o.endswith("/") for o in s.cors_origin_list)


def test_cors_can_be_locked_down() -> None:
    s = Settings(cors_allow_vercel=False)
    assert s.cors_origin_regex is None


def test_database_url_env_overrides_the_local_default() -> None:
    s = Settings(database_url="postgresql+psycopg://u:p@host/db")
    assert s.sqlalchemy_url == "postgresql+psycopg://u:p@host/db"


def test_data_dir_env_moves_storage_to_the_mounted_disk(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path)
    assert s.sqlalchemy_url.endswith("estimator.db")
    assert str(tmp_path.as_posix()) in s.sqlalchemy_url


# --- live CORS behaviour -----------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize(
    "origin,allowed",
    [
        ("https://koshtorysnyk.vercel.app", True),
        ("https://koshtorysnyk-git-main-user.vercel.app", True),
        ("http://localhost:5173", True),
        ("https://evil.example.net", False),
    ],
)
def test_browser_origins(client: TestClient, origin: str, allowed: bool) -> None:
    response = client.get("/api/health", headers={"Origin": origin})
    header = response.headers.get("access-control-allow-origin")
    assert (header is not None) is allowed, f"{origin} -> {header}"


def test_export_filename_is_visible_cross_origin(client: TestClient) -> None:
    """Without this the browser cannot name the downloaded XLSX."""
    response = client.get(
        "/api/health", headers={"Origin": "https://koshtorysnyk.vercel.app"}
    )
    exposed = response.headers.get("access-control-expose-headers", "")
    assert "Content-Disposition" in exposed


# --- shipped data ------------------------------------------------------------


def test_rule_pack_ships_with_the_code() -> None:
    """A deploy has no copy of the client's workbook, so the pack must be in git."""
    pack = ROOT / "backend" / "app" / "data" / "rules" / "template_layout.json"
    if not pack.exists():
        pytest.skip("template not compiled")
    data = json.loads(pack.read_text(encoding="utf-8"))
    rules = sum(
        1 for s in data["sections"] for l in s["lines"] if l.get("qty_status") == "derived"
    )
    assert rules > 200, f"only {rules} rules in the pack"


def test_no_guessed_coefficients_remain() -> None:
    """needs_review.json exists only when a formula could not be translated."""
    review = ROOT / "backend" / "app" / "data" / "rules" / "needs_review.json"
    if review.exists():
        pending = json.loads(review.read_text(encoding="utf-8"))
        assert not pending, f"{len(pending)} formulas still unresolved"


def test_catalog_seed_is_present_and_usable() -> None:
    if not SEED_FILE.exists():
        pytest.skip("seed not generated; run scripts/make_seed.py")
    rows = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    assert len(rows) > 500
    required = {"name", "name_norm", "unit", "unit_price", "unit_cost", "kind", "category"}
    assert required <= set(rows[0])
    assert {r["kind"] for r in rows} <= {"material", "work", "plant"}


# --- deployment manifests ----------------------------------------------------


def test_render_yaml_binds_the_platform_port() -> None:
    manifest = ROOT / "render.yaml"
    assert manifest.exists(), "render.yaml missing"
    spec = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    service = spec["services"][0]

    assert "--host 0.0.0.0" in service["startCommand"]
    assert "$PORT" in service["startCommand"], "Render assigns the port at runtime"
    assert service["healthCheckPath"] == "/api/health"
    assert service["rootDir"] == "backend"

    env = {e["key"]: e for e in service["envVars"]}
    assert env["ANTHROPIC_API_KEY"].get("sync") is False, "the key must not live in git"
    assert service["disk"]["mountPath"] == env["DATA_DIR"]["value"], (
        "the database must sit on the persistent disk"
    )


def test_vercel_json_serves_the_spa() -> None:
    manifest = ROOT / "frontend" / "vercel.json"
    assert manifest.exists(), "frontend/vercel.json missing"
    spec = json.loads(manifest.read_text(encoding="utf-8"))
    assert spec["outputDirectory"] == "dist"
    # Client-side routes must fall back to index.html or a refresh 404s.
    assert any(r["destination"] == "/index.html" for r in spec["rewrites"])
