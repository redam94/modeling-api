"""``optimize``: budget optimisation against a fitted bundle, in the sandbox (M3.5).

The run loads the verified bundle, hands the request to the model plugin's ``optimize``
hook (``mmm.bayesian``/``mmm.frequentist``: mmm-framework's ``planning`` over the
posterior's response curves) and writes::

    optimize.json   OptimizeResult: the allocation per channel (and geo) with its expected
                    contribution and marginal return, the expected KPI of the plan and of
                    the current plan, the uplift and the chance it is positive, the expected
                    regret, the shadow price and the efficient frontier
    plots.json      mmm.budget_plans: current vs optimal allocation and expected KPI

The control plane reads only the size-capped JSON (never arrays).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.models import BudgetPlan, ScoreContext
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.optimize import OPTIMIZE_OUTPUT, OptimizeJob, OptimizeResult
from mapi_spec.run import RunnerPhase

from mapi_runner.worker.context import WorkerContext
from mapi_runner.worker.data import load_bundle
from mapi_runner.worker.plots import render_for

__all__ = ["OPTIMIZE_PLOTS", "label_result", "run_optimize"]

OPTIMIZE_PLOTS = ("mmm.budget_plans",)
"""Plots of an optimize run (rendered from the plan's ``/plans`` group)."""


def _units(bundle: Bundle, var: str) -> str | None:
    rs = bundle.resolved
    stage = rs.stages.get(rs.model_stage)
    spec = stage.vars.get(var) if stage is not None else None
    units = spec.units if spec is not None else None
    return units[:64] if isinstance(units, str) and units else None


def label_result(result: OptimizeResult, bundle: Bundle) -> OptimizeResult:
    """The plugin's result with the KPI's dataset name, the KPI and spend units of the model
    stage and the time it was computed."""
    targets = bundle.model_schema.targets if bundle.model_schema is not None else []
    target = targets[0] if targets else "y"
    try:
        kpi = bundle.binding.to_dataset(target)
    except (KeyError, ValueError):
        kpi = target
    return result.model_copy(
        update={
            "kpi": str(kpi)[:128],
            "kpi_units": _units(bundle, target),
            "spend_units": _units(bundle, "spend"),
            "computed_at": datetime.now(UTC),
        }
    )


def _hook(plugin: Any, request: Any, fitted: Any, sctx: ScoreContext, model_id: str) -> BudgetPlan:
    hook = getattr(plugin, "optimize", None)
    if not callable(hook):
        raise MapiError(
            f"model {model_id!r} does not optimise budgets",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    try:
        out = hook(fitted, request, sctx)
    except MapiError:
        raise
    except NotImplementedError:
        raise MapiError(
            f"model {model_id!r} does not optimise budgets",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        ) from None
    except Exception as exc:
        raise MapiError(
            f"model {model_id!r} failed while optimising the budget",
            code=ErrorCode.USER_CODE_ERROR,
            untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
        ) from exc
    if not isinstance(out, BudgetPlan):
        raise MapiError(
            f"model {model_id!r} optimize() must return a BudgetPlan",
            code=ErrorCode.USER_CODE_ERROR,
        )
    return out


def run_optimize(ctx: WorkerContext, job: OptimizeJob) -> OptimizeResult:
    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the bundle")
    bundle = load_bundle(ctx)
    manifest = bundle.model.manifest
    model_id = f"{manifest.id}@{manifest.version}"
    if not manifest.capabilities.decision:
        raise MapiError(
            f"model {model_id!r} does not optimise budgets (no decision capability)",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    plugin = ctx.registry.model(model_id)()
    seed = job.request.seed if job.request.seed is not None else bundle.model.fit_config.seed or 0
    sctx = ScoreContext(
        mode="predict",
        kind="expected",
        seed=int(seed),
        schema=bundle.model.schema,
        policies=bundle.resolved.model.effective_policies,
        log=ctx.log,
        cancel=ctx.cancel,
    )
    ctx.state(RunnerPhase.DERIVE, "optimising the budget over the posterior")
    ctx.progress(0.1, "building response curves per posterior draw")
    plan = _hook(plugin, job.request, bundle.model, sctx, model_id)
    result = OptimizeResult.model_validate(
        label_result(plan.result, bundle).model_dump(mode="json")
    )
    ctx.write_json(OPTIMIZE_OUTPUT, result.model_dump(mode="json", exclude_none=True))
    ctx.state(RunnerPhase.PLOTS, "rendering the plan")
    if plan.plans.data_vars:
        tree = xr.DataTree.from_dict(
            {"/": xr.Dataset(attrs={"source": "optimize"}), "/plans": plan.plans}
        )
        ctx.write_json("plots.json", render_for(ctx, OPTIMIZE_PLOTS, [tree]))
    for note in result.notes_plain[:20]:
        ctx.warning("OPTIMIZE_NOTE", note)
    if result.uplift is not None:
        ctx.metric("expected_uplift", float(result.uplift.mean))
    if result.prob_positive_uplift is not None:
        ctx.metric("prob_positive_uplift", float(result.prob_positive_uplift))
    ctx.progress(1.0, "budget plan ready", force=True)
    return result
