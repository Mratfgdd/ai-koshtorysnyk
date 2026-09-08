"""Surviving a model answer that ran out of room.

A 46-page drawing set produced a site model that hit `max_tokens` and stopped
mid-string at ~25 000 characters. `json.loads` raised "Unterminated string",
which reached the estimator as a bare AIUnavailable telling them nothing.

The defences, in the order they apply: a budget large enough for the job, one
retry at double the budget, and only then salvage of the partial document.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.services.ai.client import _close_json, repair_truncated_json

# Shaped like the real thing: the site model, cut off inside a fact's note.
WHOLE = {
    "object_type": "приватна ділянка",
    "summary": "Ділянка з благоустроєм",
    "facts": [
        {"key": "lawn_area", "label": "Площа газону", "value": "259", "unit": "м2"},
        {"key": "paving_area", "label": "Площа мощення", "value": "45", "unit": "м2"},
        {"key": "green_area", "label": "Площа озеленення", "value": "406", "unit": "м2"},
    ],
    "systems": [{"key": "lawn", "label": "Газон"}],
}


def cut(payload: dict, at: int) -> str:
    return json.dumps(payload, ensure_ascii=False)[:at]


# --- the repair itself --------------------------------------------------------


def test_untouched_json_is_left_alone() -> None:
    text = json.dumps(WHOLE, ensure_ascii=False)
    assert _close_json(text) is None, "complete JSON must not be reported as open"
    assert repair_truncated_json(text) == WHOLE


def test_a_string_cut_mid_word_is_recovered() -> None:
    """The exact failure mode: the answer stops inside a string value."""
    text = json.dumps(WHOLE, ensure_ascii=False)
    broken = text[: text.index("Площа озеленення") + 6]  # mid-word, inside quotes
    assert broken.count('"') % 2 == 1, "fixture is not actually mid-string"

    recovered = repair_truncated_json(broken)
    assert recovered is not None, "a mid-string cut must be recoverable"
    assert recovered["object_type"] == "приватна ділянка"
    # Everything before the cut survives; the record it died in may not.
    labels = [f["label"] for f in recovered["facts"]]
    assert "Площа газону" in labels and "Площа мощення" in labels


def test_recovery_at_every_cut_point_never_raises() -> None:
    """Whatever character the answer stops on, salvage must not explode."""
    text = json.dumps(WHOLE, ensure_ascii=False)
    recovered_count = 0
    for at in range(1, len(text)):
        result = repair_truncated_json(text[:at])  # must not raise
        if isinstance(result, dict):
            recovered_count += 1
            # Anything returned has to be real JSON-shaped data, not debris.
            assert json.dumps(result)
    # Most cut points past the opening brace should yield something usable.
    assert recovered_count > len(text) // 2, (
        f"only {recovered_count} of {len(text)} cut points recovered"
    )


def test_a_truncated_array_keeps_its_complete_elements() -> None:
    text = json.dumps(WHOLE, ensure_ascii=False)
    broken = text[: text.index('{"key": "green_area"') + 14]
    recovered = repair_truncated_json(broken)
    assert recovered is not None
    assert len(recovered["facts"]) >= 2, "complete earlier facts must survive"


def test_hopeless_input_returns_none_rather_than_guessing() -> None:
    assert repair_truncated_json("") is None
    assert repair_truncated_json("   ") is None
    assert repair_truncated_json("не JSON взагалі") is None


def test_a_lone_trailing_backslash_does_not_escape_the_closing_quote() -> None:
    recovered = repair_truncated_json('{"note": "шлях C:\\\\')
    assert recovered is None or isinstance(recovered, dict)


# --- the budgets --------------------------------------------------------------


def test_the_token_budget_is_large_enough_for_a_big_drawing_set() -> None:
    """The failure was a budget too small, so guard the floor.

    claude-opus-5 allows 128K output tokens. The reported truncation happened at
    ~25 000 characters of Cyrillic, which is roughly 12K tokens — the old
    aggregation budget exactly.
    """
    s = Settings()
    assert s.ai_max_tokens >= 32000, (
        f"ai_max_tokens is {s.ai_max_tokens}; a 46-page set overran 16000"
    )
    assert s.ai_max_tokens_ceiling > s.ai_max_tokens, (
        "the retry must be able to raise the budget"
    )
    assert s.ai_max_tokens_ceiling <= 128000, "claude-opus-5 caps output at 128K"


def test_aggregation_asks_for_more_than_the_budget_that_failed() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "ai" / "analyzer.py"
    ).read_text(encoding="utf-8")
    assert "12000," not in source, "an aggregation call still uses the budget that truncated"


def test_findings_are_not_silently_clipped_at_180k() -> None:
    from app.services.ai.analyzer import MAX_FINDINGS_CHARS

    assert MAX_FINDINGS_CHARS >= 500_000, (
        "the old 180 000-character cap dropped pages off a large set"
    )


def test_uploads_already_allow_a_150mb_drawing_set() -> None:
    """Asked for as part of this fix; it was already in place, so keep it so."""
    s = Settings()
    assert s.max_upload_mb >= 150


# --- the call path ------------------------------------------------------------


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessage:
    def __init__(self, text: str, stop_reason: str) -> None:
        self.content = [FakeBlock(text)]
        self.stop_reason = stop_reason
        self.stop_details = None
        self.usage = None


class FakeMessages:
    """Truncates on the first call, answers fully on the retry."""

    def __init__(self, first: FakeMessage, second: FakeMessage | None = None) -> None:
        self.first, self.second = first, second
        self.budgets: list[int] = []

    def stream(self, **kwargs):
        self.budgets.append(kwargs["max_tokens"])
        message = self.first if len(self.budgets) == 1 or self.second is None else self.second
        return FakeStream(message)


@pytest.fixture
def client(tmp_path):
    from app.services.ai.client import AIClient

    settings = Settings(
        anthropic_api_key="sk-test",
        data_dir=tmp_path,
        ai_max_tokens=32000,
        ai_max_tokens_ceiling=96000,
    )
    return AIClient(settings)


def test_a_truncated_answer_is_retried_with_a_bigger_budget(client, monkeypatch) -> None:
    from pydantic import BaseModel

    class Site(BaseModel):
        object_type: str
        summary: str

    complete = json.dumps({"object_type": "ділянка", "summary": "ок"}, ensure_ascii=False)
    truncated = complete[:20]

    fake = FakeMessages(
        FakeMessage(truncated, "max_tokens"),
        FakeMessage(complete, "end_turn"),
    )
    monkeypatch.setattr(type(client), "client", property(lambda self: type("C", (), {"messages": fake})()))

    result = client.structured(
        schema_model=Site, system="s", content=[{"type": "text", "text": "x"}],
        max_tokens=32000, use_cache=False,
    )

    assert result.object_type == "ділянка"
    assert fake.budgets == [32000, 64000], f"budgets used: {fake.budgets}"


def test_a_still_truncated_answer_is_salvaged_not_crashed(client, monkeypatch) -> None:
    """Two truncations must still yield something, not an unactionable error."""
    from pydantic import BaseModel

    class Site(BaseModel):
        object_type: str
        summary: str

    complete = json.dumps(
        {"object_type": "приватна ділянка", "summary": "довгий опис ділянки"},
        ensure_ascii=False,
    )
    truncated = complete[: complete.index("довгий") + 4]  # dies inside `summary`

    fake = FakeMessages(FakeMessage(truncated, "max_tokens"))
    monkeypatch.setattr(type(client), "client", property(lambda self: type("C", (), {"messages": fake})()))

    result = client.structured(
        schema_model=Site, system="s", content=[{"type": "text", "text": "x"}],
        max_tokens=32000, use_cache=False,
    )
    assert result.object_type == "приватна ділянка"
    assert len(fake.budgets) == 2, "the retry must still happen before salvage"


def test_an_unsalvageable_answer_says_what_to_do(client, monkeypatch) -> None:
    from pydantic import BaseModel

    from app.services.ai.client import AIUnavailable

    class Site(BaseModel):
        object_type: str

    fake = FakeMessages(FakeMessage("не JSON", "max_tokens"))
    monkeypatch.setattr(type(client), "client", property(lambda self: type("C", (), {"messages": fake})()))

    with pytest.raises(AIUnavailable) as caught:
        client.structured(
            schema_model=Site, system="s", content=[{"type": "text", "text": "x"}],
            max_tokens=32000, use_cache=False,
        )
    message = str(caught.value)
    assert "обірвалася" in message and "AI_MAX_TOKENS" in message, message
