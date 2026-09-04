"""Runtime configuration.

Everything is environment-driven with sane local defaults, so the system runs
with `uvicorn app.main:app` and no setup beyond an API key. No secrets in code.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI Кошторисник"
    debug: bool = False

    # --- networking ----------------------------------------------------------
    # Render supplies PORT; uvicorn is started with it in the start command.
    port: int = 8000
    # Extra browser origins allowed to call the API, comma-separated.
    # Local dev and any *.vercel.app deployment are allowed by default, so a
    # preview deployment works without redeploying the backend.
    cors_origins: str = ""
    cors_allow_vercel: bool = True

    # Where the API sits under the host.
    #
    # cPanel's "Setup Python App" mounts the app at a URL path you choose. If
    # you mount it at /api, the routes must NOT add /api again or every path
    # becomes /api/api/... . Set API_PREFIX="" in that case. On a dedicated
    # subdomain (api.example.com) leave it as /api.
    api_prefix: str = "/api"
    # Set when a proxy strips a prefix before forwarding, so generated links
    # and the OpenAPI docs point at the public URL.
    root_path: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
        origins += [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]
        return list(dict.fromkeys(origins))  # de-duplicate, keep order

    @property
    def cors_origin_regex(self) -> str | None:
        """Match every Vercel deployment of this frontend.

        Vercel gives each build its own hostname, so pinning one origin would
        break on the next deploy.
        """
        return r"https://.*\.vercel\.app" if self.cors_allow_vercel else None

    # --- storage -------------------------------------------------------------
    # Only DATA_DIR normally needs setting: the sub-directories follow it unless
    # they are given explicitly. Without that, pointing DATA_DIR at a mounted
    # disk (or /tmp) would silently leave uploads and the page cache behind in
    # the source tree.
    data_dir: Path = PROJECT_ROOT / "data"
    database_url: str = ""
    upload_dir: Path = PROJECT_ROOT / "data" / "uploads"
    cache_dir: Path = PROJECT_ROOT / "data" / "cache"
    page_image_dir: Path = PROJECT_ROOT / "data" / "pages"

    @model_validator(mode="after")
    def _derive_storage_paths(self) -> "Settings":
        explicit = self.model_fields_set
        for field, name in (
            ("upload_dir", "uploads"),
            ("cache_dir", "cache"),
            ("page_image_dir", "pages"),
        ):
            if field not in explicit:
                object.__setattr__(self, field, self.data_dir / name)
        return self

    # --- rule pack -----------------------------------------------------------
    rules_dir: Path = BACKEND_DIR / "app" / "data" / "rules"
    template_layout_file: str = "template_layout.json"

    # --- OpenAI --------------------------------------------------------------
    # Used only for the two things it was asked for: speech-to-text on the
    # clarification recorder, and turning a free-text clarification into
    # structured edits. Document understanding stays on Anthropic.
    #
    # pydantic-settings reads OPENAI_API_KEY from the environment and from
    # .env, so `os.getenv("OPENAI_API_KEY")` and this field see the same value.
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_transcribe_model: str = "whisper-1"
    # Whisper's own cap is 25 MB; a clarification is seconds long, so anything
    # near that is a stuck recorder rather than speech.
    max_audio_mb: int = 20

    # --- AI ------------------------------------------------------------------
    anthropic_api_key: str = ""
    ai_model: str = "claude-opus-5"
    ai_model_cheap: str = "claude-haiku-4-5"
    ai_effort: str = "high"
    # claude-opus-5 allows up to 128K output tokens. 16000 was not enough for a
    # 46-page drawing set: the site model stopped mid-string at ~25 000
    # characters and the JSON would not parse. Requests stream, so a large
    # ceiling costs nothing when the answer is short — only what is generated
    # is billed.
    ai_max_tokens: int = 32000
    # How far a retry may raise the budget after a response hits the cap.
    ai_max_tokens_ceiling: int = 96000
    ai_max_concurrency: int = 6
    ai_enabled: bool = True

    # --- document processing -------------------------------------------------
    page_render_dpi: int = 130
    page_render_max_px: int = 1600
    max_vision_pages: int = 40
    max_upload_mb: int = 400

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'estimator.db').as_posix()}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.upload_dir, self.cache_dir, self.page_image_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
