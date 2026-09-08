"""OpenAI: speech-to-text, and free-text clarifications turned into edits.

Scoped deliberately. Reading drawings stays on Anthropic — the prompts, the
caching and the page schemas are built around it. OpenAI is used for the two
jobs it was asked for:

* Whisper transcribes what the estimator dictates into the clarification box;
* a small chat model reads that sentence and returns *structured edits* to the
  object analysis, never prose and never arithmetic.

The model is allowed to say what to change. It is not allowed to compute
anything: the numbers it returns are the ones the estimator dictated, and every
quantity that follows from them is derived afterwards by the rule engine. That
is the same division of labour the rest of the system keeps.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from ...config import Settings, get_settings

log = logging.getLogger(__name__)


class OpenAIUnavailable(RuntimeError):
    """Raised when the key is missing or the call could not be completed."""


# --- what the model is allowed to return -------------------------------------
#
# Every field is required: OpenAI's structured outputs reject optional fields,
# so "nothing to say" is an empty string or an empty list, not an absent key.


class FactUpdate(BaseModel):
    label: str = Field(description="Назва показника, як він названий у переліку")
    value: str = Field(description="Нове значення, лише число або короткий текст")
    unit: str = Field(description="Одиниця виміру, або порожньо")
    action: Literal["set", "exclude"] = Field(
        description="set — встановити значення; exclude — не враховувати в КП"
    )


class ComponentUpdate(BaseModel):
    name: str = Field(description="Найменування позиції у відомості покриттів")
    quantity: float | None = Field(description="Нова кількість, або null якщо видалити")
    unit: str
    action: Literal["set", "remove"]


class ResolutionPlan(BaseModel):
    understood: str = Field(description="Одне речення українською: як зрозуміло вказівку")
    fact_updates: list[FactUpdate]
    component_updates: list[ComponentUpdate]
    unresolved: str = Field(
        description="Що з написаного не вдалося перетворити на зміну, або порожньо"
    )


SYSTEM = """Ти — асистент кошторисника ландшафтних робіт.

Користувач уточнює суперечливі дані у проєкті. Твоє завдання — перетворити його
репліку на структуровані зміни показників об'єкта.

Правила, яких не можна порушувати:
* Не рахуй нічого сам. Якщо сказано «площа 121 м²» — постав 121. Похідні
  кількості (матеріали, роботи) рахує рушій правил, не ти.
* Змінюй лише те, що користувач справді сказав. Не «покращуй» інші показники.
* Якщо показник є у переліку — використай його точну назву в полі label.
  Якщо такого немає, а користувач додає новий — назви його зрозуміло.
* Якщо користувач каже не враховувати щось у кошторисі — action = "exclude".
* Якщо репліка незрозуміла або не стосується жодного показника — поверни
  порожні списки і поясни це в unresolved. Не вигадуй значень.
* Відповідай лише структурою, українською мовою в текстових полях."""


class OpenAIClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(self.settings.openai_api_key)

    def _api(self) -> Any:
        if self._client is None:
            if not self.available:
                raise OpenAIUnavailable(
                    "Не налаштовано OPENAI_API_KEY — голосове введення та "
                    "розбір уточнень недоступні."
                )
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise OpenAIUnavailable(f"Пакет openai не встановлено: {exc}") from exc
            self._client = OpenAI(api_key=self.settings.openai_api_key, timeout=120.0)
        return self._client

    # -- speech ---------------------------------------------------------------

    def transcribe(self, audio: bytes, filename: str = "clarification.webm") -> str:
        """Return what was said, in Ukrainian."""
        if not audio:
            raise OpenAIUnavailable("Порожній аудіозапис.")

        stream = io.BytesIO(audio)
        # The SDK reads the extension off the name to pick the container, so a
        # BytesIO without one is rejected as an unsupported format.
        stream.name = filename or "clarification.webm"
        try:
            result = self._api().audio.transcriptions.create(
                model=self.settings.openai_transcribe_model,
                file=stream,
                language="uk",
                prompt=(
                    "Уточнення до кошторису ландшафтних робіт: площі в м², "
                    "довжини в м.п., кількості в шт."
                ),
            )
        except OpenAIUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("transcription failed")
            raise OpenAIUnavailable(f"Не вдалося розпізнати аудіо: {exc}") from exc

        return (getattr(result, "text", "") or "").strip()

    # -- interpretation -------------------------------------------------------

    def interpret(
        self,
        comment: str,
        *,
        conflict: dict[str, Any],
        facts: list[dict[str, Any]],
        components: list[dict[str, Any]],
    ) -> ResolutionPlan:
        """Turn a dictated clarification into edits to the object analysis."""
        if not comment.strip():
            raise OpenAIUnavailable("Порожнє уточнення.")

        known_facts = [
            {
                "label": f.get("label") or f.get("key"),
                "value": f.get("value"),
                "unit": f.get("unit", ""),
                "status": f.get("status"),
            }
            for f in facts
        ][:120]
        known_components = [
            {"name": c.get("name"), "quantity": c.get("quantity"), "unit": c.get("unit", "")}
            for c in components
        ][:120]

        task = (
            f"Суперечність: {conflict.get('topic', '')}\n"
            f"Варіанти: {', '.join(conflict.get('values') or [])}\n"
            f"Вплив: {conflict.get('impact', '')}\n"
            f"Питання: {conflict.get('question', '')}\n\n"
            f"Показники об'єкта: {known_facts}\n\n"
            f"Відомість покриттів: {known_components}\n\n"
            f"Репліка користувача: {comment.strip()}"
        )

        try:
            completion = self._api().chat.completions.parse(
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": task},
                ],
                response_format=ResolutionPlan,
                temperature=0,
            )
        except OpenAIUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("clarification parsing failed")
            raise OpenAIUnavailable(f"Не вдалося розібрати уточнення: {exc}") from exc

        plan = completion.choices[0].message.parsed
        if plan is None:
            raise OpenAIUnavailable("Модель не повернула структурованої відповіді.")
        return plan
