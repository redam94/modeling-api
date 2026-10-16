"""Transform nodes (``transform.*``): data processing between tests and models.

``transform.steps`` and ``transform.select`` run catalog preprocessing steps (steps-core),
so a model fitted downstream carries them in its pipeline and replays them at score time
(:mod:`mapi_workflows.runtime.models`). Differences, seasonal differences, logs and Box-Cox
have no score-time step: their output feeds tests and searches, and models usually take
the original data with the differencing inside the model (``order[1]``, ``seasonal_order[1]``
of SARIMAX), as the gallery templates do.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_sdk.pipeline import Pipeline
from mapi_sdk.registry import default_registry
from mapi_sdk.schema import infer_schema
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_workflows.payloads import clean_number
from mapi_workflows.runtime.data import (
    data_error,
    label_of,
    schema_of,
    target_var,
    time_dim,
    with_schema,
)
from mapi_workflows.runtime.values import DatasetValue, NodeContext, NodeOutcome
from pydantic import JsonValue

__all__ = ["TRANSFORM_NODES", "derived_schema"]


def derived_schema(before: DataSchema, ds: xr.Dataset) -> DataSchema:
    """The schema of a transformed dataset: inferred, keeping the roles, units and policies
    of the variables and dims that survived."""
    inferred = infer_schema(ds)
    data = inferred.model_dump()
    for name, spec in before.vars.items():
        if name in data["vars"]:
            v = data["vars"][name]
            v["role"] = spec.role
            v["units"] = spec.units
            v["future_required"] = spec.future_required
    for name, d in before.dims.items():
        if name in data["dims"]:
            data["dims"][name]["policy"] = d.policy
            if d.freq and data["dims"][name]["kind"] == "time":
                data["dims"][name]["freq"] = d.freq
    return DataSchema.model_validate(data)


def _vars(ctx: NodeContext, dv: DatasetValue, schema: DataSchema) -> list[str]:
    raw = ctx.params.get("vars")
    if isinstance(raw, list) and raw:
        names = [str(v) for v in raw]
        missing = [n for n in names if n not in dv.data.data_vars]
        if missing:
            raise data_error(f"the data has no variable(s) {missing}")
        return names
    return [target_var(dv.data, schema)]


def _dim(ctx: NodeContext, dv: DatasetValue, schema: DataSchema) -> str:
    raw = ctx.params.get("dim")
    if isinstance(raw, str) and raw in dv.data.dims:
        return raw
    return time_dim(dv.data, schema)


def _finish(
    dv: DatasetValue,
    out: xr.Dataset,
    schema: DataSchema,
    *,
    names: list[str],
    note: str,
    replayable: bool,
    steps: list[dict[str, JsonValue]] | None = None,
    extra: Mapping[str, JsonValue] | None = None,
) -> NodeOutcome:
    new_schema = derived_schema(schema, out)
    derived = dv.derive(
        with_schema(out, new_schema),
        schema=new_schema,
        steps=steps or [],
        replayable=replayable,
        note=note,
    )
    result: dict[str, JsonValue] = {
        "vars": list(names),
        "note": note,
        "replayable": derived.replayable,
        "n_obs": int(max(out.sizes.values()) if out.sizes else 0),
    }
    result.update(extra or {})
    return NodeOutcome(result=result, outputs={"data": derived})


def _difference(ctx: NodeContext, seasonal: bool) -> NodeOutcome:
    dv = ctx.dataset("data")
    assert dv is not None
    schema = schema_of(dv)
    names = _vars(ctx, dv, schema)
    dim = _dim(ctx, dv, schema)
    order = int(clean_number(ctx.params.get("order")) or 1)
    period = 1
    if seasonal:
        p = clean_number(ctx.params.get("period"))
        if p is None or p < 2:
            raise data_error("transform.seasonal_difference needs a period of at least 2")
        period = int(p)
    n = dv.data.sizes[dim]
    lost = order * period
    if n - lost < 3:
        raise data_error(f"differencing would leave {max(n - lost, 0)} periods")
    out = dv.data
    levels: dict[str, JsonValue] = {}
    for name in names:
        da = dv.data[name]
        if dim not in da.dims:
            raise data_error(f"{name!r} does not vary along {dim!r}")
        tail = da.isel({dim: slice(-lost, None)})
        last: list[JsonValue] = []
        for x in np.asarray(tail.values, float).reshape(-1)[:200]:
            last.append(clean_number(x))
        levels[name] = last
        for _ in range(order):
            da = da - da.shift({dim: period})
        out = out.assign({name: da})
    out = out.isel({dim: slice(lost, None)})
    what = f"seasonal difference (period {period})" if seasonal else "difference"
    note = f"{what} of order {order} of {', '.join(names)} along {dim}"
    return _finish(
        dv,
        out,
        schema,
        names=names,
        note=note,
        replayable=False,
        extra={"order": order, "period": period, "lost_periods": lost, "last_levels": levels},
    )


def _log(ctx: NodeContext) -> NodeOutcome:
    dv = ctx.dataset("data")
    assert dv is not None
    schema = schema_of(dv)
    names = _vars(ctx, dv, schema)
    out = dv.data
    for name in names:
        da = dv.data[name]
        vals = np.asarray(da.values, float)
        if np.any(vals[np.isfinite(vals)] <= 0):
            raise data_error(
                f"{name!r} has zero or negative values: the logarithm needs "
                "positive values (use transform.box_cox or core.log1p)"
            )
        out = out.assign({name: np.log(da)})
    return _finish(
        dv, out, schema, names=names, note=f"log of {', '.join(names)}", replayable=False
    )


def _box_cox(ctx: NodeContext) -> NodeOutcome:
    from scipy import stats as sps
    from scipy.special import boxcox as bc

    dv = ctx.dataset("data")
    assert dv is not None
    schema = schema_of(dv)
    names = _vars(ctx, dv, schema)
    fixed = clean_number(ctx.params.get("lmbda"))
    out = dv.data
    lambdas: dict[str, JsonValue] = {}
    for name in names:
        da = dv.data[name]
        vals = np.asarray(da.values, float)
        finite = vals[np.isfinite(vals)]
        if finite.size < 3 or np.any(finite <= 0):
            raise data_error(f"{name!r} needs at least 3 positive values for Box-Cox")
        lam = float(fixed) if fixed is not None else float(sps.boxcox(finite)[1])
        out = out.assign({name: xr.apply_ufunc(lambda x, lam=lam: bc(x, lam), da)})
        lambdas[name] = lam
    first = lambdas[names[0]]
    return _finish(
        dv,
        out,
        schema,
        names=names,
        note=f"Box-Cox ({', '.join(f'{k}: {v:.3g}' for k, v in lambdas.items())})",
        replayable=False,
        extra={"lmbda": first, "lambdas": lambdas},
    )


def _steps(ctx: NodeContext) -> NodeOutcome:
    dv = ctx.dataset("data")
    assert dv is not None
    schema = schema_of(dv)
    raw = ctx.params.get("steps")
    if not isinstance(raw, list) or not raw:
        raise MapiError("transform.steps needs steps", code=ErrorCode.WORKFLOW_INVALID)
    registry = ctx.registry if ctx.registry is not None else default_registry()
    items: list[tuple[str, Any, Any]] = []
    calls: list[dict[str, JsonValue]] = []
    for i, call in enumerate(raw):
        if not isinstance(call, dict):
            raise MapiError(f"steps[{i}] is not a step call", code=ErrorCode.WORKFLOW_INVALID)
        sid, use = str(call.get("id") or f"step{i}"), str(call.get("use") or "")
        step_cls = registry.step(use)
        settings = call.get("with") or {}
        items.append((sid, step_cls, settings))
        calls.append({"id": sid, "use": use, "with": settings})
    pipe = Pipeline.build(items)
    trace = pipe.fit(dv.data, seed=ctx.seed, cancel=ctx.cancel, log=ctx.log)
    out = trace.data
    changed = [str(v) for v in out.data_vars if v not in dv.data or not out[v].equals(dv.data[v])]
    note = "steps " + ", ".join(f"{c['id']} ({c['use']})" for c in calls)
    return _finish(dv, out, schema, names=changed, note=note, replayable=True, steps=calls)


def _select(ctx: NodeContext) -> NodeOutcome:
    """Keep variables, labels and a time range. Label and range selections are recorded as
    ``core.filter_coords`` steps (replayed at score time); dropping variables needs no step
    (a model only reads the variables it binds)."""
    dv = ctx.dataset("data")
    assert dv is not None
    schema = schema_of(dv)
    out = dv.data
    calls: list[dict[str, JsonValue]] = []
    raw_vars = ctx.params.get("vars")
    if isinstance(raw_vars, list) and raw_vars:
        keep = [str(v) for v in raw_vars]
        missing = [v for v in keep if v not in out.data_vars]
        if missing:
            raise data_error(f"the data has no variable(s) {missing}")
        out = out[keep]
    sel = ctx.params.get("sel")
    if isinstance(sel, dict) and sel:
        for d, labels in sel.items():
            if d not in out.dims or d not in out.coords or not isinstance(labels, list):
                raise data_error(f"cannot select labels on {d!r}")
            have = {str(label_of(v)): v for v in out[d].values}
            idx = [have[str(x)] for x in labels if str(x) in have]
            if not idx:
                raise data_error(f"none of the labels of {d!r} are in the data")
            out = out.sel({d: idx})
            calls.append(
                {
                    "id": f"{ctx.node_id}_keep_{d}"[:63],
                    "use": "core.filter_coords@^1",
                    "with": {"dim": d, "keep": [label_of(v) for v in idx]},
                }
            )
    start, end = ctx.params.get("start"), ctx.params.get("end")
    if start is not None or end is not None:
        tdim = time_dim(out, schema)
        tlabels = [str(label_of(v)) for v in out[tdim].values] if tdim in out.coords else []
        lo = _pos(tlabels, start, 0, out.sizes[tdim])
        hi = _pos(tlabels, end, out.sizes[tdim] - 1, out.sizes[tdim]) + 1
        out = out.isel({tdim: slice(lo, hi)})
        bounds: dict[str, JsonValue] = {"dim": tdim}
        if start is not None:
            bounds["start"] = label_of(out[tdim].values[0])
        if end is not None:
            bounds["end"] = label_of(out[tdim].values[-1])
        calls.append(
            {"id": f"{ctx.node_id}_range"[:63], "use": "core.filter_coords@^1", "with": bounds}
        )
    names = [str(v) for v in out.data_vars]
    return _finish(dv, out, schema, names=names, note="selection", replayable=True, steps=calls)


def _pos(labels: list[str], at: JsonValue, default: int, n: int) -> int:
    if at is None:
        return default
    if isinstance(at, int) and not isinstance(at, bool):
        return max(0, min(at, n - 1))
    text = str(at)
    if text in labels:
        return labels.index(text)
    later = [i for i, lab in enumerate(labels) if lab >= text]
    if not later:
        raise data_error(f"{text!r} is after the end of the data")
    return later[0]


TRANSFORM_NODES: Final[dict[str, Callable[[NodeContext], NodeOutcome]]] = {
    "transform.difference": lambda ctx: _difference(ctx, seasonal=False),
    "transform.seasonal_difference": lambda ctx: _difference(ctx, seasonal=True),
    "transform.log": _log,
    "transform.box_cox": _box_cox,
    "transform.steps": _steps,
    "transform.select": _select,
}
