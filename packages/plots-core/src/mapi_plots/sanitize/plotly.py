"""Plotly figure sanitizer: structure checks against the pinned plotly.js attribute tree.

A figure is ``{data, layout, frames, config}``. Every trace type, attribute name and value
type is checked against a compact table derived from the official ``plot-schema.json`` of the
pinned plotly.js release (see ``scripts/vendor_schemas.py``). On top of that:

* tile-map traces and subplots (``scattermap``, ``choroplethmap``, ``densitymap``,
  ``layout.map``; legacy ``*mapbox``) are rejected;
* every ``source`` string (``layout.images``, the ``image`` trace, relayout arguments in
  ``updatemenus``/``sliders``) must be a base64 raster ``data:`` URI;
* method ``args``/``args2`` are checked in both the ``{attr: value}`` and the positional
  ``[attr, value]`` form, including nested trace/frame objects, and any tile-map trace type
  given as a value is rejected;
* ``url``/``href``/``topojsonURL`` keys and network ``config`` options are rejected;
* text is stripped to ``<b> <i> <br> <sub> <sup>`` (plus ``<extra>`` in hover templates);
* data arrays may be inline lists, plotly.py 6 typed arrays (``{dtype, bdata, shape}``) or
  dataset references ``{"$ref": "dataset.column"}`` (see :data:`PLOTLY_REF_SCHEMA`).

Templates (``layout.template``) are styling only: unknown keys and tile-map sections inside
them are pruned (and reported as changes) instead of rejected.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
from collections.abc import Mapping
from functools import lru_cache
from importlib import resources
from typing import Any

from mapi_plots.sanitize._common import (
    SanitizeReport,
    SanitizeRule,
    child_pointer,
    is_safe_image_uri,
    path_segments,
    scan_string,
    walk,
)
from mapi_plots.sanitize.html import ALLOWED_TAGS, HOVERTEMPLATE_TAGS, strip_html

__all__ = [
    "PLOTLY_JS_VERSION",
    "PLOTLY_REF_SCHEMA",
    "TILE_MAP_TRACES",
    "TYPED_ARRAY_DTYPES",
    "is_plotly_ref",
    "parse_plotly_ref",
    "plotly_structure",
    "sanitize_plotly",
]

PLOTLY_JS_VERSION = "4.1.1"
_STRUCTURE_FILE = f"plotly-structure-v{PLOTLY_JS_VERSION}.json"

TILE_MAP_TRACES = frozenset(
    {
        "scattermap",
        "choroplethmap",
        "densitymap",
        "scattermapbox",
        "choroplethmapbox",
        "densitymapbox",
    }
)
_TILE_LAYOUT = frozenset({"map", "mapbox"})
#: plotly.py 6 typed-array dtypes and their item sizes.
TYPED_ARRAY_DTYPES: dict[str, int] = {
    "i1": 1,
    "u1": 1,
    "u1c": 1,
    "i2": 2,
    "u2": 2,
    "i4": 4,
    "u4": 4,
    "f4": 4,
    "f8": 8,
}
_DENY_CONFIG = frozenset(
    {
        "topojsonURL",
        "mapboxAccessToken",
        "plotlyServerURL",
        "showLink",
        "linkText",
        "sendData",
        "showSources",
        "showEditInChartStudio",
        "showSendToCloud",
        "typesetMath",
        "globalTransforms",
        "locales",
        "setBackground",
    }
)
_REF_PATTERN = r"^[A-Za-z_][A-Za-z0-9_\-]{0,63}\.[A-Za-z_][A-Za-z0-9_.\-]{0,127}$"
_REF_RE = re.compile(_REF_PATTERN)

#: JSON Schema (2020-12) of a Plotly dataset reference: ``{"$ref": "<dataset>.<column>"}``.
PLOTLY_REF_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:mapi:schema:plotly-dataset-ref:v1",
    "title": "PlotlyDatasetRef",
    "description": (
        "Binds a Plotly data-array attribute to a column of a named PlotSpec dataset. The part "
        "before the first '.' is the dataset name; the rest is the column name."
    ),
    "type": "object",
    "properties": {"$ref": {"type": "string", "pattern": _REF_PATTERN}},
    "required": ["$ref"],
    "additionalProperties": False,
}

_SUBPLOT_KEY = re.compile(r"^([a-z]+?)([2-9]|[1-9][0-9]+)$")
_DATA_TYPES = frozenset({"data_array"})


def is_plotly_ref(value: Any) -> bool:
    """True for a well-formed ``{"$ref": "dataset.column"}`` object."""
    return (
        isinstance(value, Mapping)
        and set(value) == {"$ref"}
        and isinstance(value["$ref"], str)
        and bool(_REF_RE.match(value["$ref"]))
    )


def parse_plotly_ref(value: Mapping[str, Any] | str) -> tuple[str, str]:
    """Split a ``$ref`` (object or string) into ``(dataset, column)``."""
    ref = value if isinstance(value, str) else value.get("$ref")
    if not isinstance(ref, str) or not _REF_RE.match(ref):
        raise ValueError(f"not a dataset reference: {ref!r}")
    dataset, _, column = ref.partition(".")
    return dataset, column


# ------------------------------------------------------------------------------ structure


class _Structure:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.version: str = raw["version"]
        self._table: list[Any] = raw["table"]
        self.traces: dict[str, Any] = raw["traces"]
        self.layout: dict[str, Any] = raw["layout"]
        self.config: dict[str, Any] = raw["config"]
        self.frames: dict[str, Any] = raw["frames"]

    def deref(self, node: Any) -> Any:
        while isinstance(node, dict) and len(node) == 1 and "@" in node:
            node = self._table[node["@"]]
        return node

    def trace(self, kind: str) -> dict[str, Any] | None:
        node = self.traces.get(kind)
        return None if node is None else self.deref(node)


@lru_cache(maxsize=1)
def plotly_structure() -> _Structure:
    """The vendored plotly.js attribute tree."""
    text = (
        resources.files("mapi_plots.sanitize.schemas").joinpath(_STRUCTURE_FILE).read_text("utf-8")
    )
    return _Structure(json.loads(text))


@lru_cache(maxsize=512)
def _enum_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern[1:-1])


# ----------------------------------------------------------------------------- validation


class _Checker:
    def __init__(self, report: SanitizeReport) -> None:
        self.report = report
        self.s = plotly_structure()

    # -- helpers -----------------------------------------------------------------------
    def bad(self, ptr: str, message: str, untrusted: str | None = None) -> None:
        self.report.error(SanitizeRule.SCHEMA, ptr, message, untrusted=untrusted)

    def prune(self, parent: dict[str, Any], key: str, ptr: str, why: str) -> None:
        parent.pop(key, None)
        self.report.change(SanitizeRule.TEMPLATE_PRUNED, ptr, f"removed from template: {why}")

    # -- traces --------------------------------------------------------------------------
    def traces(self, traces: Any, ptr: str, *, template: bool = False) -> None:
        if not isinstance(traces, list):
            self.bad(ptr, "data must be a list of traces")
            return
        for i, trace in enumerate(traces):
            self.trace(trace, child_pointer(ptr, i), None, template=template)

    def trace(self, trace: Any, ptr: str, kind: str | None, *, template: bool) -> None:
        if not isinstance(trace, dict):
            self.bad(ptr, "a trace must be an object")
            return
        kind = trace.get("type", kind or "scatter")
        if not isinstance(kind, str):
            self.bad(child_pointer(ptr, "type"), "trace type must be a string")
            return
        if kind in TILE_MAP_TRACES:
            self.report.error(
                SanitizeRule.TILE_MAP,
                child_pointer(ptr, "type"),
                f"tile-map traces ({kind}) are not allowed; use scattergeo/choropleth",
            )
            return
        node = self.s.trace(kind)
        if node is None:
            self.bad(child_pointer(ptr, "type"), "unknown trace type", untrusted=kind[:100])
            return
        self.obj(trace, node, ptr, template=template)

    # -- objects -------------------------------------------------------------------------
    def child(self, node: dict[str, Any], key: str, template: bool) -> Any:
        keys: dict[str, Any] = node["k"]
        if key in keys:
            return self.s.deref(keys[key])
        m = _SUBPLOT_KEY.match(key)
        if m and m.group(1) in keys:
            sub = self.s.deref(keys[m.group(1)])
            if isinstance(sub, dict) and sub.get("s"):
                return sub
        if template and key.endswith("defaults"):
            item = key[: -len("defaults")]
            for v in keys.values():
                v = self.s.deref(v)
                if isinstance(v, dict) and v.get("arr") == item:
                    return {"k": v["k"]}
        return None

    def obj(self, value: Any, node: dict[str, Any], ptr: str, *, template: bool) -> None:
        if self.report.full:
            return
        if not isinstance(value, dict):
            self.bad(ptr, "expected an object")
            return
        for key in list(value):
            cptr = child_pointer(ptr, key)
            sub = self.child(node, key, template)
            if sub is None:
                if template:
                    self.prune(value, key, cptr, "unknown attribute")
                else:
                    self.bad(cptr, "unknown attribute", untrusted=key[:100])
                continue
            self.value(value[key], sub, cptr, key, template=template)

    def value(self, v: Any, node: dict[str, Any], ptr: str, key: str, *, template: bool) -> None:
        if "t" in node:
            self.leaf(v, node, ptr)
        elif "arr" in node:
            if v is None:
                return
            if not isinstance(v, list):
                self.bad(ptr, f"{key} must be a list of objects")
                return
            item = {"k": node["k"]}
            for i, it in enumerate(v):
                self.obj(it, item, child_pointer(ptr, i), template=template)
        elif v is not None:
            self.obj(v, node, ptr, template=template)

    # -- leaves --------------------------------------------------------------------------
    def leaf(self, v: Any, leaf: dict[str, Any], ptr: str) -> None:
        if v is None:
            return
        t: str = leaf["t"]
        data_ok = t in _DATA_TYPES or bool(leaf.get("a"))
        if isinstance(v, dict):
            if "$ref" in v:
                if not data_ok:
                    self.report.error(
                        SanitizeRule.DATASET_REF, ptr, "$ref is only allowed in data arrays"
                    )
                elif not is_plotly_ref(v):
                    self.report.error(
                        SanitizeRule.DATASET_REF,
                        ptr,
                        'a dataset reference must be {"$ref": "<dataset>.<column>"}',
                        untrusted=str(v.get("$ref"))[:200],
                    )
                return
            if "bdata" in v:
                if not data_ok and t != "info_array":
                    self.report.error(
                        SanitizeRule.TYPED_ARRAY, ptr, "typed arrays are only allowed in data"
                    )
                else:
                    _check_typed_array(v, ptr, self.report)
                return
            if t != "any":
                self.bad(ptr, f"expected a {t} value, not an object")
            return
        if isinstance(v, list):
            if t in ("data_array", "info_array", "colorlist", "colorscale", "any"):
                if t == "colorscale":
                    self.colorscale(v, ptr)
                return
            if leaf.get("a"):
                for i, item in enumerate(v):
                    if isinstance(item, list | dict):
                        self.bad(child_pointer(ptr, i), f"expected a {t} value per item")
                        return
                    self.scalar(item, leaf, child_pointer(ptr, i))
                return
            self.bad(ptr, f"expected a {t} value, not a list")
            return
        self.scalar(v, leaf, ptr)

    def colorscale(self, v: list[Any], ptr: str) -> None:
        for i, stop in enumerate(v):
            if not (
                isinstance(stop, list)
                and len(stop) == 2
                and isinstance(stop[0], int | float)
                and not isinstance(stop[0], bool)
                and isinstance(stop[1], str)
            ):
                self.bad(child_pointer(ptr, i), "colorscale stops are [number, color] pairs")
                return

    def scalar(self, v: Any, leaf: dict[str, Any], ptr: str) -> None:
        if v is None:
            return
        t = leaf["t"]
        num = isinstance(v, int | float) and not isinstance(v, bool)
        ok = True
        if t in ("number", "angle"):
            ok = num or (isinstance(v, str) and _is_numeric(v)) or (t == "angle" and v == "auto")
        elif t == "integer":
            ok = (isinstance(v, int) and not isinstance(v, bool)) or (
                isinstance(v, float) and v.is_integer()
            )
        elif t == "boolean":
            ok = isinstance(v, bool)
        elif t in ("string", "color"):
            ok = isinstance(v, str) or num
        elif t == "enumerated":
            ok = _enum_ok(v, leaf.get("v", []))
        elif t == "flaglist":
            ok = _flags_ok(v, leaf.get("f", []), leaf.get("x", []))
        elif t == "subplotid":
            if isinstance(leaf.get("r"), str):
                ok = isinstance(v, str) and bool(_enum_regex(leaf["r"]).match(v))
            else:
                dflt = leaf.get("d") or ""
                ok = isinstance(v, str) and bool(re.match(rf"^{re.escape(dflt)}\d*$", v))
        elif t == "colorscale":
            ok = isinstance(v, str)
        elif t in ("data_array", "info_array", "colorlist"):
            ok = False
        if not ok:
            self.bad(ptr, f"invalid {t} value", untrusted=repr(v)[:200])

    # -- layout --------------------------------------------------------------------------
    def layout(self, layout: Any, ptr: str, *, template: bool = False) -> None:
        if not isinstance(layout, dict):
            self.bad(ptr, "layout must be an object")
            return
        for key in list(layout):
            if _is_tile_key(key):
                if template:
                    self.prune(layout, key, child_pointer(ptr, key), "tile-map styling")
                else:
                    self.report.error(
                        SanitizeRule.TILE_MAP,
                        child_pointer(ptr, key),
                        "tile-map subplots are not allowed",
                    )
        tmpl = layout.get("template")
        rest = {k: v for k, v in layout.items() if k != "template" and not _is_tile_key(k)}
        self.obj(rest, self.s.layout, ptr, template=template)
        for k in list(layout):  # reflect pruning done on the shallow copy
            if k != "template" and not _is_tile_key(k) and k not in rest:
                layout.pop(k)
        if tmpl is not None:
            self.template(tmpl, child_pointer(ptr, "template"))

    def template(self, tmpl: Any, ptr: str) -> None:
        if isinstance(tmpl, str):
            self.bad(ptr, "named templates are not supported; inline the template object")
            return
        if not isinstance(tmpl, dict):
            self.bad(ptr, "template must be an object")
            return
        for key in list(tmpl):
            if key not in ("data", "layout"):
                self.prune(tmpl, key, child_pointer(ptr, key), "unknown template section")
        data = tmpl.get("data")
        if data is not None:
            dptr = child_pointer(ptr, "data")
            if not isinstance(data, dict):
                self.bad(dptr, "template data must map trace types to trace lists")
            else:
                for kind in list(data):
                    kptr = child_pointer(dptr, kind)
                    if kind in TILE_MAP_TRACES or self.s.trace(kind) is None:
                        self.prune(data, kind, kptr, "tile-map or unknown trace type")
                        continue
                    if not isinstance(data[kind], list):
                        self.bad(kptr, "template traces must be a list")
                        continue
                    for i, tr in enumerate(data[kind]):
                        self.trace(tr, child_pointer(kptr, i), kind, template=True)
        if tmpl.get("layout") is not None:
            self.layout(tmpl["layout"], child_pointer(ptr, "layout"), template=True)

    # -- frames and config -----------------------------------------------------------------
    def frames(self, frames: Any, ptr: str) -> None:
        if not isinstance(frames, list):
            self.bad(ptr, "frames must be a list")
            return
        allowed = {"name", "group", "baseframe", "traces", "data", "layout"}
        for i, fr in enumerate(frames):
            fptr = child_pointer(ptr, i)
            if not isinstance(fr, dict):
                self.bad(fptr, "a frame must be an object")
                continue
            for key in fr:
                if key not in allowed:
                    self.bad(child_pointer(fptr, key), "unknown frame attribute")
            if "data" in fr:
                self.traces(fr["data"], child_pointer(fptr, "data"))
            if "layout" in fr:
                self.layout(fr["layout"], child_pointer(fptr, "layout"))

    def config(self, config: Any, ptr: str) -> None:
        if not isinstance(config, dict):
            self.bad(ptr, "config must be an object")
            return
        safe = {}
        for key, v in config.items():
            if key in _DENY_CONFIG:
                self.report.error(
                    SanitizeRule.CONFIG,
                    child_pointer(ptr, key),
                    f"config option {key} is not allowed (network, cloud or script access)",
                )
            else:
                safe[key] = v
        self.obj(safe, self.s.config, ptr, template=False)


def _is_tile_key(key: str) -> bool:
    m = _SUBPLOT_KEY.match(key)
    return (m.group(1) if m else key) in _TILE_LAYOUT


def _is_numeric(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return text.strip().lower() not in ("nan", "inf", "-inf", "+inf", "infinity", "-infinity")


def _same_scalar(a: Any, b: Any) -> bool:
    """Equality that does not conflate booleans with 0/1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return bool(a == b)


def _enum_ok(v: Any, values: list[Any]) -> bool:
    if any(_same_scalar(v, allowed) for allowed in values):
        return True
    if isinstance(v, str):
        for allowed in values:
            if (
                isinstance(allowed, str)
                and len(allowed) > 2
                and allowed.startswith("/")
                and allowed.endswith("/")
                and _enum_regex(allowed).match(v)
            ):
                return True
    return False


def _flags_ok(v: Any, flags: list[str], extras: list[Any]) -> bool:
    if v in extras:
        return True
    if not isinstance(v, str) or not v:
        return False
    return all(part in flags for part in v.split("+"))


def _check_typed_array(v: dict[str, Any], ptr: str, report: SanitizeReport) -> None:
    extra = set(v) - {"dtype", "bdata", "shape"}
    dtype = v.get("dtype")
    if extra or dtype not in TYPED_ARRAY_DTYPES or not isinstance(v.get("bdata"), str):
        report.error(
            SanitizeRule.TYPED_ARRAY,
            ptr,
            "typed arrays are {dtype, bdata, shape?} with dtype one of "
            + ", ".join(sorted(TYPED_ARRAY_DTYPES)),
        )
        return
    text: str = v["bdata"]
    if len(text) * 3 // 4 > report.limits.max_typed_array_bytes:
        report.error(SanitizeRule.TOO_LARGE, ptr, "typed array exceeds the size limit")
        return
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        report.error(SanitizeRule.TYPED_ARRAY, ptr, "bdata is not valid base64")
        return
    size = TYPED_ARRAY_DTYPES[str(dtype)]
    if len(raw) % size:
        report.error(SanitizeRule.TYPED_ARRAY, ptr, "bdata length is not a multiple of dtype")
        return
    shape = v.get("shape")
    if shape is not None:
        dims = _parse_shape(shape)
        if dims is None:
            report.error(SanitizeRule.TYPED_ARRAY, ptr, "shape must be 'm, n' or [m, n]")
            return
        total = 1
        for d in dims:
            total *= d
        if total != len(raw) // size:
            report.error(SanitizeRule.TYPED_ARRAY, ptr, "shape does not match the data length")


def _parse_shape(shape: Any) -> list[int] | None:
    parts: list[Any]
    if isinstance(shape, str):
        parts = [p.strip() for p in shape.split(",")]
        if not all(p.isdigit() for p in parts):
            return None
        parts = [int(p) for p in parts]
    elif isinstance(shape, list):
        parts = shape
    else:
        return None
    if not 1 <= len(parts) <= 3 or not all(
        isinstance(p, int) and not isinstance(p, bool) and p >= 0 for p in parts
    ):
        return None
    return list(parts)


# ---------------------------------------------------------------------- generic deep scan


def _scan(fig: dict[str, Any], base: str, report: SanitizeReport) -> None:
    for node, ptr, _key in walk(fig, base):
        if report.full:
            return
        if isinstance(node, str):
            scan_string(node, ptr, report)
            continue
        if not isinstance(node, dict):
            continue
        for k, v in node.items():
            segs = set(path_segments(k))
            cptr = child_pointer(ptr, k)
            if "source" in segs and _has_unsafe_source(v):
                report.error(
                    SanitizeRule.EXTERNAL_IMAGE,
                    cptr,
                    "image sources must be base64 raster data: URIs (png, jpeg, gif, webp)",
                )
            if segs & {"url", "topojsonURL"}:
                report.error(SanitizeRule.URL_DATA, cptr, "URLs are not allowed in plots")
            if "href" in segs:
                report.error(SanitizeRule.HREF, cptr, "href links are not allowed in plots")
            if segs & _TILE_LAYOUT and "." in k:
                report.error(SanitizeRule.TILE_MAP, cptr, "tile-map subplots are not allowed")
            if k in _ARGS_KEYS and isinstance(v, list):
                _scan_args(v, cptr, report)


#: ``updatemenus[].buttons[]`` / ``sliders[].steps[]`` method arguments. Their schema type is
#: ``info_array`` (contents unchecked), and Plotly accepts the positional ``[attr, value]``
#: form besides ``{attr: value}`` objects and whole trace/frame objects (``animate``).
_ARGS_KEYS = frozenset({"args", "args2"})


def _scan_args(node: Any, ptr: str, report: SanitizeReport) -> None:
    """Apply the source/url/href/tile-map rules inside method arguments.

    Dict keys are already covered by :func:`_scan` (which walks every node); this adds the
    positional ``[attr, value, ...]`` form, bare ``map``/``mapbox`` keys and any tile-map
    trace type given as a value (``["type", "scattermap"]``, ``{"type": [...]}``).
    """
    if report.full:
        return
    if isinstance(node, str):
        if node in TILE_MAP_TRACES:
            report.error(
                SanitizeRule.TILE_MAP,
                ptr,
                f"tile-map traces ({node}) are not allowed; use scattergeo/choropleth",
            )
        return
    if isinstance(node, dict):
        for k, v in node.items():
            cptr = child_pointer(ptr, k)
            segs = path_segments(k)
            seen_by_scan = bool(set(segs) & _TILE_LAYOUT) and "." in k
            if not seen_by_scan and any(_is_tile_key(s) for s in segs):
                report.error(SanitizeRule.TILE_MAP, cptr, "tile-map subplots are not allowed")
            _scan_args(v, cptr, report)
        return
    if not isinstance(node, list):
        return
    if len(node) >= 2 and isinstance(node[0], str):
        _check_attr_pair(node[0], node[1], child_pointer(ptr, 0), report)
    for i, item in enumerate(node):
        _scan_args(item, child_pointer(ptr, i), report)


def _check_attr_pair(attr: str, value: Any, ptr: str, report: SanitizeReport) -> None:
    """The key rules of :func:`_scan` for a positional ``[attr, value]`` argument pair."""
    segs = path_segments(attr)
    if "source" in segs and _has_unsafe_source(value):
        report.error(
            SanitizeRule.EXTERNAL_IMAGE,
            ptr,
            "image sources must be base64 raster data: URIs (png, jpeg, gif, webp)",
        )
    if set(segs) & {"url", "topojsonURL"}:
        report.error(SanitizeRule.URL_DATA, ptr, "URLs are not allowed in plots")
    if "href" in segs:
        report.error(SanitizeRule.HREF, ptr, "href links are not allowed in plots")
    if any(_is_tile_key(s) for s in segs):
        report.error(SanitizeRule.TILE_MAP, ptr, "tile-map subplots are not allowed")


def _has_unsafe_source(v: Any) -> bool:
    if isinstance(v, str):
        return not is_safe_image_uri(v)
    if isinstance(v, list):
        return any(isinstance(x, str) and not is_safe_image_uri(x) for x in v)
    return False


def _strip(node: Any, ptr: str, key: str | None, report: SanitizeReport) -> Any:
    if isinstance(node, str):
        allowed = HOVERTEMPLATE_TAGS if key and key.startswith("hovertemplate") else ALLOWED_TAGS
        out = strip_html(node, allowed)
        if out != node:
            report.change(SanitizeRule.HTML_STRIPPED, ptr, "removed HTML outside the allowed tags")
        return out
    if isinstance(node, dict):
        if "$ref" in node or "bdata" in node:
            return node
        for k in list(node):
            node[k] = _strip(node[k], child_pointer(ptr, k), k, report)
        return node
    if isinstance(node, list):
        for i, item in enumerate(node):
            node[i] = _strip(item, child_pointer(ptr, i), key, report)
        return node
    return node


def sanitize_plotly(
    spec: dict[str, Any],
    report: SanitizeReport,
    *,
    base: str = "/spec",
    validate_schema: bool = True,
) -> dict[str, Any]:
    """Check a Plotly figure; returns a sanitized copy. Errors go to ``report``."""
    out = copy.deepcopy(spec)
    for key in out:
        if key not in ("data", "layout", "frames", "config"):
            report.error(
                SanitizeRule.SCHEMA,
                child_pointer(base, key),
                "a Plotly figure has only data, layout, frames and config",
            )
    _scan(out, base, report)
    checker = _Checker(report)
    if "data" in out:
        if validate_schema:
            checker.traces(out["data"], child_pointer(base, "data"))
        else:
            _tile_traces_only(out["data"], child_pointer(base, "data"), report)
    if "layout" in out and validate_schema:
        checker.layout(out["layout"], child_pointer(base, "layout"))
    elif isinstance(out.get("layout"), dict):
        for key in out["layout"]:
            if _is_tile_key(key):
                report.error(
                    SanitizeRule.TILE_MAP,
                    child_pointer(child_pointer(base, "layout"), key),
                    "tile-map subplots are not allowed",
                )
    if "frames" in out and validate_schema:
        checker.frames(out["frames"], child_pointer(base, "frames"))
    if "config" in out:
        checker.config(out["config"], child_pointer(base, "config"))
    stripped: dict[str, Any] = _strip(out, base, None, report)
    return stripped


def _tile_traces_only(traces: Any, ptr: str, report: SanitizeReport) -> None:
    if isinstance(traces, list):
        for i, tr in enumerate(traces):
            if isinstance(tr, dict) and tr.get("type") in TILE_MAP_TRACES:
                report.error(
                    SanitizeRule.TILE_MAP,
                    child_pointer(child_pointer(ptr, i), "type"),
                    "tile-map traces are not allowed",
                )
