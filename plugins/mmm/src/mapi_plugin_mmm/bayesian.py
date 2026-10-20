"""``mmm.bayesian``: mmm-framework's ``BayesianMMM`` behind the platform contract.

National or hierarchical over geos and/or products (MFF panels): geometric, delayed or
Weibull adstock; logistic, Hill, Michaelis-Menten, tanh or root saturation; none, linear,
piecewise, spline or GP trend; Fourier seasonality; partially pooled baselines (and
optionally channel effects) across geos; channel effects parameterised by their return on
spend. Business priors become ROI priors (:mod:`mapi_plugin_mmm.calibration`), lift tests
become mmm-framework experiment likelihoods, and budgets are optimised with
mmm-framework's ``planning`` (:mod:`mapi_plugin_mmm.planning`). See
:mod:`mapi_plugin_mmm.base` for fit, score and the bundle layout.

v1.1: time-varying channel effects (``time_varying``: a random walk on the log
coefficient, with a forecast policy after the fit window), count and proportion
likelihoods (``poisson``, ``negative_binomial``, ``beta``: channel effects are positive
coefficients on the link scale, without business priors or lift tests), a ``lift_tests``
bundle group, the ``adstock`` and ``effectiveness`` derived quantities, and model-level run
tracking (:mod:`mapi_plugin_mmm.tracking`).
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

import xarray as xr
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_sdk.models import FitContext, FitOutput, PriorTranslation
from mapi_spec.calibration import PriorTranslationJob
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import FitConfig

from mapi_plugin_mmm.base import FrameworkMMM, _clean, diagnostics_from
from mapi_plugin_mmm.calibration import (
    LiftPlan,
    check_calibration,
    lift_measurements,
    lift_test_group,
    prior_preview,
    translate_business_priors,
    unit_scales,
    with_priors,
)
from mapi_plugin_mmm.forward import LIKELIHOOD_LINK, Posterior, Replay
from mapi_plugin_mmm.framework import (
    APPROX_ENGINES,
    MCMC_ENGINES,
    PanelData,
    check_likelihood_data,
    engine_plan,
    framework_version,
    make_model,
)
from mapi_plugin_mmm.sampling import run_sampler
from mapi_plugin_mmm.settings import BayesianSettings, mcmc_fit_schema
from mapi_plugin_mmm.tracking import log_mmm_summary, summary_window

__all__ = ["MMMBayesian"]

_TRANSLATION_SOLVE_DRAWS: Final = 1000
"""Prior draws used when solving the baseline-share translation."""


class MMMBayesian(FrameworkMMM[BayesianSettings]):
    """mmm-framework's Bayesian marketing-mix model."""

    id = "mmm.bayesian"
    version = "1.1.0"
    Settings = BayesianSettings
    family = "mmm"
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "counterfactual", "evaluate"}),
        decision=True,
        contribution_scale="additive",
        identified_quantities=["y_hat", "contributions"],
        owns=frozenset({"scaling", "carryover", "inverse_units"}),
        deterministic_given_seed=False,
        checkpointable=False,
        calibration=frozenset({"business_priors", "lift_tests"}),
    )
    plots: ClassVar[list[str]] = [
        "core.ppc_fan",
        "core.convergence",
        "core.energy",
        "core.posterior_forest",
        "core.prior_posterior",
        "mmm.decomposition",
        "mmm.waterfall",
        "mmm.response_curves",
        "mmm.marginal_cost",
        "mmm.roi",
        "mmm.spend_effect",
        "mmm.adstock",
        "mmm.lift_tests",
        "mmm.effectiveness",
    ]
    resources = ResourceHint(
        min_class=ResourceClass.S, default_class=ResourceClass.M, est_fit_seconds=900
    )
    fit_schema: ClassVar[dict[str, Any]] = mcmc_fit_schema(
        engines=[*MCMC_ENGINES, *APPROX_ENGINES], default_engine="nutpie"
    )
    description = (
        "mmm-framework's Bayesian marketing-mix model: adstock and saturation per channel, "
        "trend and seasonality, national or hierarchical over geos and products, channel "
        "effects stated as returns on spend, business priors and lift tests as calibration, "
        "budget optimisation with mmm-framework planning."
    )
    default_engine = "nutpie"
    prior_scale_defaults: ClassVar[dict[str, Any]] = {
        "priors": {"roi_default": {"mu": 0.0, "sigma": 1.0}}
    }
    """The priors mmm-framework applies when the settings leave them out (governance's
    prior-sensitivity check scales them): ``roi ~ LogNormal(0, 1)`` for every channel."""

    # ------------------------------------------------------------------ fit
    def fit(
        self, ds: xr.Dataset, s: BayesianSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        data, freq = self._panel(ds, s, with_target=True)
        assert data.y is not None
        check_likelihood_data(data.y, s.likelihood)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        plan = engine_plan(
            cfg.engine,
            draws=cfg.draws,
            tune=cfg.tune,
            chains=cfg.chains,
            target_accept=cfg.target_accept,
            seed=seed,
            default_engine=self.default_engine,
        )
        calibration: dict[str, Any] = {}
        s, lift = self._calibrate(ds, data, freq, s, ctx, seed, calibration)
        if s.time_varying.channels and data.geos and s.hierarchy.media_by_geo:
            for ch in s.time_varying.channels:
                ctx.info(f"{ch} is time-varying, so its effect is the same in every geo")
        if not s.identity_link and data.geos and s.hierarchy.media_by_geo:
            ctx.info(
                f"with a {s.likelihood} likelihood, the effect of each channel is the same "
                "in every geo"
            )
        ctx.progress(0.05, "building the mmm-framework model")
        model = make_model(data, s, frequency=freq, plan=plan)
        if lift is not None and lift.measurements:
            model.add_experiment_calibration(lift.measurements)
        ctx.check_cancelled()
        ctx.progress(0.1, f"sampling ({plan.engine})")
        result = run_sampler(model.fit, plan, ctx)
        ctx.progress(0.9, "posterior ready")
        groups = self._groups(model, data, cfg, bayesian=True)
        if lift is not None:
            tests = lift_test_group(lift, groups["posterior"])
            if tests is not None:
                groups["lift_tests"] = _clean(tests)
        diagnostics: list[Diagnostic] = list(diagnostics_from(result.diagnostics))
        if plan.mcmc:
            free = [rv.name for rv in model.model.free_RVs]
            diagnostics += convergence_diagnostics(
                groups["posterior"], groups.get("sample_stats"), var_names=free
            )
        learned = self._learned(
            data,
            freq,
            model,
            {
                "engine": plan.engine,
                "method": plan.method,
                "draws": plan.draws,
                "tune": plan.tune,
                "chains": plan.chains,
                "fit_diagnostics": self._diagnostics_json(result.diagnostics),
            },
        )
        if calibration:
            learned["calibration"] = calibration
        if not s.identity_link:
            learned["media_prior"] = "coefficient"
        if ctx.tracking:
            ctx.progress(0.95, "summarising the fit")
            tvp = s.time_varying

            def replay(post: xr.Dataset) -> Replay:
                return Replay(model, data, Posterior.from_dataset(post), tvp_forecast=tvp.forecast)

            log_mmm_summary(
                ctx,
                summary_window(replay, groups["posterior"], data),
                params={
                    "mmm.framework_version": framework_version(),
                    "mmm.n_channels": len(data.channels),
                    "mmm.n_periods": data.n_periods,
                    "mmm.n_cells": data.n_cells,
                    "mmm.link": LIKELIHOOD_LINK[s.likelihood],
                    "mmm.time_varying": ",".join(tvp.channels),
                },
            )
        ctx.progress(1.0, "fit complete")
        return FitOutput(groups=groups, learned=learned, diagnostics=diagnostics)

    # ------------------------------------------------------------------ calibration
    def _calibrate(
        self,
        ds: xr.Dataset,
        data: PanelData,
        freq: str,
        s: BayesianSettings,
        ctx: FitContext,
        seed: int,
        record: dict[str, Any],
    ) -> tuple[BayesianSettings, LiftPlan | None]:
        """Apply the spec's calibration block: translate business priors that no
        ``translate_priors`` run translated yet; map lift tests to experiments."""
        cal = ctx.calibration
        if cal is None or cal.empty:
            return s, None
        check_calibration(
            s, business_priors=cal.business_priors is not None, lift_tests=bool(cal.lift_tests)
        )
        scales = unit_scales(ds, ctx.to_original)
        if cal.business_priors is not None and cal.prior_translation_run is None:
            ctx.progress(0.01, "translating business priors")
            outcome = translate_business_priors(
                cal.business_priors,
                data,
                s,
                scales,
                model_factory=lambda st: make_model(data, st, frequency=freq),
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
            s = with_priors(s, outcome.priors)
            record["translated_priors"] = outcome.priors
        lift: LiftPlan | None = None
        if cal.lift_tests:
            lift = lift_measurements(cal.lift_tests, data, scales)
            record["lift_tests"] = lift.record()
            for tid, why in lift.skipped.items():
                ctx.info(f"lift test {tid} skipped: {why}")
        return s, lift

    def translate_priors(
        self,
        ds: xr.Dataset,
        s: BayesianSettings,
        job: PriorTranslationJob,
        ctx: FitContext,
    ) -> PriorTranslation:
        """Business priors to ``priors.roi`` / ``priors.intercept`` with a prior-predictive
        preview in business units (:mod:`mapi_plugin_mmm.calibration`)."""
        check_calibration(s, business_priors=True, lift_tests=bool(job.lift_tests), pointer="")
        data, freq = self._panel(ds, s, with_target=True)
        seed = job.seed if job.seed is not None else ctx.seed
        scales = unit_scales(ds, ctx.to_original)

        def factory(st: BayesianSettings) -> Any:
            return make_model(data, st, frequency=freq)

        ctx.progress(0.1, "solving for model-space priors")
        outcome = translate_business_priors(
            job.business_priors,
            data,
            s,
            scales,
            model_factory=factory,
            draws=max(_TRANSLATION_SOLVE_DRAWS, job.draws),
            seed=seed,
        )
        ctx.check_cancelled()
        ctx.progress(0.6, "sampling the prior predictive")
        result, preview = prior_preview(
            outcome,
            data,
            s,
            scales,
            model_factory=factory,
            draws=job.draws,
            seed=seed + 1,
            quantiles=job.quantiles,
            lift_tests=job.lift_tests,
        )
        ctx.progress(1.0, "priors translated")
        return PriorTranslation(result=result, preview=preview)
