"""``BuildContext``: what a PyMC model's ``build()`` uses instead of raw coordinates (plan §5).

``build(ds, settings, ctx)`` runs twice in a model's life: at **fit** on the training data,
and at **score** on canonicalised score data (fit levels in fit order, then new levels,
padded extend dims). The helpers hide the differences so the same ``build`` serves both:

* :meth:`BuildContext.coords` - the ``pm.Model(coords=...)`` mapping; adds ``<dim>__fit`` and
  ``<dim>__new`` when the score data holds unseen levels;
* :meth:`BuildContext.data` - ``pm.Data`` over the current values (rebuild-and-predict);
* :meth:`BuildContext.observed` - the target at fit, a shape-only placeholder at score;
* :meth:`BuildContext.hier_effect` - a hierarchical effect over a ``new_levels`` dim whose new
  levels are filled according to the dim's strategy (``hyperprior`` draws them from the
  fitted hyperprior during prediction, with frozen noise so scenario and baseline agree);
* :meth:`BuildContext.position` / :attr:`BuildContext.anticipated_coords` - stable positions
  along ``extend`` dims, so trends and HSGPs see the same axis at fit and at forecast time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import numpy as np
import pandas as pd
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_sdk.coords import horizon_coords
from mapi_spec.coords import parse_freq
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema, DimPolicy
from pytensor.tensor.variable import TensorVariable

__all__ = ["BuildContext", "FrozenNoise", "HierEffect", "Phase"]

Phase = Literal["fit", "score"]
NoiseKind = Literal["normal", "uniform"]


@dataclass(frozen=True, slots=True)
class FrozenNoise:
    """A latent the platform fills with seeded draws at score time (never sampled by MCMC).

    Used for new-level offsets and for common-random-number predictive noise: the values
    depend only on the seed, the variable name and the shape, so a counterfactual's
    scenario and baseline share them exactly.
    """

    name: str
    kind: NoiseKind
    dims: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HierEffect:
    """Book-keeping for one :meth:`BuildContext.hier_effect` call."""

    name: str
    dim: str
    strategy: str
    n_fit: int
    n_new: int
    centered: bool


@dataclass(slots=True)
class BuildContext:
    """Platform helpers for ``PyMCModel.build`` (see the module docstring)."""

    phase: Phase
    ds: xr.Dataset
    schema: DataSchema | None = None
    policies: Mapping[str, DimPolicy] = field(default_factory=dict)
    fit_coords: Mapping[str, np.ndarray] = field(default_factory=dict)
    seed: int = 0
    mode: str | None = None
    kind: Literal["expected", "predictive"] | None = None
    data_vars: dict[str, tuple[str, ...]] = field(default_factory=dict)
    observed_vars: dict[str, tuple[str, ...]] = field(default_factory=dict)
    data_values: dict[str, np.ndarray] = field(default_factory=dict)
    observed_values: dict[str, np.ndarray] = field(default_factory=dict)
    hier_effects: dict[str, HierEffect] = field(default_factory=dict)
    frozen: dict[str, FrozenNoise] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.phase == "fit" and not self.fit_coords:
            self.fit_coords = {
                str(d): np.asarray(self.ds[d].values) for d in self.ds.dims if d in self.ds.coords
            }

    # ----------------------------------------------------------------- basics
    @property
    def is_fit(self) -> bool:
        return self.phase == "fit"

    @property
    def is_score(self) -> bool:
        return self.phase == "score"

    @property
    def rng(self) -> np.random.Generator:
        """A generator seeded from the run seed (fresh on each access, so reproducible)."""
        return np.random.default_rng(self.seed)

    def labels(self, dim: str) -> np.ndarray:
        """Current labels along ``dim`` (positions when the dim has no coordinate)."""
        if dim in self.ds.coords:
            return np.asarray(self.ds[dim].values)
        if dim in self.ds.sizes:
            return np.arange(int(self.ds.sizes[dim]))
        raise MapiError(f"the data has no dim {dim!r}", code=ErrorCode.SCHEMA_INVALID)

    def new_levels(self, dim: str) -> np.ndarray:
        """Levels of ``dim`` unseen at fit (always empty at fit)."""
        flag = f"{dim}__is_new"
        if self.phase == "fit" or flag not in self.ds.coords:
            return np.array([], dtype=self.labels(dim).dtype)
        mask = np.asarray(self.ds[flag].values, dtype=bool)
        return self.labels(dim)[mask]

    def fit_levels(self, dim: str) -> np.ndarray:
        """Levels of ``dim`` known at fit, in fit order."""
        if dim in self.fit_coords:
            return np.asarray(self.fit_coords[dim])
        labels = self.labels(dim)
        return labels[~np.isin(labels, self.new_levels(dim))]

    def strategy(self, dim: str) -> str:
        """The dim's new-level strategy (``hyperprior`` when no schema says otherwise)."""
        if self.schema is not None and dim in self.schema.dims:
            return self.schema.dims[dim].new_level_strategy
        return "hyperprior"

    def policy(self, dim: str) -> DimPolicy:
        if dim in self.policies:
            return DimPolicy(self.policies[dim])
        if self.schema is not None and dim in self.schema.dims:
            return self.schema.dims[dim].policy
        return DimPolicy.FIXED

    # ----------------------------------------------------------------- coords
    def coords(self, ds: xr.Dataset | None = None) -> dict[str, Any]:
        """``pm.Model`` coords for ``ds`` (default: the build data).

        Every dim gets its labels. At score time a dim with unseen levels also gets
        ``<dim>__fit`` (the fit levels, which posterior arrays are indexed by) and
        ``<dim>__new`` (the unseen levels, appended after the fit levels).
        """
        data = self.ds if ds is None else data_or_raise(ds)
        out: dict[str, Any] = {}
        for d in data.dims:
            dim = str(d)
            labels = (
                np.asarray(data[dim].values)
                if dim in data.coords
                else np.arange(int(data.sizes[dim]))
            )
            out[dim] = _coord_values(labels)
            new = self.new_levels(dim) if data is self.ds else np.array([])
            if new.size:
                flags = np.asarray(data[f"{dim}__is_new"].values, dtype=bool)
                first_new = int(np.argmax(flags))
                if not flags[first_new:].all():
                    raise MapiError(
                        f"new levels of {dim!r} must follow the fit levels; score through the "
                        "platform (score_bundle) so coordinates are canonicalised",
                        code=ErrorCode.SCHEMA_INVALID,
                    )
                out[f"{dim}__fit"] = _coord_values(labels[~flags])
                out[f"{dim}__new"] = _coord_values(labels[flags])
        return out

    # ----------------------------------------------------------------- data
    def data(
        self,
        name: str,
        values: xr.DataArray | np.ndarray | Sequence[Any] | float | None = None,
        *,
        dims: Sequence[str] | None = None,
    ) -> TensorVariable:
        """Register ``pm.Data(name, ...)`` over the current values.

        ``values`` defaults to ``ds[name]``; ``dims`` defaults to its dims. At score time the
        model is rebuilt with the score values, so authors never call ``pm.set_data``.
        Missing values are rejected (``pm.Data`` cannot hold NaN): impute them in the
        pipeline or pass explicit ``values``.
        """
        arr, used_dims = self._values(name, values, dims)
        if arr.dtype.kind == "b":
            arr = arr.astype(np.int8)
        if arr.dtype.kind == "f" and np.isnan(arr).any():
            n = int(np.isnan(arr).sum())
            raise MapiError(
                f"input variable {name!r} has {n} missing value(s) at {self.phase} time",
                code=ErrorCode.MISSING_FUTURE_COVARIATE
                if self.phase == "score"
                else ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{name}",
            )
        self.data_vars[name] = used_dims
        self.data_values[name] = arr
        var = pm.Data(name, arr, dims=list(used_dims) if used_dims else None)
        return var  # type: ignore[no-any-return]

    def observed(self, name: str, *, dims: Sequence[str] | None = None) -> np.ndarray:
        """Observed values for a likelihood: the target at fit, zeros of the right shape at
        score (prediction never conditions on them; missing targets are not imputed)."""
        if self.phase == "fit":
            arr, used_dims = self._values(name, None, dims)
            arr = arr.astype(np.float64) if arr.dtype.kind in "iub" else arr
        else:
            if dims is None:
                if name in self.ds:
                    dims = [str(d) for d in self.ds[name].dims]
                elif self.schema is not None and name in self.schema.vars:
                    dims = list(self.schema.vars[name].dims)
                else:
                    raise MapiError(
                        f"cannot size the placeholder for observed {name!r}: pass dims=",
                        code=ErrorCode.SCHEMA_INVALID,
                    )
            used_dims = tuple(str(d) for d in dims)
            shape = tuple(int(self.ds.sizes[d]) for d in used_dims)
            arr = np.zeros(shape, dtype=np.float64)
        self.observed_vars[name] = used_dims
        self.observed_values[name] = arr
        return arr

    def _values(
        self,
        name: str,
        values: xr.DataArray | np.ndarray | Sequence[Any] | float | None,
        dims: Sequence[str] | None,
    ) -> tuple[np.ndarray, tuple[str, ...]]:
        if values is None:
            if name not in self.ds:
                raise MapiError(
                    f"the {self.phase} data has no variable {name!r}",
                    code=ErrorCode.MISSING_FUTURE_COVARIATE
                    if self.phase == "score"
                    else ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{name}",
                )
            values = self.ds[name]
        if isinstance(values, xr.DataArray):
            order = tuple(str(d) for d in (dims or values.dims))
            return np.asarray(values.transpose(*order).values), order
        arr = np.asarray(values)
        if arr.ndim and dims is None:
            raise MapiError(
                f"pass dims= for array values of {name!r}", code=ErrorCode.SCHEMA_INVALID
            )
        return arr, tuple(str(d) for d in (dims or ()))

    # ----------------------------------------------------------------- hierarchy
    def hier_effect(
        self,
        name: str,
        dim: str,
        *,
        mu: Any = None,
        sigma: Any = None,
        centered: bool = False,
        mu_sigma: float = 1.0,
        sigma_scale: float = 1.0,
    ) -> TensorVariable:
        """A partially pooled effect over ``dim``: ``name[dim] ~ Normal(mu, sigma)``.

        Creates ``name`` (dims ``dim``) - non-centred by default via ``name__offset`` - plus
        hyperpriors ``name__mu ~ Normal(0, mu_sigma)`` and ``name__sigma ~ HalfNormal
        (sigma_scale)`` unless ``mu``/``sigma`` are given (``mu`` may be a scalar or a vector
        over the full ``dim``). When the score data holds unseen levels, ``name__new`` covers
        them according to the dim's new-level strategy:

        * ``hyperprior`` - ``mu + sigma * z`` with ``z`` frozen standard-normal noise, i.e. a
          fresh level drawn from the fitted hyperprior for every posterior draw;
        * ``pooled`` - the population mean ``mu``;
        * ``zero`` - ``0``;
        * ``error`` - raises ``UNSEEN_LEVELS``.

        Returns the effect over the full current ``dim`` (fit levels, then new levels).
        """
        new = self.new_levels(dim)
        n_new = int(new.size)
        n_all = int(self.labels(dim).size)
        n_fit = n_all - n_new
        strategy = self.strategy(dim)
        if n_new and strategy == "error":
            raise MapiError(
                f"{n_new} level(s) of {dim!r} were not seen at fit and its new-level strategy "
                "is 'error'",
                code=ErrorCode.UNSEEN_LEVELS,
            )
        fit_dim = f"{dim}__fit" if n_new else dim
        mu_t = pm.Normal(f"{name}__mu", 0.0, mu_sigma) if mu is None else mu
        sigma_t = pm.HalfNormal(f"{name}__sigma", sigma_scale) if sigma is None else sigma
        mu_t = pt.as_tensor_variable(mu_t)
        sigma_t = pt.as_tensor_variable(sigma_t)
        if mu_t.ndim > 1 or sigma_t.ndim > 0:
            raise MapiError(
                f"hier_effect({name!r}): mu must be a scalar or a vector over {dim!r} and "
                "sigma a scalar",
                code=ErrorCode.SCHEMA_INVALID,
            )
        mu_fit = mu_t[:n_fit] if mu_t.ndim == 1 else mu_t
        mu_new = mu_t[n_fit:] if mu_t.ndim == 1 else mu_t
        if centered:
            effect = pm.Normal(name, mu_fit, sigma_t, dims=fit_dim)
        else:
            offset = pm.Normal(f"{name}__offset", 0.0, 1.0, dims=fit_dim)
            effect = pm.Deterministic(name, mu_fit + sigma_t * offset, dims=fit_dim)
        self.hier_effects[name] = HierEffect(name, dim, strategy, n_fit, n_new, centered)
        if not n_new:
            return effect  # type: ignore[no-any-return]
        new_dim = f"{dim}__new"
        if strategy == "hyperprior":
            z = self.frozen_noise(f"{name}__new_z", dims=(new_dim,), kind="normal")
            fresh = mu_new + sigma_t * z
        elif strategy == "pooled":
            fresh = pt.zeros((n_new,)) + mu_new
        else:
            fresh = pt.zeros((n_new,))
        new_effect = pm.Deterministic(f"{name}__new", fresh, dims=new_dim)
        return pt.concatenate([effect, new_effect])  # type: ignore[no-any-return]

    def frozen_noise(
        self, name: str, *, dims: Sequence[str], kind: NoiseKind = "normal"
    ) -> TensorVariable:
        """A latent filled with seeded noise at score time (standard normal or uniform).

        Only meaningful when predicting; the platform supplies its values per posterior
        draw from ``(seed, name, shape)``, so repeated scores agree exactly.
        """
        dims_t = tuple(str(d) for d in dims)
        self.frozen[name] = FrozenNoise(name, kind, dims_t)
        if kind == "uniform":
            return pm.Uniform(name, 0.0, 1.0, dims=list(dims_t))  # type: ignore[no-any-return]
        return pm.Normal(name, 0.0, 1.0, dims=list(dims_t))  # type: ignore[no-any-return]

    # ----------------------------------------------------------------- extend dims
    @property
    def anticipated_coords(self) -> dict[str, np.ndarray]:
        """For each ``extend`` dim: the fit labels plus the ``max_extension`` labels a
        forecast may reach. Use it to size HSGP domains or to normalise time at fit so the
        normalisation is identical at forecast time."""
        out: dict[str, np.ndarray] = {}
        if self.schema is None:
            return out
        for dim, spec in self.schema.dims.items():
            if self.policy(dim) is not DimPolicy.EXTEND or dim not in self.fit_coords:
                continue
            fit = np.asarray(self.fit_coords[dim])
            if spec.max_extension and fit.size:
                future = horizon_coords(fit, spec.max_extension, freq=spec.freq)
                out[dim] = np.concatenate([fit, future.astype(fit.dtype)])
            else:
                out[dim] = fit
        return out

    def position(self, dim: str, *, scale: bool = False) -> np.ndarray:
        """Positions of the current labels along ``dim`` relative to the first fit label.

        Datetimes count periods of the dim's frequency, integers count steps of the fit
        spacing, categories use the fit order (new levels continue after it). With
        ``scale=True`` positions are divided by the last fit position so the fit range maps
        onto ``[0, 1]`` - identically at fit and at forecast time.
        """
        labels = self.labels(dim)
        fit = self.fit_levels(dim)
        if fit.size == 0:
            raise MapiError(f"no fit coordinates for {dim!r}", code=ErrorCode.SCHEMA_INVALID)
        if labels.dtype.kind == "M":
            freq = self._freq(dim, fit)
            f = parse_freq(freq)
            ords = np.array([_ordinal(f, v) for v in labels], dtype=float)
            pos = ords - _ordinal(f, fit[0])
            last = _ordinal(f, fit[-1]) - _ordinal(f, fit[0])
        elif labels.dtype.kind in "iuf":
            fsorted = np.sort(fit.astype(float))
            diffs = np.diff(fsorted)
            step = float(diffs[diffs > 0].min()) if (diffs > 0).any() else 1.0
            pos = (labels.astype(float) - fsorted[0]) / step
            last = (fsorted[-1] - fsorted[0]) / step
        else:
            order = {v: i for i, v in enumerate(fit.tolist())}
            extra = [v for v in labels.tolist() if v not in order]
            for v in extra:
                order[v] = len(order)
            pos = np.array([order[v] for v in labels.tolist()], dtype=float)
            last = float(fit.size - 1)
        if scale:
            return pos / (last if last > 0 else 1.0)
        return pos

    def _freq(self, dim: str, fit: np.ndarray) -> str:
        if self.schema is not None and dim in self.schema.dims and self.schema.dims[dim].freq:
            return str(self.schema.dims[dim].freq)
        inferred = pd.infer_freq(pd.DatetimeIndex(fit)) if fit.size >= 3 else None
        if inferred is None:
            raise MapiError(
                f"cannot position {dim!r}: declare its freq in the schema",
                code=ErrorCode.SCHEMA_INVALID,
            )
        return str(inferred)


def data_or_raise(ds: Any) -> xr.Dataset:
    if not isinstance(ds, xr.Dataset):
        raise TypeError("coords() expects an xarray Dataset")
    return ds


def _coord_values(labels: np.ndarray) -> list[Any]:
    if labels.dtype.kind == "M":
        return list(labels.astype("datetime64[ns]"))
    return labels.tolist()


def _ordinal(freq: Any, value: Any) -> int:
    ts = pd.Timestamp(value).to_pydatetime()
    assert isinstance(ts, datetime)
    out = freq.ordinal(ts)
    if out is None:
        raise MapiError(
            f"{value} is not on the {freq} grid", code=ErrorCode.NON_CONTIGUOUS_EXTENSION
        )
    return int(out)
