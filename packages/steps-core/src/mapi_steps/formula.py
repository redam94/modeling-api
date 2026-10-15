"""``core.formula``: a derived variable from a small arithmetic expression language.

Expressions are parsed with a Lark grammar and compiled to a tiny AST whose only operations
are an allow-list of element-wise xarray/numpy functions, comparisons, boolean logic and
named-dim reductions. There are no attribute lookups, no indexing, no imports and no calls
other than the allow-list, and expressions are bounded in length, size and depth.

This is a **usability feature, not a security boundary** (plan §4.1): formulas only ever
run inside the sandboxed runner, like all step code.

Grammar (precedence low → high)::

    a | b      a & b      ~a
    a < b   a <= b   a > b   a >= b   a == b   a != b
    a + b   a - b
    a * b   a / b
    -a   +a
    a ** b
    name   number   'string'   f(args...)   (expr)

Functions: ``log log1p exp expm1 sqrt abs sign floor ceil sin cos tanh expit logit``
(one argument), ``minimum maximum fillna`` (two), ``clip where`` (three), ``isnull notnull``
(one), reductions ``sum mean max min`` (``f(x, 'dim', ...)``) and ``var('odd-name')`` for
variables whose names are not identifiers.
"""

from __future__ import annotations

import functools
import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import numpy as np
import xarray as xr
from lark import Lark, Token, Transformer, v_args
from lark.exceptions import LarkError, VisitError
from mapi_sdk.steps import Step, StepContext, StepSettings, StepState
from mapi_spec.effects import StepKind
from pydantic import Field, JsonValue, field_validator

from mapi_steps._common import Name, NameList, schema_error

__all__ = ["FormulaError", "FormulaStep", "compile_formula", "evaluate"]

MAX_EXPR_CHARS = 4000
MAX_NODES = 500
MAX_DEPTH = 64

GRAMMAR = r"""
?start: or_

?or_: and_
    | or_ "|" and_            -> or_op
?and_: not_
    | and_ "&" not_           -> and_op
?not_: comparison
    | "~" not_                -> not_op
?comparison: sum
    | sum COMP_OP sum         -> compare
?sum: product
    | sum "+" product         -> add
    | sum "-" product         -> sub
?product: unary
    | product "*" unary       -> mul
    | product "/" unary       -> div
?unary: power
    | "-" unary               -> neg
    | "+" unary               -> pos
?power: atom
    | atom "**" unary         -> pow
?atom: NUMBER                 -> number
    | STRING                  -> string
    | NAME "(" [arglist] ")"  -> call
    | NAME                    -> name
    | "(" or_ ")"
arglist: or_ ("," or_)*

COMP_OP: "<=" | ">=" | "==" | "!=" | "<" | ">"
NAME: /[A-Za-z_][A-Za-z0-9_]*/
STRING: /'[^'\n]{0,128}'/ | /"[^"\n]{0,128}"/
%import common.NUMBER
%import common.WS
%ignore WS
"""

_PARSER = Lark(GRAMMAR, parser="lalr", maybe_placeholders=False)


class FormulaError(ValueError):
    """The expression does not parse or uses something outside the allow-list."""


Node = tuple[Any, ...]

UNARY: dict[str, Callable[[Any], Any]] = {
    "log": np.log,
    "log1p": np.log1p,
    "exp": np.exp,
    "expm1": np.expm1,
    "sqrt": np.sqrt,
    "abs": np.abs,
    "sign": np.sign,
    "floor": np.floor,
    "ceil": np.ceil,
    "sin": np.sin,
    "cos": np.cos,
    "tanh": np.tanh,
    "expit": lambda x: 1.0 / (1.0 + np.exp(-x)),
    "logit": lambda x: np.log(x) - np.log1p(-x),
}
PREDICATES = ("isnull", "notnull")
BINARY = ("minimum", "maximum", "fillna")
TERNARY = ("clip", "where")
REDUCTIONS = ("sum", "mean", "max", "min")
BINOPS: dict[str, Callable[[Any, Any], Any]] = {
    "add": operator.add,
    "sub": operator.sub,
    "mul": operator.mul,
    "div": operator.truediv,
    "pow": operator.pow,
    "and_op": operator.and_,
    "or_op": operator.or_,
}
COMPARE: dict[str, Callable[[Any, Any], Any]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


@v_args(inline=True)
class _ToAst(Transformer[Token, Node]):
    """Parse tree → nested tuples, checking function names and arities."""

    def number(self, tok: Token) -> Node:
        return ("num", float(tok))

    def string(self, tok: Token) -> Node:
        return ("str", str(tok)[1:-1])

    def name(self, tok: Token) -> Node:
        return ("var", str(tok))

    def neg(self, a: Node) -> Node:
        return ("neg", a)

    def pos(self, a: Node) -> Node:
        return a

    def not_op(self, a: Node) -> Node:
        return ("not", a)

    def compare(self, a: Node, op: Token, b: Node) -> Node:
        return ("cmp", str(op), a, b)

    def arglist(self, *args: Node) -> list[Node]:
        return list(args)

    def call(self, fname: Token, args: list[Node] | None = None) -> Node:
        name = str(fname)
        args = args or []
        n = len(args)

        def need(k: int) -> None:
            if n != k:
                raise FormulaError(f"{name}() takes {k} argument(s), got {n}")

        if name in UNARY or name in PREDICATES:
            need(1)
        elif name in BINARY:
            need(2)
        elif name in TERNARY:
            need(3)
        elif name in REDUCTIONS:
            if n < 2 or any(a[0] != "str" for a in args[1:]):
                raise FormulaError(f"{name}(x, 'dim', ...) needs dim names as strings")
        elif name == "var":
            need(1)
            if args[0][0] != "str":
                raise FormulaError("var() takes a variable name as a string")
            return ("var", args[0][1])
        else:
            raise FormulaError(f"unknown function {name!r}")
        if name not in REDUCTIONS and any(a[0] == "str" for a in args):
            raise FormulaError(f"{name}() does not take string arguments")
        return ("call", name, args)

    def __default__(self, data: Any, children: Any, meta: Any) -> Node:
        if data in BINOPS:
            a, b = children
            return ("bin", str(data), a, b)
        raise FormulaError(f"unsupported syntax {data!r}")


def _measure(node: Node, depth: int = 1) -> tuple[int, int]:
    """(node count, max depth) of an AST."""
    count, deepest = 1, depth
    for child in node[1:]:
        kids = child if isinstance(child, list) else [child]
        for k in kids:
            if isinstance(k, tuple):
                c, d = _measure(k, depth + 1)
                count += c
                deepest = max(deepest, d)
    return count, deepest


@dataclass(frozen=True, slots=True)
class Compiled:
    """A checked formula AST and the variables it references."""

    ast: Node
    names: frozenset[str]


def _names(node: Node) -> set[str]:
    if node[0] == "var":
        return {node[1]}
    out: set[str] = set()
    for child in node[1:]:
        kids = child if isinstance(child, list) else [child]
        for k in kids:
            if isinstance(k, tuple):
                out |= _names(k)
    return out


@functools.lru_cache(maxsize=256)
def compile_formula(expr: str) -> Compiled:
    """Parse and check ``expr`` (raises :class:`FormulaError`)."""
    if len(expr) > MAX_EXPR_CHARS:
        raise FormulaError(f"formula is longer than {MAX_EXPR_CHARS} characters")
    try:
        tree = _PARSER.parse(expr)
        ast = _ToAst().transform(tree)
    except VisitError as exc:
        if isinstance(exc.orig_exc, FormulaError):
            raise exc.orig_exc from None
        raise FormulaError(str(exc.orig_exc)) from None
    except LarkError as exc:
        raise FormulaError(f"formula does not parse: {str(exc).splitlines()[0]}") from None
    if ast[0] == "str":
        raise FormulaError("a formula must not be a bare string")
    count, depth = _measure(ast)
    if count > MAX_NODES or depth > MAX_DEPTH:
        raise FormulaError(f"formula is too large ({count} nodes, depth {depth})")
    return Compiled(ast, frozenset(_names(ast)))


def evaluate(compiled: Compiled, ds: xr.Dataset) -> xr.DataArray | float:
    """Evaluate a compiled formula over ``ds`` (variables and coordinates by name)."""

    def ev(node: Node) -> Any:
        match node[0]:
            case "num":
                return node[1]
            case "var":
                name = node[1]
                if name not in ds.variables:
                    raise FormulaError(f"unknown variable {name!r}")
                return ds[name]
            case "neg":
                return -ev(node[1])
            case "not":
                return ~_as_bool(ev(node[1]))
            case "cmp":
                return COMPARE[node[1]](ev(node[2]), ev(node[3]))
            case "bin":
                a, b = ev(node[2]), ev(node[3])
                if node[1] in ("and_op", "or_op"):
                    a, b = _as_bool(a), _as_bool(b)
                return BINOPS[node[1]](a, b)
            case "call":
                return _call(node[1], node[2])
        raise FormulaError(f"unsupported node {node[0]!r}")  # pragma: no cover

    def _call(name: str, args: list[Node]) -> Any:
        if name in UNARY:
            return _ufunc(UNARY[name], ev(args[0]))
        if name == "isnull":
            return _da(ev(args[0])).isnull()
        if name == "notnull":
            return _da(ev(args[0])).notnull()
        if name == "minimum":
            return _ufunc2(np.minimum, ev(args[0]), ev(args[1]))
        if name == "maximum":
            return _ufunc2(np.maximum, ev(args[0]), ev(args[1]))
        if name == "fillna":
            return _da(ev(args[0])).fillna(ev(args[1]))
        if name == "clip":
            return _da(ev(args[0])).clip(ev(args[1]), ev(args[2]))
        if name == "where":
            return xr.where(_as_bool(ev(args[0])), ev(args[1]), ev(args[2]))  # type: ignore[no-untyped-call]
        x = _da(ev(args[0]))
        dims = [a[1] for a in args[1:]]
        missing = [d for d in dims if d not in x.dims]
        if missing:
            raise FormulaError(f"{name}() over dims {missing} that the operand does not have")
        return getattr(x, name)(dims, skipna=True)

    with np.errstate(all="ignore"):
        result = ev(compiled.ast)
    if isinstance(result, xr.DataArray):
        return result
    return float(result)


def _da(x: Any) -> xr.DataArray:
    return x if isinstance(x, xr.DataArray) else xr.DataArray(x)


def _as_bool(x: Any) -> Any:
    if isinstance(x, xr.DataArray):
        return x if x.dtype.kind == "b" else x != 0
    return bool(x)


def _ufunc(fn: Callable[[Any], Any], x: Any) -> Any:
    if isinstance(x, xr.DataArray):
        return xr.apply_ufunc(fn, x.astype(np.float64))
    return float(fn(np.float64(x)))


def _ufunc2(fn: Callable[[Any, Any], Any], a: Any, b: Any) -> Any:
    return xr.apply_ufunc(fn, _da(a), _da(b))


# ============================================================================= the step

RoleName = Literal[
    "covariate", "media", "spend", "control", "constant", "weight", "exposure", "mask"
]


class FormulaSettings(StepSettings):
    out: Name = Field(..., description="Name of the new variable")
    expr: str = Field(..., min_length=1, max_length=MAX_EXPR_CHARS)
    dims: NameList = Field(
        default_factory=list, description="Dims of the result (broadcast if it has fewer)"
    )
    role: RoleName = "covariate"
    units: str | None = Field(None, max_length=64)

    @field_validator("expr")
    @classmethod
    def _compiles(cls, v: str) -> str:
        compile_formula(v)
        return v


class FormulaStep(Step[FormulaSettings, StepState]):
    """A new variable ``out`` computed from an allow-listed expression (float64)."""

    id = "core.formula"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = FormulaSettings
    dim_params = ("dims",)
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["out", "dims", "role", "units"],
        "adds": {
            "{out}": {
                "dims": "{dims}",
                "dtype": "float64",
                "role": "{role}",
                "units": "{units}",
                "missing": "allow",
            }
        },
    }
    summary = "Derived variable from an arithmetic expression"
    summary_plain = "Creates a new column from a formula such as price * units"

    def transform(
        self, ds: xr.Dataset, s: FormulaSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        compiled = compile_formula(s.expr)
        absent = sorted(n for n in compiled.names if n not in ds.variables)
        if absent:
            targets = set(ctx.schema.targets) if ctx.schema is not None else set()
            if set(absent) <= targets:
                return ds  # derived from a target that is absent at score time
            raise schema_error(ctx.step_id, f"formula references unknown variables {absent}")
        if s.out in ds.variables:
            raise schema_error(ctx.step_id, f"{s.out!r} already exists")
        try:
            result = evaluate(compiled, ds)
        except FormulaError as exc:
            raise schema_error(ctx.step_id, str(exc)) from None
        da = _da(result).astype(np.float64)
        extra = [d for d in da.dims if d not in s.dims]
        if extra:
            raise schema_error(
                ctx.step_id, f"formula result has dims {list(da.dims)}, declared {s.dims}"
            )
        lacking = [d for d in s.dims if d not in da.dims]
        for d in lacking:
            if d not in ds.dims:
                raise schema_error(ctx.step_id, f"declared dim {d!r} does not exist")
            da = da.expand_dims({d: ds[d].values if d in ds.coords else ds.sizes[d]})
        da = da.transpose(*s.dims)
        da = da.drop_vars([c for c in da.coords if c not in da.dims])
        da.attrs = {} if s.units is None else {"units": s.units}
        return ds.assign({s.out: da})
