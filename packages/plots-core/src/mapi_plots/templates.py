"""Role/dim-bound plot templates (plan §10.1).

A template is an ordinary :class:`~mapi_spec.plots.PlotSpec` whose ``bindings`` say *what* to
plot in schema terms (``{x: "dim:time", y: "role:target", facet: "dim:geo"}``) instead of
naming one bundle's variables. :func:`apply_template` resolves the bindings against a
:class:`~mapi_spec.schema.DataSchema` and fills the placeholders in the spec, so one
user-authored "fan chart of the target over time, faceted by geo" applies to every model of a
family. This module is JSON-only (it runs in the control plane).

Placeholders inside ``spec``:

* ``{"$bind": "y"}`` - replaced by a Vega-Lite field definition
  ``{"field": ..., "type": ..., "title": ...}``; dropped entirely when the channel is optional
  and unresolved (so ``encoding.facet`` simply disappears for a model without ``geo``);
* ``"{{y}}"`` - the bound name; ``"{{y.field}}"`` - the name escaped for Vega-Lite field
  access (dots); ``"{{y.type}}"`` - the Vega-Lite type; ``"{{y.title}}"`` - name plus units.
  Placeholders may be embedded in longer strings (titles, captions).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from mapi_spec.errors import ErrorCode, Issue, MapiError, json_pointer
from mapi_spec.plots import PlotBindings, PlotSpec
from mapi_spec.schema import DataSchema, DimSpec, Role, VarSpec

__all__ = [
    "BoundField",
    "TemplateError",
    "apply_template",
    "resolve_bindings",
    "template_issues",
    "vl_field",
]

VlType = Literal["quantitative", "temporal", "nominal", "ordinal"]
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_0-9]+)(?:\.(field|type|title))?\s*\}\}")


class TemplateError(MapiError):
    """A template's bindings do not resolve against a schema."""

    default_code = ErrorCode.SCHEMA_INVALID


@dataclass(frozen=True, slots=True)
class BoundField:
    """One binding resolved to a concrete dataset column."""

    channel: str
    expr: str
    kind: Literal["dim", "var", "coord", "stat"]
    name: str
    vl_type: VlType
    units: str | None = None

    @property
    def field(self) -> str:
        """The name escaped for Vega-Lite field access (``.`` is nested access otherwise)."""
        return vl_field(self.name)

    @property
    def title(self) -> str:
        return f"{self.name} ({self.units})" if self.units else self.name

    def encoding(self) -> dict[str, Any]:
        return {"field": self.field, "type": self.vl_type, "title": self.title}


def vl_field(name: str) -> str:
    """Escape a column name for a Vega-Lite ``field``."""
    return name.replace("\\", "\\\\").replace(".", "\\.").replace("[", "\\[").replace("]", "\\]")


def _dim_type(d: DimSpec) -> VlType:
    if d.kind == "time":
        return "temporal"
    if d.kind in ("integer", "float"):
        return "quantitative"
    if d.kind == "index" or d.ordered:
        return "ordinal"
    return "nominal"


def _var_type(v: VarSpec) -> VlType:
    if v.dtype.startswith("datetime"):
        return "temporal"
    if v.dtype in ("str", "bool"):
        return "nominal"
    return "quantitative"


def _resolve_one(channel: str, expr: str, schema: DataSchema, multi: bool) -> list[BoundField]:
    kind, _, name = expr.partition(":")
    if kind == "dim":
        if name not in schema.dims:
            raise TemplateError(
                f"binding {channel}={expr!r}: the schema has no dim {name!r}",
                pointer=json_pointer("bindings", channel),
            )
        return [BoundField(channel, expr, "dim", name, _dim_type(schema.dims[name]))]
    if kind == "var":
        v = schema.vars.get(name)
        if v is None:
            raise TemplateError(
                f"binding {channel}={expr!r}: the schema has no variable {name!r}",
                pointer=json_pointer("bindings", channel),
            )
        return [BoundField(channel, expr, "var", name, _var_type(v), v.units)]
    if kind == "role":
        names = schema.vars_with_role(Role(name))
        if not names:
            raise TemplateError(
                f"binding {channel}={expr!r}: no variable has role {name!r}",
                pointer=json_pointer("bindings", channel),
            )
        if len(names) > 1 and not multi:
            raise TemplateError(
                f"binding {channel}={expr!r} is ambiguous ({len(names)} variables have role "
                f"{name!r}); bind with var:<name> instead",
                pointer=json_pointer("bindings", channel),
            )
        return [
            BoundField(channel, expr, "var", n, _var_type(schema.vars[n]), schema.vars[n].units)
            for n in names
        ]
    if kind == "coord":
        dim = schema.dims.get(name)
        vl: VlType = _dim_type(dim) if dim is not None else "nominal"
        return [BoundField(channel, expr, "coord", name, vl)]
    # stat:<name> - a summary statistic column (mean, hdi_90_lower, q5 ...)
    return [BoundField(channel, expr, "stat", name, "quantitative")]


def resolve_bindings(
    bindings: PlotBindings,
    schema: DataSchema,
    *,
    optional: Iterable[str] = (),
) -> dict[str, BoundField | list[BoundField]]:
    """Resolve every binding; tooltips resolve to lists. Optional channels may be missing."""
    opt = set(optional)
    out: dict[str, BoundField | list[BoundField]] = {}
    for channel, expr in bindings:
        if channel == "tooltip":
            fields: list[BoundField] = []
            for e in expr:
                fields += _resolve_one("tooltip", e, schema, multi=True)
            out["tooltip"] = fields
            continue
        if expr is None:
            continue
        try:
            out[channel] = _resolve_one(channel, expr, schema, multi=False)[0]
        except TemplateError:
            if channel not in opt:
                raise
    return out


_DROP = object()


def _fill(node: Any, resolved: Mapping[str, Any], optional: set[str], ptr: str) -> Any:
    if isinstance(node, dict):
        if set(node) == {"$bind"}:
            channel = node["$bind"]
            bf = resolved.get(channel)
            if isinstance(bf, BoundField):
                return bf.encoding()
            if isinstance(bf, list):
                return [b.encoding() for b in bf]
            if channel in optional:
                return _DROP
            raise TemplateError(f"placeholder for unbound channel {channel!r}", pointer=ptr)
        out: dict[str, Any] = {}
        for k, v in node.items():
            filled = _fill(v, resolved, optional, ptr + json_pointer(k))
            if filled is not _DROP:
                out[k] = filled
        return out
    if isinstance(node, list):
        items = [_fill(v, resolved, optional, ptr + json_pointer(i)) for i, v in enumerate(node)]
        return [v for v in items if v is not _DROP]
    if isinstance(node, str) and "{{" in node:

        def repl(m: re.Match[str]) -> str:
            channel, attr = m.group(1), m.group(2)
            bf = resolved.get(channel)
            if isinstance(bf, list):
                bf = bf[0] if bf else None
            if bf is None:
                if channel in optional:
                    return ""
                raise TemplateError(f"placeholder for unbound channel {channel!r}", pointer=ptr)
            assert isinstance(bf, BoundField)
            if attr == "field":
                return bf.field
            if attr == "type":
                return bf.vl_type
            if attr == "title":
                return bf.title
            return bf.name

        return _PLACEHOLDER.sub(repl, node)
    return node


def apply_template(
    plot: PlotSpec,
    schema: DataSchema,
    *,
    optional: Iterable[str] = (),
    title: str | None = None,
) -> PlotSpec:
    """Resolve ``plot.bindings`` against ``schema`` and fill the spec's placeholders."""
    if plot.bindings is None:
        raise TemplateError("the plot has no bindings; it is not a template")
    opt = set(optional)
    resolved = resolve_bindings(plot.bindings, schema, optional=opt)
    spec = _fill(plot.spec, resolved, opt, "/spec")
    caption = plot.caption_plain
    if caption is not None:
        caption = _fill(caption, resolved, opt, "/caption_plain")
    new_title = _fill(title or plot.title, resolved, opt, "/title")
    return plot.model_copy(update={"spec": spec, "caption_plain": caption, "title": new_title})


def template_issues(
    plot: PlotSpec, schema: DataSchema, *, optional: Iterable[str] = ()
) -> list[Issue]:
    """Why a template does not apply to ``schema`` (empty list when it does)."""
    try:
        apply_template(plot, schema, optional=optional)
    except TemplateError as exc:
        return [exc.to_issue()]
    return []
