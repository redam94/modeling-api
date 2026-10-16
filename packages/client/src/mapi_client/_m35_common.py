"""M3.5 logic shared by the sync and async clients that does no I/O: workflow documents from
friendly arguments, request bodies, "is it done?" rules and the errors finished runs and
validations stand for."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from mapi_spec.api import (
    ArtifactLink,
    DataExploreRequest,
    Exploration,
    GovernancePolicyCreate,
    GovernancePolicyUpdate,
    ModelCompareRequest,
    ModelExploreRequest,
    ModelValidation,
    Optimization,
    OptimizeRequest,
    ScheduleUpdate,
)
from mapi_spec.errors import ErrorCode, MapiError, error_for_code
from mapi_spec.governance import PolicyCheck, PolicyMatch, ValidationStatus
from mapi_spec.views import explore_view_name
from mapi_spec.workflow import WorkflowRun, WorkflowRunStatus, WorkflowSpec
from pydantic import BaseModel, JsonValue

from mapi_client._common import run_error

__all__ = [
    "VALIDATION_WAITING",
    "WorkflowSource",
    "data_explore_body",
    "exploration_done",
    "exploration_error",
    "model_compare_body",
    "model_explore_body",
    "optimization_done",
    "optimization_error",
    "optimize_body",
    "policy_body",
    "policy_update_body",
    "schedule_update_body",
    "section_link",
    "validation_done",
    "workflow_run_error",
    "workflow_source",
]

WorkflowSource = WorkflowSpec | Mapping[str, Any] | str | os.PathLike[str]
"""A workflow document: a :class:`~mapi_spec.workflow.WorkflowSpec`, its JSON as a mapping,
YAML/JSON text, or the path of a ``.yaml``/``.json`` file."""

VALIDATION_WAITING: Final = frozenset({ValidationStatus.CHECKS_RUNNING})
"""Validation statuses that change by themselves (the checks are still running)."""


def _with_fields[M: BaseModel](
    model: type[M], body: M | Mapping[str, Any] | None, fields: Mapping[str, Any]
) -> M:
    """``body`` (a model or mapping) with keyword ``fields`` laid over it (``None`` ignored)."""
    if isinstance(body, model):
        base: dict[str, Any] = body.model_dump(mode="json", by_alias=True, exclude_unset=True)
    else:
        base = dict(body or {})
    base.update({k: v for k, v in fields.items() if v is not None})
    return model.model_validate(base)


# =============================================================================== workflows


def workflow_source(source: WorkflowSource) -> dict[str, Any]:
    """``{"document": ...}`` or ``{"text": ...}`` for a workflow create/validate/revise body.

    A ``str`` is YAML or JSON text; a :class:`os.PathLike` is read (UTF-8) and sent as text,
    so the server reports problems with the file's own line structure in mind.
    """
    if isinstance(source, WorkflowSpec):
        return {"document": source.model_dump(mode="json", by_alias=True, exclude_none=True)}
    if isinstance(source, os.PathLike):
        return {"text": Path(source).read_text("utf-8")}
    if isinstance(source, str):
        return {"text": source}
    if isinstance(source, Mapping):
        return {"document": dict(source)}
    raise TypeError(f"not a workflow document: {type(source).__name__}")


def workflow_run_error(run: WorkflowRun) -> MapiError | None:
    """The error a finished workflow run stands for (``None`` unless failed or cancelled)."""
    if run.status is WorkflowRunStatus.FAILED:
        if run.error is not None:
            failed = next((n.node_id for n in run.nodes if n.error is run.error), None)
            where = f" (node {failed})" if failed else ""
            return error_for_code(
                run.error.code,
                f"workflow run {run.run_id} failed{where}: {run.error.message}",
                untrusted_text=run.error.untrusted_text,
            )
        nodes = [n.node_id for n in run.nodes if n.status.value == "failed"]
        detail = f"workflow run {run.run_id} failed" + (
            f" (node {', '.join(nodes)})" if nodes else ""
        )
        return MapiError(detail, code=ErrorCode.INTERNAL_ERROR)
    if run.status is WorkflowRunStatus.CANCELLED:
        return MapiError(f"workflow run {run.run_id} was cancelled", code=ErrorCode.CANCELLED)
    return None


def schedule_update_body(
    *,
    cron: str | None,
    timezone: str | None,
    revision: int | None,
    params: Mapping[str, JsonValue] | None,
    inputs: Mapping[str, str] | None,
    active: bool | None,
    description: str | None,
) -> ScheduleUpdate:
    fields: dict[str, Any] = {
        "cron": cron,
        "timezone": timezone,
        "revision": revision,
        "params": None if params is None else dict(params),
        "inputs": None if inputs is None else dict(inputs),
        "active": active,
        "description": description,
    }
    return ScheduleUpdate.model_validate({k: v for k, v in fields.items() if v is not None})


# ============================================================================ explorations


def data_explore_body(
    request: DataExploreRequest | Mapping[str, Any] | None, fields: Mapping[str, Any]
) -> DataExploreRequest:
    return _with_fields(DataExploreRequest, request, fields)


def model_explore_body(
    request: ModelExploreRequest | Mapping[str, Any] | None, fields: Mapping[str, Any]
) -> ModelExploreRequest:
    return _with_fields(ModelExploreRequest, request, fields)


def model_compare_body(
    models: Sequence[str] | None,
    request: ModelCompareRequest | Mapping[str, Any] | None,
    fields: Mapping[str, Any],
) -> ModelCompareRequest:
    merged = dict(fields)
    if models is not None:
        merged["models"] = list(models)
    return _with_fields(ModelCompareRequest, request, merged)


def exploration_done(exploration: Exploration) -> bool:
    return exploration.done or exploration.run.state.terminal


def exploration_error(exploration: Exploration) -> MapiError | None:
    return run_error(exploration.run)


def section_link(exploration: Exploration, section: str) -> ArtifactLink:
    """The signed link of one section's view (``ui/explore/<section>.json``).

    Raises ``ValueError`` naming the sections there are, or why the section is missing
    (the explorer index says when a section was skipped, e.g. "less than two seasons").
    """
    name = explore_view_name(section)
    for link in exploration.views:
        if link.name == name or link.name.endswith(f"/{section}.json"):
            return link
    if exploration.index is not None:
        for ref in exploration.index.sections:
            if ref.section == section and ref.status != "ok":
                why = ref.message_plain or ref.status
                raise ValueError(f"section {section!r} was not computed: {why}")
    have = sorted(
        link.name.removeprefix("ui/explore/").removesuffix(".json")
        for link in exploration.views
        if link.name.startswith("ui/explore/")
    )
    raise ValueError(f"exploration {exploration.run.id} has no section {section!r} (it has {have})")


# =========================================================================== optimisation


def optimize_body(
    request: OptimizeRequest | Mapping[str, Any] | None, fields: Mapping[str, Any]
) -> OptimizeRequest:
    return _with_fields(OptimizeRequest, request, fields)


def optimization_done(opt: Optimization) -> bool:
    return opt.done or opt.run.state.terminal


def optimization_error(opt: Optimization) -> MapiError | None:
    return run_error(opt.run)


# ============================================================================== governance


def _checks(checks: Sequence[PolicyCheck | Mapping[str, Any] | str]) -> list[PolicyCheck]:
    out: list[PolicyCheck] = []
    for c in checks:
        if isinstance(c, PolicyCheck):
            out.append(c)
        elif isinstance(c, str):
            out.append(PolicyCheck(check=c))
        else:
            out.append(PolicyCheck.model_validate(dict(c)))
    return out


def policy_body(
    policy: GovernancePolicyCreate | Mapping[str, Any] | None,
    *,
    name: str | None = None,
    checks: Sequence[PolicyCheck | Mapping[str, Any] | str] | None = None,
    applies_to: PolicyMatch | Mapping[str, Any] | None = None,
    **fields: Any,
) -> GovernancePolicyCreate:
    """A policy from a :class:`GovernancePolicyCreate` (or mapping) and/or keyword fields.
    ``checks`` may be check ids (``"baseline.beats_naive"``) with the catalog defaults."""
    merged: dict[str, Any] = dict(fields)
    if name is not None:
        merged["name"] = name
    if checks is not None:
        merged["checks"] = [c.model_dump(mode="json") for c in _checks(checks)]
    if applies_to is not None:
        merged["applies_to"] = (
            applies_to.model_dump(mode="json")
            if isinstance(applies_to, PolicyMatch)
            else dict(applies_to)
        )
    return _with_fields(GovernancePolicyCreate, policy, merged)


def policy_update_body(
    policy: GovernancePolicyCreate | Mapping[str, Any] | None,
    *,
    expected_version: int | None,
    **fields: Any,
) -> GovernancePolicyUpdate:
    body = policy_body(policy, **fields).model_dump(mode="json", by_alias=True)
    if expected_version is not None:
        body["expected_version"] = expected_version
    return GovernancePolicyUpdate.model_validate(body)


def validation_done(validation: ModelValidation) -> bool:
    """The checks finished (or never started): the status no longer changes by itself."""
    return validation.status not in VALIDATION_WAITING
