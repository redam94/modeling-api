"""Tenancy: the permission matrix (exhaustively), roles, AuthContext, placement and the
X-Mapi-Project resolution rules."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

import pytest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import ExecutorCaps, Isolation
from mapi_spec.tenancy import (
    OPERATOR_PERMISSIONS,
    PERMISSIONS,
    TIER_PLACEMENT,
    Action,
    AuthContext,
    InternalService,
    Level,
    Membership,
    ProjectRole,
    ResourceKind,
    Role,
    ServicePrincipal,
    TenantStatus,
    TokenPrincipal,
    TrustTier,
    UserPrincipal,
    can,
    effective_role,
    parse_project_selector,
    placement_allows,
    region_satisfies,
    required_rule,
    resolve_project_scope,
)
from pydantic import TypeAdapter, ValidationError

V, E, R, A = ProjectRole.VIEWER, ProjectRole.EDITOR, ProjectRole.RUNNER, ProjectRole.ADMIN
ROLES: list[ProjectRole | None] = [None, V, E, R, A]

# Written independently of PERMISSIONS: resource -> action -> "<min role letter>/<level>".
# "op" = operators only, "int" = internal services only; unlisted pairs are denied.
EXPECTED: dict[str, dict[str, str]] = {
    "tenant": {"read": "V/T", "update": "A/T", "create": "op", "delete": "op", "manage": "op"},
    "tenant_member": {"read": "V/T", "create": "A/T", "update": "A/T", "delete": "A/T"},
    "project": {"read": "V/P", "create": "A/T", "update": "A/P", "delete": "A/T"},
    "member": {"read": "V/P", "create": "A/P", "update": "A/P", "delete": "A/P"},
    "token": {"read": "V/P", "create": "V/P", "delete": "V/P", "manage": "A/P"},
    "catalog": {"read": "V/P"},
    "dataset": {"read": "V/P", "create": "E/P", "update": "E/P", "delete": "A/P"},
    "spec": {"read": "V/P", "create": "E/P", "update": "E/P", "delete": "A/P"},
    "run": {"read": "V/P", "create": "R/P", "cancel": "R/P"},
    "model": {"read": "V/P", "run": "R/P", "delete": "A/P"},
    "endpoint": {
        "read": "V/P", "run": "R/P", "create": "A/P", "update": "A/P", "promote": "A/P",
        "delete": "A/P",
    },
    "plot": {"read": "V/P", "create": "E/P", "run": "R/P"},
    "plugin": {"read": "V/P", "create": "A/P", "delete": "A/P"},
    "usage": {"read": "V/P"},
    "billing": {"read": "A/T"},
    "audit": {"read": "A/T"},
    "quota": {"read": "op", "update": "op", "delete": "op"},
    "kill_switch": {"read": "op", "update": "op"},
    "executor": {"read": "op"},
    "event": {"create": "int"},
}  # fmt: skip
LETTER = {"V": V, "E": E, "R": R, "A": A}


def _expected_can(role: ProjectRole | None, action: Action, resource: ResourceKind) -> bool:
    code = EXPECTED.get(resource.value, {}).get(action.value)
    if code is None or code in ("op", "int") or role is None:
        return False
    return role >= LETTER[code[0]]


@pytest.mark.parametrize(
    ("role", "action", "resource"),
    list(itertools.product(ROLES, list(Action), list(ResourceKind))),
    ids=lambda x: getattr(x, "value", str(x)),
)
def test_permission_matrix_is_exhaustively_as_specified(
    role: ProjectRole | None, action: Action, resource: ResourceKind
) -> None:
    assert can(role, action, resource) is _expected_can(role, action, resource)
    assert can(role.value if role else None, action.value, resource.value) is _expected_can(
        role, action, resource
    )


def test_matrix_lists_exactly_the_specified_pairs_and_levels() -> None:
    listed = {(r.value, a.value): rule for (r, a), rule in PERMISSIONS.items()}
    expected = {(r, a) for r, acts in EXPECTED.items() for a in acts}
    assert set(listed) == expected
    for (r, a), rule in listed.items():
        code = EXPECTED[r][a]
        if code == "op":
            assert rule.level is Level.PLATFORM and rule.min_role is None
        elif code == "int":
            assert rule.level is Level.INTERNAL and rule.min_role is None
        else:
            assert rule.min_role is LETTER[code[0]]
            assert rule.level is (Level.TENANT if code.endswith("/T") else Level.PROJECT)


def test_every_resource_is_readable_by_someone_and_unknown_inputs_deny() -> None:
    for res in ResourceKind:
        if res is ResourceKind.EVENT:
            continue
        assert required_rule(Action.READ, res) is not None, res
    assert required_rule("nope", "run") is None and required_rule("read", "nope") is None
    assert not can("superuser", Action.READ, ResourceKind.RUN)
    assert not can(A, "teleport", ResourceKind.RUN)


def test_roles_are_totally_ordered_and_cumulative() -> None:
    assert V < E < R < A and A >= R >= E >= V and not (V > E)
    assert sorted([A, V, R, E]) == [V, E, R, A]
    assert Role is ProjectRole
    for lo, hi in itertools.combinations([V, E, R, A], 2):
        for action, resource in itertools.product(Action, ResourceKind):
            if can(lo, action, resource):
                assert can(hi, action, resource), (lo, hi, action, resource)
    assert ProjectRole.VIEWER.__lt__("x") is NotImplemented


def test_running_costs_money_so_editors_cannot_run() -> None:
    assert can(E, Action.UPDATE, ResourceKind.SPEC)
    assert not can(E, Action.CREATE, ResourceKind.RUN)
    assert not can(E, Action.RUN, ResourceKind.MODEL)
    assert can(R, Action.CREATE, ResourceKind.RUN)
    assert not can(R, Action.PROMOTE, ResourceKind.ENDPOINT)


def test_operators_read_metadata_but_never_tenant_content() -> None:
    reads = {r for (r, a) in OPERATOR_PERMISSIONS if a is Action.READ}
    assert {ResourceKind.TENANT, ResourceKind.AUDIT, ResourceKind.USAGE} <= reads
    for content in (ResourceKind.DATASET, ResourceKind.SPEC, ResourceKind.MODEL, ResourceKind.PLOT):
        assert all(r is not content for r, _ in OPERATOR_PERMISSIONS)
    platform = {k for k, rule in PERMISSIONS.items() if rule.level is Level.PLATFORM}
    assert platform <= OPERATOR_PERMISSIONS


# ------------------------------------------------------------------------------- memberships

T0 = datetime(2026, 10, 15, tzinfo=UTC)
USR = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"
OTHER = "usr_01jaf3a0b0c0d0e0f0g0h0j0k0"


def _m(role: ProjectRole, project: str | None = None, *, who: str = USR, tenant: str = "acme"):  # type: ignore[no-untyped-def]
    return Membership(
        tenant_id=tenant, project_id=project, principal_id=who, role=role, created_at=T0
    )


def test_effective_role_takes_the_highest_applicable_grant() -> None:
    ms = [_m(V), _m(A, "growth"), _m(E, "retail"), _m(A, who=OTHER), _m(A, tenant="other")]
    assert effective_role(ms, USR, "acme") is V
    assert effective_role(ms, USR, "acme", "growth") is A
    assert effective_role(ms, USR, "acme", "retail") is E
    assert effective_role(ms, USR, "acme", "unknown") is V
    assert effective_role(ms, USR, "nobody") is None
    assert effective_role([_m(R, "growth")], USR, "acme") is None


# ---------------------------------------------------------------------------- auth context


def _user() -> UserPrincipal:
    return UserPrincipal(id=USR, issuer="https://accounts.google.com", subject="1234")


def _token(*scopes: str) -> TokenPrincipal:
    return TokenPrincipal(
        id="tok_01jaf3r1s2t3v4w5x6y7z8a9b0",
        prefix="7k2m9q4x",
        owner_id=USR,
        tenant_id="acme",
        project_id="growth",
        scopes=scopes,
    )


def _ctx(**kw: Any) -> AuthContext:
    base: dict[str, Any] = {"principal": _user(), "tenant_id": "acme", "project_id": "growth"}
    base.update(kw)
    return AuthContext(**base)


def test_project_level_uses_the_better_of_tenant_and_project_roles() -> None:
    ctx = _ctx(role=V, tenant_role=R)
    assert ctx.allows(Action.CREATE, ResourceKind.RUN)
    ctx = _ctx(role=A, tenant_role=None)
    assert ctx.allows("update", "project")
    assert not ctx.allows("create", "project"), "project creation needs a tenant-wide admin"
    assert not ctx.allows("read", "audit")
    assert _ctx(tenant_role=A).allows("read", "audit")


def test_tenant_status_gates_writes_and_reads() -> None:
    suspended = _ctx(role=A, tenant_status=TenantStatus.SUSPENDED)
    assert suspended.allows("read", "dataset")
    assert not suspended.allows("create", "run")
    assert not suspended.allows("create", "token")
    deleting = _ctx(role=A, tenant_status=TenantStatus.DELETING)
    assert not deleting.allows("read", "dataset")
    assert TenantStatus.ACTIVE.allows_writes and not TenantStatus.SUSPENDED.allows_writes


def test_token_scopes_narrow_but_never_widen_the_owner_role() -> None:
    score_only = _ctx(
        principal=_token("score:geo-mmm-prod"), role=A, scopes=frozenset({"score:geo-mmm-prod"})
    )
    assert score_only.allows("run", "endpoint", resource_name="geo-mmm-prod")
    assert not score_only.allows("run", "endpoint", resource_name="other")
    assert not score_only.allows("read", "dataset")
    assert not score_only.allows("create", "run")
    viewer_admin_scope = _ctx(role=V, scopes=frozenset({"admin"}))
    assert not viewer_admin_scope.allows("create", "run"), "scopes never exceed the role"
    assert viewer_admin_scope.allows("read", "model")
    session = _ctx(role=R, scopes=None)
    assert session.allows("create", "run")


def test_operator_and_internal_principals() -> None:
    op = _ctx(operator=True, role=None, tenant_status=TenantStatus.DELETING)
    assert op.allows("update", "kill_switch") and op.allows("read", "audit")
    assert op.allows("manage", "tenant")
    assert not op.allows("read", "dataset"), "operators do not read tenant content"
    plain_admin = _ctx(role=A, tenant_role=A)
    assert not plain_admin.allows("update", "kill_switch")
    assert not plain_admin.allows("create", "event")
    svc = _ctx(
        principal=ServicePrincipal(id="svc_pubsub-push", service=InternalService.PUBSUB_PUSH)
    )
    assert svc.is_internal and svc.allows("create", "event")
    assert not svc.allows("read", "run")
    assert not _ctx(role=A).allows("create", "event")
    assert not _ctx(role=A).allows("fly", "run")


def test_require_raises_forbidden() -> None:
    with pytest.raises(MapiError) as exc:
        _ctx(role=V).require(Action.CREATE, ResourceKind.RUN)
    assert exc.value.code is ErrorCode.FORBIDDEN and exc.value.status == 403
    _ctx(role=R).require("create", "run")


def test_principals_discriminate_on_kind() -> None:
    from mapi_spec.tenancy import Principal

    ta: TypeAdapter[Any] = TypeAdapter(Principal)
    assert isinstance(ta.validate_python(_user().model_dump()), UserPrincipal)
    tok = ta.validate_python(_token("models:read").model_dump())
    assert isinstance(tok, TokenPrincipal) and not tok.is_service_token
    with pytest.raises(ValidationError):
        ta.validate_python({"kind": "robot", "id": "x"})
    with pytest.raises(ValidationError):
        UserPrincipal(id=USR, issuer="http://insecure.example", subject="1")


# -------------------------------------------------------------------------------- placement


def _caps(iso: Isolation, egress: bool = False, dns: bool = False, regions: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    return ExecutorCaps(isolation=iso, egress_enforced=egress, dns_enforced=dns, regions=regions)


@pytest.mark.parametrize("tier", list(TrustTier))
def test_placement_follows_the_tier_table(tier: TrustTier) -> None:
    req = TIER_PLACEMENT[tier]
    strong = _caps(Isolation.MICROVM, True, True)
    assert placement_allows(tier, strong)
    local = _caps(Isolation.PROCESS)
    assert placement_allows(tier, local) is (req.min_isolation <= Isolation.PROCESS)
    gvisor_open = _caps(Isolation.GVISOR, egress=True, dns=False)
    assert placement_allows(tier, gvisor_open) is (not req.dns_enforced)


def test_shared_tiers_match_executor_public_tenant_rule() -> None:
    for caps in (
        _caps(Isolation.GVISOR, True, True),
        _caps(Isolation.GVISOR, True, False),
        _caps(Isolation.PROCESS, True, True),
        _caps(Isolation.DEDICATED_VM, False, True),
    ):
        assert placement_allows(TrustTier.PUBLIC, caps) is caps.allows_public_tenants()


@pytest.mark.parametrize(
    ("residency", "region", "ok"),
    [
        ("eu", "europe-west1", True),
        ("eu", "us-central1", False),
        ("us", "us-central1", True),
        ("us", "northamerica-northeast1", True),
        ("europe-west1", "europe-west1", True),
        ("europe-west1", "europe-west4", False),
        ("asia", "asia-east1", True),
        ("eu", "local", False),
        ("mars", "mars-1", False),
    ],
)
def test_region_residency(residency: str, region: str, ok: bool) -> None:
    assert region_satisfies(residency, region) is ok


def test_placement_with_residency_needs_a_matching_offered_region() -> None:
    caps = _caps(Isolation.MICROVM, True, True, regions=("europe-west1",))
    assert placement_allows(TrustTier.PUBLIC, caps, residency="eu", region="europe-west1")
    assert not placement_allows(TrustTier.PUBLIC, caps, residency="eu", region="europe-west4")
    assert not placement_allows(TrustTier.PUBLIC, caps, residency="eu", region="us-east1")
    assert not placement_allows(TrustTier.PUBLIC, caps, residency="eu")


# ---------------------------------------------------------------------- project resolution


@pytest.mark.parametrize(
    ("value", "tenant", "project"),
    [("acme/growth", "acme", "growth"), ("growth", None, "growth"), (" acme/g-1 ", "acme", "g-1")],
)
def test_parse_project_selector(value: str, tenant: str | None, project: str) -> None:
    sel = parse_project_selector(value)
    assert (sel.tenant_id, sel.project_id) == (tenant, project)
    assert str(sel) == value.strip()


@pytest.mark.parametrize("bad", ["", "Acme/growth", "acme/", "/growth", "a/b/c", "acme/gr owth"])
def test_parse_project_selector_rejects(bad: str) -> None:
    with pytest.raises(MapiError) as exc:
        parse_project_selector(bad)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID


def _resolve(**kw: Any) -> tuple[str | None, str]:
    sel = resolve_project_scope(**kw)
    return sel.tenant_id, sel.project_id


def test_resolution_precedence_and_qualification() -> None:
    assert _resolve(header="acme/growth") == ("acme", "growth")
    assert _resolve(header="growth", member_tenants=["acme"]) == ("acme", "growth")
    assert _resolve(path_tenant="acme", path_project="growth") == ("acme", "growth")
    assert _resolve(path_tenant="acme", path_project="growth", header="growth") == (
        "acme",
        "growth",
    )
    assert _resolve(token_binding=("acme", "growth")) == ("acme", "growth")
    assert _resolve(header="growth", token_binding=("acme", "growth")) == ("acme", "growth")
    assert _resolve(default=("default", "default")) == ("default", "default")
    assert _resolve(header="growth", default=("default", "default")) == ("default", "growth")


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({}, ErrorCode.SCHEMA_INVALID),
        ({"header": "growth", "member_tenants": ["acme", "beta"]}, ErrorCode.SCHEMA_INVALID),
        ({"header": "acme/x", "path_project": "growth"}, ErrorCode.SCHEMA_INVALID),
        ({"header": "beta/growth", "path_tenant": "acme", "path_project": "growth"},
         ErrorCode.SCHEMA_INVALID),
        ({"header": "acme/retail", "token_binding": ("acme", "growth")}, ErrorCode.FORBIDDEN),
        ({"header": "beta/growth", "token_binding": ("acme", "growth")}, ErrorCode.FORBIDDEN),
    ],
)  # fmt: skip
def test_resolution_errors(kw: dict[str, Any], code: ErrorCode) -> None:
    with pytest.raises(MapiError) as exc:
        resolve_project_scope(**kw)
    assert exc.value.code is code
