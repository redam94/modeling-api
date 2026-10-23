"""Who may upload, review and use uploaded code (v0.19): permissions, tokens, routes,
placement, error codes, audit and webhook names."""

from __future__ import annotations

import re

import pytest
from mapi_spec import api
from mapi_spec.api import CLIENT_OPERATIONS, ROUTE_ACCESS, V019_ENDPOINTS, RouteScope
from mapi_spec.audit import AuditAction
from mapi_spec.errors import ERROR_INFO, PLUGIN_ERROR_CODES, ErrorCode
from mapi_spec.executor import ExecutorCaps, Isolation
from mapi_spec.tenancy import (
    PERMISSIONS,
    Action,
    AuthContext,
    Level,
    ProjectRole,
    ResourceKind,
    TokenPrincipal,
    UserPrincipal,
    can,
    untrusted_placement_problem,
)
from mapi_spec.tokens import TOKEN_NEVER, ScopeName, scope_allows
from mapi_spec.webhooks import WebhookEventType

pytestmark = pytest.mark.unit

P, S, A_ = ResourceKind.PLUGIN, ResourceKind.PLUGIN_SETTINGS, Action
USER = UserPrincipal(id="usr_01jaf3a9b8c7d6e5f4g3h2j1k0", issuer="https://idp.example", subject="u")

# ================================================================================ permissions


def test_plugin_rules() -> None:
    rule = PERMISSIONS
    assert (rule[(P, A_.READ)].min_role, rule[(P, A_.READ)].level) == (
        ProjectRole.VIEWER,
        Level.PROJECT,
    )
    for act in (A_.CREATE, A_.UPDATE, A_.DELETE):
        assert (rule[(P, act)].min_role, rule[(P, act)].level) == (
            ProjectRole.EDITOR,
            Level.PROJECT,
        )
    assert (rule[(P, A_.APPROVE)].min_role, rule[(P, A_.APPROVE)].level) == (
        ProjectRole.ADMIN, Level.TENANT,
    )  # fmt: skip
    for act in (A_.READ, A_.UPDATE):
        assert (rule[(S, act)].min_role, rule[(S, act)].level) == (ProjectRole.ADMIN, Level.TENANT)
    assert {a for (r, a) in PERMISSIONS if r is S} == {A_.READ, A_.UPDATE}
    assert {a for (r, a) in PERMISSIONS if r is P} == {
        A_.READ, A_.CREATE, A_.UPDATE, A_.DELETE, A_.APPROVE,
    }  # fmt: skip


@pytest.mark.parametrize("res", [P, S])
def test_clients_reach_nothing(res: ResourceKind) -> None:
    for (r, act), _ in PERMISSIONS.items():
        if r is res:
            assert not can(ProjectRole.CLIENT, act, r), (r, act)
    v019_ops = {e.operation_id for e in V019_ENDPOINTS}
    assert not v019_ops & CLIENT_OPERATIONS


def test_an_editor_can_create_but_not_approve() -> None:
    editor = AuthContext(
        principal=USER, tenant_id="acme", project_id="growth", role=ProjectRole.EDITOR
    )
    assert editor.allows("create", "plugin") and editor.allows("delete", "plugin")
    assert not editor.allows("approve", "plugin")
    assert not editor.allows("read", "plugin_settings")
    viewer = AuthContext(
        principal=USER, tenant_id="acme", project_id="growth", role=ProjectRole.VIEWER
    )
    assert viewer.allows("read", "plugin") and not viewer.allows("create", "plugin")
    # a project admin is not a tenant admin
    padmin = AuthContext(
        principal=USER, tenant_id="acme", project_id="growth", role=ProjectRole.ADMIN
    )
    assert not padmin.allows("approve", "plugin")
    tadmin = AuthContext(
        principal=USER, tenant_id="acme", project_id="growth", role=ProjectRole.ADMIN,
        tenant_role=ProjectRole.ADMIN,
    )  # fmt: skip
    assert tadmin.allows("approve", "plugin") and tadmin.allows("update", "plugin_settings")


# ===================================================================================== tokens


def test_publish_scope_pushes_and_asks_but_never_approves_or_yanks() -> None:
    scopes = frozenset({ScopeName.PLUGINS_PUBLISH.value})
    for act in ("read", "create", "update"):
        assert scope_allows(scopes, act, "plugin"), act
    for act in ("approve", "delete"):
        assert not scope_allows(scopes, act, "plugin"), act
    assert not scope_allows(scopes, "update", "plugin_settings")
    assert {
        ("plugin", "approve"),
        ("plugin", "delete"),
        ("plugin_settings", "update"),
    } <= TOKEN_NEVER
    # even an admin token never approves, yanks or changes who may upload
    admin = frozenset({ScopeName.ADMIN.value})
    for res, act in (("plugin", "approve"), ("plugin", "delete"), ("plugin_settings", "update")):
        assert not scope_allows(admin, act, res)
    # models:read tokens do not list uploads
    assert not scope_allows(frozenset({ScopeName.MODELS_READ.value}), "read", "plugin")


def test_a_tenant_admin_token_cannot_approve() -> None:
    token = TokenPrincipal(
        id="tok_01jaf3a9b8c7d6e5f4g3h2j1k0", prefix="abcdefgh",
        owner_id="usr_01jaf3a9b8c7d6e5f4g3h2j1k0", tenant_id="acme", project_id="growth",
        scopes=("admin",),
    )  # fmt: skip
    ctx = AuthContext(
        principal=token, tenant_id="acme", project_id="growth", role=ProjectRole.ADMIN,
        tenant_role=ProjectRole.ADMIN, scopes=frozenset({"admin"}),
    )  # fmt: skip
    assert not ctx.allows("approve", "plugin")
    assert not ctx.allows("delete", "plugin")
    assert ctx.allows("create", "plugin")


# ===================================================================================== routes


def test_v019_table() -> None:
    assert api.ENDPOINTS[-len(V019_ENDPOINTS) :] == V019_ENDPOINTS
    ops = [e.operation_id for e in V019_ENDPOINTS]
    assert len(ops) == len(set(ops)) == 18
    assert "V019_ENDPOINTS" in api.__all__
    for e in V019_ENDPOINTS:
        acc = ROUTE_ACCESS[e.operation_id]
        if e.path.startswith("/v1/plugins"):
            assert acc.scope is RouteScope.PROJECT, e.operation_id
            assert acc.resource is P, e.operation_id
            assert "X-Mapi-Project" in e.headers, e.operation_id
        else:
            assert e.path.startswith("/v1/tenants/{tenant_id}/plugin-"), e.path
            assert (acc.scope, acc.resource) == (RouteScope.TENANT, S), e.operation_id
        for param in re.findall(r"\{([^}]+)\}", e.path):
            assert re.fullmatch(r"[a-z_]+", param), e.path
        for dto in filter(None, (e.request, e.response)):
            inner = re.sub(r"^(?:Page|list)\[(\w+)\]$", r"\1", dto)
            assert inner == "PlotSpec" or inner in api.__all__, dto
    by_op = {e.operation_id: e for e in V019_ENDPOINTS}
    for op in ("approve_plugin_version", "reject_plugin_version"):
        assert ROUTE_ACCESS[op].also == ((P, A_.APPROVE),)
        assert ErrorCode.REAUTH_REQUIRED in by_op[op].errors
        assert ErrorCode.SELF_APPROVAL_FORBIDDEN in by_op[op].errors
    assert ErrorCode.UNTRUSTED_PLACEMENT_UNAVAILABLE in by_op["complete_plugin_upload"].errors
    assert by_op["complete_plugin_upload"].success == 202
    assert by_op["revoke_plugin_uploader"].success == 204


# ================================================================================== placement

_DOCKER = ExecutorCaps(isolation=Isolation.PROCESS, runs_untrusted_code=True)
_LOCAL = ExecutorCaps(isolation=Isolation.PROCESS)
_LOCAL_DEV = ExecutorCaps(
    isolation=Isolation.PROCESS, runs_untrusted_code=True, untrusted_dev_only=True
)
_GVISOR = ExecutorCaps(
    isolation=Isolation.GVISOR, egress_enforced=True, dns_enforced=True, runs_untrusted_code=True
)
_GVISOR_OPEN = ExecutorCaps(isolation=Isolation.GVISOR, runs_untrusted_code=True)
_CLOUDRUN = ExecutorCaps(
    isolation=Isolation.MICROVM, egress_enforced=True, dns_enforced=True, runs_untrusted_code=True
)


@pytest.mark.parametrize(
    ("caps", "multi_tenant", "allowed"),
    [
        (_LOCAL, False, False),
        (_LOCAL, True, False),
        (_LOCAL_DEV, False, True),
        (_LOCAL_DEV, True, False),
        (_DOCKER, False, True),
        (_DOCKER, True, False),
        (_GVISOR, False, True),
        (_GVISOR, True, True),
        (_GVISOR_OPEN, True, False),
        (_CLOUDRUN, False, True),
        (_CLOUDRUN, True, True),
    ],
)
def test_untrusted_placement(caps: ExecutorCaps, multi_tenant: bool, allowed: bool) -> None:
    problem = untrusted_placement_problem(caps, multi_tenant=multi_tenant)
    assert (problem is None) is allowed
    if problem is not None:
        assert problem.endswith(".") and len(problem) < 300


def test_new_caps_default_to_refusing_uploaded_code() -> None:
    caps = ExecutorCaps(isolation=Isolation.MICROVM, egress_enforced=True, dns_enforced=True)
    assert not caps.runs_untrusted_code and not caps.untrusted_dev_only
    assert untrusted_placement_problem(caps, multi_tenant=False) is not None


# ========================================================================= codes and events

NEW_CODES = {
    ErrorCode.PLUGIN_REFUSED: 422,
    ErrorCode.PLUGIN_NAMESPACE_TAKEN: 409,
    ErrorCode.PLUGIN_VERSION_EXISTS: 409,
    ErrorCode.PLUGIN_STATE_CONFLICT: 409,
    ErrorCode.PLUGIN_UNAVAILABLE: 409,
    ErrorCode.UPLOADED_CODE_NOT_SHAREABLE: 403,
    ErrorCode.UNTRUSTED_PLACEMENT_UNAVAILABLE: 409,
    ErrorCode.REAUTH_REQUIRED: 401,
}


def test_new_error_codes() -> None:
    for code, status in NEW_CODES.items():
        assert ERROR_INFO[code].status == status, code
        assert code not in PLUGIN_ERROR_CODES, code  # plugin code can never raise them
        assert not ERROR_INFO[code].retryable


def test_audit_actions_and_webhook_events() -> None:
    assert {a.value for a in AuditAction if a.value.startswith("plugin.")} == {
        "plugin.publish", "plugin.yank", "plugin.claim", "plugin.approval_request",
        "plugin.approve", "plugin.reject", "plugin.purge", "plugin.settings_update",
        "plugin.uploader_attest", "plugin.uploader_revoke",
    }  # fmt: skip
    assert {e.value for e in WebhookEventType if e.value.startswith("plugin.")} == {
        "plugin.version.kit_passed", "plugin.version.kit_failed",
        "plugin.version.awaiting_approval", "plugin.version.approved",
        "plugin.version.rejected", "plugin.version.yanked",
    }  # fmt: skip
