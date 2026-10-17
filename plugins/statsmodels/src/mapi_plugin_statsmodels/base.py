"""The base class of every statsmodels plugin: the panel loop, the bundle layout, scoring.

**Serialisation without pickle.** A statsmodels results object is never stored. The bundle
keeps the model-stage training data (``observed_data``: ``y``; ``constant_data``: the
drivers), every estimated parameter (``params``: estimate, standard error, statistic,
p-value and 95% interval per ``param``) and small JSON state (``learned``: parameter order,
selected orders, ranks). Scoring *rebuilds* the results from them:

* state-space models (SARIMAX, ARIMA, UnobservedComponents, VARMAX, DynamicFactor) and the
  likelihood-based ETS and Markov-switching models apply the stored parameters to the stored
  data with the model's own filter/smoother (``model.filter(params)``, ``smooth(params)``);
* least-squares and closed-form models (VAR, VECM, ARDL/UECM, Theta) are re-estimated on the
  stored data with the stored orders - deterministic - and the result is checked against
  the stored parameters (``INTEGRITY_FAILURE`` when they disagree).

**Score semantics.** Periods inside the fitted range get one-step-ahead predictions from the
fitted history (the payload's targets and drivers are not used there); periods after it get
a multi-step forecast from the end of the fit, with the drivers taken from the payload
(``MISSING_FUTURE_COVARIATE`` when they are absent). Every output is in the ``quantiles``
representation: ``y_hat(quantile, <series...>, [variable], t)`` - prediction intervals, which
include the noise, for both output kinds (statsmodels has no separate interval for the
expected value). Panels loop over the series cells; unrequested (padding) cells are skipped.

**Forecasts with actuals** (:mod:`mapi_spec.actuals`, ``capabilities.conditions_on_actuals``):
a ``forecast`` payload with target values is scored by ``forecast_with_actuals``. Per series
the stored history is extended with the actuals (:mod:`mapi_plugin_statsmodels.conditioning`;
revisions replace fitted values) and the fitted parameters are applied to it without
re-estimation (``predict_conditioned``): the filter or smoother again for the state-space,
ETS and Markov-switching models; the fitted equations on the supplied lags for VAR, VECM,
ARDL and the ECM; the fitted SES weight, drift and season for Theta. The noise variance stays
the fitted one, so the bands depend on the new data only through the forecast origin.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any, ClassVar, cast

import numpy as np
import xarray as xr
from mapi_sdk.actuals import ForecastActuals
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ScoreContext,
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import BaseModel

from mapi_plugin_statsmodels._common import (
    PARAM,
    QUANTILE,
    RESIDUAL_TESTS,
    TABLE_FIELDS,
    TARGET,
    TEST,
    TIME,
    VARIABLE,
    Cell,
    CellFit,
    StatsmodelsSettings,
    arma_df,
    data_error,
    iter_cells,
    residual_tests,
    stack_cells,
)
from mapi_plugin_statsmodels.conditioning import extend_cell

__all__ = ["GROUPS", "LOO_REASON", "StatsmodelsModel", "statsmodels_version"]

LOO_REASON = (
    "statsmodels estimates are maximum-likelihood or least-squares point estimates without "
    "posterior draws; use holdout or backtest metrics instead of LOO"
)

GROUPS = (
    "observed_data",
    "constant_data",
    "params",
    "fit_stats",
    "residuals",
    "residual_tests",
)
"""Bundle groups every statsmodels plugin writes (models add their own, e.g. ``irf``)."""

STAT_NAMES = ("aic", "bic", "hqic", "llf", "nobs", "sigma2")


def statsmodels_version() -> str:
    import statsmodels

    return str(statsmodels.__version__)


def quantile_capabilities(inference: str, *, modes: Sequence[str] | None = None) -> Capabilities:
    """Capabilities shared by the statsmodels plugins (every one conditions forecasts on
    actuals with its fitted parameters)."""
    return Capabilities.model_validate(
        {
            "inference": inference,
            "representation": Representation.QUANTILES,
            "score_modes": list(modes or ("predict", "forecast", "evaluate")),
            "loo_valid": False,
            "loo_invalid_reason": LOO_REASON,
            "deterministic_given_seed": True,
            "conditions_on_actuals": True,
        }
    )


class StatsmodelsModel[S: StatsmodelsSettings](CustomModel[S]):
    """Shared fit/score machinery; subclasses implement the three per-cell hooks.

    * :meth:`fit_cell` - estimate one cell and return a :class:`CellFit`;
    * :meth:`rebuild` - recreate the statsmodels results from the stored data and
      parameters (no pickle);
    * :meth:`predict_cell` - quantiles for in-sample positions and a forecast horizon.
    """

    version = "1.0.0"
    multivariate: ClassVar[bool] = False
    uses_exog: ClassVar[bool] = False
    requires_exog: ClassVar[bool] = False
    min_obs: ClassVar[int] = 8
    rebuild_estimates: ClassVar[bool] = False
    """Whether :meth:`rebuild` re-estimates on its data (least squares, closed form): such
    models implement :meth:`predict_conditioned` themselves."""
    pad_extend = False
    plots: ClassVar[tuple[str, ...]] = (
        "core.ppc_fan",
        "core.fit_vs_actual",
        "core.residuals_over_time",
    )
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [TIME],
            "dims_of": TARGET,
            "units_of": TARGET,
            "description": "Prediction-interval quantiles of y: one-step-ahead inside the "
            "fitted range, a multi-step forecast after it",
        }
    }

    # =================================================================== per-cell hooks
    def fit_cell(self, cell: Cell, s: S, ctx: FitContext) -> CellFit:
        raise NotImplementedError

    def rebuild(
        self, cell: Cell, s: S, params: Mapping[str, float], learned: Mapping[str, Any]
    ) -> Any:
        raise NotImplementedError

    def predict_cell(
        self,
        res: Any,
        cell: Cell,
        s: S,
        learned: Mapping[str, Any],
        insample: np.ndarray,
        x_future: np.ndarray | None,
        horizon: int,
        seed: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        """``(Q, len(insample)[, k])`` and ``(Q, horizon[, k])`` quantiles."""
        raise NotImplementedError

    # =================================================================== helpers
    def exog_names(self, s: S) -> list[str]:
        return list(getattr(s, "exog", []) or [])

    def _check_data(self, ds: xr.Dataset, s: S) -> None:
        if TARGET not in ds:
            raise data_error(f"the data has no target variable {TARGET!r}")
        need = [TIME, *s.series_dims, *([VARIABLE] if self.multivariate else [])]
        for d in need:
            if d not in ds[TARGET].dims:
                raise data_error(
                    f"{TARGET!r} has no dim {d!r}; bind your data's dims to the model's "
                    "names (data.bind) and list panel dims in settings.series_dims"
                )
        extra = [str(d) for d in ds[TARGET].dims if d not in need]
        if extra:
            raise data_error(
                f"{TARGET!r} has dims {extra} the model does not know; list panel dims in "
                "settings.series_dims"
            )
        for x in self.exog_names(s):
            if x not in ds:
                raise data_error(f"the driver {x!r} named in settings.exog is not in the data")
        if self.requires_exog and not self.exog_names(s):
            raise MapiError(
                f"model {self.id!r} needs at least one driver in settings.exog",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/exog",
            )
        if self.multivariate and ds.sizes[VARIABLE] < 2:
            raise data_error(f"model {self.id!r} needs at least two series along {VARIABLE!r}")

    def _cell(self, ds: xr.Dataset, key: Mapping[str, Any], s: S, *, check: bool) -> Cell:
        sel = {d: key[d] for d in s.series_dims}
        yda = ds[TARGET].sel(sel) if sel else ds[TARGET]
        ydims = [TIME, VARIABLE] if self.multivariate else [TIME]
        y = np.asarray(yda.transpose(*ydims).values, dtype=float)
        names = self.exog_names(s)
        x = None
        if names:
            cols = []
            for n in names:
                da = ds[n]
                part = {d: v for d, v in sel.items() if d in da.dims}
                da = da.sel(part) if part else da
                cols.append(np.asarray(da.transpose(TIME).values, dtype=float))
            x = np.column_stack(cols)
        variables = [str(v) for v in ds[VARIABLE].values] if self.multivariate else []
        cell = Cell(
            key=dict(key),
            y=y,
            x=x,
            exog_names=names,
            variables=variables,
            t=np.asarray(ds[TIME].values),
        )
        if check:
            self._validate_cell(cell)
        return cell

    def _validate_cell(self, cell: Cell) -> None:
        finite = np.isfinite(cell.y)
        if cell.n < self.min_obs:
            raise data_error(
                f"{cell.label}: {cell.n} periods are too few for {self.id!r} (at least "
                f"{self.min_obs} are needed)"
            )
        if not finite.all() and not self.capabilities_allow_missing():
            raise data_error(
                f"{cell.label}: the target has missing values; {self.id!r} needs a complete "
                "series (fill gaps with core.impute or choose a state-space model such as "
                "ts.sarimax)"
            )
        if int(finite.reshape(cell.n, -1).all(axis=1).sum()) < self.min_obs:
            raise data_error(f"{cell.label}: fewer than {self.min_obs} observed periods")
        if cell.x is not None and not np.isfinite(cell.x).all():
            raise data_error(
                f"{cell.label}: a driver has missing values over the fitted periods "
                "(fill them with core.impute)"
            )

    @classmethod
    def capabilities_allow_missing(cls) -> bool:
        tpl = cls.schema_template
        if isinstance(tpl, Mapping):
            var = tpl.get("template", {}).get("vars", {}).get(TARGET, {})
            return bool(var.get("missing") == "allow")
        return False

    # =================================================================== fit
    def fit(self, ds: xr.Dataset, s: S, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        self._check_data(ds, s)
        if TIME in ds.coords and not ds.indexes[TIME].is_monotonic_increasing:
            ds = ds.sortby(TIME)
        _check_regular(np.asarray(ds[TIME].values))
        series = list(s.series_dims)
        order = {d: np.asarray(ds[d].values) for d in series}
        keys = list(iter_cells(ds, series))
        fits: list[CellFit] = []
        for i, key in enumerate(keys):
            ctx.check_cancelled()
            cell = self._cell(ds, key, s, check=True)
            fits.append(self._fit_one(cell, s, ctx))
            ctx.progress((i + 1) / len(keys), f"fitted {cell.label}")
        return self._output(ds, s, keys, fits, order)

    def _fit_one(self, cell: Cell, s: S, ctx: FitContext) -> CellFit:
        try:
            return self.fit_cell(cell, s, ctx)
        except MapiError:
            raise
        except (np.linalg.LinAlgError, ValueError, IndexError, RuntimeError) as exc:
            raise MapiError(
                f"{self.id!r} could not be estimated on {cell.label}: the settings do not fit "
                "this data (too many lags or terms for the number of periods, or constant "
                "series)",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings",
                untrusted_text=f"{type(exc).__name__}: {exc}"[:2000],
            ) from exc

    def _output(
        self,
        ds: xr.Dataset,
        s: S,
        keys: list[dict[str, Any]],
        fits: list[CellFit],
        order: dict[str, np.ndarray],
    ) -> FitOutput:
        t = np.asarray(ds[TIME].values)
        ydims = [*s.series_dims, *([VARIABLE] if self.multivariate else []), TIME]
        groups: dict[str, xr.Dataset] = {
            "observed_data": _clean(xr.Dataset({TARGET: ds[TARGET].transpose(*ydims)}))
        }
        names = self.exog_names(s)
        if names:
            groups["constant_data"] = _clean(
                xr.Dataset(
                    {n: ds[n].transpose(*[d for d in ydims if d in ds[n].dims]) for n in names}
                )
            )
        tables, stats_, resids, tests = [], [], [], []
        k_vars = [str(v) for v in ds[VARIABLE].values] if self.multivariate else []
        for fit in fits:
            pnames = list(fit.table)
            tables.append(
                xr.Dataset(
                    {f: ((PARAM,), [fit.table[p][f] for p in pnames]) for f in TABLE_FIELDS},
                    coords={PARAM: np.array(pnames, dtype=object)},
                )
            )
            stats_.append(
                xr.Dataset(
                    {
                        **{k: ((), float(fit.stats.get(k, math.nan))) for k in STAT_NAMES},
                        "converged": ((), float(fit.converged)),
                    }
                )
            )
            resids.append(_resid_ds(fit.resid, t, k_vars))
            fit.learned.setdefault("model_df", arma_df(fit.params))
            tests.append(_tests_ds(fit.resid, k_vars, model_df=int(fit.learned["model_df"])))
        groups["params"] = _clean(stack_cells(keys, tables, order))
        groups["fit_stats"] = _clean(stack_cells(keys, stats_, order))
        groups["residuals"] = _clean(stack_cells(keys, resids, order))
        groups["residual_tests"] = _clean(stack_cells(keys, tests, order))
        extra_names = sorted({g for f in fits for g in f.groups})
        for g in extra_names:
            have = [(k, f.groups[g]) for k, f in zip(keys, fits, strict=True) if g in f.groups]
            groups[g] = _clean(stack_cells([k for k, _ in have], [d for _, d in have], order))
        learned: dict[str, Any] = {
            "statsmodels_version": statsmodels_version(),
            "n_periods": int(t.size),
            "cells": [
                {"key": _json_key(k), "params": list(f.params), **f.learned}
                for k, f in zip(keys, fits, strict=True)
            ],
        }
        diagnostics = [d for f in fits for d in f.diagnostics]
        diagnostics += self._summary_diagnostics(groups["residual_tests"], fits, keys)
        return FitOutput(groups=groups, learned=learned, diagnostics=diagnostics)

    def _summary_diagnostics(
        self, tests: xr.Dataset, fits: Sequence[CellFit], keys: Sequence[Mapping[str, Any]]
    ) -> list[Diagnostic]:
        out: list[Diagnostic] = []
        unconverged = [k for k, f in zip(keys, fits, strict=True) if not f.converged]
        if unconverged:
            out.append(
                Diagnostic(
                    code="OPTIMIZER_NOT_CONVERGED",
                    severity="warning",
                    metric="unconverged_series",
                    value=float(len(unconverged)),
                    threshold=0.0,
                    message_expert=f"the likelihood optimiser did not converge for "
                    f"{len(unconverged)} of {len(fits)} series; estimates may be unreliable",
                    message_plain="The model could not settle on its best settings for some "
                    "series, so treat their results with care.",
                    suggested_action="Simplify the model (fewer lags or terms) or check the "
                    "data for outliers and level shifts.",
                )
            )
        if "test.ljung_box" in tests[TEST].values:
            p = np.asarray(tests["p_value"].sel({TEST: "test.ljung_box"}).values, dtype=float)
            p = p[np.isfinite(p)]
            if p.size:
                share = float((p < 0.05).mean())
                out.append(
                    Diagnostic(
                        code="RESIDUAL_AUTOCORRELATION",
                        severity="warning" if share > 0 else "info",
                        metric="ljung_box_reject_share",
                        value=share,
                        threshold=0.0,
                        message_expert=f"Ljung-Box rejects white-noise residuals at 5% for "
                        f"{share:.0%} of the series",
                        message_plain="Some pattern over time is left in the errors, so the "
                        "model misses part of the structure."
                        if share > 0
                        else "No pattern over time is left in the errors.",
                        suggested_action="Try more lags or a seasonal term." if share > 0 else None,
                    )
                )
        return out

    # =================================================================== conditioning
    def predict_conditioned(
        self,
        fit_res: Callable[[], Any],
        cell: Cell,
        ext: Cell,
        s: S,
        params: Mapping[str, float],
        learned: Mapping[str, Any],
        insample: np.ndarray,
        x_future: np.ndarray | None,
        horizon: int,
        seed: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Quantiles over the history ``ext`` (the fitted cell ``cell`` extended with the
        actuals, :mod:`~mapi_plugin_statsmodels.conditioning`) with the **fitted**
        parameters: ``(Q, len(insample)[, k])`` one-step-ahead and ``(Q, horizon[, k])``
        forecast from the end of ``ext``.

        The default runs :meth:`rebuild` on ``ext`` - a filter or smoother with the stored
        parameters, right for every model whose rebuild applies them. Models whose rebuild
        re-estimates (``rebuild_estimates``) override it; ``fit_res`` returns the results
        rebuilt on the stored data (computed once, on first call).
        """
        if self.rebuild_estimates:
            raise NotImplementedError(
                f"{self.id!r} re-estimates in rebuild: it must override predict_conditioned"
            )
        res = self.rebuild(ext, s, params, learned)
        return self.predict_cell(res, ext, s, learned, insample, x_future, horizon, seed)

    # =================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        return self._predictions(fitted, ds, ctx, None)

    def forecast_with_actuals(
        self,
        fitted: Fitted,
        ds: xr.Dataset,
        actuals: ForecastActuals,
        req: ScoreRequest,
        ctx: ScoreContext,
    ) -> xr.DataTree:
        """A forecast conditioned on the actuals in ``ds`` (:mod:`mapi_spec.actuals`).

        Per series cell the stored history is extended with the actuals (revisions replace
        fitted values) and the model is run over it with its fitted parameters - a Kalman,
        exponential-smoothing or Hamilton filter, or the least-squares equations on the
        supplied lags - never re-estimated. Payload periods up to each cell's last actual get
        one-step-ahead predictions; the later ones the multi-step forecast from it.
        """
        return self._predictions(fitted, ds, ctx, actuals)

    def _predictions(
        self,
        fitted: Fitted,
        ds: xr.Dataset,
        ctx: ScoreContext,
        actuals: ForecastActuals | None,
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        obs = fitted.group("observed_data")
        hist = np.asarray(obs[TIME].values)
        labels = np.asarray(ds[TIME].values) if TIME in ds.coords else hist
        inside = np.isin(labels, hist)
        if (~inside).any() and labels[~inside].min() <= hist.max():
            raise MapiError(
                "score periods inside the fitted range must be fitted periods",
                code=ErrorCode.COORD_NOT_ALLOWED,
                pointer="/data",
            )
        order = np.argsort(labels, kind="stable")
        labels = labels[order]
        inside = inside[order]
        pos_in = np.searchsorted(hist, labels[inside]).astype(np.int64)
        horizon = int((~inside).sum())
        series = list(s.series_dims)
        coords: dict[str, np.ndarray] = {}
        for d in series:
            coords[d] = np.asarray(ds[d].values) if d in ds.coords else np.asarray(obs[d].values)
        variables = np.asarray(obs[VARIABLE].values) if self.multivariate else None
        if self.multivariate and VARIABLE in ds.coords:
            want = [str(v) for v in ds[VARIABLE].values]
            if want != [str(v) for v in obs[VARIABLE].values]:
                raise MapiError(
                    f"the {VARIABLE!r} labels must be the fitted ones", code=ErrorCode.UNSEEN_LEVELS
                )
        q = np.asarray(s.quantiles, dtype=float)
        shape = [q.size, *(coords[d].size for d in series)]
        if variables is not None:
            shape.append(variables.size)
        shape.append(labels.size)
        out = np.full(shape, np.nan)
        params = fitted.group("params")
        raw_cells = fitted.learned.get("cells")
        if not isinstance(raw_cells, list):
            raise MapiError("the fitted model has no cell state", code=ErrorCode.INTEGRITY_FAILURE)
        cells_learned: dict[tuple[Any, ...], dict[str, Any]] = {}
        for c in raw_cells:
            if isinstance(c, dict) and isinstance(c.get("key"), dict):
                key_doc = cast(dict[str, Any], c["key"])
                cells_learned[tuple(key_doc.get(d) for d in series)] = cast(dict[str, Any], c)
        names = self.exog_names(s)
        for idx in np.ndindex(*(coords[d].size for d in series)):
            ctx.check_cancelled()
            key = {d: coords[d][j].item() for d, j in zip(series, idx, strict=True)}
            if not _requested(ds, key):
                continue
            learned = cells_learned.get(tuple(_json_scalar(key[d]) for d in series))
            if learned is None:
                raise MapiError(
                    f"no fitted model for {key}", code=ErrorCode.UNSEEN_LEVELS, pointer="/data"
                )
            cell = self._stored_cell(fitted, key, s)
            row = params.sel(key) if key else params
            pnames = [str(p) for p in learned["params"]]
            est = row["estimate"].sel({PARAM: pnames}).values.astype(float)
            pmap = dict(zip(pnames, est.tolist(), strict=True))
            seed = int(ctx.seed) * 1_000_003 + sum(idx) * 7919 + len(idx)
            if actuals is None:
                res = self.rebuild(cell, s, pmap, learned)
                x_future = self._future_exog(ds, key, labels[~inside], names) if horizon else None
                q_in, q_out = self.predict_cell(
                    res, cell, s, learned, pos_in, x_future, horizon, seed
                )
            else:
                ext = extend_cell(
                    cell,
                    labels=labels,
                    y_rows=self._payload_target(ds, key, labels, variables),
                    observed=self._payload_observed(actuals, key, labels, variables),
                    x_rows=self._payload_exog(ds, key, labels, names) if names else None,
                    allow_missing=self.capabilities_allow_missing(),
                )
                fit_res = _once(partial(self.rebuild, cell, s, pmap, learned))
                q_in, q_out = self.predict_conditioned(
                    fit_res,
                    cell,
                    ext.cell,
                    s,
                    pmap,
                    learned,
                    ext.insample,
                    ext.x_future,
                    ext.horizon,
                    seed,
                )
                q_out = q_out[:, ext.steps - 1]
            block = np.concatenate([q_in, q_out], axis=1)  # (Q, T[, k])
            if variables is not None:
                block = np.moveaxis(block, 2, 1)  # (Q, k, T)
            out[(slice(None), *idx)] = block
        dims = [QUANTILE, *series, *([VARIABLE] if variables is not None else []), TIME]
        all_coords: dict[str, Any] = {QUANTILE: q, TIME: labels, **coords}
        if variables is not None:
            all_coords[VARIABLE] = variables
        y_hat = xr.DataArray(
            out,
            dims=dims,
            coords=all_coords,
            attrs={"kind": "predictive", "interval": "prediction"},
        )
        # back to the payload's time order (canonical data is sorted, but stay exact)
        y_hat = y_hat.isel({TIME: np.argsort(order, kind="stable")})
        pred = xr.Dataset({"y_hat": y_hat})
        return xr.DataTree.from_dict({PREDICTIONS: pred})

    def _stored_cell(self, fitted: Fitted, key: Mapping[str, Any], s: S) -> Cell:
        obs = fitted.group("observed_data")
        ds = obs
        if "constant_data" in fitted.groups:
            ds = xr.merge([obs, fitted.group("constant_data")], compat="override", join="exact")
        return self._cell(ds, key, s, check=False)

    def _payload_target(
        self,
        ds: xr.Dataset,
        key: Mapping[str, Any],
        labels: np.ndarray,
        variables: np.ndarray | None,
    ) -> np.ndarray:
        """The payload's target over ``labels`` (``(L,)`` or ``(L, k)``, NaN where absent)."""
        shape = (labels.size,) if variables is None else (labels.size, variables.size)
        if TARGET not in ds:
            return np.full(shape, np.nan)
        return _rows(ds[TARGET], key, labels, variables).astype(float)

    def _payload_observed(
        self,
        actuals: ForecastActuals,
        key: Mapping[str, Any],
        labels: np.ndarray,
        variables: np.ndarray | None,
    ) -> np.ndarray:
        """Where the payload's target holds an actual, like :meth:`_payload_target`."""
        return _rows(actuals.observed, key, labels, variables).astype(bool)

    def _payload_exog(
        self, ds: xr.Dataset, key: Mapping[str, Any], labels: np.ndarray, names: Sequence[str]
    ) -> np.ndarray:
        """``(L, m)`` drivers of the payload over ``labels`` (NaN where the payload has none)."""
        cols = []
        for n in names:
            if n not in ds:
                cols.append(np.full(labels.size, np.nan))
                continue
            cols.append(_rows(ds[n], key, labels, None).astype(float).reshape(-1))
        return np.column_stack(cols)

    def _future_exog(
        self, ds: xr.Dataset, key: Mapping[str, Any], future: np.ndarray, names: Sequence[str]
    ) -> np.ndarray | None:
        if not names:
            return None
        cols = []
        for n in names:
            if n not in ds:
                raise MapiError(
                    f"the forecast needs future values of the driver {n!r}",
                    code=ErrorCode.MISSING_FUTURE_COVARIATE,
                    pointer="/data",
                )
            da = ds[n]
            sel = {d: v for d, v in key.items() if d in da.dims}
            col = np.asarray(da.sel(sel).sel({TIME: future}).values, dtype=float).reshape(-1)
            if not np.isfinite(col).all():
                raise MapiError(
                    f"the driver {n!r} has missing future values over the forecast horizon",
                    code=ErrorCode.MISSING_FUTURE_COVARIATE,
                    pointer="/data",
                )
            cols.append(col)
        return np.column_stack(cols)

    # =================================================================== explorer views
    def explorer_views(self, fitted: Fitted) -> dict[str, BaseModel]:
        """Model Explorer sections (``params``, ``diagnostics``, ``residuals``, ``irf``,
        ``fevd``) built from the bundle groups; see :mod:`mapi_plugin_statsmodels.views`."""
        from mapi_plugin_statsmodels.views import explorer_views

        return explorer_views(fitted)


# ============================================================================== helpers


def _check_regular(t: np.ndarray) -> None:
    """statsmodels indexes periods by position: the fitted periods must be evenly spaced."""
    if t.size < 3 or t.dtype.kind not in "iuM":
        return
    if t.dtype.kind == "M":
        import pandas as pd

        regular = pd.infer_freq(pd.DatetimeIndex(t)) is not None
    else:
        regular = np.unique(np.diff(t)).size == 1
    if regular:
        return
    raise data_error(
        "the time index is not evenly spaced (periods are missing or repeated); resample it "
        "with core.resample or add the missing periods with empty targets (state-space "
        "models such as ts.sarimax accept missing values)"
    )


def _clean(ds: xr.Dataset) -> xr.Dataset:
    out = ds.copy()
    for v in out.variables:
        out[v].attrs = {}
    for c in out.coords:
        if out[c].dtype == object:
            out = out.assign_coords({c: out[c].values.astype(str)})
    out.attrs = {"library": "statsmodels"}
    return out


def _json_scalar(v: Any) -> Any:
    if isinstance(v, np.generic):
        return v.item()
    return v


def _json_key(key: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k): _json_scalar(v) for k, v in key.items()}


def _rows(
    da: xr.DataArray, key: Mapping[str, Any], labels: np.ndarray, variables: np.ndarray | None
) -> np.ndarray:
    """``da`` for one cell over ``labels`` as ``(L,)`` or ``(L, k)`` (fitted variable order)."""
    sel = {d: v for d, v in key.items() if d in da.dims}
    cell = da.sel(sel) if sel else da
    cell = cell.sel({TIME: labels})
    if variables is not None and VARIABLE in cell.dims:
        cell = cell.sel({VARIABLE: variables}).transpose(TIME, VARIABLE)
    else:
        cell = cell.transpose(TIME, ...)
    return np.asarray(cell.values)


def _once(fn: Callable[[], Any]) -> Callable[[], Any]:
    """``fn`` computed on first call and remembered."""
    done: list[Any] = []

    def call() -> Any:
        if not done:
            done.append(fn())
        return done[0]

    return call


def _requested(ds: xr.Dataset, key: Mapping[str, Any]) -> bool:
    for d, v in key.items():
        flag = f"{d}__requested"
        if flag in ds.coords and not bool(ds[flag].sel({d: v}).item()):
            return False
    return True


def _resid_ds(resid: np.ndarray, t: np.ndarray, variables: list[str]) -> xr.Dataset:
    r = np.asarray(resid, dtype=float)
    if variables:
        return xr.Dataset(
            {"resid": ((VARIABLE, TIME), r.reshape(t.size, len(variables)).T)},
            coords={VARIABLE: variables, TIME: t},
        )
    return xr.Dataset({"resid": ((TIME,), r.reshape(-1))}, coords={TIME: t})


def _tests_ds(resid: np.ndarray, variables: list[str], *, model_df: int) -> xr.Dataset:
    r = np.asarray(resid, dtype=float)
    cols = [r.reshape(r.shape[0], -1)[:, j] for j in range(len(variables))] if variables else [r]
    rows = [residual_tests(c, model_df=0 if variables else model_df) for c in cols]
    stat = np.array([[row[t][0] for t in RESIDUAL_TESTS] for row in rows])
    pval = np.array([[row[t][1] for t in RESIDUAL_TESTS] for row in rows])
    lags = np.array([[row[t][2] for t in RESIDUAL_TESTS] for row in rows], dtype=np.int64)
    if variables:
        dims: tuple[str, ...] = (VARIABLE, TEST)
        coords: dict[str, Any] = {VARIABLE: variables, TEST: list(RESIDUAL_TESTS)}
        return xr.Dataset(
            {"statistic": (dims, stat), "p_value": (dims, pval), "lags": (dims, lags)},
            coords=coords,
        )
    return xr.Dataset(
        {
            "statistic": ((TEST,), stat[0]),
            "p_value": ((TEST,), pval[0]),
            "lags": ((TEST,), lags[0]),
        },
        coords={TEST: list(RESIDUAL_TESTS)},
    )
