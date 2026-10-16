"""The safe condition language of workflow branches (M3.5).

Branch nodes decide which way a workflow goes from the results of upstream nodes, e.g.
``nodes.adf.p_value < params.alpha and not nodes.kpss.reject``. Conditions are parsed by a
small hand-written tokenizer and recursive-descent parser into an immutable AST and
evaluated by walking it over plain JSON data. Nothing is ever passed to ``eval``/``exec``,
no attribute of a Python object is ever read (references index dicts and lists only), and
every input is bounded (:data:`LIMITS`), so a hostile condition can at worst be refused.

Grammar (EBNF; keywords are lowercase, whitespace is insignificant)::

    expression  = or ;
    or          = and , { "or" , and } ;
    and         = not , { "and" , not } ;
    not         = "not" , not | comparison ;
    comparison  = sum , [ compare_op , sum ] ;        (* no chaining: a < b < c is refused *)
    compare_op  = "==" | "!=" | "<" | "<=" | ">" | ">=" | "in" | "not" , "in" ;
    sum         = product , { ( "+" | "-" ) , product } ;
    product     = unary , { ( "*" | "/" | "%" ) , unary } ;
    unary       = "-" , unary | primary ;
    primary     = number | string | "true" | "false" | "null" | list | call | reference
                | "(" , expression , ")" ;
    list        = "[" , [ expression , { "," , expression } ] , "]" ;
    call        = function , "(" , [ expression , { "," , expression } ] , ")" ;
    function    = "abs" | "min" | "max" | "len" | "round" | "any" | "all" | "exists"
                | "coalesce" | "if" ;
    reference   = root , { "." , name | "[" , ( integer | string ) , "]" } ;
    root        = "nodes" | "params" | "inputs" | "run" ;
    number      = digit , { digit } , [ "." , digit , { digit } ] , [ exponent ] ;
    string      = "'" , { character } , "'" | '"' , { character } , '"' ;

Semantics are strict so conditions mean what they say: ``and``/``or``/``not`` need booleans
(no truthiness), ordering compares numbers with numbers and strings with strings, ``==``
compares any JSON values (``1 == 1.0``; ``true != 1``), ``in`` tests list membership or a
substring, arithmetic is on numbers only, and a missing reference is an error unless it is
wrapped in ``exists(...)`` or ``coalesce(...)``. ``and``/``or`` short-circuit, and so does
``if(condition, a, b)`` (only the chosen value is evaluated), which turns a decision into a
value: ``if(nodes.wanders.value, 1, 0)``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, Field

from mapi_spec.errors import ErrorCode, MapiError

__all__ = [
    "FUNCTIONS",
    "LIMITS",
    "ROOTS",
    "BinaryOp",
    "BoolOp",
    "Call",
    "Compare",
    "Condition",
    "Expression",
    "ExpressionLimits",
    "ListExpr",
    "Literal_",
    "Node",
    "Not",
    "Ref",
    "Unary",
    "check_condition",
    "evaluate",
    "evaluate_condition",
    "parse_expression",
]

JsonScalar = str | int | float | bool | None
Value = JsonScalar | list["Value"] | dict[str, "Value"]


@dataclass(frozen=True, slots=True)
class ExpressionLimits:
    """Caps on what a condition may be (all checked before or during evaluation)."""

    max_chars: int = 2000
    max_tokens: int = 400
    max_depth: int = 32
    max_list: int = 100
    max_string: int = 500
    max_number_digits: int = 30
    max_steps: int = 10_000
    max_result_len: int = 10_000


LIMITS: Final = ExpressionLimits()
ROOTS: Final[frozenset[str]] = frozenset({"nodes", "params", "inputs", "run"})
"""What a reference may start with: upstream node results, workflow parameters, workflow
inputs and run metadata (``run.attempt``, ``run.trigger``)."""
KEYWORDS: Final[frozenset[str]] = frozenset({"and", "or", "not", "in", "true", "false", "null"})


def _err(message: str, pos: int | None = None) -> MapiError:
    where = f" (at character {pos + 1})" if pos is not None else ""
    return MapiError(f"{message}{where}", code=ErrorCode.EXPRESSION_INVALID)


def _eval_err(message: str) -> MapiError:
    return MapiError(message, code=ErrorCode.EXPRESSION_ERROR)


# ===================================================================================== tokens

TokenKind = Literal["num", "str", "name", "op", "end"]


@dataclass(frozen=True, slots=True)
class _Token:
    kind: TokenKind
    text: str
    pos: int
    value: JsonScalar = None


_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPS = ("==", "!=", "<=", ">=", "<", ">", "+", "-", "*", "/", "%", "(", ")", "[", "]", ",", ".")
_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}


def _tokenize(text: str, limits: ExpressionLimits) -> list[_Token]:
    if not isinstance(text, str):
        raise _err("a condition must be text")
    if len(text) > limits.max_chars:
        raise _err(f"the condition is longer than {limits.max_chars} characters")
    if not text.strip():
        raise _err("the condition is empty")
    out: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if len(out) >= limits.max_tokens:
            raise _err(f"the condition has more than {limits.max_tokens} tokens")
        if ch.isdigit():
            m = _NUMBER.match(text, i)
            assert m is not None
            raw = m.group(0)
            if len(raw) > limits.max_number_digits:
                raise _err("number literal is too long", i)
            value: float | int = float(raw) if any(c in raw for c in ".eE") else int(raw)
            if isinstance(value, float) and not math.isfinite(value):
                raise _err("number literal is not finite", i)
            out.append(_Token("num", raw, i, value))
            i = m.end()
            # "1.foo" or "2abc" are not numbers followed by something meaningful
            if i < n and (text[i].isalpha() or text[i] == "_"):
                raise _err("a number cannot be followed by a name", i)
            continue
        if ch in "'\"":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise _err("unterminated string", i)
                c = text[j]
                if c == ch:
                    break
                if c == "\\":
                    if j + 1 >= n or text[j + 1] not in _ESCAPES:
                        raise _err("unknown escape in string", j)
                    buf.append(_ESCAPES[text[j + 1]])
                    j += 2
                    continue
                if c in "\r\n":
                    raise _err("strings cannot span lines", j)
                buf.append(c)
                j += 1
                if len(buf) > limits.max_string:
                    raise _err(f"string literal is longer than {limits.max_string}", i)
            out.append(_Token("str", text[i : j + 1], i, "".join(buf)))
            i = j + 1
            continue
        if ch.isalpha() or ch == "_":
            m = _NAME.match(text, i)
            assert m is not None
            if len(m.group(0)) > 64:
                raise _err("name is longer than 64 characters", i)
            out.append(_Token("name", m.group(0), i))
            i = m.end()
            continue
        op = next((o for o in _OPS if text.startswith(o, i)), None)
        if op is None:
            raise _err(f"unexpected character {ch!r}", i)
        out.append(_Token("op", op, i))
        i += len(op)
    out.append(_Token("end", "", n))
    return out


# ======================================================================================== AST


@dataclass(frozen=True, slots=True)
class Literal_:
    """A constant: number, string, boolean or null."""

    value: JsonScalar


@dataclass(frozen=True, slots=True)
class ListExpr:
    items: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Ref:
    """``root.a.b[0]['c']``: a path into the evaluation environment."""

    root: str
    path: tuple[str | int, ...] = ()

    def __str__(self) -> str:
        out = self.root
        for p in self.path:
            out += f"[{p}]" if isinstance(p, int) else f".{p}"
        return out


@dataclass(frozen=True, slots=True)
class Unary:
    op: Literal["-"]
    operand: Node


@dataclass(frozen=True, slots=True)
class BinaryOp:
    op: Literal["+", "-", "*", "/", "%"]
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Compare:
    op: Literal["==", "!=", "<", "<=", ">", ">=", "in", "not in"]
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class BoolOp:
    op: Literal["and", "or"]
    operands: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Not:
    operand: Node


@dataclass(frozen=True, slots=True)
class Call:
    name: str
    args: tuple[Node, ...] = field(default=())


Node = Literal_ | ListExpr | Ref | Unary | BinaryOp | Compare | BoolOp | Not | Call

FUNCTIONS: Final[dict[str, tuple[int, int | None]]] = {
    "abs": (1, 1),
    "min": (1, None),
    "max": (1, None),
    "len": (1, 1),
    "round": (1, 2),
    "any": (1, 1),
    "all": (1, 1),
    "exists": (1, 1),
    "coalesce": (1, None),
    "if": (3, 3),
}
"""Allowed functions and their ``(min, max)`` argument counts (``None``: no maximum)."""


# ===================================================================================== parser


class _Parser:
    def __init__(self, tokens: list[_Token], limits: ExpressionLimits) -> None:
        self.t = tokens
        self.i = 0
        self.depth = 0
        self.limits = limits

    @property
    def cur(self) -> _Token:
        return self.t[self.i]

    def _is(self, kind: TokenKind, text: str | None = None) -> bool:
        tok = self.cur
        return tok.kind == kind and (text is None or tok.text == text)

    def _take(self, kind: TokenKind, text: str | None = None) -> _Token:
        if not self._is(kind, text):
            want = text or kind
            got = self.cur.text or "the end"
            raise _err(f"expected {want!r} but found {got!r}", self.cur.pos)
        tok = self.cur
        self.i += 1
        return tok

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > self.limits.max_depth:
            raise _err(f"the condition is nested deeper than {self.limits.max_depth}", self.cur.pos)

    def parse(self) -> Node:
        node = self.expression()
        if not self._is("end"):
            raise _err(f"unexpected {self.cur.text!r}", self.cur.pos)
        return node

    def expression(self) -> Node:
        self._enter()
        try:
            return self.or_()
        finally:
            self.depth -= 1

    def or_(self) -> Node:
        items = [self.and_()]
        while self._is("name", "or"):
            self.i += 1
            items.append(self.and_())
        return items[0] if len(items) == 1 else BoolOp("or", tuple(items))

    def and_(self) -> Node:
        items = [self.not_()]
        while self._is("name", "and"):
            self.i += 1
            items.append(self.not_())
        return items[0] if len(items) == 1 else BoolOp("and", tuple(items))

    def not_(self) -> Node:
        if self._is("name", "not") and not (
            self.t[self.i + 1].kind == "name" and self.t[self.i + 1].text == "in"
        ):
            self.i += 1
            self._enter()
            try:
                return Not(self.not_())
            finally:
                self.depth -= 1
        return self.comparison()

    def _compare_op(self) -> str | None:
        tok = self.cur
        if tok.kind == "op" and tok.text in ("==", "!=", "<", "<=", ">", ">="):
            self.i += 1
            return tok.text
        if tok.kind == "name" and tok.text == "in":
            self.i += 1
            return "in"
        if tok.kind == "name" and tok.text == "not" and self.t[self.i + 1].text == "in":
            self.i += 2
            return "not in"
        return None

    def comparison(self) -> Node:
        left = self.sum_()
        op = self._compare_op()
        if op is None:
            return left
        right = self.sum_()
        pos = self.cur.pos
        if self._compare_op() is not None:
            raise _err("comparisons cannot be chained; join them with 'and'", pos)
        return Compare(op, left, right)  # type: ignore[arg-type]

    def sum_(self) -> Node:
        node = self.product()
        while self.cur.kind == "op" and self.cur.text in ("+", "-"):
            op = self.cur.text
            self.i += 1
            node = BinaryOp(op, node, self.product())  # type: ignore[arg-type]
        return node

    def product(self) -> Node:
        node = self.unary()
        while self.cur.kind == "op" and self.cur.text in ("*", "/", "%"):
            op = self.cur.text
            self.i += 1
            node = BinaryOp(op, node, self.unary())  # type: ignore[arg-type]
        return node

    def unary(self) -> Node:
        if self._is("op", "-"):
            self.i += 1
            self._enter()
            try:
                return Unary("-", self.unary())
            finally:
                self.depth -= 1
        return self.primary()

    def _args(self, close: str) -> tuple[Node, ...]:
        items: list[Node] = []
        if self._is("op", close):
            self.i += 1
            return ()
        while True:
            items.append(self.expression())
            if len(items) > self.limits.max_list:
                raise _err(f"more than {self.limits.max_list} items", self.cur.pos)
            if self._is("op", ","):
                self.i += 1
                continue
            self._take("op", close)
            return tuple(items)

    def primary(self) -> Node:
        tok = self.cur
        if tok.kind == "num" or tok.kind == "str":
            self.i += 1
            return Literal_(tok.value)
        if tok.kind == "op" and tok.text == "(":
            self.i += 1
            node = self.expression()
            self._take("op", ")")
            return node
        if tok.kind == "op" and tok.text == "[":
            self.i += 1
            self._enter()
            try:
                return ListExpr(self._args("]"))
            finally:
                self.depth -= 1
        if tok.kind == "name":
            name = tok.text
            if name in ("true", "false"):
                self.i += 1
                return Literal_(name == "true")
            if name == "null":
                self.i += 1
                return Literal_(None)
            if name in KEYWORDS:
                raise _err(f"unexpected keyword {name!r}", tok.pos)
            if self.t[self.i + 1].kind == "op" and self.t[self.i + 1].text == "(":
                if name not in FUNCTIONS:
                    raise _err(
                        f"unknown function {name!r} (allowed: {', '.join(sorted(FUNCTIONS))})",
                        tok.pos,
                    )
                self.i += 2
                self._enter()
                try:
                    args = self._args(")")
                finally:
                    self.depth -= 1
                lo, hi = FUNCTIONS[name]
                if len(args) < lo or (hi is not None and len(args) > hi):
                    raise _err(f"{name}() takes {lo}{'' if hi == lo else '+'} argument(s)", tok.pos)
                if name == "exists" and not isinstance(args[0], Ref):
                    raise _err("exists() takes a reference such as nodes.adf.p_value", tok.pos)
                return Call(name, args)
            return self.reference()
        raise _err(f"unexpected {tok.text or 'end of condition'!r}", tok.pos)

    def reference(self) -> Ref:
        tok = self._take("name")
        if tok.text not in ROOTS:
            raise _err(
                f"unknown name {tok.text!r}: references start with {', '.join(sorted(ROOTS))}",
                tok.pos,
            )
        path: list[str | int] = []
        while True:
            if self._is("op", "."):
                self.i += 1
                part = self._take("name")
                path.append(part.text)
            elif self._is("op", "["):
                self.i += 1
                key = self.cur
                if (key.kind == "num" and isinstance(key.value, int)) or (
                    key.kind == "str" and isinstance(key.value, str)
                ):
                    path.append(key.value)
                else:
                    raise _err("an index must be a whole number or a string", key.pos)
                self.i += 1
                self._take("op", "]")
            else:
                break
            if len(path) > 16:
                raise _err("a reference may have at most 16 parts", tok.pos)
        return Ref(tok.text, tuple(path))


# ================================================================================ evaluation


class _Missing:
    """A reference that resolved to nothing (only ``exists``/``coalesce`` may see it)."""


_MISSING: Final = _Missing()


def _is_num(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _type_name(v: object) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if _is_num(v):
        return "number"
    if isinstance(v, str):
        return "text"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "object"
    return "value"


def _json_eq(a: object, b: object) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if _is_num(a) and _is_num(b):
        return float(a) == float(b)  # type: ignore[arg-type]
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_eq(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_eq(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


class _Evaluator:
    def __init__(self, env: Mapping[str, object], limits: ExpressionLimits) -> None:
        self.env = env
        self.limits = limits
        self.steps = 0

    def _tick(self) -> None:
        self.steps += 1
        if self.steps > self.limits.max_steps:
            raise _eval_err("the condition takes too many steps to evaluate")

    def lookup(self, ref: Ref) -> object:
        cur: object = (
            self.env.get(ref.root, _MISSING) if isinstance(self.env, Mapping) else _MISSING
        )
        for part in ref.path:
            self._tick()
            if isinstance(cur, Mapping) and isinstance(part, str):
                cur = cur.get(part, _MISSING)
            elif isinstance(cur, list) and isinstance(part, int) and not isinstance(part, bool):
                cur = cur[part] if 0 <= part < len(cur) else _MISSING
            else:
                cur = _MISSING
            if cur is _MISSING:
                return _MISSING
        if cur is _MISSING:
            return _MISSING
        if not isinstance(cur, str | int | float | bool | list | dict) and cur is not None:
            return _MISSING  # never expose a Python object
        return cur

    def value(self, node: Node) -> object:
        v = self.eval(node)
        if v is _MISSING:
            assert isinstance(node, Ref)
            raise _eval_err(f"{node} is not available")
        return v

    def eval(self, node: Node) -> object:
        self._tick()
        match node:
            case Literal_(value=lit):
                return lit
            case Ref():
                return self.lookup(node)
            case ListExpr(items=items):
                return [self.value(i) for i in items]
            case Unary(operand=operand):
                v = self.value(operand)
                if not _is_num(v):
                    raise _eval_err(f"cannot negate a {_type_name(v)}")
                return -v  # type: ignore[operator]
            case BinaryOp(op=op, left=left, right=right):
                return self._arith(op, self.value(left), self.value(right))
            case Not(operand=operand):
                v = self.value(operand)
                if not isinstance(v, bool):
                    raise _eval_err(f"'not' needs true or false, not a {_type_name(v)}")
                return not v
            case BoolOp(op=op, operands=operands):
                for operand in operands:
                    v = self.value(operand)
                    if not isinstance(v, bool):
                        raise _eval_err(f"'{op}' needs true or false, not a {_type_name(v)}")
                    if op == "and" and not v:
                        return False
                    if op == "or" and v:
                        return True
                return op == "and"
            case Compare(op=op, left=left, right=right):
                return self._compare(op, self.value(left), self.value(right))
            case Call(name=name, args=args):
                return self._call(name, args)
        raise _eval_err("unsupported expression")  # pragma: no cover - exhaustive

    def _arith(self, op: str, a: object, b: object) -> object:
        if op == "+" and isinstance(a, str) and isinstance(b, str):
            if len(a) + len(b) > self.limits.max_result_len:
                raise _eval_err("text result is too long")
            return a + b
        if not (_is_num(a) and _is_num(b)):
            raise _eval_err(f"cannot use {op!r} on a {_type_name(a)} and a {_type_name(b)}")
        x, y = a, b
        assert isinstance(x, int | float) and isinstance(y, int | float)
        if op in ("/", "%") and y == 0:
            raise _eval_err("division by zero")
        result: float | int
        if op == "+":
            result = x + y
        elif op == "-":
            result = x - y
        elif op == "*":
            if isinstance(x, int) and isinstance(y, int) and abs(x) * abs(y) > 10**30:
                raise _eval_err("number result is too large")
            result = x * y
        elif op == "/":
            result = x / y
        else:
            result = x % y
        if isinstance(result, float) and not math.isfinite(result):
            raise _eval_err("number result is not finite")
        return result

    def _compare(self, op: str, a: object, b: object) -> bool:
        if op == "==":
            return _json_eq(a, b)
        if op == "!=":
            return not _json_eq(a, b)
        if op in ("in", "not in"):
            if isinstance(b, list):
                found = any(_json_eq(a, x) for x in b)
            elif isinstance(b, str) and isinstance(a, str):
                found = a in b
            else:
                raise _eval_err(f"'in' needs a list or text on the right, not a {_type_name(b)}")
            return found if op == "in" else not found
        if (_is_num(a) and _is_num(b)) or (isinstance(a, str) and isinstance(b, str)):
            if op == "<":
                return a < b  # type: ignore[operator]
            if op == "<=":
                return a <= b  # type: ignore[operator]
            if op == ">":
                return a > b  # type: ignore[operator]
            return a >= b  # type: ignore[operator]
        raise _eval_err(f"cannot compare a {_type_name(a)} with a {_type_name(b)} using {op!r}")

    def _call(self, name: str, args: tuple[Node, ...]) -> object:
        if name == "exists":
            ref = args[0]
            assert isinstance(ref, Ref)
            v = self.lookup(ref)
            return v is not _MISSING and v is not None
        if name == "if":
            cond = self.value(args[0])
            if not isinstance(cond, bool):
                raise _eval_err(f"if() needs true or false first, not a {_type_name(cond)}")
            return self.value(args[1] if cond else args[2])
        if name == "coalesce":
            for a in args:
                v = self.eval(a)
                if v is not _MISSING and v is not None:
                    return v
            return None
        vals = [self.value(a) for a in args]
        if name == "abs":
            if not _is_num(vals[0]):
                raise _eval_err(f"abs() needs a number, not a {_type_name(vals[0])}")
            return abs(vals[0])  # type: ignore[arg-type]
        if name == "len":
            if not isinstance(vals[0], str | list | dict):
                raise _eval_err(f"len() needs text or a list, not a {_type_name(vals[0])}")
            return len(vals[0])
        if name == "round":
            x = vals[0]
            nd = vals[1] if len(vals) > 1 else 0
            if not _is_num(x) or not isinstance(nd, int) or isinstance(nd, bool):
                raise _eval_err("round() needs a number and a whole number of digits")
            if not 0 <= nd <= 15:
                raise _eval_err("round() keeps between 0 and 15 digits")
            return round(x, nd)  # type: ignore[call-overload]
        if name in ("any", "all"):
            seq = vals[0]
            if not isinstance(seq, list) or not all(isinstance(x, bool) for x in seq):
                raise _eval_err(f"{name}() needs a list of true/false values")
            return any(seq) if name == "any" else all(seq)
        # min / max
        items = vals[0] if len(vals) == 1 else vals
        if not isinstance(items, list) or not items:
            raise _eval_err(f"{name}() needs at least one value")
        if not (all(_is_num(x) for x in items) or all(isinstance(x, str) for x in items)):
            raise _eval_err(f"{name}() needs numbers or texts of one kind")
        pick: Callable[..., object] = min if name == "min" else max
        return pick(items)


# ==================================================================================== public


@dataclass(frozen=True, slots=True)
class Expression:
    """A parsed condition: its source text and AST. Build with :func:`parse_expression`."""

    source: str
    root: Node
    limits: ExpressionLimits = LIMITS

    def evaluate(self, env: Mapping[str, object]) -> Value:
        """Evaluate over ``env`` (``{"nodes": {...}, "params": {...}, ...}``, JSON data).

        Raises ``MapiError(EXPRESSION_ERROR)`` with a plain message on a type error, a missing
        reference or a blown limit."""
        out = _Evaluator(env, self.limits).value(self.root)
        return out  # type: ignore[return-value]

    def evaluate_bool(self, env: Mapping[str, object]) -> bool:
        """Evaluate a branch condition, which must give ``true`` or ``false``."""
        out = self.evaluate(env)
        if not isinstance(out, bool):
            raise _eval_err(f"the condition gave a {_type_name(out)}, not true or false")
        return out

    def references(self) -> list[Ref]:
        """Every reference in the condition, in order of appearance (for static checks)."""
        return list(_walk_refs(self.root))

    def node_refs(self) -> list[tuple[str, tuple[str | int, ...]]]:
        """``(node id, field path)`` of every ``nodes.<id>...`` reference."""
        out: list[tuple[str, tuple[str | int, ...]]] = []
        for r in self.references():
            if r.root == "nodes" and r.path and isinstance(r.path[0], str):
                out.append((r.path[0], r.path[1:]))
        return out


def _walk_refs(node: Node) -> Iterator[Ref]:
    match node:
        case Ref():
            yield node
        case ListExpr(items=items) | Call(args=items):
            for i in items:
                yield from _walk_refs(i)
        case Unary(operand=o) | Not(operand=o):
            yield from _walk_refs(o)
        case BinaryOp(left=a, right=b) | Compare(left=a, right=b):
            yield from _walk_refs(a)
            yield from _walk_refs(b)
        case BoolOp(operands=ops):
            for o in ops:
                yield from _walk_refs(o)
        case _:
            return


def parse_expression(text: str, *, limits: ExpressionLimits = LIMITS) -> Expression:
    """Parse a condition (raises ``MapiError(EXPRESSION_INVALID)`` with the position)."""
    tokens = _tokenize(text, limits)
    return Expression(text, _Parser(tokens, limits).parse(), limits)


def evaluate(text: str, env: Mapping[str, object]) -> Value:
    """Parse and evaluate ``text`` over ``env`` in one call."""
    return parse_expression(text).evaluate(env)


def evaluate_condition(text: str, env: Mapping[str, object]) -> bool:
    """Parse and evaluate a branch condition that must give ``true`` or ``false``."""
    return parse_expression(text).evaluate_bool(env)


def check_condition(text: str) -> str:
    """Pydantic validator: the text parses as a condition (``ValueError`` otherwise)."""
    try:
        parse_expression(text)
    except MapiError as exc:
        raise ValueError(exc.detail) from None
    return text


Condition = Annotated[
    str,
    Field(
        min_length=1,
        max_length=LIMITS.max_chars,
        examples=["nodes.adf.p_value < params.alpha and not nodes.kpss.reject"],
        description="A condition in the workflow expression language (see mapi_spec.expr)",
    ),
    AfterValidator(check_condition),
]
"""A branch condition (validated by parsing)."""
