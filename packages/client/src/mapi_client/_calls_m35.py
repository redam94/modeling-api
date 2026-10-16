"""Request builders for the M3.5 rows of ``mapi_spec.api.ENDPOINTS`` (workflows, governance,
explorers, budget optimisation and About cards, v0.5): one pure function per operation
(operation ids in comments).

Like :mod:`mapi_client._calls` they validate arguments, encode paths and return a
:class:`~mapi_client._http.Call`; ``tests/test_endpoint_coverage.py`` checks every one of them
against the contract (method, path, statuses, body, query, headers and project scoping).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from mapi_spec.about import CHOOSER_QUESTIONS, AboutKind, ChooserAnswer
from mapi_spec.api import (
    API_PREFIX,
    ApprovalCreate,
    ArtifactLink,
    CatalogAbout,
    CheckResult,
    ChooserQuestionnaire,
    ChooserResult,
    DataExploreRequest,
    DatasetAbout,
    DatasetAboutUpdate,
    EvidencePack,
    EvidencePackRequest,
    Exploration,
    GovernanceCheckDef,
    GovernancePolicy,
    GovernancePolicyCreate,
    GovernancePolicyUpdate,
    ModelCompareRequest,
    ModelExploreRequest,
    ModelValidation,
    NodeImplementation,
    NodeRun,
    Optimization,
    OptimizeRequest,
    Page,
    ScheduleCreate,
    ScheduleUpdate,
    TriggerCreate,
    ValidationNote,
    ValidationRequest,
    Workflow,
    WorkflowCreate,
    WorkflowRevision,
    WorkflowRevisionCreate,
    WorkflowRun,
    WorkflowRunCreate,
    WorkflowSchedule,
    WorkflowTemplate,
    WorkflowTrigger,
    WorkflowUpdate,
    WorkflowValidateRequest,
    WorkflowValidation,
)
from mapi_spec.governance import PolicyScope, ValidationStatus
from mapi_spec.workflow import NodeRunStatus, WorkflowEvent, WorkflowRunStatus
from mapi_spec.workflow_nodes import NodeKind

from mapi_client._calls import page_params, parse_none
from mapi_client._calls_m3 import prefer_wait_headers
from mapi_client._http import Call, dump, idempotency_headers, parse_model, segment

__all__ = [
    "CHOOSER_QUESTION_IDS",
    "chooser_params",
    "workflow_events_path",
]

V1: Final = API_PREFIX

_CREATED: Final = frozenset({201})
_ACCEPTED: Final = frozenset({202})
_ACCEPTED_OR_REPLAY: Final = frozenset({200, 202})
_NO_CONTENT: Final = frozenset({204})

CHOOSER_QUESTION_IDS: Final = tuple(q.id for q in CHOOSER_QUESTIONS)
"""The chooser's question ids, in the order to ask them (``goal``, ``data_shape``, ...)."""

_RECOMMEND_LIMIT_MAX: Final = 50


def _mv(model_id: str, version: int) -> str:
    return f"{V1}/models/{segment(model_id)}/versions/{segment(version)}"


def _flag(value: bool | None) -> str | None:
    return None if value is None else ("true" if value else "false")


# ============================================================== About cards and the chooser


def list_catalog_abouts(
    limit: int | None, cursor: str | None, *, kind: AboutKind | str | None = None
) -> Call[Page[CatalogAbout]]:
    params = page_params(limit, cursor, kind=None if kind is None else AboutKind(kind).value)
    return Call(
        "GET",
        f"{V1}/catalog/abouts",
        parse_model(Page[CatalogAbout]),
        params=params,
        scoped=False,
    )


def get_chooser() -> Call[ChooserQuestionnaire]:
    return Call("GET", f"{V1}/catalog/chooser", parse_model(ChooserQuestionnaire), scoped=False)


def chooser_params(answers: Mapping[str, str | Sequence[str]]) -> dict[str, list[str]]:
    """The recommendations query for chooser ``answers`` (question id -> one answer or
    several), checked against the questions first so a typo fails here, not as a ``422``."""
    params: dict[str, list[str]] = {}
    for question, raw in answers.items():
        values = [raw] if isinstance(raw, str) else list(raw)
        ChooserAnswer(question_id=question, values=values)  # raises ValueError when unknown
        params[question] = values
    return params


def recommend_catalog_items(
    answers: Mapping[str, str | Sequence[str]],
    *,
    kind: AboutKind | str | None = None,
    limit: int | None = None,
) -> Call[ChooserResult]:
    params: dict[str, str | int | list[str]] = dict(chooser_params(answers))
    if kind is not None:
        params["kind"] = AboutKind(kind).value
    if limit is not None:
        if not 1 <= limit <= _RECOMMEND_LIMIT_MAX:
            raise ValueError(f"limit must be between 1 and {_RECOMMEND_LIMIT_MAX}")
        params["limit"] = limit
    return Call(
        "GET",
        f"{V1}/catalog/recommendations",
        parse_model(ChooserResult),
        params=params,
        scoped=False,
    )


def list_workflow_nodes(
    limit: int | None, cursor: str | None, *, kind: NodeKind | str | None = None
) -> Call[Page[NodeImplementation]]:
    params = page_params(limit, cursor, kind=None if kind is None else NodeKind(kind).value)
    return Call(
        "GET",
        f"{V1}/catalog/workflow-nodes",
        parse_model(Page[NodeImplementation]),
        params=params,
        scoped=False,
    )


def get_workflow_node(node_id: str) -> Call[NodeImplementation]:
    path = f"{V1}/catalog/workflow-nodes/{segment(node_id)}"
    return Call("GET", path, parse_model(NodeImplementation), scoped=False)


def list_governance_checks(limit: int | None, cursor: str | None) -> Call[Page[GovernanceCheckDef]]:
    return Call(
        "GET",
        f"{V1}/catalog/governance-checks",
        parse_model(Page[GovernanceCheckDef]),
        params=page_params(limit, cursor),
        scoped=False,
    )


# ============================================================ datasets: About and explorer


def get_dataset_about(dataset_id: str) -> Call[DatasetAbout]:
    return Call("GET", f"{V1}/datasets/{segment(dataset_id)}/about", parse_model(DatasetAbout))


def update_dataset_about(dataset_id: str, body: DatasetAboutUpdate) -> Call[DatasetAbout]:
    # sets the description to the given value: safe to resend
    return Call(
        "PATCH",
        f"{V1}/datasets/{segment(dataset_id)}/about",
        parse_model(DatasetAbout),
        json=body.model_dump(mode="json", by_alias=True),
        retry_safe=True,
    )


def explore_dataset_version(
    dataset_id: str, version: int, body: DataExploreRequest, idempotency_key: str | None
) -> Call[Exploration]:
    path = f"{V1}/datasets/{segment(dataset_id)}/versions/{segment(version)}/explore"
    return Call(
        "POST",
        path,
        parse_model(Exploration),
        _ACCEPTED_OR_REPLAY,  # 200: the same request was explored before (cached)
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


def get_exploration(run_id: str) -> Call[Exploration]:
    return Call("GET", f"{V1}/explorations/{segment(run_id)}", parse_model(Exploration))


# ================================================== models: explorer, comparison, optimiser


def explore_model_version(
    model_id: str, version: int, body: ModelExploreRequest, idempotency_key: str | None
) -> Call[Exploration]:
    return Call(
        "POST",
        f"{_mv(model_id, version)}/explore",
        parse_model(Exploration),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


def compare_model_versions(
    body: ModelCompareRequest, idempotency_key: str | None
) -> Call[Exploration]:
    return Call(
        "POST",
        f"{V1}/models:compare",
        parse_model(Exploration),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


def optimize_model_version(
    model_id: str,
    version: int,
    body: OptimizeRequest,
    *,
    wait: int | None,
    idempotency_key: str | None,
) -> Call[Optimization]:
    headers = prefer_wait_headers(wait, idempotency_key)
    return Call(
        "POST",
        f"{_mv(model_id, version)}:optimize",
        parse_model(Optimization),
        _ACCEPTED_OR_REPLAY,  # 200: done within the wait, 202: still running
        json=dump(body),
        headers=headers,
        server_wait=float(wait) if wait else None,
    )


def get_optimization(run_id: str) -> Call[Optimization]:
    return Call("GET", f"{V1}/optimizations/{segment(run_id)}", parse_model(Optimization))


# =============================================================================== workflows


def _wf(workflow_id: str) -> str:
    return f"{V1}/workflows/{segment(workflow_id)}"


def _wfr(run_id: str) -> str:
    return f"{V1}/workflow-runs/{segment(run_id)}"


def list_workflows(
    limit: int | None, cursor: str | None, *, archived: bool | None = None
) -> Call[Page[Workflow]]:
    return Call(
        "GET",
        f"{V1}/workflows",
        parse_model(Page[Workflow]),
        params=page_params(limit, cursor, archived=_flag(archived)),
    )


def create_workflow(body: WorkflowCreate) -> Call[Workflow]:
    # a second create of the same name is a 409, never a duplicate: not resent blindly
    return Call("POST", f"{V1}/workflows", parse_model(Workflow), _CREATED, json=dump(body))


def validate_workflow(body: WorkflowValidateRequest) -> Call[WorkflowValidation]:
    # static checks only: safe to resend
    return Call(
        "POST",
        f"{V1}/workflows:validate",
        parse_model(WorkflowValidation),
        json=dump(body),
        retry_safe=True,
    )


def get_workflow(workflow_id: str) -> Call[Workflow]:
    return Call("GET", _wf(workflow_id), parse_model(Workflow))


def update_workflow(workflow_id: str, body: WorkflowUpdate) -> Call[Workflow]:
    return Call("PATCH", _wf(workflow_id), parse_model(Workflow), json=dump(body), retry_safe=True)


def delete_workflow(workflow_id: str) -> Call[None]:
    return Call("DELETE", _wf(workflow_id), parse_none, _NO_CONTENT)


def list_workflow_revisions(
    workflow_id: str, limit: int | None, cursor: str | None
) -> Call[Page[WorkflowRevision]]:
    return Call(
        "GET",
        f"{_wf(workflow_id)}/revisions",
        parse_model(Page[WorkflowRevision]),
        params=page_params(limit, cursor),
    )


def create_workflow_revision(
    workflow_id: str, body: WorkflowRevisionCreate
) -> Call[WorkflowRevision]:
    return Call(
        "POST",
        f"{_wf(workflow_id)}/revisions",
        parse_model(WorkflowRevision),
        _CREATED,
        json=dump(body),
    )


def get_workflow_revision(workflow_id: str, revision: int) -> Call[WorkflowRevision]:
    path = f"{_wf(workflow_id)}/revisions/{segment(revision)}"
    return Call("GET", path, parse_model(WorkflowRevision))


def start_workflow_run(
    workflow_id: str, body: WorkflowRunCreate, idempotency_key: str | None
) -> Call[WorkflowRun]:
    return Call(
        "POST",
        f"{_wf(workflow_id)}/runs",
        parse_model(WorkflowRun),
        _ACCEPTED_OR_REPLAY,  # 200: an idempotent replay returns the first run
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


def list_workflow_runs(
    workflow_id: str,
    limit: int | None,
    cursor: str | None,
    *,
    status: WorkflowRunStatus | str | None = None,
) -> Call[Page[WorkflowRun]]:
    st = None if status is None else WorkflowRunStatus(status).value
    return Call(
        "GET",
        f"{_wf(workflow_id)}/runs",
        parse_model(Page[WorkflowRun]),
        params=page_params(limit, cursor, status=st),
    )


def get_workflow_run(run_id: str) -> Call[WorkflowRun]:
    return Call("GET", _wfr(run_id), parse_model(WorkflowRun))


def cancel_workflow_run(run_id: str) -> Call[WorkflowRun]:
    # cancelling twice is cancelling once
    return Call(
        "POST", f"{_wfr(run_id)}:cancel", parse_model(WorkflowRun), _ACCEPTED, retry_safe=True
    )


def workflow_events_path(run_id: str) -> str:
    return f"{_wfr(run_id)}/events"


def workflow_run_events(
    run_id: str, after: int | None, limit: int | None
) -> Call[Page[WorkflowEvent]]:
    """The persisted events as a JSON page (``after``: the last ``seq`` seen). The live
    stream (``Accept: text/event-stream``) is followed by ``workflows.stream_events``."""
    params: dict[str, str | int] = page_params(limit)
    if after is not None:
        if after < 0:
            raise ValueError("after must be >= 0")
        params["after"] = after
    return Call(
        "GET",
        workflow_events_path(run_id),
        parse_model(Page[WorkflowEvent]),
        params=params,
        headers={"Accept": "application/json"},
    )


def list_workflow_node_runs(
    run_id: str,
    limit: int | None,
    cursor: str | None,
    *,
    status: NodeRunStatus | str | None = None,
) -> Call[Page[NodeRun]]:
    st = None if status is None else NodeRunStatus(status).value
    return Call(
        "GET",
        f"{_wfr(run_id)}/nodes",
        parse_model(Page[NodeRun]),
        params=page_params(limit, cursor, status=st),
    )


def get_workflow_node_run(run_id: str, node_id: str) -> Call[NodeRun]:
    return Call("GET", f"{_wfr(run_id)}/nodes/{segment(node_id)}", parse_model(NodeRun))


def get_workflow_node_output(run_id: str, node_id: str, port: str) -> Call[ArtifactLink]:
    path = f"{_wfr(run_id)}/nodes/{segment(node_id)}/outputs/{segment(port)}"
    return Call("GET", path, parse_model(ArtifactLink))


def list_workflow_schedules(
    workflow_id: str, limit: int | None, cursor: str | None
) -> Call[Page[WorkflowSchedule]]:
    return Call(
        "GET",
        f"{_wf(workflow_id)}/schedules",
        parse_model(Page[WorkflowSchedule]),
        params=page_params(limit, cursor),
    )


def create_workflow_schedule(workflow_id: str, body: ScheduleCreate) -> Call[WorkflowSchedule]:
    return Call(
        "POST",
        f"{_wf(workflow_id)}/schedules",
        parse_model(WorkflowSchedule),
        _CREATED,
        json=dump(body),
    )


def update_workflow_schedule(
    workflow_id: str, schedule_id: str, body: ScheduleUpdate
) -> Call[WorkflowSchedule]:
    return Call(
        "PATCH",
        f"{_wf(workflow_id)}/schedules/{segment(schedule_id)}",
        parse_model(WorkflowSchedule),
        json=dump(body),
        retry_safe=True,
    )


def delete_workflow_schedule(workflow_id: str, schedule_id: str) -> Call[None]:
    path = f"{_wf(workflow_id)}/schedules/{segment(schedule_id)}"
    return Call("DELETE", path, parse_none, _NO_CONTENT)


def list_workflow_triggers(
    workflow_id: str, limit: int | None, cursor: str | None
) -> Call[Page[WorkflowTrigger]]:
    return Call(
        "GET",
        f"{_wf(workflow_id)}/triggers",
        parse_model(Page[WorkflowTrigger]),
        params=page_params(limit, cursor),
    )


def create_workflow_trigger(workflow_id: str, body: TriggerCreate) -> Call[WorkflowTrigger]:
    return Call(
        "POST",
        f"{_wf(workflow_id)}/triggers",
        parse_model(WorkflowTrigger),
        _CREATED,
        json=dump(body),
    )


def delete_workflow_trigger(workflow_id: str, trigger_id: str) -> Call[None]:
    path = f"{_wf(workflow_id)}/triggers/{segment(trigger_id)}"
    return Call("DELETE", path, parse_none, _NO_CONTENT)


def list_workflow_templates(limit: int | None, cursor: str | None) -> Call[Page[WorkflowTemplate]]:
    return Call(
        "GET",
        f"{V1}/workflow-templates",
        parse_model(Page[WorkflowTemplate]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def get_workflow_template(template_id: str) -> Call[WorkflowTemplate]:
    path = f"{V1}/workflow-templates/{segment(template_id)}"
    return Call("GET", path, parse_model(WorkflowTemplate), scoped=False)


# ============================================================================ governance


_POL: Final = f"{V1}/governance/policies"


def _tpol(tenant_id: str) -> str:
    return f"{V1}/tenants/{segment(tenant_id)}/governance/policies"


def list_policies(
    limit: int | None, cursor: str | None, *, scope: PolicyScope | None = None
) -> Call[Page[GovernancePolicy]]:
    if scope is not None and scope not in ("tenant", "project"):
        raise ValueError("scope must be 'tenant' or 'project'")
    return Call(
        "GET",
        _POL,
        parse_model(Page[GovernancePolicy]),
        params=page_params(limit, cursor, scope=scope),
    )


def create_policy(body: GovernancePolicyCreate) -> Call[GovernancePolicy]:
    return Call("POST", _POL, parse_model(GovernancePolicy), _CREATED, json=dump(body))


def get_policy(policy_id: str) -> Call[GovernancePolicy]:
    return Call("GET", f"{_POL}/{segment(policy_id)}", parse_model(GovernancePolicy))


def update_policy(policy_id: str, body: GovernancePolicyUpdate) -> Call[GovernancePolicy]:
    # PUT saves a new version each time: only resent when it names the version it replaces
    return Call(
        "PUT",
        f"{_POL}/{segment(policy_id)}",
        parse_model(GovernancePolicy),
        json=dump(body),
    )


def delete_policy(policy_id: str) -> Call[None]:
    return Call("DELETE", f"{_POL}/{segment(policy_id)}", parse_none, _NO_CONTENT)


def list_policy_versions(
    policy_id: str, limit: int | None, cursor: str | None
) -> Call[Page[GovernancePolicy]]:
    return Call(
        "GET",
        f"{_POL}/{segment(policy_id)}/versions",
        parse_model(Page[GovernancePolicy]),
        params=page_params(limit, cursor),
    )


def list_tenant_policies(
    tenant_id: str, limit: int | None, cursor: str | None
) -> Call[Page[GovernancePolicy]]:
    return Call(
        "GET",
        _tpol(tenant_id),
        parse_model(Page[GovernancePolicy]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def create_tenant_policy(tenant_id: str, body: GovernancePolicyCreate) -> Call[GovernancePolicy]:
    return Call(
        "POST",
        _tpol(tenant_id),
        parse_model(GovernancePolicy),
        _CREATED,
        json=dump(body),
        scoped=False,
    )


def get_tenant_policy(tenant_id: str, policy_id: str) -> Call[GovernancePolicy]:
    path = f"{_tpol(tenant_id)}/{segment(policy_id)}"
    return Call("GET", path, parse_model(GovernancePolicy), scoped=False)


def update_tenant_policy(
    tenant_id: str, policy_id: str, body: GovernancePolicyUpdate
) -> Call[GovernancePolicy]:
    path = f"{_tpol(tenant_id)}/{segment(policy_id)}"
    return Call("PUT", path, parse_model(GovernancePolicy), json=dump(body), scoped=False)


def delete_tenant_policy(tenant_id: str, policy_id: str) -> Call[None]:
    path = f"{_tpol(tenant_id)}/{segment(policy_id)}"
    return Call("DELETE", path, parse_none, _NO_CONTENT, scoped=False)


# ========================================================================== validations


def _val(validation_id: str) -> str:
    return f"{V1}/validations/{segment(validation_id)}"


def request_model_validation(
    model_id: str, version: int, body: ValidationRequest, idempotency_key: str | None
) -> Call[ModelValidation]:
    return Call(
        "POST",
        f"{_mv(model_id, version)}/validations",
        parse_model(ModelValidation),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


def list_model_validations(
    model_id: str, version: int, limit: int | None, cursor: str | None
) -> Call[Page[ModelValidation]]:
    return Call(
        "GET",
        f"{_mv(model_id, version)}/validations",
        parse_model(Page[ModelValidation]),
        params=page_params(limit, cursor),
    )


def list_validations(
    limit: int | None, cursor: str | None, *, status: ValidationStatus | str | None = None
) -> Call[Page[ModelValidation]]:
    st = None if status is None else ValidationStatus(status).value
    return Call(
        "GET",
        f"{V1}/validations",
        parse_model(Page[ModelValidation]),
        params=page_params(limit, cursor, status=st),
    )


def get_validation(validation_id: str) -> Call[ModelValidation]:
    return Call("GET", _val(validation_id), parse_model(ModelValidation))


def list_validation_evidence(
    validation_id: str, limit: int | None, cursor: str | None
) -> Call[Page[CheckResult]]:
    return Call(
        "GET",
        f"{_val(validation_id)}/evidence",
        parse_model(Page[CheckResult]),
        params=page_params(limit, cursor),
    )


def cancel_validation(validation_id: str, body: ValidationNote) -> Call[ModelValidation]:
    return Call(
        "POST",
        f"{_val(validation_id)}:cancel",
        parse_model(ModelValidation),
        json=dump(body),
    )


def resubmit_validation(
    validation_id: str, body: ValidationNote, idempotency_key: str | None
) -> Call[ModelValidation]:
    return Call(
        "POST",
        f"{_val(validation_id)}:resubmit",
        parse_model(ModelValidation),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


def decide_validation(validation_id: str, body: ApprovalCreate) -> Call[ModelValidation]:
    # a decision is recorded once per reviewer and round: never resent blindly
    return Call(
        "POST",
        f"{_val(validation_id)}/approvals",
        parse_model(ModelValidation),
        json=dump(body),
    )


def deprecate_validation(validation_id: str, body: ValidationNote) -> Call[ModelValidation]:
    return Call(
        "POST",
        f"{_val(validation_id)}:deprecate",
        parse_model(ModelValidation),
        json=dump(body),
    )


def export_evidence_pack(validation_id: str, body: EvidencePackRequest) -> Call[EvidencePack]:
    # renders the pack again from stored records: safe to resend
    return Call(
        "POST",
        f"{_val(validation_id)}/evidence-pack",
        parse_model(EvidencePack),
        json=dump(body),
        retry_safe=True,
    )
