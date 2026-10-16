"""Distributed-lag plugins: ``ts.ardl`` and the single-equation error-correction ``ts.ecm``.

Both are least-squares models, so a fitted cell is rebuilt by re-estimating the same
specification (stored lags per driver) on the stored data and checking the parameters.

``ts.ecm`` estimates the ARDL in levels and reparameterises it as the unrestricted
error-correction model (statsmodels ``UECM.from_ardl``): the coefficient on ``y.L1`` is the
speed of adjustment, the cointegrating (long-run) coefficients come with delta-method
standard errors, and the Pesaran-Shin-Smith bounds test says whether a levels relationship
exists. Both forms have identical fitted values, so forecasts use the ARDL form.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import numpy as np
import xarray as xr
from mapi_sdk.models import FitContext
from mapi_spec.capabilities import Diagnostic
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import Field, model_validator

from mapi_plugin_statsmodels._common import (
    Cell,
    CellFit,
    ExogMixin,
    StatsmodelsSettings,
    normal_quantiles,
    param_rows,
    quiet,
    schema_template,
    settings_error,
)
from mapi_plugin_statsmodels.base import StatsmodelsModel, quantile_capabilities

__all__ = ["ArdlModel", "ArdlSettings", "EcmModel", "EcmSettings"]


class ArdlSettings(StatsmodelsSettings, ExogMixin):
    """Settings of ``ts.ardl``."""

    exog: list[str] = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Driver variables (at least one); their future values are needed for forecasts",
        json_schema_extra={"x-ui-level": "basic"},
    )
    lags: int = Field(1, ge=0, le=12, description="Past values of the result to use")
    order: int = Field(
        1, ge=0, le=12, description="Past values of each driver to use (0 = same period only)"
    )
    trend: Literal["n", "c", "t", "ct"] = Field(
        "c", description="n none, c constant, t trend, ct both"
    )
    auto: bool = Field(
        False,
        description="Choose lags and driver orders per series by an information criterion "
        "(up to lags/order)",
    )
    ic: Literal["aic", "bic", "hqic"] = Field("aic", description="Criterion for auto")
    causal: bool = Field(
        False,
        description="Use only past values of the drivers (no same-period effect)",
        json_schema_extra={"x-ui-level": "advanced"},
    )


class ArdlModel(StatsmodelsModel[ArdlSettings]):
    """Autoregressive distributed lag regression (statsmodels ``ARDL``)."""

    id = "ts.ardl"
    Settings = ArdlSettings
    family = "forecast"
    uses_exog = True
    requires_exog = True
    min_obs = 12
    description = (
        "statsmodels ARDL: the result regressed on its own lags and current and past values "
        "of drivers by least squares; optional lag selection by information criterion; one "
        "model per series of a panel; forecasts need future drivers."
    )
    capabilities = quantile_capabilities("point")
    schema_template = schema_template(multivariate=False, exog=True)

    # ------------------------------------------------------------------ specification
    def _spec(self, cell: Cell, s: ArdlSettings) -> tuple[list[int], dict[str, list[int]]]:
        """Lags of y and of each driver: from the settings or chosen by ``ardl_select_order``."""
        if not s.auto:
            first = 1 if s.causal else 0
            dl = {x: list(range(first, s.order + 1)) for x in cell.exog_names}
            dl = {x: v for x, v in dl.items() if v}
            if not dl:
                raise settings_error("with causal=true, order must be at least 1", "order")
            return list(range(1, s.lags + 1)), dl
        from statsmodels.tsa.ardl import ardl_select_order

        with quiet():
            sel = ardl_select_order(
                cell.endog(),
                max(s.lags, 1),
                cell.exog(),
                max(s.order, 1 if s.causal else 0),
                trend=s.trend,
                ic=s.ic,
                causal=s.causal,
            )
        ar = [int(v) for v in (sel.model.ar_lags or [])]
        dl = {str(k): [int(i) for i in v] for k, v in (sel.model.dl_lags or {}).items() if v}
        if not dl:
            # keep every driver in the model with its same-period effect at least
            dl = {x: [1 if s.causal else 0] for x in cell.exog_names}
        return ar, dl

    def _model(
        self, cell: Cell, s: ArdlSettings, ar: list[int], dl: Mapping[str, list[int]]
    ) -> Any:
        from statsmodels.tsa.ardl import ARDL

        exog = cell.exog()
        assert exog is not None
        need = max([*ar, *(max(v) for v in dl.values()), 0]) + len(ar) + sum(map(len, dl.values()))
        if cell.n < need + 6:
            raise settings_error(
                f"{cell.label}: {cell.n} periods are too few for these lags (about {need + 6} "
                "are needed)",
                "lags",
            )
        with quiet():
            return ARDL(
                cell.endog(), ar or 0, exog[list(dl)], dict(dl), trend=s.trend, causal=False
            )

    # ------------------------------------------------------------------ hooks
    def fit_cell(self, cell: Cell, s: ArdlSettings, ctx: FitContext) -> CellFit:
        ar, dl = self._spec(cell, s)
        model = self._model(cell, s, ar, dl)
        with quiet():
            res = model.fit()
        fit = self._describe(model, res, cell)
        fit.learned.update({"ar_lags": ar, "dl_lags": {k: list(v) for k, v in dl.items()}})
        return fit

    def _describe(self, model: Any, res: Any, cell: Cell) -> CellFit:
        names = [str(n) for n in res.params.index]
        with quiet():
            table = param_rows(
                names, res.params, res.bse, res.tvalues, res.pvalues, res.conf_int(alpha=0.05)
            )
        resid = np.full(cell.n, np.nan)
        r = np.asarray(res.resid, dtype=float)
        resid[cell.n - r.size :] = r
        stats_ = {
            "aic": float(res.aic),
            "bic": float(res.bic),
            "hqic": float(res.hqic),
            "llf": float(res.llf),
            "nobs": float(res.nobs),
            "sigma2": float(res.sigma2),
        }
        return CellFit(
            params=dict(zip(names, np.asarray(res.params, dtype=float).tolist(), strict=True)),
            table=table,
            stats=stats_,
            resid=resid,
        )

    def _refit(self, cell: Cell, s: ArdlSettings, learned: Mapping[str, Any]) -> tuple[Any, Any]:
        ar = [int(v) for v in learned.get("ar_lags", [])]
        dl = {str(k): [int(i) for i in v] for k, v in dict(learned.get("dl_lags", {})).items()}
        model = self._model(cell, s, ar, dl)
        with quiet():
            return model, model.fit()

    def rebuild(
        self, cell: Cell, s: ArdlSettings, params: Mapping[str, float], learned: Mapping[str, Any]
    ) -> Any:
        _, res = self._refit(cell, s, learned)
        _verify(
            dict(zip(map(str, res.params.index), np.asarray(res.params, float), strict=True)),
            params,
        )
        return res

    def predict_cell(
        self,
        res: Any,
        cell: Cell,
        s: ArdlSettings,
        learned: Mapping[str, Any],
        insample: np.ndarray,
        x_future: np.ndarray | None,
        horizon: int,
        seed: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        return ardl_predict(res, cell, insample, x_future, horizon, s.quantiles)


def ardl_predict(
    res: Any,
    cell: Cell,
    insample: np.ndarray,
    x_future: np.ndarray | None,
    horizon: int,
    quantiles: list[float],
) -> tuple[np.ndarray, np.ndarray]:
    """One-step in-sample and dynamic multi-step forecast quantiles of an ``ARDLResults``."""
    import pandas as pd

    sigma2 = float(res.sigma2)
    with quiet():
        fitted = np.full(cell.n, np.nan)
        fv = np.asarray(res.fittedvalues, dtype=float)
        fitted[cell.n - fv.size :] = fv
        q_in = normal_quantiles(fitted[insample], np.full(insample.size, sigma2), quantiles)
        if not horizon:
            return q_in, np.empty((len(quantiles), 0))
        names = (
            list(res.model.data.orig_exog.columns) if res.model.data.orig_exog is not None else []
        )
        xf = pd.DataFrame(
            np.asarray(x_future, dtype=float)[:, [cell.exog_names.index(n) for n in names]],
            columns=names,
            index=pd.RangeIndex(cell.n, cell.n + horizon),
        )
        pred = res.get_prediction(start=cell.n, end=cell.n + horizon - 1, exog_oos=xf)
        mean = np.asarray(pred.predicted_mean, dtype=float)
        var = np.asarray(pred.var_pred_mean, dtype=float)
    return q_in, normal_quantiles(mean, var, quantiles)


def _verify(got: Mapping[str, float], want: Mapping[str, float]) -> None:
    keys = list(want)
    if set(got) != set(keys) or not np.allclose(
        [got[k] for k in keys], [want[k] for k in keys], rtol=1e-6, atol=1e-9
    ):
        raise MapiError(
            "re-estimating the model on its stored data gave different parameters; refit it",
            code=ErrorCode.INTEGRITY_FAILURE,
        )


# ===================================================================================== ECM


class EcmSettings(StatsmodelsSettings, ExogMixin):
    """Settings of ``ts.ecm`` (error-correction form of an ARDL)."""

    exog: list[str] = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Driver variables (at least one); their future values are needed for forecasts",
        json_schema_extra={"x-ui-level": "basic"},
    )
    lags: int = Field(1, ge=1, le=12, description="Past values of the result in the levels ARDL")
    order: int = Field(1, ge=1, le=12, description="Past values of each driver in the levels ARDL")
    trend: Literal["n", "c", "ct"] = Field(
        "c", description="n none, c constant (bounds case 3), ct constant and trend (case 5)"
    )
    bounds_case: Literal[1, 2, 3, 4, 5] | None = Field(
        None,
        description="Pesaran-Shin-Smith case of the bounds test (default from the trend)",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    bounds_alpha: float = Field(0.05, gt=0, lt=0.5, description="Significance of the bounds test")

    @model_validator(mode="after")
    def _case(self) -> EcmSettings:
        case = self.effective_case()
        if self.trend == "n" and case != 1:
            raise ValueError("without a constant the bounds test uses case 1")
        if self.trend == "c" and case not in (2, 3):
            raise ValueError("with a constant the bounds test uses case 2 or 3")
        if self.trend == "ct" and case not in (4, 5):
            raise ValueError("with a constant and a trend the bounds test uses case 4 or 5")
        return self

    def effective_case(self) -> int:
        if self.bounds_case is not None:
            return self.bounds_case
        return {"n": 1, "c": 3, "ct": 5}[self.trend]


class EcmModel(StatsmodelsModel[EcmSettings]):
    """Single-equation error correction (statsmodels ``UECM`` + bounds test)."""

    id = "ts.ecm"
    Settings = EcmSettings
    family = "forecast"
    uses_exog = True
    requires_exog = True
    min_obs = 16
    description = (
        "Single-equation error-correction model: an ARDL in levels reparameterised as an "
        "unrestricted ECM (statsmodels UECM) with long-run coefficients, the speed of "
        "adjustment and the Pesaran-Shin-Smith bounds test; forecasts need future drivers."
    )
    capabilities = quantile_capabilities("point")
    schema_template = schema_template(multivariate=False, exog=True)

    def _models(self, cell: Cell, s: EcmSettings) -> tuple[Any, Any, Any, Any]:
        from statsmodels.tsa.ardl import ARDL, UECM

        exog = cell.exog()
        assert exog is not None
        if cell.n < 3 * (s.lags + s.order * len(cell.exog_names)) + 10:
            raise settings_error(
                f"{cell.label}: {cell.n} periods are too few for these lags", "lags"
            )
        with quiet():
            ardl = ARDL(cell.endog(), s.lags, exog, s.order, trend=s.trend)
            ares = ardl.fit()
            uecm = UECM.from_ardl(ardl)
            ures = uecm.fit()
        return ardl, ares, uecm, ures

    def fit_cell(self, cell: Cell, s: EcmSettings, ctx: FitContext) -> CellFit:
        _, ares, _, ures = self._models(cell, s)
        names = [str(n) for n in ures.params.index]
        with quiet():
            table = param_rows(
                names, ures.params, ures.bse, ures.tvalues, ures.pvalues, ures.conf_int(alpha=0.05)
            )
            ci_names = [str(n) for n in ures.ci_params.index]
            long_run = param_rows(
                [f"long_run.{n}" for n in ci_names],
                ures.ci_params,
                ures.ci_bse,
                ures.ci_tvalues,
                ures.ci_pvalues,
                ures.ci_conf_int(alpha=0.05),
            )
            bounds = ures.bounds_test(case=s.effective_case(), asymptotic=True)
        table.update(long_run)
        speed = float(ures.params["y.L1"])
        half_life = float(np.log(0.5) / np.log1p(speed)) if -1 < speed < 0 else float("nan")
        p_lower = float(bounds.pvalue["lower"])
        p_upper = float(bounds.pvalue["upper"])
        crit = bounds.critical_values
        ecm = xr.Dataset(
            {
                "speed_of_adjustment": ((), speed),
                "half_life": ((), half_life),
                "bounds_statistic": ((), float(bounds.statistic)),
                "bounds_p_lower": ((), p_lower),
                "bounds_p_upper": ((), p_upper),
                "bounds_case": ((), int(s.effective_case())),
                "bounds_crit_lower": (("significance",), np.asarray(crit["lower"], dtype=float)),
                "bounds_crit_upper": (("significance",), np.asarray(crit["upper"], dtype=float)),
            },
            coords={"significance": [str(i) for i in crit.index]},
        )
        fit = ArdlModel()._describe(None, ares, cell)
        fit.params = dict(zip(names, np.asarray(ures.params, dtype=float).tolist(), strict=True))
        fit.table = table
        fit.groups["ecm"] = ecm
        verdict = _bounds_verdict(p_lower, p_upper, s.bounds_alpha)
        fit.learned.update({"bounds_verdict": verdict, "speed_of_adjustment": speed})
        fit.diagnostics.append(_bounds_diagnostic(cell, verdict, p_upper, s.bounds_alpha))
        return fit

    def rebuild(
        self, cell: Cell, s: EcmSettings, params: Mapping[str, float], learned: Mapping[str, Any]
    ) -> Any:
        _, ares, _, ures = self._models(cell, s)
        _verify(
            dict(zip(map(str, ures.params.index), np.asarray(ures.params, float), strict=True)),
            params,
        )
        return ares

    def predict_cell(
        self,
        res: Any,
        cell: Cell,
        s: EcmSettings,
        learned: Mapping[str, Any],
        insample: np.ndarray,
        x_future: np.ndarray | None,
        horizon: int,
        seed: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        return ardl_predict(res, cell, insample, x_future, horizon, s.quantiles)


def _bounds_verdict(p_lower: float, p_upper: float, alpha: float) -> str:
    """``levels_relationship`` when even the I(1) bound rejects, ``none`` when even the I(0)
    bound keeps the null, ``inconclusive`` in between."""
    if p_upper < alpha:
        return "levels_relationship"
    if p_lower >= alpha:
        return "none"
    return "inconclusive"


def _bounds_diagnostic(cell: Cell, verdict: str, p_upper: float, alpha: float) -> Diagnostic:
    plain = {
        "levels_relationship": "The result and the drivers are tied together in the long run.",
        "none": "No long-run relationship was found: the long-run effects are not reliable.",
        "inconclusive": "The test cannot tell whether a long-run relationship exists.",
    }[verdict]
    return Diagnostic(
        code="ECM_BOUNDS_TEST",
        severity="info" if verdict == "levels_relationship" else "warning",
        metric="bounds_p_upper",
        value=p_upper,
        threshold=alpha,
        message_expert=f"PSS bounds test on {cell.label}: {verdict} at {alpha:g} (I(1)-bound "
        f"p-value {p_upper:.3g})",
        message_plain=plain,
        suggested_action=None
        if verdict == "levels_relationship"
        else "Model the changes with ts.ardl on differenced data, or check the drivers.",
        coords={k: v for k, v in cell.key.items()},
    )
