"""``mmm.frequentist``: mmm-framework's fast frequentist marketing-mix model.

Same data contract, structure and outputs as ``mmm.bayesian`` (adstock, saturation,
trend, seasonality, geo/product baselines), estimated in seconds instead of minutes:
mmm-framework searches the adstock and saturation parameters by an out-of-sample
(rolling-origin) criterion, fixes them, solves the now-linear model by ridge regression
with non-negative channel effects, and repeats the solve on block-bootstrap resamples. The
bootstrap replicates are the "draws" of every output, so intervals are *confidence*
intervals, not posterior ones - there are no priors, no convergence diagnostics, and no
business priors or lift tests (use ``mmm.bayesian`` for those).

``estimator: constrained`` (a convex program with the same non-negativity) needs the
optional ``cvxpy`` dependency (mmm-framework's ``frequentist`` extra), which this
distribution does not install; the fit then fails with ``CAPABILITY_UNSUPPORTED``.
"""

from __future__ import annotations

from typing import Any, ClassVar

import xarray as xr
from mapi_sdk.models import FitContext, FitOutput
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import FitConfig

from mapi_plugin_mmm.base import FrameworkMMM
from mapi_plugin_mmm.framework import EnginePlan, make_model, quiet
from mapi_plugin_mmm.settings import FrequentistSettings

__all__ = ["MMMFrequentist", "cvxpy_available"]


def cvxpy_available() -> bool:
    """Is the optional ``cvxpy`` (the constrained estimator) importable?"""
    try:
        import cvxpy  # noqa: F401
    except ImportError:
        return False
    return True


class MMMFrequentist(FrameworkMMM[FrequentistSettings]):
    """mmm-framework's ridge estimator with bootstrap intervals."""

    id = "mmm.frequentist"
    version = "1.0.0"
    Settings = FrequentistSettings
    family = "mmm"
    capabilities = Capabilities(
        inference="bootstrap",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "counterfactual", "evaluate"}),
        decision=True,
        contribution_scale="additive",
        identified_quantities=["y_hat", "contributions"],
        owns=frozenset({"scaling", "carryover", "inverse_units"}),
        deterministic_given_seed=True,
        loo_valid=False,
        loo_invalid_reason="bootstrap replicates are not posterior draws",
    )
    plots: ClassVar[list[str]] = [
        "core.ppc_fan",
        "mmm.decomposition",
        "mmm.response_curves",
        "mmm.marginal_cost",
    ]
    resources = ResourceHint(
        min_class=ResourceClass.S, default_class=ResourceClass.S, est_fit_seconds=30
    )
    fit_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "mode": {
                "enum": ["evaluate_then_refit", "train_only"],
                "default": "evaluate_then_refit",
                "x-ui-level": "advanced",
            },
            "seed": {"type": "integer", "minimum": 0, "x-ui-level": "advanced"},
        },
    }
    description = (
        "mmm-framework's fast frequentist marketing-mix model: adstock and saturation chosen "
        "by an out-of-sample search, a ridge solve with non-negative channel effects and "
        "bootstrap intervals; seconds instead of minutes."
    )

    def fit(
        self, ds: xr.Dataset, s: FrequentistSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        if s.estimator == "constrained" and not cvxpy_available():
            raise MapiError(
                "the constrained estimator needs the optional cvxpy dependency, which is not "
                "installed; use estimator: ridge",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
                pointer="/model/settings/estimator",
            )
        data, freq = self._panel(ds, s, with_target=True)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        plan = EnginePlan(
            engine="ridge" if s.estimator == "ridge" else "constrained",
            method="frequentist",
            nuts_sampler=None,
            draws=s.bootstrap,
            tune=0,
            chains=1,
            target_accept=None,
            seed=int(seed),
        )
        ctx.progress(0.05, "building the mmm-framework model")
        model = make_model(data, s, frequency=freq, plan=plan)
        ctx.check_cancelled()
        ctx.progress(0.1, "searching transforms and bootstrapping")
        try:
            with quiet():
                result = model.fit(random_seed=int(seed))
        except MapiError:
            raise
        except Exception as exc:
            raise MapiError(
                "mmm-framework failed to fit the model",
                code=ErrorCode.USER_CODE_ERROR,
                untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
            ) from exc
        ctx.check_cancelled()
        groups = self._groups(model, data, cfg, bayesian=False)
        diag = self._diagnostics_json(result.diagnostics)
        diagnostics = [
            Diagnostic(
                code="FREQUENTIST_FIT",
                severity="info",
                message_plain="This is a fast first estimate. The ranges are bootstrap "
                "confidence intervals, not Bayesian intervals. Convergence checks do not apply.",
                message_expert=f"estimator={diag.get('estimator')}, "
                f"n_boot={diag.get('n_boot_effective')}, "
                f"selection={diag.get('selection_criterion')}",
            )
        ]
        learned = self._learned(
            data,
            freq,
            model,
            {"engine": plan.engine, "method": "frequentist", "draws": s.bootstrap,
             "fit_diagnostics": diag},
        )  # fmt: skip
        ctx.progress(1.0, "fit complete")
        return FitOutput(groups=groups, learned=learned, diagnostics=diagnostics)
