"""A conservative static checker for Vega expression strings.

Vega-Lite specs carry expressions in ``calculate``/``filter`` transforms, ``test`` conditions,
``expr`` references, ``labelExpr`` and bracketed event-stream filters. The renderer evaluates
them with ``vega-interpreter`` (no ``eval``), which is the real boundary; this checker is
defence in depth and rejects anything outside a small, boring subset:

* calls only to an allow-list of Vega expression functions, never method calls;
* no prototype-reaching names (``constructor``, ``__proto__`` ...) anywhere, including inside
  string literals, and no browser globals (``window``, ``document``, ``fetch`` ...);
* no ``this``/``new``, assignment, statements, template literals, regex literals, identifier
  escapes or non-ASCII outside string literals, and no ``\\u``/``\\x`` escapes in strings.
"""

from __future__ import annotations

import re

__all__ = ["VEGA_FUNCTIONS", "check_expression"]

#: Vega (v5/v6) expression functions considered safe in user-authored specs.
VEGA_FUNCTIONS = frozenset(
    {
        # type checking and coercion
        "isArray", "isBoolean", "isDate", "isDefined", "isNumber", "isObject", "isRegExp",
        "isString", "isValid", "toBoolean", "toDate", "toNumber", "toString",
        # control flow
        "if",
        # math
        "isNaN", "isFinite", "abs", "acos", "asin", "atan", "atan2", "ceil", "clamp", "cos",
        "exp", "exp2", "floor", "hypot", "log", "max", "min", "pow", "random", "round", "sin",
        "sqrt", "tan",
        # statistics
        "sampleNormal", "cumulativeNormal", "densityNormal", "quantileNormal",
        "sampleLogNormal", "cumulativeLogNormal", "densityLogNormal", "quantileLogNormal",
        "sampleUniform", "cumulativeUniform", "densityUniform", "quantileUniform",
        # dates
        "now", "datetime", "date", "day", "dayofyear", "year", "quarter", "month", "week",
        "hours", "minutes", "seconds", "milliseconds", "time", "timezoneoffset", "timeOffset",
        "timeSequence", "utc", "utcdate", "utcday", "utcdayofyear", "utcyear", "utcquarter",
        "utcmonth", "utcweek", "utchours", "utcminutes", "utcseconds", "utcmilliseconds",
        "utcOffset", "utcSequence",
        # arrays
        "extent", "clampRange", "indexof", "inrange", "join", "lastindexof", "length", "lerp",
        "peek", "pluck", "reverse", "sequence", "slice", "sort", "span",
        # strings
        "lower", "pad", "parseFloat", "parseInt", "replace", "split", "substring", "trim",
        "truncate", "upper",
        # objects
        "merge",
        # formatting
        "dayFormat", "dayAbbrevFormat", "format", "monthFormat", "monthAbbrevFormat",
        "timeUnitSpecifier", "timeFormat", "timeParse", "utcFormat", "utcParse",
        # regular expressions
        "regexp", "test",
        # colours
        "rgb", "hsl", "lab", "hcl", "luminance", "contrast",
        # scenegraph / data / scales (read-only)
        "item", "group", "xy", "x", "y", "inScope", "data", "indata", "scale", "invert",
        "domain", "range", "bandwidth", "bandspace", "gradient", "panLinear", "panLog",
        "panPow", "panSymlog", "zoomLinear", "zoomLog", "zoomPow", "zoomSymlog",
        "geoArea", "geoBounds", "geoCentroid", "geoScale",
        "treePath", "treeAncestors",
    }
)  # fmt: skip

#: Names rejected anywhere (also as members and inside string literals).
_DENY_ANYWHERE = frozenset(
    {
        "constructor", "prototype", "__proto__", "__defineGetter__", "__defineSetter__",
        "__lookupGetter__", "__lookupSetter__", "eval", "Function", "import", "fetch",
        "XMLHttpRequest", "WebSocket", "innerHTML", "outerHTML", "srcdoc", "cookie",
        "localStorage", "sessionStorage", "indexedDB", "postMessage", "document", "window",
        "globalThis", "javascript", "setTimeout", "setInterval", "importScripts",
    }
)  # fmt: skip
#: Names rejected when used as a free (global) identifier.
_DENY_GLOBAL = frozenset({"this", "new", "self", "top", "frames", "opener", "location",
                          "navigator", "arguments", "require", "process"})  # fmt: skip

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|0[xX][0-9a-fA-F]+)
  | (?P<id>[A-Za-z_$][A-Za-z0-9_$]*)
  | (?P<op>===|!==|==|!=|<=|>=|&&|\|\||\*\*|[-+*/%<>!?:.,()\[\]{}&|^~])
  | (?P<bad>.)
    """,
    re.X | re.S,
)
_STR_ESCAPE = re.compile(r"\\[ux]", re.I)
_WORD = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def check_expression(expr: str, *, max_chars: int = 2000) -> list[str]:
    """Problems found in a Vega expression (empty list when it is acceptable)."""
    if len(expr) > max_chars:
        return [f"expression longer than {max_chars} characters"]
    problems: list[str] = []
    tokens: list[tuple[str, str]] = []
    for m in _TOKEN.finditer(expr):
        kind = m.lastgroup or "bad"
        text = m.group(0)
        if kind == "ws":
            continue
        if kind == "bad":
            if text == "`":
                problems.append("template literals are not allowed")
            elif text == "\\":
                problems.append("escapes outside string literals are not allowed")
            elif text == ";":
                problems.append("statements are not allowed")
            elif text == "=":
                problems.append("assignment is not allowed")
            elif ord(text) > 127:
                problems.append("non-ASCII characters are only allowed inside strings")
            else:
                problems.append(f"unexpected character {text!r}")
            continue
        tokens.append((kind, text))
    for i, (kind, text) in enumerate(tokens):
        prev = tokens[i - 1][1] if i else ""
        nxt = tokens[i + 1][1] if i + 1 < len(tokens) else ""
        if kind == "str":
            if _STR_ESCAPE.search(text):
                problems.append("\\u and \\x escapes are not allowed in strings")
            for word in _WORD.findall(text[1:-1]):
                if word in _DENY_ANYWHERE:
                    problems.append(f"{word!r} is not allowed")
        elif kind == "id":
            member = prev == "."
            if text in _DENY_ANYWHERE or (not member and text in _DENY_GLOBAL):
                problems.append(f"{text!r} is not allowed")
            elif member and text == "view" and i >= 2 and tokens[i - 2][1] == "event":
                problems.append("'event.view' is not allowed")
            if nxt == "(":
                if member:
                    problems.append(f"method calls ({text}(...)) are not allowed")
                elif text not in VEGA_FUNCTIONS:
                    problems.append(f"function {text!r} is not on the allow-list")
        elif kind == "op" and text == "(" and prev in (")", "]"):
            problems.append("calling the result of an expression is not allowed")
        elif kind == "op" and text == "[" and i and (tokens[i - 1][0] == "id" or prev in ")]"):
            # computed member access: only a literal key, so names cannot be assembled
            key_kind = tokens[i + 1][0] if i + 1 < len(tokens) else ""
            close = tokens[i + 2][1] if i + 2 < len(tokens) else ""
            if key_kind not in ("str", "num") or close != "]":
                problems.append("computed member access needs a string or number literal")
            elif prev == "event" and tokens[i + 1][1][1:-1] == "view":
                problems.append("'event.view' is not allowed")
        elif kind == "op" and text == "/" and prev in ("", "(", ",", "=", ":", "?", "[", "!"):
            problems.append("regular expression literals are not allowed")
    # stable, de-duplicated order
    return list(dict.fromkeys(problems))
