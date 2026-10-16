"""``mmm.hierarchical_geo``: the E33 hierarchical geo marketing-mix model (plan §5, §6.1).

Model-stage data (canonical names, after a pipeline such as fx_convert → resample →
mean_scale → lag_tensor → fourier → holidays)::

    y(geo, time)                  target, mean-scaled
    spend(geo, channel, time, lag) spend lag tensor (lag 0 = the period's own spend)
    <controls>(…, time, …)        fourier features, holidays, other controls

Generative model (model units)::

    w[c, l]          = adstock weights (geometric alpha**l or delayed), normalised
    a[g, c, t]       = sum_l spend[g, c, t, l] * w[c, l]                 carry-over
    s[g, c, t]       = saturation(a[g, c, t]; lam[c])                     diminishing returns
    beta[g, c]       = beta_channel[c] * exp(beta_sigma[c] * z[g, c])     partial pooling
    contrib[g, c, t] = beta[g, c] * s[g, c, t]
    mu[g, t]         = intercept[g] + sum_c contrib + sum_f X[g, t, f] * gamma[f]
    y[g, t]          ~ Normal(mu[g, t], sigma)

``intercept`` is a :meth:`~mapi_pymc.BuildContext.hier_effect` over ``geo``; with
``pooling: partial`` the channel effects are partially pooled too. A geo unseen at fit
(``geo: new_levels(hyperprior)``) gets an intercept and channel effects drawn from the
fitted hyperpriors for every posterior draw (frozen noise, so a counterfactual's scenario
and baseline share them). ``pooling: none`` makes ``geo`` a fixed dim.

Outputs: ``y_hat(geo, time)`` (``expected`` = ``mu``; ``predictive`` adds observation noise
with common random numbers) and ``contributions(component, geo, time)`` - channels,
``baseline`` and one component per control - which sum to ``y_hat``
(``contribution_scale: additive``). Both are inverted to the output space by the platform
(weekly EUR for E33).

Derived quantities (cached at fit, original units): ``contributions``,
``response_curves`` (per thinned draw: spend multiplier x channel x geo; Guided-mode
sliders) and ``marginal_cost``. The decision objective scores a per-period budget
``spend(channel)`` or ``spend(geo, channel)`` at steady state, per posterior draw.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.engines import ENGINES
from mapi_pymc.frozen import FrozenPredictorSpec
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import FitContext, FitOutput, Fitted, PriorTranslation, ScoreContext
from mapi_spec.calibration import PriorTranslationJob
from mapi_spec.capabilities import Capabilities, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import FitConfig

from mapi_plugin_mmm.calibration import (
    LIFT_DIM,
    LIFT_VAR,
    LiftDesign,
    lift_design,
    model_scales,
    prior_preview,
    translate_business_priors,
)
from mapi_plugin_mmm.derived import (
    ChannelCurves,
    baseline_cube,
    contributions_dataset,
    draw_samples,
    marginal_cost_dataset,
    response_curves_dataset,
)
from mapi_plugin_mmm.design import (
    CHANNEL,
    GEO,
    SPEND,
    TARGET,
    TIME,
    components,
    control_design,
    control_vars,
    spend_tensor,
)
from mapi_plugin_mmm.priors import PriorSpec, build_prior
from mapi_plugin_mmm.settings import HierarchicalGeoSettings, mcmc_fit_schema
from mapi_plugin_mmm.transforms import adstock_weights, saturate, saturation_params

__all__ = ["SCHEMA_TEMPLATE", "HierarchicalGeoMMM"]

SCHEMA_TEMPLATE: dict[str, Any] = {
    "params": {
        "lag_dim": {"type": "string", "default": "lag"},
        "pooling": {
            "type": "string",
            "path": "pooling",
            "default": "partial",
            "enum": ["partial", "complete", "none"],
        },
    },
    "template": {
        "dims": {
            GEO: {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "hyperprior",
            },
            CHANNEL: {"kind": "categorical", "policy": "fixed"},
            TIME: {"kind": "time", "policy": "extend"},
            "{lag_dim}": {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            TARGET: {"dims": [GEO, TIME], "dtype": "float64", "role": "target"},
            SPEND: {"dims": [GEO, CHANNEL, TIME, "{lag_dim}"], "dtype": "float64", "role": "spend"},
        },
    },
    "cases": [
        {
            "when": {"pooling": "none"},
            "merge": {"dims": {GEO: {"policy": "fixed", "new_level_strategy": "error"}}},
        }
    ],
}

_MODES = frozenset({"predict", "forecast", "counterfactual", "evaluate", "simulate"})
_TRANSLATION_SOLVE_DRAWS = 1000
"""Monte Carlo draws of the other priors when solving a business-prior translation."""


def _vector(var: Any, n: int) -> Any:
    """A per-channel parameter as a length-``n`` vector (shared priors are broadcast)."""
    t = pt.as_tensor_variable(var)
    return t * pt.ones((n,)) if t.ndim == 0 else t


class HierarchicalGeoMMM(PyMCModel[HierarchicalGeoSettings]):
    """Hierarchical geo MMM with adstock, saturation and partially pooled channel effects."""

    id = "mmm.hierarchical_geo"
    version = "1.3.0"
    Settings = HierarchicalGeoSettings
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=_MODES,
        decision=True,
        fast_score=True,
        contribution_scale="additive",
        identified_quantities=["y_hat", "contributions"],
        deterministic_given_seed=False,
        checkpointable=True,
        calibration=frozenset({"business_priors", "lift_tests"}),
    )
    schema_template = SCHEMA_TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [GEO, TIME],
            "units_of": TARGET,
            "description": "Expected (or predictive) target",
        },
        "contributions": {
            "dims": ["component", GEO, TIME],
            "units_of": TARGET,
            "description": "Additive contribution of each channel, the baseline and each "
            "control; sums to y_hat",
        },
    }
    expected: ClassVar[dict[str, str]] = {"y_hat": "mu", "contributions": "contributions"}
    predictive: ClassVar[dict[str, str]] = {"y_hat": TARGET}
    derived: ClassVar[list[dict[str, Any]]] = [
        {
            "name": "contributions",
            "dims": ["sample", CHANNEL, GEO, TIME],
            "units": "target",
            "cache": "fit",
            "description": "Channel, baseline and control contributions over the fit window "
            "per thinned draw, in original units",
        },
        {
            "name": "response_curves",
            "dims": ["sample", CHANNEL, "multiplier", GEO],
            "units": "target",
            "cache": "fit",
            "description": "Total contribution per spend multiplier, channel and geo per "
            "thinned draw (Guided-mode slider grids)",
        },
        {
            "name": "marginal_cost",
            "dims": ["sample", CHANNEL, GEO],
            "units": "spend per target",
            "cache": "fit",
            "description": "Extra spend per extra unit of the target at current spend",
        },
    ]
    plots: ClassVar[list[str]] = [
        "core.ppc_fan",
        "core.convergence",
        "core.posterior_forest",
        "mmm.decomposition",
        "mmm.response_curves",
        "mmm.marginal_cost",
    ]
    resources = ResourceHint(
        min_class=ResourceClass.S, default_class=ResourceClass.M, est_fit_seconds=900
    )
    fit_schema: ClassVar[dict[str, Any]] = mcmc_fit_schema(
        engines=[e for e in ENGINES if e in ("nutpie", "pymc", "numpyro", "blackjax", "advi")],
        default_engine="nutpie",
    )
    description = (
        "Hierarchical geo marketing-mix model: geometric/delayed adstock over a lag tensor, "
        "logistic or Hill saturation, channel effects partially pooled across geos, new geos "
        "drawn from the hyperprior, additive contributions, response curves and marginal "
        "costs in original units, and a budget objective for optimisation."
    )
    pad_extend = False
    default_engine = "nutpie"
    fast_score_spec = FrozenPredictorSpec(response_exprs={"y_hat": "mu"}, input_vars=[SPEND])
    _lift: LiftDesign | None = None
    """The lift-test cells of the fit in progress (set by :meth:`fit` around the build)."""

    # ------------------------------------------------------------------ build
    def build(self, ds: xr.Dataset, s: HierarchicalGeoSettings, ctx: BuildContext) -> pm.Model:
        lag_dim = s.lag_dim
        fill = ctx.is_score
        x = spend_tensor(ds, lag_dim, align=ctx.align, fill_missing=fill)
        n_lag = x.shape[-1]
        if s.adstock.l_max is not None and s.adstock.l_max != n_lag:
            raise MapiError(
                f"adstock.l_max is {s.adstock.l_max} but the spend tensor has {n_lag} lags; "
                "make them equal (core.lag_tensor l_max)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/model/settings/adstock/l_max",
            )
        controls = control_vars(ds, s.controls)
        design = control_design(ds, controls, align=ctx.align, fill_missing=fill)
        channels = ctx.labels(CHANNEL)
        comps = components([str(c) for c in channels], controls)
        priors = s.resolved_priors()
        coords = ctx.coords()
        coords["component"] = comps
        if not design.empty:
            coords["control_feature"] = design.features
        lift = self._lift if ctx.is_fit else None
        if lift is not None:
            coords[LIFT_DIM] = lift.ids
        n_ch, n_time = x.shape[1], x.shape[2]
        with pm.Model(coords=coords) as model:
            xd = ctx.data(SPEND, x, dims=[GEO, CHANNEL, TIME, lag_dim])
            weights = self._adstock(s, priors, n_ch, n_lag)  # (channel, lag)
            adstocked = (xd * weights[None, :, None, :]).sum(axis=-1)
            sat_vectors = {
                p: _vector(build_prior(p, priors[p], dims=[CHANNEL], coords=coords), n_ch)
                for p in saturation_params(s.saturation.kind)
            }
            sat_params = {p: v[None, :, None] for p, v in sat_vectors.items()}
            response = saturate(adstocked, s.saturation.kind, xp=pt, **sat_params)
            beta = self._channel_effects(s, priors, ctx, coords, n_ch)  # (geo, channel)
            if lift is not None:
                _lift_likelihood(lift, weights, sat_vectors, beta, s)
            channel = pm.Deterministic(
                "channel_contribution", beta[:, :, None] * response, dims=[GEO, CHANNEL, TIME]
            )
            mu0 = build_prior("intercept__mu", priors["intercept"])
            sigma0 = build_prior("intercept__sigma", priors["intercept_sigma"])
            intercept = ctx.hier_effect("intercept", GEO, mu=mu0, sigma=sigma0, centered=s.centred)
            parts = [
                channel.dimshuffle(1, 0, 2),
                (intercept[:, None] * pt.ones((1, n_time)))[None, :, :],
            ]
            if not design.empty:
                xc = ctx.data("controls", design.values, dims=[GEO, TIME, "control_feature"])
                gamma = build_prior(
                    "gamma_control", priors["gamma_control"], iid_dims=["control_feature"]
                )
                for name in controls:
                    block = design.blocks[name]
                    parts.append(pt.dot(xc[:, :, block], gamma[block])[None, :, :])
            stacked = pt.concatenate(parts, axis=0)
            contributions = pm.Deterministic(
                "contributions", stacked, dims=["component", GEO, TIME]
            )
            mu = pm.Deterministic("mu", contributions.sum(axis=0), dims=[GEO, TIME])
            sigma = build_prior("sigma", priors["sigma"])
            observed = ctx.observed(TARGET, dims=[GEO, TIME])
            if ctx.is_fit and not np.isfinite(observed).all():
                raise MapiError(
                    f"the target has {int((~np.isfinite(observed)).sum())} missing value(s); "
                    "impute or filter them in the pipeline",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/data/y",
                )
            pm.Normal(TARGET, mu, sigma, observed=observed, dims=[GEO, TIME])
        return model

    def _adstock(
        self, s: HierarchicalGeoSettings, priors: dict[str, PriorSpec], n_ch: int, n_lag: int
    ) -> Any:
        alpha = _vector(build_prior("alpha", priors["alpha"], dims=[CHANNEL]), n_ch)
        theta = None
        if s.adstock.kind == "delayed":
            theta = _vector(build_prior("theta", priors["theta"], dims=[CHANNEL]), n_ch)[:, None]
        return adstock_weights(
            s.adstock.kind,
            np.arange(n_lag),
            alpha=alpha[:, None],
            theta=theta,
            normalize=s.adstock.normalize,
            xp=pt,
        )

    def _channel_effects(
        self,
        s: HierarchicalGeoSettings,
        priors: dict[str, PriorSpec],
        ctx: BuildContext,
        coords: dict[str, Any],
        n_ch: int,
    ) -> Any:
        """``beta(geo, channel)`` over the current geos (fit levels, then new levels)."""
        n_geo = len(ctx.labels(GEO))
        new = ctx.new_levels(GEO)
        n_new = int(new.size)
        fit_dim = f"{GEO}__fit" if n_new else GEO
        bc = _vector(
            build_prior("beta_channel", priors["beta_channel"], dims=[CHANNEL], coords=coords),
            n_ch,
        )
        if s.pooling == "complete":
            beta = bc[None, :] * pt.ones((n_geo, 1))
            return pm.Deterministic("beta", beta, dims=[GEO, CHANNEL])
        strategy = ctx.strategy(GEO)
        if n_new and strategy == "error":
            raise MapiError(
                f"{n_new} geo(s) were not seen at fit and the geo new-level strategy is 'error'",
                code=ErrorCode.UNSEEN_LEVELS,
            )
        if s.pooling == "none":
            spec = priors["beta_channel"]
            beta_fit = build_prior("beta__geo", spec, dims=[CHANNEL], iid_dims=[fit_dim])
            if beta_fit.ndim < 2:  # one prior shared by every channel
                beta_fit = beta_fit[:, None] * pt.ones((1, n_ch))
            if n_new:
                raise MapiError(
                    "pooling 'none' estimates every geo separately; new geos cannot be scored",
                    code=ErrorCode.UNSEEN_LEVELS,
                )
            return pm.Deterministic("beta", beta_fit, dims=[GEO, CHANNEL])
        bs = _vector(
            build_prior("beta_sigma", priors["beta_sigma"], dims=[CHANNEL], coords=coords), n_ch
        )
        if s.centred:
            log_beta = pm.Normal(
                "beta__log", pt.log(bc)[None, :], bs[None, :], dims=[fit_dim, CHANNEL]
            )
            beta_fit = pt.exp(log_beta)
        else:
            z = pm.Normal("beta__offset", 0.0, 1.0, dims=[fit_dim, CHANNEL])
            beta_fit = bc[None, :] * pt.exp(bs[None, :] * z)
        if not n_new:
            return pm.Deterministic("beta", beta_fit, dims=[GEO, CHANNEL])
        if strategy == "hyperprior":
            z_new = ctx.frozen_noise("beta__new_z", dims=(f"{GEO}__new", CHANNEL))
            beta_new = bc[None, :] * pt.exp(bs[None, :] * z_new)
        elif strategy == "pooled":
            beta_new = bc[None, :] * pt.ones((n_new, 1))
        else:  # zero: a new geo gets no media effect
            beta_new = pt.zeros((n_new, n_ch))
        pm.Deterministic("beta__new", beta_new, dims=[f"{GEO}__new", CHANNEL])
        return pm.Deterministic(
            "beta", pt.concatenate([beta_fit, beta_new], axis=0), dims=[GEO, CHANNEL]
        )

    # ------------------------------------------------------------------ fit
    def fit(
        self, ds: xr.Dataset, s: HierarchicalGeoSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        calibration: dict[str, Any] = {}
        s, self._lift = self._calibrate(ds, s, cfg, ctx, calibration)
        try:
            out = super().fit(ds, s, cfg, ctx)
        finally:
            self._lift = None
        if calibration:
            out.learned["calibration"] = calibration
            ll = out.groups.get("log_likelihood")
            if ll is not None and LIFT_VAR in ll:
                # elpd covers the KPI; the lift tests are calibration data
                out.groups["log_likelihood"] = ll.drop_vars(LIFT_VAR)
        post = out.groups["posterior"]
        # time-indexed deterministics are recomputed at score time; keeping every draw of
        # them would dominate the bundle size
        heavy = [str(v) for v in post.data_vars if TIME in post[v].dims]
        out.groups["posterior"] = post.drop_vars(heavy)
        out.learned["dropped_deterministics"] = list(sorted(heavy))
        out.learned["controls"] = list(control_vars(ds, s.controls))
        return out

    # ------------------------------------------------------------------ calibration
    def _calibrate(
        self,
        ds: xr.Dataset,
        s: HierarchicalGeoSettings,
        cfg: FitConfig,
        ctx: FitContext,
        record: dict[str, Any],
    ) -> tuple[HierarchicalGeoSettings, LiftDesign | None]:
        """Apply the spec's calibration block: translate business priors that no
        ``translate_priors`` run translated yet, and build the lift-test design."""
        cal = ctx.calibration
        if cal is None or cal.empty:
            return s, None
        scales = model_scales(ds, s, ctx.to_original)
        if cal.business_priors is not None and cal.prior_translation_run is None:
            seed = cfg.seed if cfg.seed is not None else ctx.seed
            outcome = translate_business_priors(
                cal.business_priors,
                scales,
                s,
                ds,
                draws=_TRANSLATION_SOLVE_DRAWS,
                seed=seed,
                pointer="/calibration/business_priors",
            )
            errors = [i for i in outcome.issues if i.severity is Severity.ERROR]
            if errors:
                raise MapiError(
                    errors[0].message,
                    code=ErrorCode.BUSINESS_PRIORS_INVALID,
                    pointer=errors[0].pointer,
                    issues=errors,
                )
            s = _with_priors(s, outcome.priors)
            record["translated_priors"] = outcome.priors
        lift = None
        if cal.lift_tests:
            lift = lift_design(cal.lift_tests, scales, log=ctx.info)
            record["lift_tests"] = {
                "used": [] if lift is None else list(lift.ids),
                "skipped": [] if lift is None else list(lift.skipped),
            }
        return s, lift

    def translate_priors(
        self,
        ds: xr.Dataset,
        s: HierarchicalGeoSettings,
        job: PriorTranslationJob,
        ctx: FitContext,
    ) -> PriorTranslation:
        """Business priors to ``beta_channel``/``intercept`` priors with a prior-predictive
        preview in business units (:mod:`mapi_plugin_mmm.calibration`)."""
        seed = job.seed if job.seed is not None else ctx.seed
        scales = model_scales(ds, s, ctx.to_original)
        ctx.progress(0.1, "solving for model-space priors")
        outcome = translate_business_priors(
            job.business_priors,
            scales,
            s,
            ds,
            draws=max(_TRANSLATION_SOLVE_DRAWS, job.draws),
            seed=seed,
        )
        ctx.check_cancelled()
        ctx.progress(0.6, "sampling the prior predictive")
        result, preview = prior_preview(
            outcome,
            scales,
            s,
            ds,
            draws=job.draws,
            seed=seed + 1,
            quantiles=job.quantiles,
            lift_tests=job.lift_tests,
        )
        ctx.progress(1.0, "priors translated")
        return PriorTranslation(result=result, preview=preview)

    # ------------------------------------------------------------------ derived
    def _curves(
        self, fitted: Fitted, ds: xr.Dataset, ctx: ScoreContext
    ) -> tuple[HierarchicalGeoSettings, ChannelCurves, xr.Dataset]:
        s = self.parse_settings(fitted.settings)
        post = draw_samples(fitted.posterior, ctx.draw_index)
        x = spend_tensor(ds, s.lag_dim)
        curves = ChannelCurves.from_samples(
            post,
            x,
            s,
            geos=np.asarray(ds[GEO].values),
            channels=np.asarray(ds[CHANNEL].values),
            times=np.asarray(ds[TIME].values),
        )
        return s, curves, post

    def derive(self, fitted: Fitted, name: str, ds: xr.Dataset, ctx: ScoreContext) -> xr.Dataset:
        """Derived quantities over ``ds`` (the model-stage fit data); see the module doc."""
        to_original = getattr(ctx, "to_original", None)
        s, curves, post = self._curves(fitted, ds, ctx)
        if name == "contributions":
            controls = control_vars(ds, s.controls)
            design = control_design(ds, controls)
            return contributions_dataset(curves, post, controls, design, to_original)
        if name == "response_curves":
            design = control_design(ds, control_vars(ds, s.controls))
            base = baseline_cube(curves, post, design)
            return response_curves_dataset(curves, s.grid, to_original, baseline=base)
        if name == "marginal_cost":
            return marginal_cost_dataset(curves, s.grid, to_original)
        raise MapiError(
            f"model {self.id!r} has no derived quantity {name!r}",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            pointer="/outputs/derived",
        )

    # ------------------------------------------------------------------ decision
    def objective(self, fitted: Fitted, decision: xr.Dataset, ctx: ScoreContext) -> xr.DataArray:
        """Expected total target of a budget, per posterior draw ``(chain, draw)``.

        ``decision["spend"]`` is spend **per period** over ``channel`` (split across geos in
        proportion to each channel's fit-window spend) or over ``(geo, channel)``; channels
        left out get zero. ``decision.attrs``: ``units`` - ``original`` (default; needs the
        ``response_curves`` derived quantity for the unit conversions) or ``model``;
        ``periods`` - number of periods the budget runs (default 1). The spend is held
        constant until the carry-over settles (steady state), so the value is
        ``periods x sum_{geo, channel} beta * saturation(spend * sum_l w_l)``.
        """
        s = self.parse_settings(fitted.settings)
        if "spend" not in decision:
            raise MapiError(
                "the decision needs a 'spend' variable over channel or (geo, channel)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/spend",
            )
        units = str(decision.attrs.get("units", "original"))
        if units not in ("original", "model"):
            raise MapiError(
                "decision units must be 'original' or 'model'",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/units",
            )
        periods = float(decision.attrs.get("periods", 1))
        if not periods > 0:
            raise MapiError(
                "decision periods must be > 0",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/periods",
            )
        geos = np.asarray(fitted.coords[GEO])
        channels = np.asarray(fitted.coords[CHANNEL])
        spend = self._decision_spend(fitted, decision["spend"], geos, channels, units)
        rc = fitted.derived.get("response_curves")
        target_scale = np.ones(geos.size)
        if units == "original":
            assert rc is not None  # checked in _decision_spend
            target_scale = np.asarray(rc["target_scale"].sel({GEO: geos}).values, dtype=float)
        posterior = fitted.posterior
        post = draw_samples(posterior, None)
        lags = fitted.coords.get(s.lag_dim)
        if lags is None or not len(lags):
            raise MapiError(
                f"the fitted model has no {s.lag_dim!r} coordinate to size the adstock",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        n_lag = len(lags)
        x = np.zeros((geos.size, channels.size, 1, n_lag))  # shapes only (steady state)
        curves = ChannelCurves.from_samples(
            post, x, s, geos=geos, channels=channels, times=np.arange(1)
        )
        per_period = curves.steady_state(spend)  # (sample, geo, channel)
        total = (per_period.sum(axis=2) * target_scale[None, :]).sum(axis=1) * periods
        out = xr.DataArray(
            total.reshape(posterior.sizes["chain"], posterior.sizes["draw"]),
            dims=["chain", "draw"],
            coords={"chain": posterior["chain"].values, "draw": posterior["draw"].values},
            name="objective",
        )
        out.attrs = {
            "units": "target (original units)" if units == "original" else "target (model units)",
            "periods": periods,
            "description": "expected total target of the budget at steady state",
        }
        return out

    def _decision_spend(
        self,
        fitted: Fitted,
        spend: xr.DataArray,
        geos: np.ndarray,
        channels: np.ndarray,
        units: str,
    ) -> np.ndarray:
        """Per-period model-unit spend ``(geo, channel)`` for a decision."""
        extra = sorted(set(map(str, spend.dims)) - {GEO, CHANNEL})
        if CHANNEL not in spend.dims or extra:
            raise MapiError(
                f"decision spend must be over channel or (geo, channel), got {list(spend.dims)}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/spend",
            )
        unknown = sorted(set(map(str, spend[CHANNEL].values)) - set(map(str, channels)))
        if unknown:
            raise MapiError(
                f"unknown channel(s) {unknown}",
                code=ErrorCode.UNSEEN_LEVELS,
                pointer="/decision/spend",
            )
        if GEO in spend.dims:
            bad = sorted(set(map(str, spend[GEO].values)) - set(map(str, geos)))
            if bad:
                raise MapiError(
                    f"geo(s) {bad} were not seen at fit; the objective covers fitted geos",
                    code=ErrorCode.UNSEEN_LEVELS,
                    pointer="/decision/spend",
                )
        values = np.asarray(spend.values, dtype=float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise MapiError(
                "decision spend must be finite and non-negative",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/spend",
            )
        rc = fitted.derived.get("response_curves")
        if units == "original" and rc is None:
            raise MapiError(
                "budgets in original units need the response_curves derived quantity; fit "
                "with outputs.derived: [response_curves] or pass units: model",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
                pointer="/decision/units",
            )
        if GEO in spend.dims:
            full = spend.reindex({GEO: geos, CHANNEL: channels}, fill_value=0.0)
            grid = np.asarray(full.transpose(GEO, CHANNEL).values, dtype=float)
        else:
            per_channel = np.asarray(
                spend.reindex({CHANNEL: channels}, fill_value=0.0).values, dtype=float
            )
            grid = per_channel[None, :] * self._geo_shares(fitted, geos, channels, units)
        if units == "original":
            assert rc is not None
            scale = np.asarray(
                rc["spend_scale"].sel({GEO: geos, CHANNEL: channels}).transpose(GEO, CHANNEL)
            )
            grid = grid / np.where(scale > 0, scale, 1.0)
        return grid

    def _geo_shares(
        self, fitted: Fitted, geos: np.ndarray, channels: np.ndarray, units: str
    ) -> np.ndarray:
        """``(geo, channel)`` share of each channel's fit-window spend by geo."""
        rc = fitted.derived.get("response_curves")
        if units == "original" and rc is not None:
            by_geo = rc["spend_by_geo"].sel(multiplier=1.0, method="nearest")
            tot = np.asarray(by_geo.sel({GEO: geos, CHANNEL: channels}).transpose(GEO, CHANNEL))
        else:
            data = fitted.groups.get("constant_data")
            if data is None or SPEND not in data:
                raise MapiError(
                    "the fitted model stores no spend to split a channel budget across geos; "
                    "give the decision over (geo, channel)",
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                )
            lag0 = data[SPEND].isel(
                {d: 0 for d in data[SPEND].dims if d not in (GEO, CHANNEL, TIME)}
            )
            tot = np.asarray(
                lag0.sum(TIME).sel({GEO: geos, CHANNEL: channels}).transpose(GEO, CHANNEL)
            )
        col = tot.sum(axis=0, keepdims=True)
        even = np.full_like(tot, 1.0 / max(geos.size, 1))
        return np.where(col > 0, tot / np.where(col > 0, col, 1.0), even)


def _with_priors(s: HierarchicalGeoSettings, priors: dict[str, Any]) -> HierarchicalGeoSettings:
    """``s`` with prior overrides replaced by ``priors`` (translated keys win)."""
    raw = s.model_dump(mode="json", exclude_none=True)
    raw["priors"] = {**raw.get("priors", {}), **priors}
    return HierarchicalGeoSettings.model_validate(raw)


def _lift_likelihood(
    lift: LiftDesign,
    weights: Any,
    sat_vectors: dict[str, Any],
    beta: Any,
    s: HierarchicalGeoSettings,
) -> None:
    """``kpi_delta ~ Normal(model lift, se)`` per lift test (see
    :mod:`mapi_plugin_mmm.calibration`); the model lift is kept as
    ``lift_tests__model(lift_test)``."""
    channel = lift.channel
    w = weights[channel]  # (cell, lag)
    a0 = (pt.as_tensor_variable(lift.x) * w).sum(axis=1)
    a1 = (pt.as_tensor_variable(lift.x + lift.dx) * w).sum(axis=1)
    params = {p: v[channel] for p, v in sat_vectors.items()}
    kind = s.saturation.kind
    gain = saturate(a1, kind, xp=pt, **params) - saturate(a0, kind, xp=pt, **params)
    cell = pt.as_tensor_variable(lift.target_scale) * beta[lift.geo, channel] * gain
    modelled = pm.Deterministic(
        f"{LIFT_VAR}__model", pt.dot(pt.as_tensor_variable(lift.segments()), cell), dims=LIFT_DIM
    )
    pm.Normal(LIFT_VAR, mu=modelled, sigma=lift.sigma, observed=lift.observed, dims=LIFT_DIM)
