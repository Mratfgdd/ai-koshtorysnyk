"""End-to-end: drawing quantities -> rules -> catalog prices -> validation -> XLSX.

The inputs are exactly what the Budkiv drawings state, and nothing more:

    Схема розпланування   садовий бордюр 147 м.п., газон 259 м², плити 37 шт
    Генплан, ТЕП          площа озеленення 406 м², тверде покриття 45 м²
    Асортим. відомість    15 позицій, 3 з них позначені «існуючі»

Everything else -- anchors, fabric, hooks, soil, sand, deliveries and all labour
-- must come out of the rule engine, and is checked against the proposal the
company actually issued to this customer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import CatalogItem
from app.services.estimate.builder import EstimateBuilder, TemplateLayout, visible_lines
from app.services.export.xlsx import ExportMeta, export_estimate
from app.services.validation.validators import ERROR, NEEDS_USER_INPUT, validate

# --- what the drawings state -------------------------------------------------

PLANTS = [
    {"name": "Кизильник горизонтальний", "quantity": 21},
    {"name": "Сосна чорна 1м", "quantity": 11},
    {"name": 'Сосна гірська "Мопс" d30-40', "quantity": 8},
    {"name": "Верба японська Hakuro nishiki 1,5м", "quantity": 3},
    {"name": "Ірга Ламарка 2м", "quantity": 1},
    {"name": "Гортензія волотиста с5 білий", "quantity": 12},
    {"name": "Спірея японська с3", "quantity": 10},
    {"name": "Ірис сибірський", "quantity": 12},
    {"name": "Костриця сиза", "quantity": 21},
    {"name": "Пеннісетум лисохвостий", "quantity": 24},
    {"name": "Котовник", "quantity": 37},
    {"name": "Гераль Кембриджська", "quantity": 14},
    # Marked "існуючі" on the schedule -- already growing, must not be priced.
    {"name": "Піон деревовидний", "quantity": 2, "is_existing": True},
    {"name": "Плодові дерева", "quantity": 6, "is_existing": True},
    {"name": "Троянди", "quantity": 4, "is_existing": True},
]

QUANTITIES = {
    "planting": {
        "Садовий бордюр Макспол L 1м": 147,   # Схема розпланування: 147 м.п.
        "Агрополотно площа (клумб)": 175,     # площа клумб під агроволокном
    },
    "lawn": {"Площа газону (рулонного)": 259},  # Відомість покриттів: газон 259 м²
    "pathway": {
        "Плита ходова бетонна 120х40х6см": 37,  # Плити ходові бетонні: 37 шт
        "Загальна площа плит": 18,              # 37 × 1,2 × 0,4 ≈ 18 м²
        "Гідрофобізатор для бетонних плит, 5л": 1,
    },
}

OPTIONS = {
    "Газон рулонний універсальний": True,
    "Сітка від кротів": True,
}

SECTIONS = ["pathway", "planting", "lawn"]


@pytest.fixture(scope="module")
def builder() -> EstimateBuilder:
    init_db()
    session = SessionLocal()
    if session.scalar(select(CatalogItem).limit(1)) is None:
        session.close()
        pytest.skip("catalog not imported; run scripts/import_catalog.py")
    try:
        layout = TemplateLayout.load()
    except FileNotFoundError:
        session.close()
        pytest.skip("template not compiled; run scripts/compile_template.py")
    return EstimateBuilder(session, layout)


@pytest.fixture(scope="module")
def built(builder: EstimateBuilder):
    return builder.build(
        sections=SECTIONS, quantities=QUANTITIES, plants=PLANTS, options=OPTIONS
    )


def qty(built, section: str, name: str) -> float:
    """Quantity of a line, scoped to its section.

    Fabric, hooks and geotextile appear in several sections at once, so an
    unscoped lookup would read whichever section happened to be built first.
    """
    line = built.draft.by_name(name, section)
    assert line is not None, f"line «{name}» is missing from the draft"
    assert line.section == section, f"«{name}» resolved to section {line.section}"
    return line.quantity


# --- plants ------------------------------------------------------------------


def test_existing_plants_are_excluded_from_the_estimate(built) -> None:
    names = {l.name for l in built.draft.lines if l.block == "plants"}
    assert "Піон деревовидний" not in names
    assert "Плодові дерева" not in names
    assert "Троянди" not in names
    assert "Сосна чорна 1м" in names


def test_plant_quantities_come_straight_from_the_schedule(built) -> None:
    by_name = {l.name: l for l in built.draft.lines if l.block == "plants"}
    assert by_name["Кизильник горизонтальний"].quantity == 21
    assert by_name["Котовник"].quantity == 37
    assert by_name["Пеннісетум лисохвостий"].quantity == 24


def test_plants_resolve_against_the_assortment(built) -> None:
    matched = [l for l in built.draft.lines if l.block == "plants" and l.match_status == "matched"]
    assert matched, "no plant resolved against the assortment list"


def test_price_free_plants_become_a_question_not_a_guess(built) -> None:
    """The client's assortment sheet carries no plant prices, by design."""
    unpriced = [
        l for l in built.draft.lines
        if l.block == "plants" and l.match_status == "matched" and l.unit_price == 0
    ]
    if not unpriced:
        pytest.skip("this catalog build has plant prices")

    for line in unpriced:
        assert any("ціну постачальника" in r for r in line.reasons)

    report = validate(built.draft, built.totals)
    plant_questions = [f for f in report.findings if f.code == "plant_price_required"]
    assert plant_questions
    assert all(f.severity == NEEDS_USER_INPUT for f in plant_questions)
    assert not report.exportable, "an estimate with unpriced plants must not export clean"


# --- derived quantities, checked against the issued proposal ------------------


def test_planting_derivations_match_the_proposal(built) -> None:
    # Садовий бордюр 147 -> анкера 147 * 4
    assert qty(built, "planting", "Анкера для садового бордюру") == 588
    # Агрополотно = площа клумб * 1.2 ("Враховано 20% агроволокна" on the drawing)
    assert qty(built, "planting", "Агрополотно 50г/м² (чорне)") == 210
    # Гачок = агрополотно * 4
    assert qty(built, "planting", "Гачок Металевий (для Сітки та Агрополотна)") == 840
    # Робота прив'язана до бордюру 1:1
    assert qty(built, "planting", "Монтаж садового бордюру, м.п.") == 147
    # Наявність матеріалів вмикає накладні позиції рівно один раз
    assert qty(built, "planting", "Витратні матеріали") == 1
    assert qty(built, "planting", "Транспортні витрати") == 1


def test_pathway_section_reproduces_the_proposal_exactly(built) -> None:
    """Every quantity in the Доріжка section of the real КП, from two inputs."""
    assert qty(built, "pathway", "Підготовка подушки під плити") == 18
    assert qty(built, "pathway", "Позиціонування та монтаж плит") == 37
    assert qty(built, "pathway", "Обробка плит гідрофобізатором") == 37
    assert qty(built, "pathway", "Розвантаження, перенесення плит") == 1
    assert qty(built, "pathway", "Доставка плит (гідроборт)") == 1


def test_lawn_derivations_match_the_proposal(built) -> None:
    # Рулон = площа + 5 м² запасу
    assert qty(built, "lawn", "Газон рулонний універсальний") == 264
    # Сітка від кротів = площа * 1.2
    assert qty(built, "lawn", "Сітка від кротів") == 311
    assert qty(built, "lawn", "Гачок Металевий (для Сітки та Агрополотна)") == 1244
    # Роботи прив'язані до площі газону
    assert qty(built, "lawn", "Монтаж сітки від кротів") == 259
    assert qty(built, "lawn", "Чистове планування, монтаж рулонів, розвезення рулонів,каткування, полив.") == 259
    # 259 м² потрапляє у діапазон 200–400 -> один великий маніпулятор
    assert qty(built, "lawn", "Доставка газону (великий маніпулятор)") == 1
    assert qty(built, "lawn", "Доставка газону (малий маніпулятор)") == 0


def test_options_gate_the_lawn_type(builder: EstimateBuilder) -> None:
    """Turning the roll-lawn toggle off must zero it and its dependants."""
    off = builder.build(
        sections=["lawn"],
        quantities={"lawn": {"Площа газону (рулонного)": 259}},
        options={"Газон рулонний універсальний": False, "Сітка від кротів": False},
    )
    assert off.draft.by_name("Газон рулонний універсальний").quantity == 0
    assert off.draft.by_name("Сітка від кротів").quantity == 0
    assert off.draft.by_name("Гачок Металевий (для Сітки та Агрополотна)").quantity == 0
    assert off.draft.by_name("Монтаж сітки від кротів").quantity == 0


def test_driver_rows_never_reach_the_invoice(built) -> None:
    """Input quantities feed the rules but are not billable lines."""
    driver = built.draft.by_name("Площа газону (рулонного)")
    assert driver is not None and driver.block == "driver"
    assert driver.quantity == 259
    assert driver not in visible_lines(built.draft)
    assert all(l.block != "driver" for l in visible_lines(built.draft))


# --- integrity ---------------------------------------------------------------


def test_every_priced_line_can_explain_itself(built) -> None:
    for line in visible_lines(built.draft):
        explained = (
            line.qty_trace
            or line.reasons
            or line.qty_source in ("document", "user_input", "rule")
        )
        assert explained, f"{line.name} has a quantity with no justification"


def test_totals_are_internally_consistent(built) -> None:
    totals = built.totals
    lines = visible_lines(built.draft)
    materials = sum(l.total for l in lines if l.block in ("materials", "plants"))
    works = sum(l.total for l in lines if l.block == "works")

    assert totals.materials_total == pytest.approx(materials, abs=0.05)
    assert totals.works_total == pytest.approx(works, abs=0.05)
    assert totals.grand_total == pytest.approx(materials + works, abs=0.05)
    assert totals.balance == pytest.approx(
        totals.grand_total - totals.prepayment_materials - totals.prepayment_works, abs=0.05
    )


def test_validation_is_actionable(built) -> None:
    report = validate(built.draft, built.totals)
    for finding in report.findings:
        assert finding.title
        assert finding.fix_hint, f"{finding.code} has no fix hint"
        assert finding.impact or finding.severity != ERROR


def test_unmatched_items_are_reported_not_invented(builder: EstimateBuilder) -> None:
    result = builder.build(
        sections=["planting"],
        plants=[{"name": "Дерево вигадане звичайне", "quantity": 5}],
    )
    line = result.draft.by_name("Дерево вигадане звичайне")
    assert line is not None
    assert line.match_status == "not_in_catalog"
    assert line.unit_price == 0
    assert any(u["name"] == "Дерево вигадане звичайне" for u in result.unmatched)


def test_a_user_override_survives_recalculation(builder: EstimateBuilder, built) -> None:
    """The Budkiv proposal itself overrides two computed soil volumes by hand."""
    result = builder.build(
        sections=["lawn"],
        quantities={"lawn": {"Площа газону (рулонного)": 259}},
        options=OPTIONS,
    )
    soil = result.draft.by_name("Грунт родючий чорний")
    assert soil is not None
    assert soil.quantity == 21  # round(259 * 0.08)

    soil.quantity = 20  # what the estimator actually invoiced
    soil.locked = True
    again = builder.recalculate(result.draft)

    assert again.draft.by_name("Грунт родючий чорний").quantity == 20
    # The dependent haulage line follows the override, not the original rule.
    assert again.draft.by_name("Перевезення грунту/піску під чистий рівень").quantity == 25


def test_export_produces_a_readable_workbook(built, tmp_path) -> None:
    import openpyxl

    report = validate(built.draft, built.totals)
    out = export_estimate(
        tmp_path / "kp.xlsx",
        built.draft,
        built.totals,
        meta=ExportMeta(client_name="Галина", address="с. Будьків", project_name="Будьків"),
        section_titles={"pathway": "Доріжка", "planting": "Озеленення", "lawn": "Газон"},
        section_order=SECTIONS,
        report=report,
    )
    assert out.exists() and out.stat().st_size > 5_000

    wb = openpyxl.load_workbook(out)
    assert {"Кошторис", "Обґрунтування", "Підсумки"} <= set(wb.sheetnames)

    ws = wb["Кошторис"]
    assert ws.sheet_properties.outlinePr.summaryBelow is True

    # The proposal must scroll and print as one continuous document: a frozen
    # letterhead hung over the tables on screen and reprinted on every page.
    assert ws.freeze_panes is None, "proposal sheet must not freeze the letterhead"
    assert not ws.print_title_rows, "letterhead must not repeat on every printed page"
    assert ws.sheet_view.showGridLines is True

    text = " ".join(
        str(c.value) for row in ws.iter_rows(max_row=300) for c in row if c.value is not None
    )
    assert "Комерційна пропозиція" in text
    assert "Замовник:" in text and "Адреса об'єкту:" in text
    assert "Пропозиція дійсна протягом:" in text
    assert "Разом за матеріали:" in text
    assert "Загальний рахунок" in text
    assert "Аванс (на роботи)" in text
    assert "Рахунок загальний:" in text
    # Driver rows must never appear on the customer's copy.
    assert "Площа газону (рулонного)" not in text
    # Sums are live formulas, not baked numbers.
    assert any(
        isinstance(c.value, str) and c.value.startswith("=C")
        for row in ws.iter_rows(max_row=300)
        for c in row
    )
    # The audit sheet carries the rule behind each quantity.
    audit = " ".join(
        str(c.value) for row in wb["Обґрунтування"].iter_rows(max_row=300)
        for c in row if c.value is not None
    )
    assert "qty(" in audit


def test_export_sums_survive_without_recalculation(built, tmp_path) -> None:
    """Every sum must be readable without Excel recalculating the workbook.

    openpyxl writes `<f>SUM(...)</f><v/>` — a formula with an empty cached
    result. Excel recalculates on load and looked fine; LibreOffice, Google
    Sheets, any preview, and `data_only=True` all read that empty cache and
    showed a proposal whose every total was blank.
    """
    import openpyxl

    from app.services.export.xlsx import LAST_COL

    out = export_estimate(
        tmp_path / "cached.xlsx",
        built.draft,
        built.totals,
        meta=ExportMeta(client_name="Галина", address="с. Будьків"),
        section_titles={"pathway": "Доріжка", "planting": "Озеленення", "lawn": "Газон"},
        section_order=SECTIONS,
    )

    live = openpyxl.load_workbook(out)["Кошторис"]
    cached = openpyxl.load_workbook(out, data_only=True)["Кошторис"]

    blank: list[str] = []
    checked = 0
    for row in range(1, live.max_row + 1):
        formula = live.cell(row, LAST_COL).value
        if not (isinstance(formula, str) and formula.startswith("=")):
            continue
        checked += 1
        value = cached.cell(row, LAST_COL).value
        if value is None or value == "":
            label = live.cell(row, 1).value or live.cell(row, 2).value
            blank.append(f"F{row} ({label})")

    assert checked > 5, "no formula cells found — the export stopped writing sums"
    assert not blank, f"sums blank without recalculation: {blank}"

    # Line sums and their subtotals must agree.
    for row in range(1, live.max_row + 1):
        qty, price = cached.cell(row, 3).value, cached.cell(row, 5).value
        total = cached.cell(row, LAST_COL).value
        if isinstance(live.cell(row, 1).value, int) and None not in (qty, price, total):
            assert abs(total - qty * price) < 0.011, f"row {row}: {total} != {qty} × {price}"


def test_export_matches_the_reference_proposal_styling(built, tmp_path) -> None:
    """Palette, columns and branding are taken from the issued Budkiv proposal."""
    import openpyxl

    from app.services.export.xlsx import BAND_BLUE, BAND_GREEN, GREEN, LOGO_PATH

    out = export_estimate(
        tmp_path / "style.xlsx",
        built.draft,
        built.totals,
        meta=ExportMeta(client_name="Галина", address="с. Будьків"),
        section_titles={"pathway": "Доріжка", "planting": "Озеленення", "lawn": "Газон"},
        section_order=SECTIONS,
    )
    ws = openpyxl.load_workbook(out)["Кошторис"]

    fills = {
        c.fill.fgColor.rgb[-6:]
        for row in ws.iter_rows(max_row=200)
        for c in row
        if c.fill and c.fill.fgColor and isinstance(c.fill.fgColor.rgb, str)
    }
    assert GREEN in fills, "green banner / table header missing"
    assert BAND_BLUE in fills, "pale blue section marker missing"
    assert BAND_GREEN in fills, "pale green section title band missing"

    # Column headers, in the company's own order.
    header_rows = [
        [ws.cell(r, c).value for c in range(1, 7)]
        for r in range(1, 200)
        if ws.cell(r, 1).value == "#"
    ]
    assert header_rows, "no table header row found"
    for header in header_rows:
        assert header[0] == "#"
        assert header[2:] == ["К-сть", "Од. вим.", "Ціна", "Сума"]
        assert header[1] in ("Матеріали", "Рослини", "Робота",
                             "Матеріали за видами послуг", "Робота за видами послуг")

    if LOGO_PATH.exists():
        assert ws._images, "GARDENER logo not embedded"
        anchor = ws._images[0].anchor
        # Anchored top-right, in columns E-F, above the title row.
        assert anchor._from.col >= 4
        assert anchor._from.row <= 4
        # The anchor extent is what Excel actually draws, not the source size.
        drawn = anchor.ext.cx / anchor.ext.cy
        assert abs(drawn - 646 / 601) < 0.02, f"logo aspect distorted: {drawn:.3f}"
        assert 80 <= anchor.ext.cy / 9525 <= 140, "logo height out of range"

    # Zero shows as an em dash, not a bare 0 or a blank.
    formats = {
        c.number_format
        for row in ws.iter_rows(max_row=200)
        for c in row
        if c.number_format and c.number_format != "General"
    }
    assert any("—" in f for f in formats), "zero values must render as «—»"


# --- «Не враховувати в КП» ----------------------------------------------------


def test_excluded_facts_are_kept_out_of_the_estimate(builder) -> None:
    """A fact the estimator marks «Не враховувати в КП» must not drive anything.

    The value stays on the analysis — the evidence that it was found is worth
    keeping — but it must not reach the quantities the rules run on, exactly
    like «Невідомо» and «Потрібне рішення» already did.
    """
    from app.models import ObjectAnalysis
    from app.services.ai.schemas import FACT_STATUSES_OUT_OF_ESTIMATE
    from app.services.pipeline import _from_analysis

    assert "excluded" in FACT_STATUSES_OUT_OF_ESTIMATE

    layout = builder.layout
    session = builder.session

    def inputs_for(status: str) -> dict:
        analysis = ObjectAnalysis(
            project_id=0,
            version=1,
            status="draft",
            object_type="ділянка",
            summary="",
            facts=[
                {
                    "key": "lawn_area",
                    "label": "Площа газону (рулонного)",
                    "value": "259",
                    "unit": "м2",
                    "status": status,
                    "section": "lawn",
                }
            ],
            systems=[{"key": "lawn", "label": "Газон", "evidence": "", "confidence": "high"}],
            components=[],
            plants=[],
        )
        return _from_analysis(analysis, layout, session)

    counted = inputs_for("confirmed")
    assert counted["quantities"].get("lawn", {}), "a confirmed fact must reach the rules"

    for status in ("excluded", "unknown", "needs_user_input"):
        held_back = inputs_for(status)
        assert not held_back["quantities"].get("lawn"), (
            f"a fact with status «{status}» must not drive a quantity"
        )


# --- PDF ----------------------------------------------------------------------


def test_pdf_export_carries_every_figure(built, tmp_path) -> None:
    """The PDF must read as a finished proposal, not an empty shell.

    A PDF has no formulas, so every number is baked in here; the check is that
    the totals in the document equal the ones the engine computed.
    """
    import pymupdf

    from app.services.export.pdf import export_estimate_pdf, money

    out = export_estimate_pdf(
        tmp_path / "kp.pdf",
        built.draft,
        built.totals,
        meta=ExportMeta(client_name="Галина", address="с. Будьків", date="02.09.2026"),
        section_titles={"pathway": "Доріжка", "planting": "Озеленення", "lawn": "Газон"},
        section_order=SECTIONS,
    )
    assert out.exists() and out.stat().st_size > 10_000
    # Story embeds whole font files; without subsetting this document is 1.3 MB,
    # which is too heavy to email. Guard the subsetting step.
    assert out.stat().st_size < 500_000, (
        f"PDF is {out.stat().st_size:,} bytes — font subsetting is not running"
    )

    doc = pymupdf.open(out)
    assert doc.page_count >= 1
    text = "\n".join(page.get_text() for page in doc)

    for probe in (
        "Комерційна пропозиція",
        "Замовник:",
        "Галина",
        "с. Будьків",
        "Рахунок загальний:",
        "Загальний рахунок",
        "Разом за матеріали:",
        "Аванс (на роботи)",
    ):
        assert probe in text, f"«{probe}» missing from the PDF"

    # Cyrillic must render as glyphs, not as boxes or dropped characters.
    assert "Кошторис" in text or "Комерційна" in text

    # The headline figure has to match the engine, to the kopeck.
    materials = sum(
        l.quantity * l.unit_price
        for l in built.draft.lines
        if l.quantity > 0 and l.block in ("materials", "plants")
    )
    works = sum(
        l.quantity * l.unit_price
        for l in built.draft.lines
        if l.quantity > 0 and l.block == "works"
    )
    grand = materials + works + (built.totals.surcharge or 0.0)
    assert money(grand) in text, f"grand total {money(grand)} not printed in the PDF"

    # Driver rows are internal inputs and must never reach the customer's copy.
    assert "Площа газону (рулонного)" not in text

    if (LOGO_PATH := __import__("app.services.export.xlsx", fromlist=["LOGO_PATH"]).LOGO_PATH).exists():
        assert doc[0].get_images(), "logo not embedded in the PDF"
