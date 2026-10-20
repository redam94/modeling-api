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

import _thread
import contextlib
import threading
from collections.abc import Callable, Iterator
from typing import Any, ClassVar, Final

import xarray as xr
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_pymc.engines import (
    EngineHooks,
    nutpie_progress,
    pymc_draw_callback,
    pymc_nutpie_progress,
)
from mapi_sdk.errors import RunCancelled
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
    EnginePlan,
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
        live: contextlib.AbstractContextManager[Any] = contextlib.nullcontext()
        if plan.method == "nuts":
            kwargs["nuts_sampler"] = plan.nuts_sampler
            kwargs["tune"] = plan.tune
            kwargs["chains"] = plan.chains
            if plan.target_accept is not None:
                kwargs["target_accept"] = plan.target_accept
            hooks = EngineHooks.for_context(ctx, progress=_window(ctx, 0.1, 0.9))
            if plan.nuts_sampler == "nutpie":
                live = _nutpie_live(hooks, ctx)
            elif plan.nuts_sampler == "pymc":
                kwargs["callback"] = _pymc_callback(hooks, plan)
        elif plan.method == "smc":
            kwargs["chains"] = plan.chains
        else:
            kwargs.pop("progressbar")
        try:
            with quiet(), live:
                result = model.fit(
                    draws=plan.draws, random_seed=plan.seed, method=plan.method, **kwargs
                )
        except MapiError:
            raise
        except KeyboardInterrupt:
            # nutpie stops on the interrupt the cancel request raised (see _nutpie_live)
            if _cancelled(ctx):
                raise RunCancelled("run was cancelled while sampling") from None
            raise
        except Exception as exc:
            if _cancelled(ctx):
                raise RunCancelled("run was cancelled while sampling") from exc
            raise MapiError(
                "mmm-framework failed to fit the model",
                code=ErrorCode.USER_CODE_ERROR,
                untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
            ) from exc
        # an aborted nutpie run returns its partial trace: never make a model from it
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


# ---------------------------------------------------------------------- live sampling


def _window(ctx: FitContext, lo: float, hi: float) -> Callable[[float, str], None]:
    def report(fraction: float, message: str) -> None:
        ctx.progress(lo + (hi - lo) * fraction, message)

    return report


def _cancelled(ctx: FitContext) -> bool:
    return ctx.cancel is not None and ctx.cancel.cancelled


@contextlib.contextmanager
def _nutpie_live(hooks: EngineHooks, ctx: FitContext) -> Iterator[None]:
    """Live progress, chains and step metrics of PyMC's nutpie sampler; a cancel request
    interrupts the main thread once (nutpie's blocking ``sample`` then stops and returns
    its partial trace, which :meth:`MMMBayesian.fit` discards)."""
    report = nutpie_progress(hooks)
    interrupted = threading.Event()

    def on_progress(progress: list[Any]) -> None:
        report(progress)
        if _cancelled(ctx) and not interrupted.is_set():
            interrupted.set()
            _thread.interrupt_main()

    with pymc_nutpie_progress(on_progress):
        yield


def _pymc_callback(hooks: EngineHooks, plan: EnginePlan) -> Callable[[Any, Any], None]:
    """``pm.sample`` callback for PyMC's own NUTS: progress, chains, the step metrics
    ``step_size``, ``num_steps``, ``tree_depth`` and cumulative ``divergences`` per chain
    (as the sdk-pymc ``pymc`` engine) and cancellation (``RunCancelled`` raised here stops
    the sampler)."""
    return pymc_draw_callback(hooks, plan)
