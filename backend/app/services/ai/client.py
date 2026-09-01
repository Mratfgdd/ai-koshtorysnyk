"""Anthropic client wrapper: caching, concurrency, structured output, retries.

Design rules this file enforces so the rest of the codebase cannot break them:

* The model is asked for *understanding*, never for arithmetic. Every helper
  returns structured data that the deterministic layers then compute over.
* Results are cached on disk by a hash of the exact request, so a resumed or
  repeated analysis never pays for the same page twice.
* Independent pages run concurrently with a bounded worker pool.
* Structured outputs are used everywhere, so no response parsing by regex.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TypeVar

import anthropic
from pydantic import BaseModel

from ...config import Settings, get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class AIUnavailable(RuntimeError):
    """No API key configured, or AI disabled. Callers degrade, never fabricate."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    cache_hits: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.calls += other.calls
        self.cache_hits += other.cache_hits

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
        }


class ResponseCache:
    """Content-addressed cache of model responses."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def key(payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        path = self.dir / f"{key}.json"
        tmp = path.with_suffix(".tmp")
        with self._lock:
            try:
                tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                tmp.replace(path)
            except OSError as exc:  # pragma: no cover
                log.warning("cache write failed: %s", exc)


class AIClient:
    """Thin, opinionated wrapper over the Anthropic Messages API."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.cache = ResponseCache(self.settings.cache_dir / "ai")
        self.usage = Usage()
        self._usage_lock = threading.Lock()
        self._client: anthropic.Anthropic | None = None

    # -- availability ---------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.settings.ai_enabled and self._resolve_key())

    def _resolve_key(self) -> str:
        import os

        return self.settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            key = self._resolve_key()
            if not key:
                # The SDK also resolves `ant auth login` profiles; a bare
                # constructor is correct when no explicit key is present.
                self._client = anthropic.Anthropic(max_retries=3, timeout=600.0)
            else:
                self._client = anthropic.Anthropic(api_key=key, max_retries=3, timeout=600.0)
        return self._client

    def _require(self) -> None:
        if not self.settings.ai_enabled:
            raise AIUnavailable("AI вимкнено налаштуванням AI_ENABLED=false.")

    # -- core call ------------------------------------------------------------
    def structured(
        self,
        *,
        schema_model: type[T],
        system: str,
        content: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        cache_system: bool = True,
        use_cache: bool = True,
    ) -> T:
        """One structured-output call, cached.

        ``schema_model`` is a Pydantic model; the API is constrained to it, so
        the caller gets a validated object instead of parsing prose.
        """
        self._require()
        model = model or self.settings.ai_model
        max_tokens = max_tokens or self.settings.ai_max_tokens
        effort = effort or self.settings.ai_effort
        schema = schema_model.model_json_schema()

        cache_key = self.cache.key(
            {
                "model": model,
                "system": system,
                "content": _cache_shape(content),
                "schema": schema,
                "effort": effort,
                "v": 3,
            }
        )
        if use_cache:
            hit = self.cache.get(cache_key)
            if hit is not None:
                with self._usage_lock:
                    self.usage.cache_hits += 1
                return schema_model.model_validate(hit)

        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache_system:
            # The system prompt and rule context are identical across pages of
            # one document; caching the prefix is where the savings are.
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            thinking={"type": "adaptive"},
            output_config={
                "effort": effort,
                "format": {
                    "type": "json_schema",
                    "schema": _strict_schema(schema),
                },
            },
            messages=[{"role": "user", "content": content}],
        )
        self._record(response)

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            raise AIUnavailable(
                f"Модель відхилила запит ({getattr(detail, 'category', 'невідомо')})."
            )

        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text.strip():
            raise AIUnavailable("Порожня відповідь моделі.")
        data = json.loads(text)
        parsed = schema_model.model_validate(data)
        if use_cache:
            self.cache.put(cache_key, parsed.model_dump(mode="json"))
        return parsed

    def _record(self, response: Any) -> None:
        u = getattr(response, "usage", None)
        if u is None:
            return
        with self._usage_lock:
            self.usage.calls += 1
            self.usage.input_tokens += getattr(u, "input_tokens", 0) or 0
            self.usage.output_tokens += getattr(u, "output_tokens", 0) or 0
            self.usage.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
            self.usage.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0

    # -- concurrency ----------------------------------------------------------
    def map_parallel(
        self,
        fn: Callable[[Any], Any],
        items: Sequence[Any],
        *,
        max_workers: int | None = None,
        on_result: Callable[[int, Any], None] | None = None,
        on_error: Callable[[int, Exception], None] | None = None,
    ) -> list[Any]:
        """Run independent per-page work concurrently.

        One page failing must not sink the document: errors are reported per
        item and the caller records them as issues, keeping the run resumable.
        """
        workers = max_workers or self.settings.ai_max_concurrency
        results: list[Any] = [None] * len(items)
        if not items:
            return results

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
            futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
            for future, index in futures.items():
                try:
                    value = future.result()
                    results[index] = value
                    if on_result:
                        on_result(index, value)
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    log.warning("parallel task %s failed: %s", index, exc)
                    if on_error:
                        on_error(index, exc)
        return results


# --- helpers -----------------------------------------------------------------


def image_block(path: str | Path, media_type: str = "image/jpeg") -> dict[str, Any]:
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _cache_shape(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hash images by digest rather than by their base64 payload."""
    shaped: list[dict[str, Any]] = []
    for block in content:
        if block.get("type") == "image":
            raw = block.get("source", {}).get("data", "")
            shaped.append({"type": "image", "sha": hashlib.sha256(raw.encode()).hexdigest()})
        else:
            shaped.append(block)
    return shaped


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a Pydantic JSON schema acceptable as a strict output format."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: walk(v) for k, v in node.items()}
            if node.get("type") == "object":
                node.setdefault("additionalProperties", False)
                props = node.get("properties")
                if isinstance(props, dict):
                    node["required"] = list(props.keys())
            return node
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    out = walk(schema)
    if defs:
        out["$defs"] = walk(defs)
    return out


def merge_usage(clients: Iterable[AIClient]) -> Usage:
    total = Usage()
    for c in clients:
        total.add(c.usage)
    return total
