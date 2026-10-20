"""The default plots of a score run, chosen by the shape of each output.

==================================================== ========================================
output (manifest declaration, dims of the result)    default plot
==================================================== ========================================
a ``grid`` output (y and x in the result)            ``sci.field_map`` (frame: the slider)
a ``grid`` output with a 2-level ``component``       ``sci.vector_field``
a non-grid output over time, at most one other dim   ``core.ppc_fan`` (predict and forecast)
==================================================== ========================================

Models without grid outputs, and counterfactuals, keep :data:`DEFAULT_PLOTS` of their mode.
A grid model with no such time output gets no fan: a fan of a grid sums it over space.
Evaluate keeps its defaults and adds the grid plots. Recipe options are keyed by recipe id,
so each recipe draws one output: the first grid output with a frame, else the first one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_spec.capabilities import OutputDecl
from mapi_spec.schema import DimPolicy

from mapi_runner.worker.fieldview import field_dim
from mapi_runner.worker.modelview import time_dim_of

__all__ = [
    "DEFAULT_PLOTS",
    "ScorePlots",
    "default_score_plots",
    "field_plot_tree",
    "has_grid_outputs",
    "time_dims",
]

DEFAULT_PLOTS: Final[dict[str, list[str]]] = {
    "predict": ["core.ppc_fan"],
    "forecast": ["core.ppc_fan"],
    "counterfactual": ["core.counterfactual"],
    "evaluate": ["core.fit_vs_actual", "core.residuals_over_time"],
}
"""The plots of a mode for models without grid outputs (and every counterfactual)."""

_NOT_SHAPE: Final = frozenset({"stat", "chain", "draw", "sample", "quantile", "bound", "moment"})
"""Dims of the representation, not of the output."""
_GROUPS: Final = ("predictions", "summary", "draws")


@dataclass(frozen=True)
class ScorePlots:
    """The recipes to render and their settings, by recipe id."""

    refs: list[str]
    options: dict[str, dict[str, Any]] = field(default_factory=dict)


def _grid_decls(bundle: Bundle) -> dict[str, OutputDecl]:
    return {n: d for n, d in bundle.model.manifest.outputs.items() if d.grid is not None}


def has_grid_outputs(bundle: Bundle) -> bool:
    """Whether the model declares an output on a 2-D grid (a map, image or field)."""
    return bool(_grid_decls(bundle))


def time_dims(bundle: Bundle, ds: xr.Dataset) -> list[str]:
    """The time dim of ``ds`` under its dataset name, then the canonical names of the
    schema's time and ``extend`` dims (a result with ``names: canonical``)."""
    out: list[str] = []
    t = time_dim_of(bundle, ds)
    if t is not None:
        out.append(t)
    for name, d in bundle.raw_schema.dims.items():
        if d.kind == "time" or d.policy is DimPolicy.EXTEND:
            canon = bundle.binding.to_canonical(name)
            if canon in ds.dims and canon not in out:
                out.append(canon)
    return out


def _find(bundle: Bundle, tree: xr.DataTree, name: str) -> tuple[str, xr.Dataset] | None:
    """The name of output ``name`` in the result and the group dataset that holds it."""
    for group in _GROUPS:
        if group not in tree:
            continue
        node = tree[group]
        assert isinstance(node, xr.DataTree)
        ds = node.to_dataset(inherit=False)
        for candidate in dict.fromkeys((name, bundle.binding.to_dataset(name))):
            if candidate in ds.data_vars:
                return candidate, ds
    return None


def _grid_plot(
    bundle: Bundle, decl: OutputDecl, var: str, da: xr.DataArray
) -> tuple[str, dict[str, Any]] | None:
    """The recipe and settings that draw grid output ``var`` (``None`` when its grid dims
    are not in the result, e.g. aggregated away)."""
    g = decl.grid
    assert g is not None
    y, x = field_dim(bundle, da, g.y), field_dim(bundle, da, g.x)
    if y is None or x is None:
        return None
    opts: dict[str, Any] = {"var": var, "y": y, "x": x}
    frame = field_dim(bundle, da, g.frame)
    if frame is not None:
        opts["frame"] = frame
    if g.component is None:
        return "sci.field_map", opts
    comp = field_dim(bundle, da, g.component)
    if comp is None or da.sizes[comp] != 2:
        return None
    return "sci.vector_field", {**opts, "component": comp}


def _time_output(bundle: Bundle, tree: xr.DataTree) -> str | None:
    """The first non-grid output over time with at most one other dim of size > 1."""
    for name, decl in bundle.model.manifest.outputs.items():
        if decl.grid is not None or "component" in decl.dims:
            continue
        found = _find(bundle, tree, name)
        if found is None:
            continue
        var, ds = found
        da = ds[var]
        t = next((d for d in time_dims(bundle, ds) if d in da.dims), None)
        if t is None:
            continue
        other = [d for d in da.dims if d != t and d not in _NOT_SHAPE and da.sizes[d] > 1]
        if len(other) <= 1:
            return var
    return None


def default_score_plots(bundle: Bundle, mode: str, tree: xr.DataTree) -> ScorePlots:
    """The plots of a ``mode`` score result ``tree`` (still holding ``/predictions`` when the
    model has grid outputs), by the shape of its outputs (see the module docs)."""
    fallback = ScorePlots(list(DEFAULT_PLOTS.get(mode, [])))
    if mode == "counterfactual" or not has_grid_outputs(bundle):
        return fallback
    chosen: dict[str, dict[str, Any]] = {}
    for name, decl in _grid_decls(bundle).items():
        found = _find(bundle, tree, name)
        if found is None:
            continue
        var, ds = found
        got = _grid_plot(bundle, decl, var, ds[var])
        if got is None:
            continue
        rid, opts = got
        if rid not in chosen or ("frame" in opts and "frame" not in chosen[rid]):
            chosen[rid] = opts
    refs = list(chosen)
    options = dict(chosen)
    if mode == "evaluate":
        return ScorePlots([*fallback.refs, *refs], options)
    kpi = _time_output(bundle, tree)
    if kpi is not None:
        refs.append("core.ppc_fan")
        options["core.ppc_fan"] = {"var": kpi}
    return ScorePlots(refs, options)


def field_plot_tree(bundle: Bundle, tree: xr.DataTree) -> xr.DataTree | None:
    """The root attrs and the full ``/predictions`` of the grid outputs, for the field
    recipes (which need draws) once the score result is trimmed; ``None`` when the result
    holds none."""
    if "predictions" not in tree:
        return None
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    pred = node.to_dataset(inherit=False)
    keep: list[str] = []
    for name, decl in _grid_decls(bundle).items():
        g = decl.grid
        assert g is not None
        for candidate in dict.fromkeys((name, bundle.binding.to_dataset(name))):
            if candidate not in pred.data_vars:
                continue
            da = pred[candidate]
            if field_dim(bundle, da, g.y) is not None and field_dim(bundle, da, g.x) is not None:
                keep.append(candidate)
            break
    if not keep:
        return None
    return xr.DataTree.from_dict(
        {"/": xr.Dataset(attrs=dict(tree.attrs)), "/predictions": pred[keep]}
    )
