"""Structured shapes the model is constrained to produce.

Two rules encoded in every schema here:

* Every extracted value carries ``confidence`` and a ``source`` note, so the UI
  can show where a number came from and how much to trust it.
* "Unknown" is always representable. A field the model cannot read from the
  document is reported as unknown, never filled with a plausible number.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low"]

# The company's own drawing vocabulary, taken from their drawing registers.
PageKind = Literal[
    "title",
    "contents",
    "marketing",
    "photo",
    "concept",
    "visual",
    "master_plan",
    "layout_plan",
    "dendro_plan",
    "plant_schedule",
    "planting_plan",
    "engineering",
    "spec_table",
    "brief",
    "blank",
    "unknown",
]

# Estimate sections, keyed as in the compiled template layout.
SectionKey = Literal[
    "prep",
    "water_supply",
    "irrigation",
    "lighting",
    "drainage_storm",
    "drainage_ground",
    "paving",
    "pathway",
    "geogrid",
    "planting",
    "lawn",
    "planters",
    "concrete",
    "fire_zone",
    "extra_works",
]


class Measurement(BaseModel):
    """A quantity read off a drawing or schedule."""

    label: str = Field(description="Назва показника як у документі, напр. «Площа озеленення»")
    value: float | None = Field(description="Числове значення; null, якщо прочитати не вдалося")
    unit: str = Field(description="Одиниця виміру як у документі: м², м.п, шт, м³, Га")
    section: SectionKey | None = Field(
        description="До якої секції кошторису належить цей показник, якщо очевидно"
    )
    confidence: Confidence
    source: str = Field(description="Де саме на сторінці це прочитано, напр. «таблиця ТЕП»")


class PlantRow(BaseModel):
    """One line of an ``Асортиментна відомість рослин``."""

    number: str = Field(description="№ у відомості, якщо є; інакше порожній рядок")
    name: str = Field(description="Назва рослини українською, як у документі")
    latin_name: str = Field(description="Латинська назва, якщо вказана; інакше порожній рядок")
    quantity: float | None = Field(description="Кількість, шт; null якщо не вказано")
    size: str = Field(description="Розмір/висота/контейнер, якщо вказано")
    note: str = Field(description="Примітка з документа, напр. «існуючі», «крок 0,8 м»")
    is_existing: bool = Field(
        description="true, якщо позиція позначена як існуюча — такі рослини не входять у кошторис"
    )
    confidence: Confidence


class CoverageRow(BaseModel):
    """One line of a ``Відомість елементів покриття``."""

    name: str = Field(description="Найменування покриття/елемента як у документі")
    quantity: float | None
    unit: str
    note: str
    section: SectionKey | None
    confidence: Confidence


class DetectedSystem(BaseModel):
    key: SectionKey
    label: str = Field(description="Назва системи українською")
    evidence: str = Field(description="Що саме в документі вказує на цю систему")
    confidence: Confidence


class PageFindings(BaseModel):
    """What one page contributes. Empty lists are a valid, honest answer."""

    page_kind: PageKind
    summary: str = Field(description="Один-два речення: що зображено на сторінці")
    is_useful: bool = Field(
        description="false для титулів, візуалізацій, маркетингових сторінок без даних"
    )
    measurements: list[Measurement]
    plants: list[PlantRow]
    coverage: list[CoverageRow]
    systems: list[DetectedSystem]
    zones: list[str] = Field(description="Виявлені зони: тераса, альтанка, зона вогню, парковка тощо")
    notes: list[str] = Field(description="Технічні примітки з креслення, важливі для кошторису")
    unreadable: list[str] = Field(
        description="Що на сторінці є, але прочитати не вдалося — не вигадувати значення"
    )
    confidence: Confidence


class Fact(BaseModel):
    key: str = Field(description="Стабільний ключ, напр. area_total, area_lawn, entrances_count")
    label: str = Field(description="Назва показника українською")
    value: str = Field(description="Значення як текст; порожній рядок якщо невідомо")
    unit: str
    status: Literal["confirmed", "assumption", "unknown", "needs_user_input"]
    confidence: Confidence
    source_type: Literal[
        "uploaded_pdf", "floor_plan", "product_catalog", "historical_estimate",
        "user_input", "derived_rule", "unknown",
    ]
    source_ref: str = Field(description="Файл і сторінка, напр. «Будьків.pdf, с. 15»")
    note: str


class Conflict(BaseModel):
    topic: str
    values: list[str] = Field(description="Суперечливі значення з їх джерелами")
    impact: str = Field(description="На що це вплине у кошторисі")
    question: str = Field(description="Коротке конкретне питання користувачу")


class OpenQuestion(BaseModel):
    code: str = Field(description="Стабільний код питання, напр. lawn_type")
    group: str = Field(description="Логічна група для об'єднання питань")
    text: str = Field(description="Коротке конкретне питання українською")
    why: str = Field(description="Чому без відповіді не можна порахувати")
    kind: Literal["text", "number", "choice", "boolean"]
    choices: list[str]
    affects: list[str] = Field(description="Які позиції/секції залежать від відповіді")


# --- Aggregation -------------------------------------------------------------
# The per-page schema above uses Literal unions freely: one page's worth of
# structure compiles to a small grammar and works. The aggregation schema
# covers every page at once, and the same Literals there compile to a grammar
# the API rejects outright ("The compiled grammar is too large, which would
# cause performance issues"). The aggregate models below therefore take plain
# strings for the enum-like fields, and we normalise them in Python straight
# after parsing -- same guarantees, a fraction of the grammar.


class AggFact(BaseModel):
    key: str = Field(description="Стабільний ключ, напр. area_total, area_lawn")
    label: str = Field(description="Назва показника українською")
    value: str = Field(description="Значення як текст; порожній рядок якщо невідомо")
    unit: str
    section: str = Field(description="Ключ секції кошторису або порожній рядок")
    status: str = Field(description="confirmed | assumption | unknown | needs_user_input")
    confidence: str = Field(description="high | medium | low")
    source_type: str = Field(description="uploaded_pdf | floor_plan | user_input | derived_rule")
    source_ref: str = Field(description="Файл і сторінка, напр. «Будьків.pdf, с. 15»")
    note: str


class AggSystem(BaseModel):
    key: str = Field(description="Ключ секції кошторису: planting, lawn, paving, pathway тощо")
    label: str = Field(description="Назва системи українською")
    evidence: str = Field(description="Що саме в документі вказує на цю систему")
    confidence: str


class AggPlant(BaseModel):
    number: str
    name: str
    latin_name: str
    quantity: float | None
    size: str
    note: str
    is_existing: bool = Field(
        description="true для позначених «існуючі» — вони не входять у кошторис"
    )
    confidence: str


class AggCoverage(BaseModel):
    name: str
    quantity: float | None
    unit: str
    section: str = Field(description="Ключ секції кошторису або порожній рядок")
    note: str
    confidence: str


class AggConflict(BaseModel):
    topic: str
    values: list[str]
    impact: str
    question: str


class AggQuestion(BaseModel):
    code: str
    group: str
    text: str
    why: str
    kind: str = Field(description="text | number | choice | boolean")
    choices: list[str]
    affects: list[str]


# Aggregation runs as three focused calls rather than one.
#
# Grammar cost scales with the number of unbounded arrays-of-objects, not with
# schema text length: a single combined model with six object arrays plus four
# string arrays is rejected with "The compiled grammar is too large", while
# PageFindings -- a physically larger schema with four object arrays -- compiles
# fine. Splitting also gives three sharper prompts and three cache entries.


class SiteModel(BaseModel):
    """Call 1: what the object is and what it measures."""

    object_type: str = Field(description="Тип об'єкта: приватна ділянка, тераса, ЖК тощо")
    summary: str = Field(description="2-4 речення про об'єкт і склад робіт")
    facts: list[AggFact]
    systems: list[AggSystem]
    zones: list[str] = Field(description="Зони: тераса, альтанка, зона вогню, парковка тощо")
    confidence: str


class ScheduleModel(BaseModel):
    """Call 2: the plant schedule and the coverage schedule."""

    plants: list[AggPlant]
    coverage: list[AggCoverage]


class ReviewModel(BaseModel):
    """Call 3: what is uncertain, contradictory, or needs the operator."""

    assumptions: list[str]
    unknowns: list[str]
    risks: list[str]
    conflicts: list[AggConflict]
    questions: list[AggQuestion]


class ObjectAnalysisResult(BaseModel):
    """The merged site model. Assembled in Python from the three calls above;
    never sent to the API as an output schema."""

    object_type: str = ""
    summary: str = ""
    facts: list[AggFact] = Field(default_factory=list)
    systems: list[AggSystem] = Field(default_factory=list)
    plants: list[AggPlant] = Field(default_factory=list)
    coverage: list[AggCoverage] = Field(default_factory=list)
    zones: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    conflicts: list[AggConflict] = Field(default_factory=list)
    questions: list[AggQuestion] = Field(default_factory=list)
    confidence: str = "medium"


# --- Normalisation of the aggregate result -----------------------------------

SECTION_KEYS: frozenset[str] = frozenset(SectionKey.__args__)  # type: ignore[attr-defined]
CONFIDENCES = frozenset({"high", "medium", "low"})
# "excluded" is set by the estimator in the UI, never by the model: it drops a
# value out of the estimate while keeping the evidence that it was found, which
# deleting the row would throw away.
FACT_EXCLUDED = "excluded"
FACT_STATUSES = frozenset(
    {"confirmed", "assumption", "unknown", "needs_user_input", FACT_EXCLUDED}
)

# Statuses whose value must not reach the estimate. "unknown" and
# "needs_user_input" have no trustworthy number yet; "excluded" has one the
# estimator has decided not to bill.
FACT_STATUSES_OUT_OF_ESTIMATE = frozenset({"unknown", "needs_user_input", FACT_EXCLUDED})
QUESTION_KINDS = frozenset({"text", "number", "choice", "boolean"})
SOURCE_TYPES = frozenset({
    "historical_estimate", "uploaded_pdf", "floor_plan", "product_catalog",
    "user_input", "derived_rule", "unknown",
})


def clean_confidence(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in CONFIDENCES else "medium"


def clean_section(value: str) -> str | None:
    """Keep a section key only when the template actually has that section."""
    v = (value or "").strip().lower()
    return v if v in SECTION_KEYS else None


def clean_fact_status(value: str) -> str:
    """Default to ``assumption``: an unrecognised status must not read as fact."""
    v = (value or "").strip().lower()
    return v if v in FACT_STATUSES else "assumption"


def clean_question_kind(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in QUESTION_KINDS else "text"


def clean_source_type(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in SOURCE_TYPES else "uploaded_pdf"


class SectionDriver(BaseModel):
    """A driver quantity that feeds the template's rules.

    These map onto the template's own input rows -- "Довжина траншей (м)",
    "Загальна площа бруківка", "Площа газону (рулонного)" -- which is exactly
    how the client's workbook is designed to be filled in.
    """

    section: SectionKey
    driver_name: str = Field(description="Назва рядка-драйвера у шаблоні")
    value: float | None
    unit: str
    basis: str = Field(description="На підставі чого визначено значення")
    confidence: Confidence
    needs_user_input: bool


class LineRequest(BaseModel):
    """A requested estimate line, before catalog matching and pricing."""

    section: SectionKey
    block: Literal["materials", "works", "plants"]
    query: str = Field(description="Назва позиції як у базі клієнта, якщо відома")
    quantity: float | None
    unit: str
    reason: str = Field(description="Чому ця позиція потрібна — коротко і конкретно")
    confidence: Confidence


class EstimatePlan(BaseModel):
    """The model's proposal for what the estimate should contain."""

    sections: list[SectionKey]
    drivers: list[SectionDriver]
    lines: list[LineRequest]
    questions: list[OpenQuestion]
    notes: list[str]
