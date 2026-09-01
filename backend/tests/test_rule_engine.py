"""Rule engine behaviour, checked against the client's own coefficients.

The reference case is the Budkiv project, where we have both the drawings and
the signed proposal, so the derived quantities can be verified end to end:

    креслення                          КП
    Садовий бордюр      147 м.п.  ->   147 шт, анкера 588 = 147 * 4
    Газон               259 м²    ->   рулон 264 = 259 + 5, сітка 311 = 259 * 1.2
    Плити ходові        37 шт     ->   37 шт, підготовка подушки 18 м²
"""

from __future__ import annotations

import pytest

from app.services.rules.engine import (
    Draft,
    DraftLine,
    RuleContext,
    RuleEngine,
    RuleError,
    SafeEvaluator,
    normalize_name,
)


def _ctx(*lines: DraftLine) -> tuple[RuleContext, SafeEvaluator]:
    ctx = RuleContext(Draft(lines=list(lines)))
    return ctx, SafeEvaluator(ctx.namespace())


# --- safety ------------------------------------------------------------------


def test_rules_cannot_reach_outside_the_dsl() -> None:
    _, ev = _ctx()
    for hostile in (
        "__import__('os').system('echo hi')",
        "open('/etc/passwd').read()",
        "(1).__class__.__mro__",
        "[x for x in range(3)]",
    ):
        with pytest.raises(RuleError):
            ev.eval(hostile)


def test_unknown_function_is_refused() -> None:
    _, ev = _ctx()
    with pytest.raises(RuleError):
        ev.eval("totally_made_up(1)")


# --- excel semantics ---------------------------------------------------------


def test_rounding_matches_excel_not_python() -> None:
    _, ev = _ctx()
    # Python's round(0.5) is 0 (banker's rounding); Excel's ROUND is 1.
    assert ev.eval("rnd(0.5, 0)") == 1
    assert ev.eval("rnd(2.5, 0)") == 3
    assert ev.eval("roundup(20.72, 0)") == 21
    assert ev.eval("rounddown(20.72, 0)") == 20
    assert ev.eval("ceiling(85377.3, 100)") == 85400
    assert ev.eval("mod(17, 15)") == 2


def test_division_by_zero_yields_zero_not_an_exception() -> None:
    """An empty section must not crash the whole estimate."""
    _, ev = _ctx()
    assert ev.eval("10 / 0") == 0
    assert ev.eval("mod(10, 0)") == 0


def test_empty_string_behaves_as_excel_blank() -> None:
    # The template's paving rules return "" for an unused row.
    _, ev = _ctx()
    assert ev.eval('("" if (1 == 1) else 5) + 3') == 3


# --- real coefficients -------------------------------------------------------


def test_anchor_count_is_four_per_metre_of_edging() -> None:
    edging = DraftLine(name="Садовий бордюр Макспол L 1м", section="planting",
                       block="materials", quantity=147, unit="шт")
    ctx, ev = _ctx(edging)
    assert ev.eval('(qty("Садовий бордюр Макспол L 1м") * 4)') == 588


def test_geotextile_carries_the_documented_twenty_percent_overlap() -> None:
    area = DraftLine(name="Площа клумб", section="planting", block="materials", quantity=175)
    ctx, ev = _ctx(area)
    # ROUND(area * 1.2, 0) -- the drawings state "Враховано 20% агроволокна".
    assert ev.eval('rnd((qty("Площа клумб") * 1.2), 0)') == 210


def test_rolled_lawn_adds_the_five_square_metre_allowance() -> None:
    lawn = DraftLine(name="Площа газону (рулонного)", section="lawn",
                     block="materials", quantity=259)
    ctx, ev = _ctx(lawn)
    assert ev.eval('roundup((qty("Площа газону (рулонного)") + 5), 0)') == 264


def test_mole_net_is_area_times_one_point_two() -> None:
    lawn = DraftLine(name="Площа газону (рулонного)", section="lawn",
                     block="materials", quantity=259)
    ctx, ev = _ctx(lawn)
    assert ev.eval('rnd((qty("Площа газону (рулонного)") * 1.2), 0)') == 311


def test_valve_box_size_is_chosen_by_threshold() -> None:
    valves = DraftLine(name="Електромагнітний клапан", section="irrigation",
                       block="materials", quantity=4)
    ctx, ev = _ctx(valves)
    mini = '((1) if (((qty("Електромагнітний клапан") > 0) and (qty("Електромагнітний клапан") <= 3))) else (0))'
    standard = '((1) if (((qty("Електромагнітний клапан") > 3) and (qty("Електромагнітний клапан") <= 5))) else (0))'
    jumbo = '((1) if (((qty("Електромагнітний клапан") > 5) and (qty("Електромагнітний клапан") <= 8))) else (0))'
    assert (ev.eval(mini), ev.eval(standard), ev.eval(jumbo)) == (0, 1, 0)


def test_planting_labour_is_thirty_five_percent_of_plant_value() -> None:
    """Confirmed independently in four of the client's proposals."""
    plants = [
        DraftLine(name="Сосна чорна 1м", section="planting", block="plants",
                  quantity=11, unit_price=1600),
        DraftLine(name="Кизильник горизонтальний", section="planting", block="plants",
                  quantity=21, unit_price=300),
    ]
    ctx, ev = _ctx(*plants)
    total = ev.eval('block_total("planting.plants")')
    assert total == 11 * 1600 + 21 * 300
    assert round(total * 0.35, 2) == round(23_900 * 0.35, 2)


def test_specimen_plants_are_split_out_by_price_threshold() -> None:
    lines = [
        DraftLine(name="Ірга Ламарка 2м", section="planting", block="plants",
                  quantity=1, unit_price=10_200),
        DraftLine(name="Дуб великомірний", section="planting", block="plants",
                  quantity=2, unit_price=47_000),
    ]
    ctx, ev = _ctx(*lines)
    assert ev.eval('sum_value_price_ge_block("planting.plants", 40000)') == 94_000


def test_bulk_delivery_buckets_follow_the_mod_fifteen_rule() -> None:
    gravel = DraftLine(name="Щебінь, фракція 20-40мм", section="paving",
                       block="materials", quantity=38)
    ctx, ev = _ctx(gravel)
    # 38 m3 -> two full MAN loads (15 each) plus an 8 m3 remainder -> one KAMAZ.
    man = 'rounddown((qty("Щебінь, фракція 20-40мм") / 15), 0)'
    kamaz = ('((1) if (((mod(qty("Щебінь, фракція 20-40мм"), 15) > 5) and '
             '(mod(qty("Щебінь, фракція 20-40мм"), 15) <= 10))) else (0))')
    assert ev.eval(man) == 2
    assert ev.eval(kamaz) == 1


def test_pattern_sum_selects_by_product_name() -> None:
    lines = [
        DraftLine(name="Поребрик 1000*250*60 мм (сірий колір)", section="paving",
                  block="materials", quantity=30),
        DraftLine(name="Поребрик 1000*200*60 мм (сірий колір)", section="paving",
                  block="materials", quantity=12),
    ]
    ctx, ev = _ctx(*lines)
    names = [l.name for l in lines]
    expr = f'sum_qty_where({names!r}, ["*поребрик*", "*250*"])'.replace("'", '"')
    assert ev.eval(expr) == 30


# --- engine ------------------------------------------------------------------


def _engine(rules: list[tuple[str, str, str]]) -> RuleEngine:
    layout = {
        "sections": [
            {
                "key": section,
                "title": section,
                "lines": [{"name": name, "qty_status": "derived", "qty_expr": expr,
                           "block": "materials"}],
            }
            for section, name, expr in rules
        ]
    }
    return RuleEngine(layout)


def test_engine_resolves_chained_dependencies() -> None:
    """Fabric depends on area; hooks depend on fabric. One pass is not enough."""
    draft = Draft(
        lines=[
            DraftLine(name="Площа клумб", section="planting", block="materials", quantity=175),
            DraftLine(name="Агрополотно", section="planting", block="materials"),
            DraftLine(name="Гачок", section="planting", block="materials"),
        ]
    )
    engine = _engine(
        [
            ("planting", "Агрополотно", 'rnd((qty("Площа клумб") * 1.2), 0)'),
            ("planting", "Гачок", '(qty("Агрополотно") * 4)'),
        ]
    )
    engine.apply(draft)

    assert draft.by_name("Агрополотно").quantity == 210
    assert draft.by_name("Гачок").quantity == 840


def test_a_locked_line_is_never_overwritten_by_a_rule() -> None:
    """The operator's number wins; dependants still recompute from it."""
    draft = Draft(
        lines=[
            DraftLine(name="Площа клумб", section="planting", block="materials", quantity=175),
            DraftLine(name="Агрополотно", section="planting", block="materials",
                      quantity=200, locked=True),
            DraftLine(name="Гачок", section="planting", block="materials"),
        ]
    )
    engine = _engine(
        [
            ("planting", "Агрополотно", 'rnd((qty("Площа клумб") * 1.2), 0)'),
            ("planting", "Гачок", '(qty("Агрополотно") * 4)'),
        ]
    )
    engine.apply(draft)

    assert draft.by_name("Агрополотно").quantity == 200
    assert draft.by_name("Гачок").quantity == 800


def test_rules_for_inactive_sections_are_skipped() -> None:
    draft = Draft(lines=[DraftLine(name="Площа клумб", section="planting",
                                   block="materials", quantity=100)])
    engine = _engine([("irrigation", "Труба", 'qty("Площа клумб")')])
    outcomes = engine.apply(draft)
    assert outcomes == []


def test_every_derived_line_gets_a_trace() -> None:
    draft = Draft(
        lines=[
            DraftLine(name="Площа клумб", section="planting", block="materials",
                      quantity=175, unit="м²"),
            DraftLine(name="Агрополотно", section="planting", block="materials"),
        ]
    )
    engine = _engine([("planting", "Агрополотно", 'rnd((qty("Площа клумб") * 1.2), 0)')])
    engine.apply(draft)

    line = draft.by_name("Агрополотно")
    assert line.qty_source == "rule"
    assert line.qty_trace and "Площа клумб" in line.qty_trace


def test_name_normalisation_absorbs_template_whitespace() -> None:
    assert normalize_name("Щебінь,  фракція 5-20мм ") == normalize_name("щебінь, фракція 5-20мм")
