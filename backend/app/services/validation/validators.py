"""Validation engine.

Runs before export and after every recalculation. Each finding says four
things, because a bare warning is not actionable:

    ПРОБЛЕМА   what is wrong
    ЧОМУ       why it is wrong
    ЩО ЗРОБИТИ what would fix it
    ВПЛИВ      what it does to the estimate

Some checks are the client's own. Their template carries this cell rule::

    =ifs(K=0,"ВНЕСИ СОБІВАРТІСТЬ 1 од", K<=G,"ОК", K>G,"ПОМИЛКА")

i.e. a line whose cost exceeds its sale price is an error, and a line with no
cost recorded needs one entered. Both are reproduced verbatim below.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..rules.engine import Draft, DraftLine, normalize_name
from ..rules.totals import EstimateTotals

OK = "OK"
WARNING = "WARNING"
ERROR = "ERROR"
NEEDS_USER_INPUT = "NEEDS_USER_INPUT"

SEVERITY_ORDER = {ERROR: 0, NEEDS_USER_INPUT: 1, WARNING: 2, OK: 3}

# Quantities above these are almost certainly a unit mix-up rather than a real
# order. Tuned from the largest values seen across the sample proposals.
SUSPICIOUS_QTY = {
    "м²": 5_000.0,
    "м³": 500.0,
    "м.п": 5_000.0,
    "шт": 5_000.0,
    "кг": 50_000.0,
    "т": 200.0,
    "год": 500.0,
    "день": 200.0,
}


@dataclass
class Finding:
    code: str
    severity: str
    title: str
    detail: str = ""
    fix_hint: str = ""
    impact: str = ""
    line_name: str | None = None
    section: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "fix_hint": self.fix_hint,
            "impact": self.impact,
            "line_name": self.line_name,
            "section": self.section,
        }


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def questions(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == NEEDS_USER_INPUT]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    @property
    def status(self) -> str:
        for severity in (ERROR, NEEDS_USER_INPUT, WARNING):
            if any(f.severity == severity for f in self.findings):
                return severity
        return OK

    @property
    def exportable(self) -> bool:
        """Warnings do not block export; errors and open questions do."""
        return not self.errors and not self.questions

    def sorted(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.code))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exportable": self.exportable,
            "counts": {
                ERROR: len(self.errors),
                NEEDS_USER_INPUT: len(self.questions),
                WARNING: len(self.warnings),
            },
            "findings": [f.to_dict() for f in self.sorted()],
        }


Validator = Callable[[Draft, EstimateTotals, dict[str, Any]], Iterable[Finding]]
_VALIDATORS: list[Validator] = []


def billable(draft: Draft) -> list[DraftLine]:
    """Lines that actually appear on the invoice.

    Driver rows carry the estimator's input quantities (trench length, paved
    area) and are read by the rules but never priced, so pricing and duplicate
    checks must not look at them.
    """
    return [l for l in draft.lines if l.block != "driver"]


def validator(fn: Validator) -> Validator:
    _VALIDATORS.append(fn)
    return fn


def _money(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ")


# --- line-level checks -------------------------------------------------------


@validator
def check_missing_price(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    """A quantity with no price silently under-quotes the job.

    The client's plant assortment is deliberately price-free -- nursery quotes
    change per project and per season -- so a matched plant without a price is
    a decision for the operator, not a data error. Anything else is an error.
    """
    for line in billable(draft):
        if line.quantity <= 0 or line.unit_price > 0:
            continue
        if line.match_status != "matched":
            # The missing catalog match is the real problem and is already
            # reported; a second "no price" finding would just be noise.
            continue
        if line.block == "plants":
            yield Finding(
                code="plant_price_required",
                severity=NEEDS_USER_INPUT,
                title=f"Потрібна ціна рослини: «{line.name}»",
                detail=(
                    "Позиція є в асортименті, але у базі цін для рослин ціни не ведуться — "
                    "вони визначаються за прайсом розсадника на конкретний проєкт."
                ),
                fix_hint="Внесіть ціну постачальника для цієї позиції.",
                impact=(
                    f"Без ціни {line.quantity:g} {line.unit or 'шт'} не потраплять у суму, "
                    "а також занижується робота «Висадка рослин» (35% від вартості рослин)."
                ),
                line_name=line.name,
                section=line.section,
            )
        else:
            yield Finding(
                code="missing_price",
                severity=ERROR,
                title=f"Немає ціни: «{line.name}»",
                detail="Позиція має кількість, але ціна реалізації дорівнює нулю.",
                fix_hint="Внесіть ціну в базу «2026 База 1» або оберіть іншу позицію каталогу.",
                impact="Позиція не потрапить у суму — кошторис буде занижено на її вартість.",
                line_name=line.name,
                section=line.section,
            )


@validator
def check_cost_above_price(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    """The client's own ``ПОМИЛКА`` rule from column N of the template."""
    for line in billable(draft):
        if line.quantity <= 0 or line.unit_price <= 0:
            continue
        if line.unit_cost > line.unit_price:
            loss = (line.unit_cost - line.unit_price) * line.quantity
            yield Finding(
                code="cost_above_price",
                severity=ERROR,
                title=f"Собівартість вища за ціну: «{line.name}»",
                detail=(
                    f"Собівартість {_money(line.unit_cost)} грн перевищує ціну реалізації "
                    f"{_money(line.unit_price)} грн за одиницю."
                ),
                fix_hint="Перевірте ціну в базі — у шаблоні це позначається як «ПОМИЛКА».",
                impact=f"Збиток по позиції {_money(loss)} грн.",
                line_name=line.name,
                section=line.section,
            )
        elif line.unit_cost <= 0:
            yield Finding(
                code="missing_cost",
                severity=WARNING,
                title=f"Не внесено собівартість: «{line.name}»",
                detail="У базі немає собівартості 1 од., тому маржинальність не рахується.",
                fix_hint="Внесіть собівартість у колонку «Собівартість 1од(грн)».",
                impact="Підсумкова маржинальність буде завищена.",
                line_name=line.name,
                section=line.section,
            )


@validator
def check_quantities(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    for line in billable(draft):
        if line.quantity < 0:
            yield Finding(
                code="negative_quantity",
                severity=ERROR,
                title=f"Від'ємна кількість: «{line.name}»",
                detail=f"Кількість {line.quantity:g} {line.unit}.",
                fix_hint="Виправте кількість або правило, що її обчислює.",
                impact=f"Зменшує підсумок на {_money(abs(line.total))} грн.",
                line_name=line.name,
                section=line.section,
            )
            continue
        limit = SUSPICIOUS_QTY.get(normalize_name(line.unit))
        if limit and line.quantity > limit:
            yield Finding(
                code="suspicious_quantity",
                severity=WARNING,
                title=f"Підозріла кількість: «{line.name}»",
                detail=f"{line.quantity:g} {line.unit} — це помітно більше за типове значення.",
                fix_hint="Перевірте одиницю виміру та вихідні дані з креслення.",
                impact=f"Позиція формує {_money(line.total)} грн.",
                line_name=line.name,
                section=line.section,
            )


@validator
def check_unit_consistency(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    """A line priced per m² but quantified in pieces is a costly mistake."""
    catalog_units: dict[str, str] = ctx.get("catalog_units", {})
    for line in billable(draft):
        if line.quantity <= 0 or not line.unit:
            continue
        expected = catalog_units.get(normalize_name(line.name))
        if expected and normalize_name(expected) != normalize_name(line.unit):
            yield Finding(
                code="unit_mismatch",
                severity=ERROR,
                title=f"Невідповідність одиниць: «{line.name}»",
                detail=f"У кошторисі «{line.unit}», у базі «{expected}».",
                fix_hint="Приведіть одиницю у відповідність до бази або оберіть іншу позицію.",
                impact="Ціна множиться на кількість в іншій одиниці — сума хибна.",
                line_name=line.name,
                section=line.section,
            )


@validator
def check_duplicates(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    seen: dict[tuple[str, str], list[DraftLine]] = defaultdict(list)
    for line in billable(draft):
        if line.quantity > 0:
            seen[(line.section, normalize_name(line.name))].append(line)
    for (section, _name), lines in seen.items():
        if len(lines) > 1:
            total = sum(l.total for l in lines)
            yield Finding(
                code="duplicate_line",
                severity=WARNING,
                title=f"Дубльована позиція: «{lines[0].name}»",
                detail=f"У секції «{section}» ця позиція трапляється {len(lines)} разів.",
                fix_hint="Об'єднайте позиції або видаліть зайву.",
                impact=f"Можливе подвійне врахування на {_money(total)} грн.",
                line_name=lines[0].name,
                section=section,
            )


def _sold_before(line: DraftLine) -> str | None:
    """Where this line's price was actually charged, if it was.

    Strict on purpose, because the whole point of the not-in-catalogue block is
    that the system must not put a number on a line it cannot account for. Three
    things must all hold, and none of them can be produced by guessing:

    * the line carries a price above zero;
    * it carries a ``historical_estimate`` reference, which only
      :meth:`EstimateBuilder._apply_last_sold` attaches, and only from a row of
      a proposal that reconciled with its own printed subtotals;
    * that reference names the proposal.

    A price typed in by hand does not qualify -- that one keeps its question,
    because a person's number is a decision to record, not a sale to cite.
    """
    if line.unit_price <= 0:
        return None
    for ref in line.source_refs or []:
        if not isinstance(ref, dict):
            continue
        if ref.get("source_type") != "historical_estimate":
            continue
        source = str(ref.get("source_ref") or "").strip()
        if source:
            return f"КП «{source[:60]}»"
    return None


@validator
def check_matching(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    for line in billable(draft):
        if line.quantity <= 0:
            continue
        if line.match_status == "not_in_catalog":
            sold = _sold_before(line)
            if sold is not None:
                # Priced by a proposal the company issued and the customer
                # signed. The article is missing from «2026 База 1», which is a
                # gap in the base rather than an unknown price, so this is worth
                # saying and not worth blocking the export over.
                yield Finding(
                    code="priced_from_history",
                    severity=WARNING,
                    title=f"Ціна з історії КП: «{line.name}»",
                    detail=(
                        f"Позиції немає в «2026 База 1», але вона продавалась: "
                        f"{sold}. Ціну взято звідти, не вигадано."
                    ),
                    fix_hint="Внесіть позицію в прайс, щоб вона була в базі й надалі.",
                    impact=f"У підсумку враховано {_money(line.total)} грн.",
                    line_name=line.name,
                    section=line.section,
                )
                continue
            yield Finding(
                code="not_in_catalog",
                severity=NEEDS_USER_INPUT,
                title=f"Позиції немає в каталозі: «{line.name}»",
                detail="Система не вигадує товар — потрібно обрати позицію вручну.",
                fix_hint="Оберіть аналог із каталогу або додайте позицію в базу.",
                impact="Без ціни позиція не потрапить у підсумок.",
                line_name=line.name,
                section=line.section,
            )
        elif line.match_status == "ambiguous":
            yield Finding(
                code="ambiguous_match",
                severity=NEEDS_USER_INPUT,
                title=f"Кілька відповідників: «{line.name}»",
                detail="Кандидати з каталогу оцінені майже однаково.",
                fix_hint="Оберіть правильну позицію зі списку кандидатів.",
                impact=f"Ціна може змінитись; поточна сума {_money(line.total)} грн.",
                line_name=line.name,
                section=line.section,
            )


@validator
def check_low_confidence(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    for line in billable(draft):
        if line.quantity > 0 and line.confidence == "low" and line.match_status == "matched":
            yield Finding(
                code="low_confidence",
                severity=WARNING,
                title=f"Низька впевненість: «{line.name}»",
                detail="Позицію додано з низькою впевненістю — варто перевірити.",
                fix_hint="Підтвердіть позицію або замініть її.",
                impact=f"Сума позиції {_money(line.total)} грн.",
                line_name=line.name,
                section=line.section,
            )


@validator
def check_unknown_quantity_source(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    for line in billable(draft):
        if line.quantity > 0 and line.qty_source in ("unknown", "template_default"):
            yield Finding(
                code="unexplained_quantity",
                severity=WARNING,
                title=f"Кількість без обґрунтування: «{line.name}»",
                detail="Немає ні правила, ні джерела з документа, ні рішення користувача.",
                fix_hint="Вкажіть джерело кількості або підтвердіть значення.",
                impact=f"Сума позиції {_money(line.total)} грн.",
                line_name=line.name,
                section=line.section,
            )


# --- estimate-level checks ---------------------------------------------------


@validator
def check_arithmetic(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    """Independent re-addition. Cheap, and catches a whole class of silent bugs."""
    materials = round(
        sum(l.total for l in billable(draft) if l.quantity > 0 and l.block in ("materials", "plants")), 2
    )
    works = round(sum(l.total for l in billable(draft) if l.quantity > 0 and l.block == "works"), 2)

    if abs(materials - totals.materials_total) > 0.05:
        yield Finding(
            code="arithmetic_materials",
            severity=ERROR,
            title="Підсумок за матеріали не сходиться",
            detail=f"Сума позицій {_money(materials)} грн, у підсумку {_money(totals.materials_total)} грн.",
            fix_hint="Перерахуйте кошторис; якщо повторюється — це помилка розрахунку.",
            impact="Підсумкова сума кошторису недостовірна.",
        )
    if abs(works - totals.works_total) > 0.05:
        yield Finding(
            code="arithmetic_works",
            severity=ERROR,
            title="Підсумок за роботи не сходиться",
            detail=f"Сума позицій {_money(works)} грн, у підсумку {_money(totals.works_total)} грн.",
            fix_hint="Перерахуйте кошторис.",
            impact="Підсумкова сума кошторису недостовірна.",
        )

    expected_balance = round(
        totals.grand_total - totals.prepayment_materials - totals.prepayment_works, 2
    )
    if abs(expected_balance - totals.balance) > 0.05:
        yield Finding(
            code="arithmetic_balance",
            severity=ERROR,
            title="Залишок не сходиться",
            detail=f"Очікувано {_money(expected_balance)} грн, у підсумку {_money(totals.balance)} грн.",
            fix_hint="Перерахуйте кошторис.",
            impact="Помилка в графіку платежів.",
        )


@validator
def check_empty_estimate(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    if not any(l.quantity > 0 for l in billable(draft)):
        yield Finding(
            code="empty_estimate",
            severity=ERROR,
            title="Кошторис порожній",
            detail="Жодна позиція не має кількості більшої за нуль.",
            fix_hint="Заповніть аналіз об'єкта або внесіть кількості вручну.",
            impact="Немає що експортувати.",
        )


@validator
def check_section_has_works(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    """Materials without labour usually means a missing block, not a free install."""
    by_section: dict[str, dict[str, float]] = defaultdict(lambda: {"materials": 0.0, "works": 0.0})
    for line in billable(draft):
        if line.quantity <= 0:
            continue
        bucket = "works" if line.block == "works" else "materials"
        by_section[line.section][bucket] += line.total

    for section, sums in by_section.items():
        if sums["materials"] > 0 and sums["works"] == 0:
            yield Finding(
                code="section_without_works",
                severity=WARNING,
                title=f"У секції «{section}» немає робіт",
                detail=f"Матеріалів на {_money(sums['materials'])} грн, робіт — 0 грн.",
                fix_hint="Перевірте, чи не пропущено блок «Робота» для цієї секції.",
                impact="Кошторис може бути занижений на вартість монтажу.",
                section=section,
            )


@validator
def check_open_questions(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    for question in ctx.get("open_questions", []):
        yield Finding(
            code="open_question",
            severity=NEEDS_USER_INPUT,
            title=str(question.get("text", "Потрібна відповідь користувача")),
            detail=str(question.get("why", "")),
            fix_hint="Дайте відповідь у розділі «Питання», після чого кошторис перерахується.",
            impact=", ".join(question.get("affects", [])) or "Впливає на кількості у кошторисі.",
        )


@validator
def check_conflicts(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    for conflict in ctx.get("conflicts", []):
        yield Finding(
            code="data_conflict",
            severity=NEEDS_USER_INPUT,
            title=f"Суперечливі дані: {conflict.get('topic', '')}",
            detail="Варіанти: " + "; ".join(str(v) for v in conflict.get("values", [])),
            fix_hint=str(conflict.get("question", "Оберіть правильний варіант.")),
            impact=str(conflict.get("impact", "")),
        )


@validator
def check_plan_vs_estimate(draft: Draft, totals: EstimateTotals, ctx: dict[str, Any]):
    """Compare figures stated on the drawings against what the estimate uses."""
    for expectation in ctx.get("plan_expectations", []):
        name = str(expectation.get("line", ""))
        line = draft.by_name(name)
        expected = expectation.get("value")
        if line is None or expected is None:
            continue
        if abs(line.quantity - float(expected)) > max(0.5, float(expected) * 0.02):
            yield Finding(
                code="plan_mismatch",
                severity=WARNING,
                title=f"Розбіжність із кресленням: «{name}»",
                detail=(
                    f"У кошторисі {line.quantity:g} {line.unit}, "
                    f"у документі {expected:g} ({expectation.get('source', 'креслення')})."
                ),
                fix_hint="Підтвердіть, яке значення правильне.",
                impact=f"Різниця впливає на суму позиції ({_money(line.total)} грн).",
                line_name=name,
                section=line.section,
            )


# --- entry point -------------------------------------------------------------


def validate(
    draft: Draft,
    totals: EstimateTotals,
    context: dict[str, Any] | None = None,
) -> ValidationReport:
    """Run every registered check. Order of findings is by severity."""
    ctx = context or {}
    report = ValidationReport()
    for fn in _VALIDATORS:
        try:
            report.findings.extend(fn(draft, totals, ctx))
        except Exception as exc:  # noqa: BLE001 - a broken check must not hide the rest
            report.findings.append(
                Finding(
                    code="validator_failed",
                    severity=WARNING,
                    title=f"Перевірка «{fn.__name__}» не виконалась",
                    detail=repr(exc),
                    fix_hint="Це технічна помилка перевірки, а не кошторису.",
                )
            )
    return report
