"""The default plots of a score run, chosen by the shape of each output.

================================================ ==================================================
output (manifest declaration, result dims)       default plot
================================================ ==================================================
a ``grid`` output (y and x in the result)        ``sci.field_map`` (frame: the slider)
a ``grid`` output with a 2-level ``component``   ``sci.vector_field``
an output on the areas of ``ring_x``/``ring_y``  ``sci.area_map`` (polygons of the bundle)
probabilities over a ``state``/``regime`` dim    ``core.state_sequence``
evaluate: a binary output (0/1 observed)         ``core.calibration``, ``core.roc_pr``,
                                                 ``core.confusion``
a non-grid output over time, <= 1 other dim      ``core.ppc_fan`` (predict and forecast)
================================================ ==================================================

Every mode except counterfactual (which keeps ``core.counterfactual``) starts from
:data:`DEFAULT_PLOTS`. A model with grid or area outputs gets the fan only when it has such an
output over time (a fan of a map sums it over space). Evaluate draws ``core.fit_vs_actual``
and adds ``core.residuals_over_time`` only when some output is over time; a binary output
(0/1 observed values, predictions within [0, 1]) replaces both with
:data:`BINARY_EVALUATE_PLOTS`. Recipe options are keyed by recipe id, so each recipe draws
one output: the first grid output with a frame (else the first one), and the first areal,
state or binary output in manifest order. A state output has a dim named exactly ``state`` or
``regime`` and values that are probabilities over it (each cell sums to 1): a panel dim such as
US ``state`` holds sales, not hidden-state probabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_spec.capabilities import OutputDecl
from mapi_spec.schema import DimPolicy

from mapi_runner.worker.fieldview import field_dim
from mapi_runner.worker.modelview import time_dim_of

__all__ = [
    "BINARY_EVALUATE_PLOTS",
    "DEFAULT_PLOTS",
    "ScorePlots",
    "area_outputs",
    "area_plot_tree",
    "binary_output",
    "default_score_plots",
    "field_plot_tree",
    "has_area_outputs",
    "has_grid_outputs",
    "state_outputs",
    "time_dims",
]

DEFAULT_PLOTS: Final[dict[str, list[str]]] = {
    "predict": ["core.ppc_fan"],
    "forecast": ["core.ppc_fan"],
    "counterfactual": ["core.counterfactual"],
    "evaluate": ["core.fit_vs_actual", "core.residuals_over_time"],
}
"""The plots of a mode for models without grid or area outputs (and every counterfactual)."""

BINARY_EVALUATE_PLOTS: Final = ("core.calibration", "core.roc_pr", "core.confusion")
"""The evaluate plots of a binary output, in place of fit vs actual and the residuals."""

_NOT_SHAPE: Final = frozenset({"stat", "chain", "draw", "sample", "quantile", "bound", "moment"})
"""Dims of the representation, not of the output."""
_SAMPLE: Final = ("chain", "draw", "sample")
_GROUPS: Final = ("predictions", "summary", "draws")
_GEOMETRY_GROUPS: Final = ("constant_data", "observed_data", "geometry")
"""Model groups that may hold the polygons (``ring_x``, ``ring_y``) of an areal model."""
_STATE_DIMS: Final = ("state", "regime")
"""Dim names of hidden-state (regime) probabilities; ``us_state`` or ``real_estate`` are not."""
_SIMPLEX_TOL: Final = 1e-3


@dataclass(frozen=True)
class ScorePlots:
    """The recipes to render and their settings, by recipe id."""

    refs: list[str]
    options: dict[str, dict[str, Any]] = field(default_factory=dict)

    def by_var(self) -> dict[str, list[str]]:
        """The recipe ids that draw each result variable (the ``var`` of their options)."""
        out: dict[str, list[str]] = {}
        for ref in self.refs:
            rid = ref.split("@")[0]
            var = self.options.get(rid, {}).get("var")
            if isinstance(var, str) and rid not in out.get(var, []):
                out.setdefault(var, []).append(rid)
        return out


def _grid_decls(bundle: Bundle) -> dict[str, OutputDecl]:
    return {n: d for n, d in bundle.model.manifest.outputs.items() if d.grid is not None}


def has_grid_outputs(bundle: Bundle) -> bool:
    """Whether the model declares an output on a 2-D grid (a map, image or field)."""
    return bool(_grid_decls(bundle))


def _geometry(bundle: Bundle) -> tuple[xr.Dataset, str] | None:
    """``ring_x``, ``ring_y`` (and ``adjacency``) of the first model group that holds both
    on the same two dims, and their area dim (the dim that is not ``vertex``)."""
    groups = bundle.model.groups
    for name in _GEOMETRY_GROUPS:
        ds = groups.get(name)
        if ds is None or "ring_x" not in ds.data_vars or "ring_y" not in ds.data_vars:
            continue
        rx, ry = ds["ring_x"], ds["ring_y"]
        if rx.ndim != 2 or rx.dims != ry.dims:
            continue
        dims = [str(d) for d in rx.dims]
        area = next(d for d in dims if d != "vertex") if "vertex" in dims else dims[0]
        keep = ["ring_x", "ring_y"] + (["adjacency"] if "adjacency" in ds.data_vars else [])
        return ds[keep], area
    return None


def area_outputs(bundle: Bundle) -> dict[str, str]:
    """Each non-grid output over the areas of the bundle's polygons -> the area dim
    (canonical, dim 0 of ``ring_x``/``ring_y`` unless that is ``vertex``)."""
    geo = _geometry(bundle)
    if geo is None:
        return {}
    area = geo[1]
    schema = bundle.resolved.model.input_schema
    return {
        name: area
        for name, decl in bundle.model.manifest.outputs.items()
        if decl.grid is None and area in decl.full_dims(schema)
    }


def has_area_outputs(bundle: Bundle) -> bool:
    """Whether the model has outputs over the areas of polygons stored in the bundle."""
    return bool(area_outputs(bundle))


def state_outputs(bundle: Bundle) -> dict[str, str]:
    """Each output with a declared ``state`` or ``regime`` dim -> that dim (canonical).

    These are candidates only: the plot also needs the result's values to be probabilities
    over the dim (:func:`_state_probabilities`); a ``sales(time, state)`` panel is not.
    """
    out: dict[str, str] = {}
    for name, decl in bundle.model.manifest.outputs.items():
        dim = next((d for d in decl.dims if d in _STATE_DIMS), None)
        if dim is not None:
            out[name] = dim
    return out


def _state_probabilities(da: xr.DataArray, dim: str) -> bool:
    """Whether ``da`` holds probabilities over ``dim``: every value in [0, 1] and every
    complete cell summing to 1 (the mean of a summary, the mean over draws)."""
    if "stat" in da.dims:
        stats = [str(v) for v in da["stat"].values] if "stat" in da.coords else []
        stat = next((s for s in ("mean", "point") if s in stats), None)
        if stat is None:
            return False
        da = da.sel(stat=stat, drop=True)
    samples = [d for d in _SAMPLE if d in da.dims]
    if samples:
        da = da.mean(samples)
    if da.dtype.kind not in "fiu" or da.sizes[dim] < 2:
        return False
    rows = np.asarray(da.transpose(..., dim).values, dtype=float).reshape(-1, da.sizes[dim])
    rows = rows[np.isfinite(rows).all(axis=1)]  # padded steps (NaN) are left out
    if not rows.size:
        return False
    in_range = rows.min() >= -_SIMPLEX_TOL and rows.max() <= 1.0 + _SIMPLEX_TOL
    return bool(in_range and np.abs(rows.sum(axis=1) - 1.0).max() <= _SIMPLEX_TOL)


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


def _group(tree: xr.DataTree, name: str) -> xr.Dataset | None:
    if name not in tree:
        return None
    node = tree[name]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset(inherit=False)


def _find(
    bundle: Bundle, tree: xr.DataTree, name: str, groups: tuple[str, ...] = _GROUPS
) -> tuple[str, xr.Dataset] | None:
    """The name of output ``name`` in the result and the group dataset that holds it."""
    for group in groups:
        ds = _group(tree, group)
        if ds is None:
            continue
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


def _grid_plots(bundle: Bundle, tree: xr.DataTree) -> dict[str, dict[str, Any]]:
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
    return chosen


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


def _over_time(bundle: Bundle, tree: xr.DataTree) -> bool:
    """Whether some output of the result has a time dim."""
    for name in bundle.model.manifest.outputs:
        found = _find(bundle, tree, name)
        if found is not None:
            var, ds = found
            if any(d in ds[var].dims for d in time_dims(bundle, ds)):
                return True
    return False


def _area_plot(bundle: Bundle, tree: xr.DataTree) -> str | None:
    """The first areal output (manifest order) in the result, by its name there."""
    for name in area_outputs(bundle):
        found = _find(bundle, tree, name)
        if found is not None:
            return found[0]
    return None


def _state_plot(bundle: Bundle, tree: xr.DataTree) -> dict[str, Any] | None:
    """``core.state_sequence`` settings of the first state output in the result."""
    for name, dim in state_outputs(bundle).items():
        found = _find(bundle, tree, name)
        if found is None:
            continue
        var, ds = found
        state = field_dim(bundle, ds[var], dim)
        if state is not None and _state_probabilities(ds[var], state):
            return {"var": var, "state_dim": state}
    return None


def _binary(observed: np.ndarray, predicted: np.ndarray) -> bool:
    obs = observed[np.isfinite(observed)]
    p = predicted[np.isfinite(predicted)]
    if not obs.size or not p.size:
        return False
    return np.unique(obs).size == 2 and bool(p.min() >= 0.0 and p.max() <= 1.0)


def binary_output(bundle: Bundle, tree: xr.DataTree) -> tuple[str, str] | None:
    """``(prediction, observed)`` names in the result of the first output in
    ``/predictions`` that predicts a binary target: the observed values (in ``/observed``)
    take exactly two values and the predictions (their mean over draws) lie in [0, 1]."""
    observed = _group(tree, "observed")
    if observed is None or not observed.data_vars:
        return None
    schema = bundle.resolved.model.input_schema
    targets = list(schema.targets) if schema is not None else []
    for name in bundle.model.manifest.outputs:
        found = _find(bundle, tree, name, ("predictions",))
        if found is None:
            continue
        pred, ds = found
        candidates = [pred, name]
        if len(targets) == 1:
            candidates += [targets[0], bundle.binding.to_dataset(targets[0])]
        obs = next((c for c in candidates if c in observed.data_vars), None)
        if obs is None and len(observed.data_vars) == 1:
            obs = str(next(iter(observed.data_vars)))
        if obs is None:
            continue
        da = ds[pred]
        samples = [d for d in _SAMPLE if d in da.dims]
        mean = da.mean(samples) if samples else da
        try:
            p = np.asarray(mean.values, dtype=float)
            y = np.asarray(observed[obs].values, dtype=float)
        except (TypeError, ValueError):  # a categorical (string) output
            continue
        if _binary(y, p):
            return pred, obs
    return None


def default_score_plots(bundle: Bundle, mode: str, tree: xr.DataTree) -> ScorePlots:
    """The plots of a ``mode`` score result ``tree`` (still holding ``/predictions`` when the
    model has grid or area outputs, and ``/observed`` for an evaluate), by the shape of its
    outputs (see the module docs)."""
    if mode == "counterfactual":
        return ScorePlots(list(DEFAULT_PLOTS[mode]))
    options: dict[str, dict[str, Any]] = {}
    if mode == "evaluate":
        binary = binary_output(bundle, tree)
        if binary is not None:
            pred, obs = binary
            options = {r: {"var": pred, "observed_var": obs} for r in BINARY_EVALUATE_PLOTS}
            refs = list(BINARY_EVALUATE_PLOTS)
        else:
            refs = ["core.fit_vs_actual"]
            if _over_time(bundle, tree):
                refs.append("core.residuals_over_time")
    else:
        refs = []
    spatial = has_grid_outputs(bundle) or has_area_outputs(bundle)
    for rid, opts in _grid_plots(bundle, tree).items():
        refs.append(rid)
        options[rid] = opts
    area = _area_plot(bundle, tree)
    if area is not None:
        refs.append("sci.area_map")
        options["sci.area_map"] = {"var": area}
    state = _state_plot(bundle, tree)
    if state is not None:
        refs.append("core.state_sequence")
        options["core.state_sequence"] = state
    if mode in ("predict", "forecast"):
        if not spatial:
            refs.insert(0, "core.ppc_fan")
        else:
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


def area_plot_tree(bundle: Bundle, tree: xr.DataTree) -> xr.DataTree | None:
    """The input of ``sci.area_map`` (which a score result lacks: the polygons are in the
    bundle): the root attrs, the areal outputs in ``/predictions`` (draws: the exceedance
    panel) or else ``/summary``, ``/observed`` when present, and ``/geometry`` with
    ``ring_x``, ``ring_y`` (and ``adjacency``) under the result's names. ``None`` when the
    model or the result has no areal output."""
    geo = _geometry(bundle)
    outputs = area_outputs(bundle)
    if geo is None or not outputs:
        return None
    for group in ("predictions", "summary"):
        ds = _group(tree, group)
        if ds is None:
            continue
        keep = [
            c
            for name in outputs
            for c in dict.fromkeys((name, bundle.binding.to_dataset(name)))
            if c in ds.data_vars
        ]
        if keep:
            break
    else:
        return None
    geometry = geo[0]
    if tree.attrs.get("names") == "dataset":
        geometry = bundle.binding.reverse(geometry)
    nodes: dict[str, xr.Dataset] = {
        "/": xr.Dataset(attrs=dict(tree.attrs)),
        f"/{group}": ds[keep],
        "/geometry": geometry,
    }
    observed = _group(tree, "observed")
    if observed is not None:
        nodes["/observed"] = observed
    return xr.DataTree.from_dict(nodes)
