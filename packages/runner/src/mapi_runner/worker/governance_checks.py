"""The governance checks as workflow check runners (entry point ``mapi.governance_checks``).

``governance.policy`` and ``governance.check`` workflow nodes run the checks their policy
requires through *check runners* (``(CheckContext) -> CheckResult``, see
:mod:`mapi_workflows.runtime.governance`). This module exports every check of
:data:`~mapi_runner.worker.governance.CHECK_IMPLEMENTATIONS` in that form, so a workflow
measures a model exactly as a ``governance`` run does: the same family-aware
implementations, applicability rules and refit budget.

The checks of one node share a :class:`~mapi_runner.worker.governance.GovernanceEnv` (its
predictions and its :class:`~mapi_runner.worker.refit.Refitter`, so the node's refits count
against one :class:`~mapi_spec.governance.RefitBudget`). The data is the data the model
was fitted on when the workflow carries it, else the node's ``data`` input.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Final

from mapi_sdk.registry import Registry, default_registry
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.governance import CheckResult, RefitBudget

from mapi_runner.worker.governance import CHECK_IMPLEMENTATIONS, GovernanceEnv, measure
from mapi_runner.worker.refit import Refitter

__all__ = ["WORKFLOW_REFIT_BUDGET", "check_runners", "env_for"]

WORKFLOW_REFIT_BUDGET: Final = RefitBudget()
"""The refit budget of the checks of one workflow node (the platform default)."""

_MAX_ENVS: Final = 4
_ENVS: OrderedDict[tuple[int, int, int], tuple[Any, Any, GovernanceEnv]] = OrderedDict()


def env_for(ctx: Any) -> GovernanceEnv:
    """The shared :class:`GovernanceEnv` of ``ctx`` (a workflow ``CheckContext``)."""
    model = ctx.model
    bundle = model.bundle
    data = model.data if getattr(model, "data", None) is not None else None
    if data is None and ctx.data is not None:
        data = ctx.data.data
    if data is None:
        raise MapiError(
            "the check needs the data the model was fitted on; connect the node's 'data' input",
            code=ErrorCode.SCHEMA_INVALID,
        )
    registry: Registry = ctx.registry if ctx.registry is not None else default_registry()
    key = (id(bundle), id(data), int(ctx.seed))
    hit = _ENVS.get(key)
    if hit is not None and hit[0] is bundle and hit[1] is data:
        _ENVS.move_to_end(key)
        return hit[2]
    refitter = Refitter(bundle, registry, WORKFLOW_REFIT_BUDGET, cancel=ctx.cancel)
    log = ctx.log if ctx.log is not None else (lambda _m: None)
    env = GovernanceEnv(bundle, data, registry, refitter, seed=int(ctx.seed), log=log)
    _ENVS[key] = (bundle, data, env)
    while len(_ENVS) > _MAX_ENVS:
        _ENVS.popitem(last=False)
    return env


def _runner(check_id: str) -> Callable[[Any], CheckResult]:
    def run(ctx: Any) -> CheckResult:
        if ctx.check.check != check_id:  # pragma: no cover - the node looks runners up by id
            raise MapiError(f"runner of {check_id!r} got {ctx.check.check!r}")
        return measure(env_for(ctx), ctx.check)

    run.__name__ = f"check_{check_id.replace('.', '_')}"
    return run


def check_runners() -> dict[str, Callable[[Any], CheckResult]]:
    """Every governance check, by id, as a workflow check runner."""
    return {check_id: _runner(check_id) for check_id in CHECK_IMPLEMENTATIONS}
