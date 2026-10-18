"""Model-agnostic reads of a fitted bundle, shared by the Model Explorer and the governance
checks (M3.5): in-sample and holdout predictions through the score path, parameter tables
for every model family, convergence statistics and the holdout split.

**Parameters.** Every family exposes its estimates differently; :func:`param_rows` reads, in
order:

1. Bayesian models: the ``posterior`` group (``chain``, ``draw``): mean, sd, a highest-density
   interval, split R-hat and bulk ESS per cell;
2. a ``params`` or ``coefficients`` group (frequentist and machine-learning plugins), either
   **table** layout - variables ``estimate`` (or ``coef``/``mean``) with optional ``std_err``
   (``se``, ``bse``), ``p_value`` (``pvalue``), ``ci_lower``/``ci_upper`` (``lower``/``upper``)
   along one parameter dim - or **wide** layout, one variable per parameter (``coefficient
   (feature)``, ``intercept``);
3. ``learned["params"]``: ``{name: number}`` or ``{name: {estimate, std_err, p_value,
   ci_lower, ci_upper}}``.

Frequentist intervals without explicit bounds are ``estimate ± z * std_err``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import numpy as np
import xarray as xr
from mapi_plots.recipes._stats import ess_bulk, rhat
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import _var_sources, score_bundle
from mapi_sdk.metrics import point_estimate
from mapi_sdk.registry import Registry
from mapi_sdk.summarise import hdi, interp_quantile
from mapi_spec.capabilities import Representation
from mapi_spec.errors import MapiError
from mapi_spec.governance import ModelKind, model_kind
from mapi_spec.schema import DimPolicy, Role
from mapi_spec.spec import AsOfSplit, ScoreRequest, TimeHoldoutSplit
from mapi_spec.views import ParamRow

__all__ = [
    "PARAM_GROUPS",
    "ParamEstimate",
    "Prediction",
    "bundle_kind",
    "convergence_stats",
    "holdout_mask",
    "param_estimates",
    "param_rows",
    "predict",
    "target_vars",
    "time_dim_of",
    "train_bundle",
]

PARAM_GROUPS: Final = ("params", "coefficients")
_DRAW_DIMS: Final = ("chain", "draw")
_MAX_CELLS_PER_VAR: Final = 200
_Z: Final = {"ci_90": 1.6448536269514722, "ci_95": 1.959963984540054}
_EST_KEYS: Final = ("estimate", "coef", "mean", "value")
_SE_KEYS: Final = ("std_err", "se", "bse", "sd")
_P_KEYS: Final = ("p_value", "pvalue", "pvalues")
_LO_KEYS: Final = ("ci_lower", "lower")
_HI_KEYS: Final = ("ci_upper", "upper")


def _fv(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def bundle_kind(bundle: Bundle) -> ModelKind:
    caps = bundle.model.manifest.capabilities
    return model_kind(
        caps.inference, representation=caps.representation.value, model_id=bundle.model.manifest.id
    )


def time_dim_of(bundle: Bundle, data: xr.Dataset) -> str | None:
    """The dataset's time dim: the raw schema's ``time`` dim, else its first ``extend``
    dim, else a datetime coordinate."""
    schema = bundle.raw_schema
    for name, d in schema.dims.items():
        if d.kind == "time" and name in data.dims:
            return name
    for name, d in schema.dims.items():
        if d.policy is DimPolicy.EXTEND and name in data.dims:
            return name
    for dim in data.dims:
        if str(dim) in data.coords and np.asarray(data[dim].values).dtype.kind == "M":
            return str(dim)
    return None


def target_vars(bundle: Bundle) -> list[str]:
    """The model's targets under their dataset names."""
    out = [n for n, v in bundle.raw_schema.vars.items() if v.role is Role.TARGET]
    if out:
        return out
    schema = bundle.resolved.model.input_schema
    canon = schema.targets if schema is not None else []
    return [bundle.binding.to_dataset(t) for t in canon]


def train_bundle(bundle: Bundle) -> Bundle:
    """The bundle fitted on the training data only (the evaluation bundle of an
    ``evaluate_then_refit`` fit, else the bundle itself)."""
    return bundle.evaluation or bundle


def holdout_mask(
    bundle: Bundle, data: xr.Dataset | xr.DataArray, evaluation: Bundle | None = None
) -> xr.DataArray | None:
    """Boolean mask along the split dim of ``data`` (a dataset, or a prediction's observed
    target at the model's stage): ``True`` for held-out periods (after the training data's
    last label). ``None`` without a time-based split."""
    split = bundle.spec.data.split
    if not isinstance(split, TimeHoldoutSplit | AsOfSplit):
        return None
    trained = evaluation or train_bundle(bundle)
    stage = trained.resolved.split_stage or "raw"
    coords = trained.fit_coords.get(stage)
    if coords is None or split.dim not in coords.coords:
        return None
    end = np.asarray(coords[split.dim].values).max()
    dim = trained.binding.to_dataset(split.dim)
    if dim not in data.dims or dim not in data.coords:
        return None
    labels = np.asarray(data[dim].values)
    mask = labels > end
    if not mask.any():
        return None
    return xr.DataArray(mask, dims=[dim], coords={dim: labels})


# ------------------------------------------------------------------------ predictions


@dataclass
class Prediction:
    """One target's prediction on a dataset (dataset names, original units)."""

    target: str
    output: str
    observed: xr.DataArray
    point: xr.DataArray
    lower: xr.DataArray | None = None
    upper: xr.DataArray | None = None
    draws: xr.DataArray | None = None
    contributions: xr.DataArray | None = None
    representation: Representation = Representation.POINT

    @property
    def resid(self) -> xr.DataArray:
        return self.observed - self.point


def _band(
    da: xr.DataArray, rep: Representation, level: float
) -> tuple[xr.DataArray, xr.DataArray] | None:
    a = (1 - level) / 2
    try:
        if rep is Representation.DRAWS:
            q = da.quantile([a, 1 - a], dim=list(_DRAW_DIMS))
            return q.isel(quantile=0, drop=True), q.isel(quantile=1, drop=True)
        if rep is Representation.QUANTILES:
            return interp_quantile(da, a), interp_quantile(da, 1 - a)
        if rep is Representation.INTERVAL:
            bounds = [str(b) for b in da["bound"].values]
            if "lower" in bounds and "upper" in bounds:
                return da.sel(bound="lower", drop=True), da.sel(bound="upper", drop=True)
        if rep is Representation.MOMENTS:
            mean, sd = da.sel(moment="mean", drop=True), da.sel(moment="sd", drop=True)
            z = 1.6448536269514722 if abs(level - 0.9) < 1e-9 else 1.959963984540054
            return mean - z * sd, mean + z * sd
    except Exception:
        return None
    return None


def supplied_tables(bundle: Bundle, side_tables: Mapping[str, xr.Dataset] | None) -> dict[str, str]:
    """The ``side_tables`` of a score request on ``bundle``: the spec's ``score_policy:
    supplied`` tables that are available (a score must name them to replay their steps)."""
    from mapi_spec.spec import ScorePolicy

    have = set(side_tables or {})
    return {
        name: str(st.ref)
        for name, st in bundle.spec.data.side_tables.items()
        if st.score_policy is ScorePolicy.SUPPLIED and name in have
    }


def predict(
    bundle: Bundle,
    data: xr.Dataset,
    registry: Registry,
    *,
    kind: Literal["expected", "predictive"] = "expected",
    side_tables: Mapping[str, xr.Dataset] | None = None,
    seed: int = 0,
    level: float = 0.9,
    tree: xr.DataTree | None = None,
) -> list[Prediction]:
    """Score ``bundle`` on ``data`` (``predict`` mode, dataset names, original units) and
    pair every target-shaped output with its observed target. ``tree`` is the tree dataset
    ``data`` was selected from (M4: a tree-input model gets it as ``ScoreContext.tree``)."""
    request = ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": f"model://{bundle.spec.metadata.project}/{bundle.spec.metadata.name}@latest",
            "mode": "predict",
            "data": "dataset://runner/explore@latest",
            "output": {"kind": kind},
            "seed": seed,
            "side_tables": supplied_tables(bundle, side_tables),
        }
    )
    try:  # ``metrics`` adds the observed targets in the predictions' space and stage
        scored = score_bundle(
            bundle,
            request,
            data,
            side_tables=side_tables,
            registry=registry,
            return_full=True,
            metrics=["rmse"],
            tree=tree,
        )
    except MapiError:
        scored = score_bundle(
            bundle,
            request,
            data,
            side_tables=side_tables,
            registry=registry,
            return_full=True,
            tree=tree,
        )
    node = scored["predictions"]
    assert isinstance(node, xr.DataTree)
    pred = node.to_dataset(inherit=False)
    obs_node = scored.children.get("observed")
    staged = (
        obs_node.to_dataset(inherit=False) if isinstance(obs_node, xr.DataTree) else xr.Dataset()
    )
    rep = bundle.model.manifest.capabilities.representation
    sources = _var_sources(bundle.model, bundle.resolved.model.input_schema)
    out: list[Prediction] = []
    used: set[str] = set()
    contributions = None
    for name, d in bundle.model.manifest.outputs.items():
        if "component" in d.dims and name in pred:
            contributions = point_estimate(pred[name], rep)
            break
    for name, src in sources.items():
        if name not in pred:
            continue
        target = bundle.binding.to_dataset(src)
        if target in used or target not in data:
            continue
        da = pred[name]
        if "component" in da.dims:
            continue
        point = point_estimate(da, rep)
        # the observed target at the model's stage (after resampling, currency conversion,
        # ...) when the pipeline reshapes it, else the data's own column
        source = staged[target] if target in staged else data[target]
        if set(point.dims) != set(source.dims):
            continue
        band = _band(da, rep, level)
        draws = None
        if rep is Representation.DRAWS:
            draws = da.stack(sample=list(_DRAW_DIMS)).transpose("sample", ...)
        used.add(target)
        observed = source.astype(float)
        out.append(
            Prediction(
                target=target,
                output=name,
                observed=observed,
                point=point.transpose(*observed.dims),
                lower=band[0].transpose(*observed.dims) if band else None,
                upper=band[1].transpose(*observed.dims) if band else None,
                draws=draws,
                contributions=contributions,
                representation=rep,
            )
        )
    return out


# ------------------------------------------------------------------------- parameters


@dataclass
class ParamEstimate:
    """A scalar parameter's estimate with a 90% interval (``draws`` for Bayesian models)."""

    name: str
    estimate: float
    sd: float | None = None
    lower: float | None = None
    upper: float | None = None
    p_value: float | None = None
    draws: np.ndarray | None = field(default=None, repr=False)


def _label(var: str, coords: Mapping[str, Any]) -> str:
    if not coords:
        return var
    return f"{var}[{','.join(str(v) for v in coords.values())}]"


def _cells(da: xr.DataArray, lead: Sequence[str] = ()) -> list[tuple[dict[str, Any], np.ndarray]]:
    """``(coords, values over the lead dims)`` for every cell of ``da``'s other dims."""
    other = [str(d) for d in da.dims if d not in lead]
    arr = np.asarray(da.transpose(*lead, *other).values, dtype=float)
    shape = arr.shape[len(lead) :]
    n = int(np.prod(shape)) if shape else 1
    flat = arr.reshape(*arr.shape[: len(lead)], n)
    out: list[tuple[dict[str, Any], np.ndarray]] = []
    for i in range(n):
        idx = np.unravel_index(i, shape) if shape else ()
        coords: dict[str, Any] = {}
        for d, j in zip(other, idx, strict=True):
            label = da[d].values[j] if d in da.coords else int(j)
            coords[d] = label.item() if hasattr(label, "item") else label
            if isinstance(coords[d], bytes):
                coords[d] = coords[d].decode()
        out.append((coords, flat[..., i]))
    return out


def _pick(ds: xr.Dataset, keys: Sequence[str]) -> xr.DataArray | None:
    for k in keys:
        if k in ds.data_vars:
            return ds[k]
    return None


def _group_estimates(
    grp: xr.Dataset, interval: str
) -> list[tuple[str, dict[str, Any], ParamEstimate]]:
    out: list[tuple[str, dict[str, Any], ParamEstimate]] = []
    est = _pick(grp, _EST_KEYS)
    z = _Z.get(interval, _Z["ci_90"])
    if est is not None:  # table layout
        se, pv = _pick(grp, _SE_KEYS), _pick(grp, _P_KEYS)
        lo, hi = _pick(grp, _LO_KEYS), _pick(grp, _HI_KEYS)
        for coords, v in _cells(est):
            sel = {
                d: est[d].values.tolist().index(c) if d in est.coords else c
                for d, c in coords.items()
            }

            def at(da: xr.DataArray | None, sel: dict[str, int] = sel) -> float | None:
                return None if da is None else _fv(da.isel(sel).values)

            e = _fv(v)
            if e is None:
                continue
            s = at(se)
            lower, upper = at(lo), at(hi)
            if (lower is None or upper is None) and s is not None:
                lower, upper = e - z * s, e + z * s
            name = "/".join(str(c) for c in coords.values()) or "estimate"
            out.append((name, {}, ParamEstimate(name, e, s, lower, upper, at(pv))))
        return out
    for var in sorted(map(str, grp.data_vars)):  # wide layout
        da = grp[var]
        if not np.issubdtype(da.dtype, np.number) or da.size > _MAX_CELLS_PER_VAR:
            continue
        for coords, v in _cells(da):
            e = _fv(v)
            if e is not None:
                out.append((var, coords, ParamEstimate(_label(var, coords), e)))
    return out


def _learned_estimates(
    learned: Mapping[str, Any], interval: str
) -> list[tuple[str, dict[str, Any], ParamEstimate]]:
    raw = learned.get("params")
    if not isinstance(raw, Mapping):
        return []
    z = _Z.get(interval, _Z["ci_90"])
    out: list[tuple[str, dict[str, Any], ParamEstimate]] = []
    for name, v in list(raw.items())[:5000]:
        if isinstance(v, int | float) and not isinstance(v, bool):
            e = _fv(v)
            if e is not None:
                out.append((str(name), {}, ParamEstimate(str(name), e)))
            continue
        if not isinstance(v, Mapping):
            continue
        e = next((_fv(v[k]) for k in _EST_KEYS if k in v), None)
        if e is None:
            continue
        s = next((_fv(v[k]) for k in _SE_KEYS if k in v), None)
        lo = next((_fv(v[k]) for k in _LO_KEYS if k in v), None)
        hi = next((_fv(v[k]) for k in _HI_KEYS if k in v), None)
        if (lo is None or hi is None) and s is not None:
            lo, hi = e - z * s, e + z * s
        p = next((_fv(v[k]) for k in _P_KEYS if k in v), None)
        out.append((str(name), {}, ParamEstimate(str(name), e, s, lo, hi, p)))
    return out


def _posterior(bundle: Bundle) -> xr.Dataset | None:
    post = bundle.model.groups.get("posterior")
    if post is None or not set(_DRAW_DIMS) <= set(map(str, post.dims)):
        return None
    return post


def param_estimates(
    bundle: Bundle, *, interval: str = "hdi_90", max_params: int = 500
) -> list[tuple[str, dict[str, Any], ParamEstimate]]:
    """``(variable, coords, estimate)`` for every scalar parameter cell (see the module
    docstring for where they come from), at most ``max_params``."""
    out: list[tuple[str, dict[str, Any], ParamEstimate]] = []
    post = _posterior(bundle)
    if post is not None:
        prob = 0.95 if interval.endswith("95") else 0.9
        for var in sorted(map(str, post.data_vars)):
            da = post[var]
            if da.size // max(1, da.sizes["chain"] * da.sizes["draw"]) > _MAX_CELLS_PER_VAR:
                continue
            for coords, x in _cells(da, _DRAW_DIMS):
                if not np.isfinite(x).all():
                    continue
                lo, hi = hdi(x.reshape(-1), prob)
                out.append(
                    (
                        var,
                        coords,
                        ParamEstimate(
                            _label(var, coords),
                            float(x.mean()),
                            float(x.std()),
                            _fv(lo),
                            _fv(hi),
                            draws=x.reshape(-1),
                        ),
                    )
                )
                if len(out) >= max_params:
                    return out
        return out
    for g in PARAM_GROUPS:
        grp = bundle.model.groups.get(g)
        if grp is not None:
            out.extend(_group_estimates(grp, interval if interval.startswith("ci") else "ci_90"))
    if not out:
        out.extend(
            _learned_estimates(
                bundle.model.learned, interval if interval.startswith("ci") else "ci_90"
            )
        )
    return out[:max_params]


def param_rows(
    bundle: Bundle, *, interval: str = "hdi_90", max_params: int = 500
) -> tuple[list[ParamRow], int, str]:
    """``(rows, omitted, interval label)`` of the Model Explorer's parameter table."""
    post = _posterior(bundle)
    label = interval
    if post is None and interval.startswith("hdi"):
        label = "ci_95" if interval.endswith("95") else "ci_90"
    if post is not None and interval.startswith("ci"):
        label = "hdi_95" if interval.endswith("95") else "hdi_90"
    est = param_estimates(bundle, interval=label, max_params=max_params + 1)
    omitted = max(0, len(est) - max_params)
    rows: list[ParamRow] = []
    for var, coords, e in est[:max_params]:
        r_hat = ess = None
        if e.draws is not None and post is not None:
            x = e.draws.reshape(int(post.sizes["chain"]), -1)
            r_hat = _fv(rhat(x)) if x.shape[0] > 1 else None
            ess = _fv(ess_bulk(x))
        rows.append(
            ParamRow(
                name=var[:256],
                coords={
                    k: v if isinstance(v, str | int | float | bool) else str(v)
                    for k, v in coords.items()
                },
                mean=_fv(e.estimate),
                sd=_fv(e.sd) if e.sd is not None else None,
                lower=_fv(e.lower) if e.lower is not None else None,
                upper=_fv(e.upper) if e.upper is not None else None,
                p_value=min(1.0, max(0.0, e.p_value)) if e.p_value is not None else None,
                r_hat=r_hat,
                ess_bulk=ess,
            )
        )
    return rows, omitted, label


# ------------------------------------------------------------------------ convergence


def convergence_stats(bundle: Bundle) -> dict[str, float | None]:
    """``r_hat_max``, ``ess_bulk_min``, ``divergences`` (Bayesian), ``converged`` (1/0 from
    the plugin's ``learned`` or diagnostics when it reports one) and information criteria
    the plugin recorded (``aic``, ``bic``, ``llf``)."""
    out: dict[str, float | None] = {}
    for d in bundle.diagnostics:
        if d.code == "RHAT" and d.value is not None:
            out["r_hat_max"] = d.value
        elif d.code == "ESS_BULK" and d.value is not None:
            out["ess_bulk_min"] = d.value
    post = _posterior(bundle)
    if (
        post is not None
        and int(post.sizes["chain"]) >= 2
        and ("r_hat_max" not in out or "ess_bulk_min" not in out)
    ):
        worst_r: float | None = None
        worst_e: float | None = None
        for name in sorted(map(str, post.data_vars)):
            da = post[name]
            if da.size // max(1, da.sizes["chain"] * da.sizes["draw"]) > 1024:
                continue
            for _c, x in _cells(da, _DRAW_DIMS):
                if not np.isfinite(x).all():
                    continue
                r, e = rhat(x), ess_bulk(x)
                if math.isfinite(r):
                    worst_r = r if worst_r is None else max(worst_r, r)
                if math.isfinite(e):
                    worst_e = e if worst_e is None else min(worst_e, e)
        out.setdefault("r_hat_max", worst_r)
        out.setdefault("ess_bulk_min", worst_e)
    stats = bundle.model.groups.get("sample_stats")
    if stats is not None and "diverging" in stats:
        out["divergences"] = float(np.asarray(stats["diverging"].values).astype(bool).sum())
    learned = bundle.model.learned
    for key in ("aic", "bic", "llf", "hqic"):
        v = learned.get(key)
        if isinstance(v, int | float) and not isinstance(v, bool):
            out[key] = _fv(v)
    conv = learned.get("converged")
    if isinstance(conv, bool):
        out["converged"] = 1.0 if conv else 0.0
    for d in bundle.diagnostics:
        if d.code in ("NOT_CONVERGED", "CONVERGENCE_FAILED", "OPTIMIZER_FAILED"):
            out["converged"] = 0.0
        elif d.code in ("CONVERGED", "OPTIMIZER_CONVERGED") and "converged" not in out:
            out["converged"] = 1.0
    return out
