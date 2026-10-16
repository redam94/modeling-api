"""``mmm.bayesian``: mmm-framework's ``BayesianMMM`` behind the platform contract.

National or hierarchical over geos and/or products (MFF panels): geometric, delayed or
Weibull adstock; logistic, Hill, Michaelis-Menten, tanh or root saturation; none, linear,
piecewise, spline or GP trend; Fourier seasonality; partially pooled baselines (and
optionally channel effects) across geos; channel effects parameterised by their return on
spend. Business priors become ROI priors (:mod:`mapi_plugin_mmm.calibration`), lift tests
become mmm-framework experiment likelihoods, and budgets are optimised with
mmm-framework's ``planning`` (:mod:`mapi_plugin_mmm.planning`). See
:mod:`mapi_plugin_mmm.base` for fit, score and the bundle layout.
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

from mapi_plugin_mmm.base import FrameworkMMM, diagnostics_from
from mapi_plugin_mmm.calibration import (
    lift_measurements,
    prior_preview,
    translate_business_priors,
    unit_scales,
    with_priors,
)
from mapi_plugin_mmm.framework import (
    APPROX_ENGINES,
    MCMC_ENGINES,
    PanelData,
    engine_plan,
    make_model,
    quiet,
)
from mapi_plugin_mmm.settings import BayesianSettings, mcmc_fit_schema

__all__ = ["MMMBayesian"]

_TRANSLATION_SOLVE_DRAWS: Final = 1000
"""Prior draws used when solving the baseline-share translation."""


class MMMBayesian(FrameworkMMM[BayesianSettings]):
    """mmm-framework's Bayesian marketing-mix model."""

    id = "mmm.bayesian"
    version = "1.0.0"
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
        "core.posterior_forest",
        "mmm.decomposition",
        "mmm.response_curves",
        "mmm.marginal_cost",
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

    # ------------------------------------------------------------------ fit
    def fit(
        self, ds: xr.Dataset, s: BayesianSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        data, freq = self._panel(ds, s, with_target=True)
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
        s, experiments = self._calibrate(ds, data, freq, s, ctx, seed, calibration)
        ctx.progress(0.05, "building the mmm-framework model")
        model = make_model(data, s, frequency=freq, plan=plan)
        if experiments:
            model.add_experiment_calibration(experiments)
        ctx.check_cancelled()
        ctx.progress(0.1, f"sampling ({plan.engine})")
        kwargs: dict[str, Any] = {"progressbar": False}
        if plan.method == "nuts":
            kwargs["nuts_sampler"] = plan.nuts_sampler
            kwargs["tune"] = plan.tune
            kwargs["chains"] = plan.chains
            if plan.target_accept is not None:
                kwargs["target_accept"] = plan.target_accept
        elif plan.method == "smc":
            kwargs["chains"] = plan.chains
        else:
            kwargs.pop("progressbar")
        try:
            with quiet():
                result = model.fit(
                    draws=plan.draws, random_seed=plan.seed, method=plan.method, **kwargs
                )
        except MapiError:
            raise
        except Exception as exc:
            raise MapiError(
                "mmm-framework failed to fit the model",
                code=ErrorCode.USER_CODE_ERROR,
                untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
            ) from exc
        ctx.check_cancelled()
        ctx.progress(0.9, "posterior ready")
        groups = self._groups(model, data, cfg, bayesian=True)
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
    ) -> tuple[BayesianSettings, list[Any]]:
        """Apply the spec's calibration block: translate business priors that no
        ``translate_priors`` run translated yet; map lift tests to experiments."""
        cal = ctx.calibration
        if cal is None or cal.empty:
            return s, []
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
        experiments: list[Any] = []
        if cal.lift_tests:
            plan = lift_measurements(cal.lift_tests, data, scales)
            experiments = plan.measurements
            record["lift_tests"] = plan.record()
            for tid, why in plan.skipped.items():
                ctx.info(f"lift test {tid} skipped: {why}")
        return s, experiments

    def translate_priors(
        self,
        ds: xr.Dataset,
        s: BayesianSettings,
        job: PriorTranslationJob,
        ctx: FitContext,
    ) -> PriorTranslation:
        """Business priors to ``priors.roi`` / ``priors.intercept`` with a prior-predictive
        preview in business units (:mod:`mapi_plugin_mmm.calibration`)."""
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
