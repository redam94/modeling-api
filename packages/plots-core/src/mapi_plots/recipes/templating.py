"""Render role/dim-bound plot templates against real data (the data half of templates).

:func:`mapi_plots.templates.apply_template` resolves bindings against a schema (JSON only);
:func:`render_template` additionally builds the template's dataset from an ``xr.Dataset``
(or a tree group): one tidy row per cell, with a column per bound dim/variable and, for
``stat:`` bindings, one column per summary statistic of the value variable.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import numpy as np
import xarray as xr
from mapi_spec.plots import PlotBindings, PlotProvenance, PlotSpec
from mapi_spec.schema import DataSchema

from mapi_plots.convert import frame_to_arrow
from mapi_plots.recipes._base import RecipeDataError, RecipeInput, as_tree
from mapi_plots.sanitize import sanitize_plot
from mapi_plots.templates import BoundField, apply_template, resolve_bindings

__all__ = ["builtin_templates", "infer_schema", "render_template"]


def infer_schema(ds: xr.Dataset) -> DataSchema:
    """A best-effort DataSchema: ``attrs['mapi_schema']`` if present, else from dtypes/attrs.

    Variable roles come from each variable's ``role`` attribute (default ``covariate``).
    """
    raw = ds.attrs.get("mapi_schema")
    if isinstance(raw, str):
        return DataSchema.model_validate_json(raw)
    if isinstance(raw, dict):
        return DataSchema.model_validate(raw)
    dims: dict[str, Any] = {}
    for d in ds.dims:
        coord = ds[d] if d in ds.coords else None
        if coord is not None and np.issubdtype(coord.dtype, np.datetime64):
            kind = "time"
        elif coord is not None and coord.dtype.kind in "OUSb":
            kind = "categorical"
        elif coord is not None and coord.dtype.kind == "f":
            kind = "float"
        else:
            kind = "integer"
        dims[str(d)] = {"kind": kind}
    variables: dict[str, Any] = {}
    for name, da in ds.data_vars.items():
        kind = da.dtype.kind
        dtype = (
            "float64"
            if kind == "f"
            else "int64"
            if kind in "iu"
            else "bool"
            if kind == "b"
            else "datetime64[ns]"
            if kind == "M"
            else "str"
        )
        variables[str(name)] = {
            "dims": [str(d) for d in da.dims if str(d) in dims],
            "dtype": dtype,
            "role": da.attrs.get("role", "covariate"),
            "units": da.attrs.get("units"),
        }
    return DataSchema.model_validate({"dims": dims, "vars": variables})


def render_template(
    template: PlotSpec,
    data: Any,
    schema: DataSchema | None = None,
    *,
    group: str | None = None,
    value: str | None = None,
    optional: Iterable[str] = (),
    dataset: str | None = None,
    max_rows: int = 50_000,
) -> PlotSpec:
    """Fill a template's placeholders and its dataset from ``data``; returns a sanitized plot.

    ``value`` names the variable whose ``stat`` coordinate feeds ``stat:`` bindings (default:
    the single target, else the first variable with a ``stat`` dim).
    """
    if template.bindings is None:
        raise RecipeDataError("the plot has no bindings; it is not a template")
    inp = RecipeInput(as_tree(data))
    ds = inp.require(group) if group else (inp.root() or inp.require("summary", "observed_data"))
    sch = schema or infer_schema(ds)
    opt = set(optional)
    resolved = resolve_bindings(template.bindings, sch, optional=opt)
    plot = apply_template(template, sch, optional=opt)
    fields: list[BoundField] = []
    for bf in resolved.values():
        fields += bf if isinstance(bf, list) else [bf]
    dims = list(dict.fromkeys(f.name for f in fields if f.kind in ("dim", "coord")))
    var_names = list(dict.fromkeys(f.name for f in fields if f.kind == "var"))
    stats = list(dict.fromkeys(f.name for f in fields if f.kind == "stat"))
    columns: dict[str, xr.DataArray] = {}
    for v in var_names:
        if v not in ds.data_vars:
            raise RecipeDataError(f"the data has no variable {v!r}")
        columns[v] = ds[v]
    if stats:
        vname = value or next(
            (t for t in sch.targets if t in ds.data_vars and "stat" in ds[t].dims),
            next((str(v) for v in ds.data_vars if "stat" in ds[v].dims), None),
        )
        if vname is None:
            raise RecipeDataError("stat: bindings need a variable with a 'stat' dim")
        labels = [str(x) for x in ds["stat"].values]
        for st in stats:
            if st not in labels:
                raise RecipeDataError(f"the summary has no statistic {st!r}")
            columns[st] = ds[vname].sel(stat=st, drop=True)
    if not columns:
        raise RecipeDataError("the template binds no variables or statistics")
    frame = (
        xr.Dataset(
            {k: c.drop_vars([x for x in c.coords if x not in c.dims]) for k, c in columns.items()}
        )
        .to_dataframe()
        .reset_index()
    )
    missing = [d for d in dims if d not in frame.columns]
    if missing:
        raise RecipeDataError(f"bound dims {missing} are not dims of the bound variables")
    frame = frame[[*dims, *columns]]
    if len(frame) > max_rows:
        raise RecipeDataError(f"{len(frame)} rows; select fewer coordinates (max {max_rows})")
    names = list(template.datasets) or ["data"]
    target = dataset or names[0]
    out = plot.model_copy(
        update={
            "datasets": {**plot.datasets, target: frame_to_arrow(frame)},
            "provenance": template.provenance.model_copy(
                update={
                    "generated_at": datetime.now(UTC),
                    "bundle_id": inp.bundle_id or template.provenance.bundle_id,
                }
            ),
        }
    )
    return sanitize_plot(out).plot


def _template(
    id_: str, title: str, spec: dict[str, Any], bindings: dict[str, Any], caption: str
) -> PlotSpec:
    return PlotSpec(
        id=id_,
        title=title,
        kind="vega-lite",
        spec=json.loads(json.dumps(spec)),
        datasets={"data": {"kind": "inline_arrow", "data": ""}},
        bindings=PlotBindings(**bindings),
        schema_version="v6.4.1",
        persona="both",
        caption_plain=caption,
        provenance=PlotProvenance(
            recipe=f"{id_}@1.0.0", generated_at=datetime(2026, 1, 1, tzinfo=UTC)
        ),
    )


def builtin_templates() -> dict[str, PlotSpec]:
    """Role/dim-bound templates that apply to any model family with matching roles."""
    target_over_time = _template(
        "tmpl.target_over_time",
        "{{y}} over {{x}}",
        {
            "data": {"name": "data"},
            "mark": {"type": "line", "point": False},
            "encoding": {"x": {"$bind": "x"}, "y": {"$bind": "y"}, "color": {"$bind": "color"}},
            "width": 480,
            "height": 200,
        },
        {"x": "dim:time", "y": "role:target", "color": "dim:geo"},
        "The {{y}} series over {{x}}, one line per {{color}}.",
    )
    summary_fan = _template(
        "tmpl.summary_fan",
        "Forecast fan",
        {
            "data": {"name": "data"},
            "layer": [
                {
                    "mark": {"type": "area", "opacity": 0.3},
                    "encoding": {
                        "x": {"$bind": "x"},
                        "y": {"$bind": "band_lower"},
                        "y2": {"field": "{{band_upper.field}}"},
                    },
                },
                {"mark": "line", "encoding": {"x": {"$bind": "x"}, "y": {"$bind": "y"}}},
            ],
            "width": 480,
            "height": 200,
        },
        {
            "x": "dim:time",
            "y": "stat:mean",
            "band_lower": "stat:hdi_90_lower",
            "band_upper": "stat:hdi_90_upper",
        },
        "The mean forecast with its 90% highest-density band.",
    )
    return {p.id: p for p in (target_over_time, summary_fan)}
