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


def _close_json(text: str) -> str | None:
    """Close a JSON fragment: finish an open string, then shut open brackets.

    Returns None when the text was not actually left open, so the caller can
    tell "nothing to repair" from "repaired".
    """
    stack: list[str] = []
    in_string = False
    escaped = False

    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack:
            stack.pop()

    if not stack and not in_string:
        return None

    out = text
    if in_string:
        if escaped:  # a lone trailing backslash would escape our own quote
            out = out[:-1]
        out += '"'
    out = out.rstrip().rstrip(",")
    return out + "".join(reversed(stack))


def repair_truncated_json(text: str, attempts: int = 400) -> Any | None:
    """Best-effort parse of JSON that was cut off mid-way.

    A response that hits ``max_tokens`` stops at whatever character it reached —
    typically inside a string, which is why the failure reads "Unterminated
    string". Closing the open string and brackets recovers the whole document
    except the record it died in; if that record is itself malformed, step back
    to the previous separator and try again, dropping one element at a time.

    This is a salvage path, not a parser. It runs only after a retry with a
    larger budget has already failed, and the caller says so in the log — a
    partial analysis the estimator can see beats an exception they cannot act
    on, but it must never pass silently for a complete one.
    """
    candidate = text.strip()
    for _ in range(attempts):
        if not candidate:
            return None
        closed = _close_json(candidate)
        if closed is not None:
            try:
                return json.loads(closed)
            except json.JSONDecodeError:
                pass
        else:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        cut = max(candidate.rfind(","), candidate.rfind("["), candidate.rfind("{"))
        if cut <= 0:
            return None
        candidate = candidate[:cut] if candidate[cut] == "," else candidate[: cut + 1]
    return None


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

        output_config = {
            "effort": effort,
            "format": {"type": "json_schema", "schema": _strict_schema(schema)},
        }

        parsed = self._call_with_retry(
            schema_model=schema_model,
            model=model,
            max_tokens=max_tokens,
            system_blocks=system_blocks,
            output_config=output_config,
            content=content,
        )
        if use_cache:
            self.cache.put(cache_key, parsed.model_dump(mode="json"))
        return parsed

    def _call_with_retry(
        self,
        *,
        schema_model: type[T],
        model: str,
        max_tokens: int,
        system_blocks: list[dict[str, Any]],
        output_config: dict[str, Any],
        content: list[dict[str, Any]],
    ) -> T:
        """Make the call, and survive a response that ran out of room.

        A 46-page drawing set produces a long site model, and when the answer
        hits ``max_tokens`` the JSON simply stops — mid-string, 25 000 characters
        in — and `json.loads` raised "Unterminated string", which surfaced to the
        estimator as a bare AIUnavailable with nothing to act on.

        Three lines of defence, in order of preference:
          1. a budget large enough for the job in the first place;
          2. on `stop_reason == "max_tokens"`, one retry at double the budget;
          3. only then, salvage the partial document, and say so loudly.
        """
        ceiling = max(self.settings.ai_max_tokens_ceiling, max_tokens)
        budget = min(max_tokens, ceiling)
        last_text = ""

        for attempt in (1, 2):
            # Streaming, not create(): the SDK refuses non-streaming requests
            # whose max_tokens implies a long generation, and a large
            # aggregation is exactly that.
            with self.client.messages.stream(
                model=model,
                max_tokens=budget,
                system=system_blocks,
                thinking={"type": "adaptive"},
                output_config=output_config,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                response = stream.get_final_message()
            self._record(response)

            if response.stop_reason == "refusal":
                detail = getattr(response, "stop_details", None)
                raise AIUnavailable(
                    f"Модель відхилила запит ({getattr(detail, 'category', 'невідомо')})."
                )

            text = next((b.text for b in response.content if b.type == "text"), "")
            if not text.strip():
                raise AIUnavailable("Порожня відповідь моделі.")
            last_text = text

            truncated = response.stop_reason == "max_tokens"
            if not truncated:
                try:
                    return schema_model.model_validate(json.loads(text))
                except json.JSONDecodeError as exc:
                    # Complete by the API's reckoning but still unparseable:
                    # treat it like a truncation and retry once.
                    log.warning("model returned invalid JSON (%s); retrying", exc)
                    truncated = True

            if attempt == 1 and budget < ceiling:
                bigger = min(budget * 2, ceiling)
                log.warning(
                    "response hit max_tokens at %d (%d chars); retrying with %d",
                    budget, len(text), bigger,
                )
                budget = bigger
                continue
            break

        salvaged = repair_truncated_json(last_text)
        if salvaged is not None:
            try:
                parsed = schema_model.model_validate(salvaged)
                log.error(
                    "answer truncated at %d tokens even after retry; recovered a "
                    "partial %s from %d characters — some findings are missing",
                    budget, schema_model.__name__, len(last_text),
                )
                return parsed
            except Exception as exc:  # noqa: BLE001 - salvage is best-effort
                log.warning("partial JSON did not satisfy the schema: %s", exc)

        raise AIUnavailable(
            f"Відповідь моделі обірвалася на ліміті {budget} токенів "
            f"({len(last_text)} символів) і не піддалася відновленню. "
            "Збільште AI_MAX_TOKENS або зменште обсяг документа."
        )

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
