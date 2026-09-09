"""The deployment contract: env-driven config, CORS, and the bundled seed.

A broken deploy config fails silently — the app boots with an empty catalog and
a browser that cannot reach it. These pin the parts Render and Vercel rely on.
"""

from __future__ import annotations

import json
import os
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


def test_render_free_tier_declares_no_disk() -> None:
    """Render rejects a Blueprint with a disk on the free plan.

    "services[0] disks are not supported for free tier services" — so a free
    service must declare none, and its storage paths must be writable ones
    rather than a disk mount point that will not exist.
    """
    spec = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    service = spec["services"][0]
    env = {e["key"]: e["value"] for e in service["envVars"] if "value" in e}

    if service.get("plan") == "free":
        assert "disk" not in service, "free tier services cannot declare a disk"
        assert not env["DATA_DIR"].startswith("/var/data"), (
            "/var/data only exists when a disk is mounted"
        )
        assert "/var/data" not in env["DATABASE_URL"]
    else:
        assert service["disk"]["mountPath"] == env["DATA_DIR"]


def test_storage_paths_follow_data_dir(tmp_path: Path, monkeypatch) -> None:
    """DATA_DIR must move uploads and the cache with it.

    Otherwise pointing it at /tmp or a disk leaves PDFs and rendered pages
    behind in the source tree, where the deploy cannot rely on them.
    """
    # This is about the derivation, so the ambient environment must not answer
    # for it -- conftest sets these three to keep the suite out of a live
    # deployment's upload tree, and they would satisfy the assertions falsely.
    for name in ("UPLOAD_DIR", "CACHE_DIR", "PAGE_IMAGE_DIR"):
        monkeypatch.delenv(name, raising=False)

    s = Settings(data_dir=tmp_path)
    assert s.upload_dir == tmp_path / "uploads"
    assert s.cache_dir == tmp_path / "cache"
    assert s.page_image_dir == tmp_path / "pages"

    # An explicit override still wins.
    other = Settings(data_dir=tmp_path, upload_dir=tmp_path / "custom")
    assert other.upload_dir == tmp_path / "custom"
    assert other.cache_dir == tmp_path / "cache"


def test_api_prefix_is_configurable_for_a_path_mounted_app() -> None:
    """cPanel mounts a Python app at a URL path you choose.

    Mounting it at /api while the router also prefixes /api yields
    /api/api/health — a 404 that looks like a broken deployment. The prefix is
    therefore a setting, checked here in a subprocess because the app module
    reads it once at import.
    """
    import subprocess
    import sys

    code = (
        "from app.main import app;"
        "print(sorted(r.path for r in app.routes "
        "if getattr(r,'path','').endswith('/health')))"
    )
    for prefix, expected in (("", "/health"), ("/api", "/api/health")):
        env = {**os.environ, "API_PREFIX": prefix}
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT / "backend",
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert expected in result.stdout, (
            f"API_PREFIX={prefix!r} produced {result.stdout.strip()}"
        )


def test_no_content_routes_declare_no_body() -> None:
    """A 204 handler must not carry a response model.

    FastAPI infers the response model from the return annotation, so a plain
    `-> None` yields NoneType — a body — and the route is rejected while the
    module is still being imported:

        AssertionError: Status code 204 must not have a response body

    That is an import-time failure, so the whole service refuses to boot, not
    just the one endpoint. Newer FastAPI tolerates it; the version we deploy
    does not.
    """
    from fastapi.routing import APIRoute

    no_content = [
        r for r in app.routes
        if isinstance(r, APIRoute) and r.status_code == 204
    ]
    assert no_content, "expected at least one 204 route"
    for route in no_content:
        assert route.response_model is None, (
            f"{route.path} returns 204 but declares a response model "
            f"({route.response_model!r}); annotate it `-> Response`"
        )
        assert route.response_class.__name__ == "Response", (
            f"{route.path} should use response_class=Response"
        )


def test_installed_packages_match_requirements() -> None:
    """The pins must describe the environment the suite actually runs in.

    requirements.txt was written from memory once and drifted from the local
    environment by ten packages, so nothing here exercised what Render
    installed — and the deploy broke on a FastAPI assertion the local version
    does not raise.
    """
    import importlib.metadata as md
    import re

    text = (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
    mismatches: list[str] = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]+\])?==(.+)$", line)
        if not m:
            continue  # a range spec; not pinned to one version on purpose
        name, want = m.group(1), m.group(2).strip()
        try:
            have = md.version(name)
        except md.PackageNotFoundError:
            mismatches.append(f"{name}: pinned {want}, not installed")
            continue
        if have != want:
            mismatches.append(f"{name}: pinned {want}, installed {have}")

    assert not mismatches, (
        "requirements.txt does not describe this environment, so the suite is "
        "not testing what gets deployed:\n  " + "\n  ".join(mismatches)
    )


def test_shipped_data_is_not_gitignored() -> None:
    """The three files a deployment cannot start without must reach the repo.

    A bare `data/` rule matches `backend/app/data/` as well, which silently
    excluded the rule pack, the catalog seed and the logo — the deploy would
    boot with zero rules and an empty catalog.

    This asks git what it would do with a path, so it can only run inside a
    working copy. On the server there is none: redeploy.sh packs the tree with
    `--exclude='./.git'`, deliberately, because the deployment has no business
    carrying the history. There `git check-ignore` exits 128 for every path,
    which the old helper could not tell apart from "not ignored" — so the suite
    on the server reported `.env must never be committed` about a `.gitignore`
    that has listed `.env` all along.
    """
    import subprocess

    must_ship = [
        "backend/app/data/rules/template_layout.json",
        "backend/app/data/seed/catalog.json",
        "backend/app/data/brand/logo.png",
        "render.yaml",
        "frontend/vercel.json",
    ]
    must_not_ship = [".env", "data/estimator.db", "backend/estimator.db"]

    def check_ignore(path: str) -> int:
        """git's own verdict: 0 ignored, 1 not ignored, anything else broken."""
        return subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=ROOT, capture_output=True
        ).returncode

    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=ROOT, capture_output=True, text=True,
        )
    except OSError:  # pragma: no cover - git is not installed
        pytest.skip("git unavailable")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        pytest.skip(f"{ROOT} is not a git working copy — nothing to ask git about")

    for path in must_ship:
        if (ROOT / path).exists():
            code = check_ignore(path)
            assert code == 1, (
                f"{path} is gitignored but the deploy needs it"
                if code == 0
                else f"git check-ignore {path} failed with {code}"
            )
    for path in must_not_ship:
        code = check_ignore(path)
        assert code == 0, (
            f"{path} must never be committed"
            if code == 1
            else f"git check-ignore {path} failed with {code}"
        )


def test_vercel_json_serves_the_spa() -> None:
    manifest = ROOT / "frontend" / "vercel.json"
    assert manifest.exists(), "frontend/vercel.json missing"
    spec = json.loads(manifest.read_text(encoding="utf-8"))
    assert spec["outputDirectory"] == "dist"
    # Client-side routes must fall back to index.html or a refresh 404s.
    assert any(r["destination"] == "/index.html" for r in spec["rewrites"])
