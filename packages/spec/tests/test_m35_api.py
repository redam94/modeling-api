"""M3.5 endpoint table: conventions, access rules, the permission matrix with the reviewer
capability, token scopes on the new routes, problem codes, audit actions, webhook events
and the reference docs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mapi_spec import api
from mapi_spec.api import ENDPOINTS, M35_ENDPOINTS, ROUTE_ACCESS, RouteScope
from mapi_spec.audit import AuditAction
from mapi_spec.errors import ERROR_INFO, ErrorCode
from mapi_spec.tenancy import (
    PERMISSIONS,
    PROJECT_HEADER,
    Action,
    AuthContext,
    Capability,
    Membership,
    ProjectRole,
    ResourceKind,
    TokenPrincipal,
    UserPrincipal,
    can,
)
from mapi_spec.tokens import TOKEN_NEVER, scope_allows
from mapi_spec.webhooks import WebhookEventType
from pydantic import BaseModel

pytestmark = pytest.mark.unit
USER = UserPrincipal(
    id="usr_01jaf3a9b8c7d6e5f4g3h2j1k0", issuer="https://accounts.google.com", subject="1"
)


def _resolve(name: str) -> type[BaseModel]:
    m = re.fullmatch(r"Page\[(\w+)\]", name)
    obj = getattr(api, m.group(1) if m else name)
    assert isinstance(obj, type) and issubclass(obj, BaseModel), name
    return obj


def test_the_table_is_additive_and_unique() -> None:
    assert ENDPOINTS[-len(M35_ENDPOINTS) :] == M35_ENDPOINTS
    assert len(M35_ENDPOINTS) >= 55
    keys = [(e.method, e.path) for e in ENDPOINTS]
    ops = [e.operation_id for e in ENDPOINTS]
    assert len(keys) == len(set(keys)) and len(ops) == len(set(ops))
    assert set(ROUTE_ACCESS) == set(ops)


@pytest.mark.parametrize("e", M35_ENDPOINTS, ids=lambda e: e.operation_id)
def test_m35_rows_follow_the_conventions(e: api.EndpointDecl) -> None:
    assert e.path.startswith("/v1/"), e.path
    assert e.success in (200, 201, 202, 204), e
    assert (e.success == 204) <= (e.response is None), e
    for param in re.findall(r"\{([^}]+)\}", e.path):
        assert re.fullmatch(r"[a-z_]+", param), e.path
    if e.request is not None:
        assert e.method in ("POST", "PUT", "PATCH"), e
        _resolve(e.request)
    if e.response is not None:
        _resolve(e.response)
    assert e.paginated == (e.response or "").startswith("Page["), e
    if e.paginated:
        assert "limit" in e.query, e
    if e.method in ("PATCH", "PUT"):
        assert e.request is not None
    assert e.streaming == (e.operation_id == "workflow_run_events")
    access = ROUTE_ACCESS[e.operation_id]
    if access.scope is RouteScope.PROJECT:
        assert PROJECT_HEADER in e.headers, e.operation_id
    if access.scope is RouteScope.TENANT:
        assert e.path.startswith("/v1/tenants/{tenant_id}/"), e.operation_id
    for code in e.errors:
        assert code in ERROR_INFO
    if e.success == 202 and e.method == "POST":
        assert api.IDEMPOTENCY_KEY_HEADER in e.headers or e.operation_id == (
            "cancel_workflow_run"
        ), e.operation_id


def test_catalog_routes_are_open_to_every_signed_in_principal() -> None:
    open_ = {e.operation_id for e in M35_ENDPOINTS
             if ROUTE_ACCESS[e.operation_id].scope is RouteScope.AUTHENTICATED}  # fmt: skip
    assert open_ == {"list_catalog_abouts", "get_chooser", "recommend_catalog_items",
                     "list_workflow_nodes", "get_workflow_node", "list_governance_checks",
                     "list_workflow_templates", "get_workflow_template"}  # fmt: skip
    assert not [e for e in M35_ENDPOINTS if ROUTE_ACCESS[e.operation_id].scope
                in (RouteScope.PUBLIC, RouteScope.PLATFORM, RouteScope.INTERNAL)]  # fmt: skip


def _ctx(role: ProjectRole, *, caps: frozenset[Capability] = frozenset(),
         scopes: frozenset[str] | None = None) -> AuthContext:  # fmt: skip
    return AuthContext(principal=USER, tenant_id="acme", project_id="growth", role=role,
                       tenant_role=role, capabilities=caps, scopes=scopes)  # fmt: skip


def _allowed(ctx: AuthContext, op: str) -> bool:
    acc = ROUTE_ACCESS[op]
    if acc.resource is None or acc.action is None:
        return True
    needs = [(acc.resource, acc.action), *acc.also]
    return all(ctx.allows(a, r) for r, a in needs)


@pytest.mark.parametrize(
    ("op", "minimum"),
    [
        ("list_workflows", ProjectRole.VIEWER),
        ("create_workflow", ProjectRole.EDITOR),
        ("create_workflow_revision", ProjectRole.EDITOR),
        ("start_workflow_run", ProjectRole.RUNNER),
        ("create_workflow_schedule", ProjectRole.RUNNER),
        ("delete_workflow", ProjectRole.ADMIN),
        ("list_policies", ProjectRole.VIEWER),
        ("create_policy", ProjectRole.ADMIN),
        ("create_tenant_policy", ProjectRole.ADMIN),
        ("request_model_validation", ProjectRole.RUNNER),
        ("deprecate_validation", ProjectRole.ADMIN),
        ("export_evidence_pack", ProjectRole.VIEWER),
        ("explore_dataset_version", ProjectRole.RUNNER),
        ("optimize_model_version", ProjectRole.RUNNER),
        ("update_dataset_about", ProjectRole.EDITOR),
        ("decide_validation", ProjectRole.ADMIN),  # without the reviewer capability
    ],
)
def test_role_ladder_on_m35_routes(op: str, minimum: ProjectRole) -> None:
    for role in ProjectRole:
        assert _allowed(_ctx(role), op) is (role >= minimum), (op, role)


def test_the_reviewer_capability() -> None:
    rule = PERMISSIONS[(ResourceKind.MODEL, Action.APPROVE)]
    assert rule.capability is Capability.REVIEWER and rule.min_role is ProjectRole.EDITOR
    reviewer = frozenset({Capability.REVIEWER})
    assert not _allowed(_ctx(ProjectRole.VIEWER, caps=reviewer), "decide_validation")
    assert _allowed(_ctx(ProjectRole.EDITOR, caps=reviewer), "decide_validation")
    assert not _allowed(_ctx(ProjectRole.RUNNER), "decide_validation")
    assert can("editor", "approve", "model", ["reviewer"]) and not can("editor", "approve", "model")
    assert can(ProjectRole.ADMIN, Action.APPROVE, ResourceKind.MODEL)
    m = Membership.model_validate({**Membership.model_json_schema()["examples"][0],
                                   "capabilities": ["reviewer"]})  # fmt: skip
    assert m.capabilities == [Capability.REVIEWER]
    api.MembershipUpdate(capabilities=[Capability.REVIEWER])
    with pytest.raises(ValueError, match="role, capabilities"):
        api.MembershipUpdate()


def test_no_token_can_approve_a_validation() -> None:
    assert ("model", "approve") in TOKEN_NEVER
    token = TokenPrincipal(
        id="tok_01jaf3a9b8c7d6e5f4g3h2j1k0", prefix="abcdefgh",
        owner_id="usr_01jaf3a9b8c7d6e5f4g3h2j1k0", tenant_id="acme", project_id="growth",
        scopes=("admin",),
    )  # fmt: skip
    ctx = AuthContext(principal=token, tenant_id="acme", project_id="growth",
                      role=ProjectRole.ADMIN, scopes=frozenset({"admin"}),
                      capabilities=frozenset({Capability.REVIEWER}))  # fmt: skip
    assert not ctx.allows(Action.APPROVE, ResourceKind.MODEL)
    assert ctx.allows(Action.VALIDATE, ResourceKind.MODEL)


@pytest.mark.parametrize(
    ("scope", "ops"),
    [
        ("workflows:write", {"list_workflows", "create_workflow", "validate_workflow",
                             "get_workflow", "update_workflow", "list_workflow_revisions",
                             "create_workflow_revision", "get_workflow_revision",
                             "list_workflow_runs", "get_workflow_run", "workflow_run_events",
                             "list_workflow_node_runs", "get_workflow_node_run",
                             "list_workflow_schedules", "list_workflow_triggers",
                             "delete_workflow_schedule", "delete_workflow_trigger"}),
        ("models:validate", {"request_model_validation", "list_model_validations",
                             "list_validations", "get_validation", "list_validation_evidence",
                             "cancel_validation", "resubmit_validation", "export_evidence_pack",
                             "list_policies", "get_policy", "list_policy_versions",
                             "get_exploration", "get_optimization", "explore_model_version"}),
        ("policies:manage", {"list_policies", "create_policy", "get_policy", "update_policy",
                             "delete_policy", "list_policy_versions"}),
    ],
)  # fmt: skip
def test_token_scopes_on_m35_routes(scope: str, ops: set[str]) -> None:
    got = set()
    for e in M35_ENDPOINTS:
        acc = ROUTE_ACCESS[e.operation_id]
        if acc.scope is not RouteScope.PROJECT or acc.resource is None or acc.action is None:
            continue
        needs = [(acc.resource, acc.action), *acc.also]
        if all(scope_allows([scope], a.value, r.value) for r, a in needs):
            got.add(e.operation_id)
    assert got == ops


def test_workflows_run_scope_runs_and_schedules_workflows() -> None:
    for op in ("start_workflow_run", "cancel_workflow_run", "create_workflow_schedule",
               "update_workflow_schedule", "create_workflow_trigger"):  # fmt: skip
        acc = ROUTE_ACCESS[op]
        assert acc.resource is not None and acc.action is not None
        for r, a in [(acc.resource, acc.action), *acc.also]:
            assert scope_allows(["workflows:run"], a.value, r.value), op
    assert not scope_allows(["workflows:run"], "create", "workflow")


def test_m35_codes_audit_actions_and_events() -> None:
    for code, status in (("WORKFLOW_INVALID", 422), ("EXPRESSION_INVALID", 422),
                         ("EXPRESSION_ERROR", 422), ("NODE_UNKNOWN", 422),
                         ("SCHEDULE_INVALID", 422), ("WORKFLOW_NOT_RUNNABLE", 409),
                         ("POLICY_INVALID", 422), ("VALIDATION_TRANSITION_INVALID", 409),
                         ("SELF_APPROVAL_FORBIDDEN", 403), ("NOT_VALIDATED", 409),
                         ("ABOUT_INCOMPLETE", 422), ("CHOOSER_ANSWER_INVALID", 422),
                         ("EXPLORE_INVALID", 422), ("OPTIMIZE_INFEASIBLE", 422)):  # fmt: skip
        assert ERROR_INFO[ErrorCode(code)].status == status
    for action in ("workflow.create", "workflow.revision", "workflow.run",
                   "workflow.schedule_create", "workflow.trigger_create", "policy.update",
                   "model.validation_request", "model.validation_transition",
                   "model.approve", "model.reject", "model.deprecate",
                   "model.evidence_export", "endpoint.promote_not_validated",
                   "dataset.about_update"):  # fmt: skip
        assert AuditAction(action).value == action
    for event in ("workflow.run.succeeded", "workflow.run.failed", "model.validated",
                  "model.validation.awaiting_approval", "dataset.version.created",
                  "optimize.completed"):  # fmt: skip
        assert WebhookEventType(event).value == event


def _docs() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "reference" / "http-api.md").exists():
            return parent / "docs"
    return None


def test_reference_and_concept_docs_cover_m35() -> None:
    docs = _docs()
    if docs is None:  # pragma: no cover - sdist without the docs tree
        pytest.skip("docs/ not available")
    text = (docs / "reference" / "http-api.md").read_text("utf-8")
    for e in M35_ENDPOINTS:
        assert f"`{e.method} {e.path}`" in text, f"http-api.md is missing {e.method} {e.path}"
        for dto in filter(None, (e.request, e.response)):
            assert dto.removeprefix("Page[").removesuffix("]") in text, dto
    from mapi_spec.governance import GOVERNANCE_CHECKS
    from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS
    from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES

    workflows = (docs / "concepts" / "workflows.md").read_text("utf-8")
    for node in NODE_IMPLEMENTATIONS:
        assert f"`{node}`" in workflows, node
    for template in WORKFLOW_TEMPLATES:
        assert f"`{template}`" in workflows, template
    governance = (docs / "concepts" / "governance.md").read_text("utf-8")
    for check in GOVERNANCE_CHECKS:
        assert f"`{check}`" in governance, check
    about = (docs / "concepts" / "about-and-choosing.md").read_text("utf-8")
    for needle in ("summary_plain", "use_when", "Help me choose", "/v1/catalog/recommendations"):
        assert needle in about, needle


def test_m35_modules_stay_json_only() -> None:
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import mapi_spec.about, mapi_spec.abouts, mapi_spec.expr, mapi_spec.cron\n"
        "import mapi_spec.splitplan, mapi_spec.stattests, mapi_spec.workflow\n"
        "import mapi_spec.workflow_nodes, mapi_spec.workflow_templates, mapi_spec.governance\n"
        "import mapi_spec.explore, mapi_spec.optimize\n"
        "mapi_spec.abouts.builtin_abouts()\n"
        "bad = sorted(m for m in ('numpy', 'xarray', 'pandas', 'scipy', 'statsmodels',"
        " 'sklearn', 'mapi_sdk', 'mapi_server', 'mapi_runner') if m in sys.modules)\n"
        "assert not bad, bad\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stderr
