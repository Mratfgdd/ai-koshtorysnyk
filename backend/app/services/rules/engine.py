"""Deterministic rule engine.

Every number in an estimate is produced here, never by the model. The engine
evaluates the DSL emitted by
:mod:`app.services.ingest.formula_parser` against a draft estimate, resolves
dependencies between lines, and records a trace so each quantity can explain
itself.

Safety: expressions are parsed with :mod:`ast` and evaluated over an explicit
whitelist of node types and functions. No attribute access, no imports, no
name lookups outside the registered helpers -- a rule pack is data, not code.
"""

from __future__ import annotations

import ast
import fnmatch
import math
import operator
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# --- Safe expression evaluation ---------------------------------------------

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.Mod,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)

_BINOPS: dict[type[ast.AST], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: lambda a, b: (a / b) if b else 0.0,
    ast.Pow: operator.pow,
    ast.Mod: lambda a, b: (a % b) if b else 0.0,
}

_CMPOPS: dict[type[ast.AST], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


class RuleError(Exception):
    """A rule could not be evaluated. Surfaced as an ERROR issue, never hidden."""


class SafeEvaluator:
    """Evaluate a DSL expression against a function namespace."""

    def __init__(self, functions: dict[str, Callable[..., Any]]):
        self.functions = functions

    def eval(self, expr: str) -> Any:
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as exc:  # pragma: no cover - compiler output is valid
            raise RuleError(f"cannot parse rule {expr!r}: {exc}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise RuleError(f"disallowed syntax {type(node).__name__} in rule")
        return self._eval(tree.body)

    def _eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._eval(e) for e in node.elts]
        if isinstance(node, ast.Name):
            if node.id in ("True", "False", "None"):
                return {"True": True, "False": False, "None": None}[node.id]
            raise RuleError(f"unknown name {node.id!r} in rule")
        if isinstance(node, ast.UnaryOp):
            v = self._eval(node.operand)
            if isinstance(node.op, ast.USub):
                return -_num(v)
            if isinstance(node.op, ast.UAdd):
                return _num(v)
            return not _truthy(v)
        if isinstance(node, ast.BinOp):
            fn = _BINOPS.get(type(node.op))
            if fn is None:
                raise RuleError(f"unsupported operator {type(node.op).__name__}")
            return fn(_num(self._eval(node.left)), _num(self._eval(node.right)))
        if isinstance(node, ast.BoolOp):
            vals = [self._eval(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return all(_truthy(v) for v in vals)
            return any(_truthy(v) for v in vals)
        if isinstance(node, ast.Compare):
            left = self._eval(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = self._eval(comp)
                fn = _CMPOPS.get(type(op))
                if fn is None:
                    raise RuleError(f"unsupported comparison {type(op).__name__}")
                if isinstance(left, str) or isinstance(right, str):
                    ok = fn(left, right)
                else:
                    ok = fn(_num(left), _num(right))
                if not ok:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self._eval(node.body) if _truthy(self._eval(node.test)) else self._eval(node.orelse)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise RuleError("only direct function calls are allowed in rules")
            fn = self.functions.get(node.func.id)
            if fn is None:
                raise RuleError(f"unknown rule function {node.func.id!r}")
            if node.keywords:
                raise RuleError("keyword arguments are not allowed in rules")
            return fn(*[self._eval(a) for a in node.args])
        raise RuleError(f"unsupported expression node {type(node).__name__}")


def _num(v: Any) -> float:
    """Coerce a value to a number.

    Excel's empty string is the template's "not applicable"; it becomes 0 so a
    guarded rule contributes nothing rather than blowing up.
    """
    if v is None or v is False:
        return 0.0
    if v is True:
        return 1.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(" ", "").replace(" ", "").replace(",", ".")
        if not s:
            return 0.0
        try:
            return float(s)
        except ValueError:
            return 0.0
    return 0.0


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "так", "yes", "1")
    return _num(v) != 0.0


# --- Draft model the engine operates on --------------------------------------


@dataclass
class DraftLine:
    """One estimate line as the engine sees it."""

    name: str
    section: str
    block: str  # materials | works | plants
    unit: str = ""
    quantity: float = 0.0
    unit_price: float = 0.0
    unit_cost: float = 0.0
    attributes: dict[str, float] = field(default_factory=dict)
    option: bool | None = None  # H-column toggle, None when the line has none

    # provenance
    qty_source: str = "unknown"  # rule | user_input | document | historical | template_default
    qty_expr: str | None = None
    qty_trace: str | None = None
    catalog_id: int | None = None
    match_status: str = "unmatched"  # matched | ambiguous | not_in_catalog | unmatched
    confidence: str = "medium"  # high | medium | low
    reasons: list[str] = field(default_factory=list)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    locked: bool = False  # user overrode the quantity; rules must not clobber it

    @property
    def total(self) -> float:
        return round(self.quantity * self.unit_price, 2)

    @property
    def cost_total(self) -> float:
        return round(self.quantity * self.unit_cost, 2)

    @property
    def block_id(self) -> str:
        return f"{self.section}.{self.block}"


@dataclass
class Draft:
    """A whole estimate under construction."""

    lines: list[DraftLine] = field(default_factory=list)
    options: dict[str, bool] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    def by_name(self, name: str, section: str | None = None) -> DraftLine | None:
        """Find a line by name, preferring one inside ``section``.

        Several articles appear in more than one section -- geotextile, fabric
        and hooks are used by paving, planting, lawn and geogrid alike. In the
        workbook each formula points at a row inside its own section, so name
        lookup has to be section-scoped or a lawn rule would read the paving
        section's quantity.
        """
        key = normalize_name(name)
        if section is not None:
            for line in self.lines:
                if line.section == section and normalize_name(line.name) == key:
                    return line
        for line in self.lines:
            if normalize_name(line.name) == key:
                return line
        return None

    def in_block(self, block_id: str) -> list[DraftLine]:
        return [l for l in self.lines if l.block_id == block_id]


def normalize_name(name: str) -> str:
    """Collapse the whitespace and case differences that break exact lookup.

    The client's workbook matches items by exact name through VLOOKUP, so
    trailing spaces and double spaces in the template are a real source of
    mismatches; we normalise both sides rather than editing their data.
    """
    s = (name or "").replace(" ", " ").replace("ʼ", "'").replace("’", "'")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


# --- Rule functions ----------------------------------------------------------


class RuleContext:
    """Binds the DSL helper names to a concrete draft."""

    def __init__(self, draft: Draft):
        self.draft = draft
        self.reads: set[str] = set()  # names read during the last evaluation
        self.section: str | None = None  # section of the rule being evaluated

    # -- line lookups ---------------------------------------------------------
    def _line(self, name: str) -> DraftLine | None:
        self.reads.add(normalize_name(name))
        return self.draft.by_name(name, self.section)

    def qty(self, name: str) -> float:
        line = self._line(name)
        return line.quantity if line else 0.0

    def value(self, name: str) -> float:
        line = self._line(name)
        return line.total if line else 0.0

    def present(self, name: str) -> int:
        line = self._line(name)
        return 1 if line and line.quantity > 0 else 0

    def option(self, name: str) -> bool:
        line = self._line(name)
        if line is not None and line.option is not None:
            return line.option
        return bool(self.draft.options.get(normalize_name(name), False))

    def attr(self, name: str, key: str) -> float:
        line = self._line(name)
        return float(line.attributes.get(key, 0.0)) if line else 0.0

    # -- aggregates -----------------------------------------------------------
    def sum_qty(self, names: Iterable[str]) -> float:
        return sum(self.qty(n) for n in names)

    def sum_value(self, names: Iterable[str]) -> float:
        return sum(self.value(n) for n in names)

    def sum_qty_block(self, block_id: str) -> float:
        self.reads.add(f"@{block_id}")
        return sum(l.quantity for l in self.draft.in_block(block_id))

    def sum_value_block(self, block_id: str) -> float:
        self.reads.add(f"@{block_id}")
        return sum(l.total for l in self.draft.in_block(block_id))

    def block_total(self, block_id: str) -> float:
        self.reads.add(f"@{block_id}")
        if block_id.endswith(".total"):
            section = block_id[: -len(".total")]
            return sum(l.total for l in self.draft.lines if l.section == section)
        return sum(l.total for l in self.draft.in_block(block_id))

    def sum_qty_where(self, names: Iterable[str], patterns: Iterable[str]) -> float:
        pats = [normalize_name(p) for p in patterns]
        total = 0.0
        for n in names:
            key = normalize_name(n)
            if all(fnmatch.fnmatchcase(key, p) for p in pats):
                total += self.qty(n)
        return total

    def sum_value_price_ge(self, names: Iterable[str], threshold: float) -> float:
        total = 0.0
        for n in names:
            line = self._line(n)
            if line and line.unit_price >= _num(threshold):
                total += line.total
        return total

    def sum_value_price_ge_block(self, block_id: str, threshold: float) -> float:
        self.reads.add(f"@{block_id}")
        return sum(
            l.total for l in self.draft.in_block(block_id) if l.unit_price >= _num(threshold)
        )

    # -- math -----------------------------------------------------------------
    @staticmethod
    def rnd(x: Any, digits: Any = 0) -> float:
        d = int(_num(digits))
        # Excel rounds halves away from zero; Python rounds to even.
        f = 10 ** d
        v = _num(x) * f
        return math.floor(v + 0.5) / f if v >= 0 else math.ceil(v - 0.5) / f

    @staticmethod
    def roundup(x: Any, digits: Any = 0) -> float:
        f = 10 ** int(_num(digits))
        v = _num(x) * f
        return (math.ceil(v) if v >= 0 else math.floor(v)) / f

    @staticmethod
    def rounddown(x: Any, digits: Any = 0) -> float:
        f = 10 ** int(_num(digits))
        v = _num(x) * f
        return (math.floor(v) if v >= 0 else math.ceil(v)) / f

    @staticmethod
    def ceiling(x: Any, significance: Any = 1) -> float:
        s = _num(significance)
        if s == 0:
            return 0.0
        return math.ceil(_num(x) / s) * s

    @staticmethod
    def mod(a: Any, b: Any) -> float:
        bb = _num(b)
        return (_num(a) % bb) if bb else 0.0

    @staticmethod
    def _min(*args: Any) -> float:
        return min(_num(a) for a in args) if args else 0.0

    @staticmethod
    def _max(*args: Any) -> float:
        return max(_num(a) for a in args) if args else 0.0

    def namespace(self) -> dict[str, Callable[..., Any]]:
        return {
            "qty": self.qty,
            "value": self.value,
            "present": self.present,
            "option": self.option,
            "attr": self.attr,
            "sum_qty": self.sum_qty,
            "sum_value": self.sum_value,
            "sum_qty_block": self.sum_qty_block,
            "sum_value_block": self.sum_value_block,
            "block_total": self.block_total,
            "sum_qty_where": self.sum_qty_where,
            "sum_value_price_ge": self.sum_value_price_ge,
            "sum_value_price_ge_block": self.sum_value_price_ge_block,
            "rnd": self.rnd,
            "roundup": self.roundup,
            "rounddown": self.rounddown,
            "ceiling": self.ceiling,
            "mod": self.mod,
            "min": self._min,
            "max": self._max,
            "abs": lambda x: abs(_num(x)),
        }


# --- Engine ------------------------------------------------------------------

MAX_PASSES = 12


@dataclass
class RuleOutcome:
    name: str
    section: str
    expression: str
    value: float
    depends_on: list[str]
    error: str | None = None


class RuleEngine:
    """Applies the compiled quantity rules to a draft until it settles.

    The template's formulas form a dependency graph (works depend on materials,
    deliveries depend on volumes, planting labour depends on plant totals). We
    iterate to a fixed point rather than topologically sorting: the graph is
    shallow, cycles would be a data error, and a fixed point makes the engine
    robust to the client reordering rows in their workbook.
    """

    def __init__(self, layout: dict[str, Any]):
        self.layout = layout
        self.rules: list[tuple[str, str, str]] = []  # (section, name, expr)
        for sec in layout.get("sections", []):
            for line in sec.get("lines", []):
                if line.get("qty_status") == "derived" and line.get("qty_expr"):
                    self.rules.append((sec["key"], line["name"], line["qty_expr"]))

    def apply(self, draft: Draft) -> list[RuleOutcome]:
        ctx = RuleContext(draft)
        ev = SafeEvaluator(ctx.namespace())
        outcomes: dict[str, RuleOutcome] = {}

        for _pass in range(MAX_PASSES):
            changed = False
            for section, name, expr in self.rules:
                line = draft.by_name(name, section)
                if line is None or line.section != section:
                    continue  # this section is not part of the current estimate
                if line.locked:
                    continue  # a human decided this number; rules defer to them

                ctx.reads = set()
                ctx.section = section
                try:
                    raw = ev.eval(expr)
                    value = round(_num(raw), 4)
                    err = None
                except RuleError as exc:
                    value, err = line.quantity, str(exc)

                key = f"{section}|{name}"
                outcomes[key] = RuleOutcome(
                    name=name,
                    section=section,
                    expression=expr,
                    value=value,
                    depends_on=sorted(ctx.reads),
                    error=err,
                )
                if err is None and abs(value - line.quantity) > 1e-9:
                    line.quantity = value
                    line.qty_source = "rule"
                    line.qty_expr = expr
                    changed = True
            if not changed:
                break

        # Attach a human-readable trace to every rule-driven line.
        for outcome in outcomes.values():
            line = draft.by_name(outcome.name, outcome.section)
            if line and line.section == outcome.section:
                line.qty_trace = describe(outcome, draft)
        return list(outcomes.values())


def describe(outcome: RuleOutcome, draft: Draft) -> str:
    """Render a short, human-facing justification for a computed quantity.

    Deliberately not the model's chain of thought: it is the rule, its inputs
    and their current values -- which is what a estimator needs to audit it.
    """
    if outcome.error:
        return f"Правило не обчислено: {outcome.error}"
    parts: list[str] = []
    for dep in outcome.depends_on[:8]:
        if dep.startswith("@"):
            parts.append(f"{dep[1:]} = {sum(l.total for l in draft.in_block(dep[1:])):.2f} грн")
            continue
        line = draft.by_name(dep, outcome.section)
        if line and line.quantity:
            parts.append(f"{line.name} = {_fmt(line.quantity)} {line.unit}".strip())
    inputs = "; ".join(parts) if parts else "без залежностей"
    return f"Розраховано за правилом шаблону. Вхідні дані: {inputs}."


def _fmt(v: float) -> str:
    return f"{v:g}"
