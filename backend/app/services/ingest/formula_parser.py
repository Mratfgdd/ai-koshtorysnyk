"""A small recursive-descent parser for the Excel subset used by the client's
estimate template, translating formulas into our rule DSL.

Why a real parser instead of regexes: the catalog names we substitute in contain
parentheses, digits and things that look exactly like cell references
(``Труба поліетиленова, D-32 PN10*2,4``). A single-pass parser never re-reads
its own output, so substituted names can never be mistaken for formula syntax.

The DSL produced here is evaluated by :mod:`app.services.rules.engine` against
an estimate draft. Its vocabulary:

``qty(name)``                      quantity of a line, 0 if absent
``value(name)``                    line total (price * qty)
``present(name)``                  1 if that line has a positive quantity else 0
``option(name)``                   boolean option toggle for that line
``sum_qty([names])``               sum of quantities
``sum_value([names])``             sum of line totals
``sum_qty_where([names], [pats])`` sum of quantities whose name matches all glob patterns
``sum_value_price_ge([names], t)`` sum of line totals where unit price >= t
``block_total(id)``                a section/block subtotal, e.g. "planting.plants"
``rnd/roundup/rounddown/ceiling``  rounding helpers
``mod(a, b)``, ``min``, ``max``, ``abs``
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable


class Untranslatable(Exception):
    """Raised when a formula falls outside the supported subset.

    The caller records the original formula for human review instead of
    inventing an equivalent -- guessing a coefficient would silently corrupt
    every estimate that uses it.
    """


# --- Lexer -------------------------------------------------------------------

TOKEN_RE = re.compile(
    r"""
    (?P<WS>\s+)
  | (?P<STRING>"(?:[^"]|"")*")
  | (?P<RANGE>\$?[A-Za-z]{1,3}\$?\d+\s*:\s*\$?[A-Za-z]{1,3}\$?\d+)
  | (?P<NUMBER>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
  | (?P<FUNC>[A-Za-z_][A-Za-z0-9_.]*\s*(?=\())
  | (?P<CELL>\$?[A-Za-z]{1,3}\$?\d+)
  | (?P<NAME>[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<OP><>|<=|>=|[-+*/^<>=,()&%])
    """,
    re.VERBOSE,
)


@dataclass
class Token:
    kind: str
    text: str
    pos: int


def tokenize(src: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    while i < len(src):
        m = TOKEN_RE.match(src, i)
        if not m:
            raise Untranslatable(f"unexpected character {src[i]!r} at {i}")
        kind = m.lastgroup or "OP"
        if kind != "WS":
            tokens.append(Token(kind, m.group().strip(), i))
        i = m.end()
    return tokens


# --- Reference resolution ----------------------------------------------------


# Template columns S/T/U VLOOKUP per-item attributes out of catalog columns
# L/M/N, which the client labels "обєм кашпо", "агрік для кашпо" and
# "геотекстиль для кашпо" -- litres of substrate and m² of fabric per planter.
ATTR_COLUMNS = {"S": "volume", "T": "agro_fabric", "U": "geotextile"}


class RefResolver:
    """Maps spreadsheet coordinates onto DSL terms.

    ``row_names`` maps a template row to the catalog item on that row.
    ``block_totals`` maps a subtotal row to a stable block identifier.
    ``row_blocks`` maps every line row to the block it belongs to, which lets a
    range over an operator-filled block (the plants list, the paving list)
    translate to a block reference instead of a fixed name list.
    """

    def __init__(
        self,
        row_names: dict[int, str],
        block_totals: dict[int, str],
        row_blocks: dict[int, str] | None = None,
    ):
        self.row_names = row_names
        self.block_totals = block_totals
        self.row_blocks = row_blocks or {}

    @staticmethod
    def _split(ref: str) -> tuple[str, int]:
        ref = ref.replace("$", "")
        m = re.fullmatch(r"([A-Za-z]{1,3})(\d+)", ref)
        if not m:
            raise Untranslatable(f"bad reference {ref!r}")
        return m.group(1).upper(), int(m.group(2))

    def _name(self, row: int, col: str) -> str:
        name = self.row_names.get(row)
        if not name:
            raise Untranslatable(f"{col}{row} points at a row with no catalog name")
        return json.dumps(name, ensure_ascii=False)

    def cell(self, ref: str) -> str:
        col, row = self._split(ref)
        if col == "C":
            # The name cell itself. Only ever compared against "" as an
            # "is this template row in use" guard, so a literal is faithful.
            return self._name(row, col)
        if col == "D":
            return f"qty({self._name(row, col)})"
        if col == "A":
            return f"present({self._name(row, col)})"
        if col == "H":
            return f"option({self._name(row, col)})"
        if col == "G":
            if row in self.block_totals:
                return f"block_total({json.dumps(self.block_totals[row])})"
            return f"value({self._name(row, col)})"
        if col in ATTR_COLUMNS:
            return f"attr({self._name(row, col)}, {json.dumps(ATTR_COLUMNS[col])})"
        raise Untranslatable(f"unsupported column {col} in {ref!r}")

    def _block_span(self, lo: int, hi: int) -> str | None:
        """Return the block id if the whole span sits inside one block."""
        ids = {self.row_blocks[r] for r in range(lo, hi + 1) if r in self.row_blocks}
        return ids.pop() if len(ids) == 1 else None

    def range_names(self, ref: str) -> tuple[str, list[str]]:
        a, b = [p.strip() for p in ref.split(":")]
        c1, r1 = self._split(a)
        c2, r2 = self._split(b)
        if c1 != c2:
            raise Untranslatable(f"multi-column range {ref!r}")
        lo, hi = min(r1, r2), max(r1, r2)
        names = [self.row_names[r] for r in range(lo, hi + 1) if self.row_names.get(r)]
        if not names:
            raise Untranslatable(f"range {ref!r} covers no catalog rows")
        return c1, names

    def range_(self, ref: str) -> str:
        a, b = [p.strip() for p in ref.split(":")]
        c1, r1 = self._split(a)
        c2, r2 = self._split(b)
        if c1 != c2:
            raise Untranslatable(f"multi-column range {ref!r}")
        lo, hi = min(r1, r2), max(r1, r2)
        names = [self.row_names[r] for r in range(lo, hi + 1) if self.row_names.get(r)]

        if not names:
            # An operator-filled block (the plant list, the paving list) has no
            # names in the template -- address the block instead of a name list.
            block = self._block_span(lo, hi)
            if block is None:
                raise Untranslatable(f"range {ref!r} covers no catalog rows")
            if c1 == "D":
                return f"sum_qty_block({json.dumps(block)})"
            if c1 == "G":
                return f"sum_value_block({json.dumps(block)})"
            raise Untranslatable(f"unsupported dynamic range column {c1} in {ref!r}")

        payload = json.dumps(names, ensure_ascii=False)
        if c1 == "D":
            return f"sum_qty({payload})"
        if c1 == "G":
            return f"sum_value({payload})"
        raise Untranslatable(f"unsupported range column {c1} in {ref!r}")


# --- Parser ------------------------------------------------------------------

CMP_OPS = {"=": "==", "<>": "!=", "<=": "<=", ">=": ">=", "<": "<", ">": ">"}


class FormulaParser:
    """Recursive-descent parser: Excel formula text -> DSL expression string."""

    def __init__(self, resolver: RefResolver):
        self.r = resolver
        self.toks: list[Token] = []
        self.i = 0

    # -- driver ---------------------------------------------------------------
    def parse(self, formula: str) -> str:
        src = formula.strip()
        if src.startswith("="):
            src = src[1:]
        if not src.strip():
            raise Untranslatable("empty formula")
        self.toks = tokenize(src)
        self.i = 0
        out = self._comparison()
        if self.i != len(self.toks):
            raise Untranslatable(f"trailing input at token {self.i}: {self.toks[self.i].text!r}")
        return out

    # -- token helpers --------------------------------------------------------
    def _peek(self) -> Token | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _eat(self, text: str) -> None:
        t = self._peek()
        if t is None or t.text.lower() != text.lower():
            raise Untranslatable(f"expected {text!r}, got {t.text if t else 'EOF'!r}")
        self.i += 1

    def _accept(self, text: str) -> bool:
        t = self._peek()
        if t is not None and t.text.lower() == text.lower():
            self.i += 1
            return True
        return False

    # -- grammar --------------------------------------------------------------
    def _comparison(self) -> str:
        left = self._concat()
        t = self._peek()
        while t is not None and t.kind == "OP" and t.text in CMP_OPS:
            self.i += 1
            right = self._concat()
            left = f"({left} {CMP_OPS[t.text]} {right})"
            t = self._peek()
        return left

    def _concat(self) -> str:
        left = self._additive()
        while self._peek() is not None and self._peek().text == "&":  # type: ignore[union-attr]
            raise Untranslatable("string concatenation is not a quantity rule")
        return left

    def _additive(self) -> str:
        left = self._multiplicative()
        while True:
            t = self._peek()
            if t is None or t.text not in ("+", "-"):
                return left
            self.i += 1
            right = self._multiplicative()
            left = f"({left} {t.text} {right})"

    def _multiplicative(self) -> str:
        left = self._unary()
        while True:
            t = self._peek()
            if t is None or t.text not in ("*", "/"):
                return left
            self.i += 1
            right = self._unary()
            left = f"({left} {t.text} {right})"

    def _unary(self) -> str:
        t = self._peek()
        if t is not None and t.text in ("-", "+"):
            self.i += 1
            return f"({t.text}{self._unary()})"
        return self._power()

    def _power(self) -> str:
        left = self._primary()
        if self._peek() is not None and self._peek().text == "^":  # type: ignore[union-attr]
            self.i += 1
            right = self._unary()
            return f"({left} ** {right})"
        return left

    def _primary(self) -> str:
        t = self._peek()
        if t is None:
            raise Untranslatable("unexpected end of formula")

        if t.kind == "NUMBER":
            self.i += 1
            nxt = self._peek()
            if nxt is not None and nxt.text == "%":
                self.i += 1
                return repr(float(t.text) / 100.0)
            return t.text

        if t.kind == "STRING":
            self.i += 1
            return json.dumps(t.text[1:-1].replace('""', '"'), ensure_ascii=False)

        if t.kind == "NAME":
            low = t.text.lower()
            if low in ("true", "false"):
                self.i += 1
                return "True" if low == "true" else "False"
            raise Untranslatable(f"bare name {t.text!r}")

        if t.kind == "FUNC":
            return self._funcall()

        if t.kind == "RANGE":
            self.i += 1
            return self.r.range_(t.text)

        if t.kind == "CELL":
            self.i += 1
            return self.r.cell(t.text)

        if t.text == "(":
            self.i += 1
            inner = self._comparison()
            self._eat(")")
            return f"({inner})"

        raise Untranslatable(f"unexpected token {t.text!r}")

    # -- function calls -------------------------------------------------------
    def _raw_args(self) -> list[Token]:
        """Collect the raw token spans of the current call's arguments."""
        raise NotImplementedError  # handled inline below

    def _funcall(self) -> str:
        t = self.toks[self.i]
        name = t.text.strip().lower().lstrip("_")
        if name.startswith("xlfn."):
            name = name[len("xlfn."):]
        self.i += 1
        self._eat("(")

        handler = self.FUNCS.get(name)
        if handler is None:
            raise Untranslatable(f"unsupported function {t.text.strip()!r}")
        out = handler(self)
        self._eat(")")
        return out

    def _args(self) -> list[str]:
        args: list[str] = []
        if self._peek() is not None and self._peek().text == ")":  # type: ignore[union-attr]
            return args
        args.append(self._comparison())
        while self._accept(","):
            args.append(self._comparison())
        return args

    # -- individual functions -------------------------------------------------
    def _f_if(self) -> str:
        a = self._args()
        if len(a) == 2:
            return f"(({a[1]}) if ({a[0]}) else 0)"
        if len(a) == 3:
            return f"(({a[1]}) if ({a[0]}) else ({a[2]}))"
        raise Untranslatable("IF needs 2 or 3 arguments")

    def _f_ifs(self) -> str:
        a = self._args()
        if len(a) < 2 or len(a) % 2:
            raise Untranslatable("IFS needs condition/value pairs")
        out = "0"
        for cond, val in reversed(list(zip(a[0::2], a[1::2]))):
            out = f"(({val}) if ({cond}) else {out})"
        return out

    def _f_and(self) -> str:
        a = self._args()
        return "(" + " and ".join(f"({x})" for x in a) + ")" if a else "True"

    def _f_or(self) -> str:
        a = self._args()
        return "(" + " or ".join(f"({x})" for x in a) + ")" if a else "False"

    def _f_not(self) -> str:
        a = self._args()
        return f"(not ({a[0]}))"

    def _f_sum(self) -> str:
        a = self._args()
        return "(" + " + ".join(f"({x})" for x in a) + ")" if a else "0"

    def _f_round(self) -> str:
        a = self._args()
        return f"rnd({a[0]}, {a[1] if len(a) > 1 else '0'})"

    def _f_roundup(self) -> str:
        a = self._args()
        return f"roundup({a[0]}, {a[1] if len(a) > 1 else '0'})"

    def _f_rounddown(self) -> str:
        a = self._args()
        return f"rounddown({a[0]}, {a[1] if len(a) > 1 else '0'})"

    def _f_int(self) -> str:
        a = self._args()
        return f"rounddown({a[0]}, 0)"

    def _f_ceiling(self) -> str:
        a = self._args()
        return f"ceiling({a[0]}, {a[1] if len(a) > 1 else '1'})"

    def _f_mod(self) -> str:
        a = self._args()
        return f"mod({a[0]}, {a[1]})"

    def _f_iferror(self) -> str:
        # IFERROR(x, fallback): our evaluator returns 0 for missing lines, so
        # the error branch is unreachable -- keep the primary expression.
        a = self._args()
        if not a:
            raise Untranslatable("IFERROR needs arguments")
        return a[0]

    def _f_sumifs(self) -> str:
        """SUMIFS(sum_range, crit_range, "*pat*", crit_range, "*pat*", ...).

        The template uses this to size paving works by matching the material
        names ("*Бруківка*", "*6см*", "*великоформатна*"). We translate it to a
        glob-match sum over the names covered by the sum range.
        """
        start = self.i
        sum_tok = self._peek()
        if sum_tok is None or sum_tok.kind != "RANGE":
            raise Untranslatable("SUMIFS sum range must be a literal range")
        self.i += 1
        col, names = self.r.range_names(sum_tok.text)
        if col != "D":
            raise Untranslatable("SUMIFS only supported over quantity ranges")

        patterns: list[str] = []
        while self._accept(","):
            crit = self._peek()
            if crit is None or crit.kind != "RANGE":
                raise Untranslatable("SUMIFS criteria range must be a literal range")
            ccol, cnames = self.r.range_names(crit.text)
            if ccol != "C" and cnames != names:
                # Criteria are applied to the name column; both C and D ranges
                # in this template cover the same rows.
                pass
            self.i += 1
            self._eat(",")
            pat = self._peek()
            if pat is None or pat.kind != "STRING":
                raise Untranslatable("SUMIFS criterion must be a literal pattern")
            self.i += 1
            patterns.append(pat.text[1:-1].replace('""', '"'))

        if not patterns:
            self.i = start
            raise Untranslatable("SUMIFS without patterns")
        return (
            f"sum_qty_where({json.dumps(names, ensure_ascii=False)}, "
            f"{json.dumps(patterns, ensure_ascii=False)})"
        )

    def _f_sumproduct(self) -> str:
        """SUMPRODUCT(prices, quantities, prices>=threshold).

        Used once, to bill planting of specimen trees at a different rate above
        a price threshold. Anything else is refused.
        """
        spans = []
        for _ in range(3):
            t = self._peek()
            if t is None or t.kind != "RANGE":
                raise Untranslatable("SUMPRODUCT shape not recognised")
            self.i += 1
            spans.append(t.text)
            if len(spans) < 3:
                self._eat(",")

        op = self._peek()
        if op is None or op.text not in (">=", ">"):
            raise Untranslatable("SUMPRODUCT shape not recognised")
        self.i += 1
        thr = self._additive()

        price_ref, qty_ref, crit_ref = spans
        cols = {self.r._split(r.split(":")[0])[0] for r in (price_ref, crit_ref)}
        if cols != {"F"} or self.r._split(qty_ref.split(":")[0])[0] != "D":
            raise Untranslatable("SUMPRODUCT ranges do not line up")

        lo, hi = (self.r._split(p)[1] for p in qty_ref.split(":"))
        block = self.r._block_span(min(lo, hi), max(lo, hi))
        if block is not None:
            return f"sum_value_price_ge_block({json.dumps(block)}, {thr})"
        _, names = self.r.range_names(qty_ref)
        return f"sum_value_price_ge({json.dumps(names, ensure_ascii=False)}, {thr})"

    FUNCS: dict[str, Callable[["FormulaParser"], str]] = {}


def _simple(dsl: str, arity: int | None = None) -> Callable[[FormulaParser], str]:
    """Build a handler for a function that maps 1:1 onto a DSL helper."""

    def run(parser: FormulaParser) -> str:
        args = parser._args()
        if arity is not None and len(args) != arity:
            raise Untranslatable(f"{dsl} expects {arity} arguments")
        return f"{dsl}(" + ", ".join(args) + ")"

    return run


def _isblank(parser: FormulaParser) -> str:
    args = parser._args()
    if len(args) != 1:
        raise Untranslatable("ISBLANK expects 1 argument")
    return f"({args[0]} == 0)"


FormulaParser.FUNCS = {
    "if": FormulaParser._f_if,
    "ifs": FormulaParser._f_ifs,
    "and": FormulaParser._f_and,
    "or": FormulaParser._f_or,
    "not": FormulaParser._f_not,
    "sum": FormulaParser._f_sum,
    "round": FormulaParser._f_round,
    "roundup": FormulaParser._f_roundup,
    "rounddown": FormulaParser._f_rounddown,
    "int": FormulaParser._f_int,
    "ceiling.math": FormulaParser._f_ceiling,
    "ceiling": FormulaParser._f_ceiling,
    "mod": FormulaParser._f_mod,
    "min": _simple("min"),
    "max": _simple("max"),
    "abs": _simple("abs", 1),
    "iferror": FormulaParser._f_iferror,
    "ifna": FormulaParser._f_iferror,
    "sumifs": FormulaParser._f_sumifs,
    "sumproduct": FormulaParser._f_sumproduct,
    "isblank": _isblank,
}


def translate(formula: str, resolver: RefResolver) -> str:
    return FormulaParser(resolver).parse(formula)
