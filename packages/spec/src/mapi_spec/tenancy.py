"""Tenants, projects, roles, principals and authorization (plan §2, §8.1, §9).

* A **tenant** is the billing, isolation and quota boundary, with a :class:`TrustTier` that
  decides where its runs may be placed (:func:`placement_allows`) and a data-residency
  region (:func:`region_satisfies`).
* A **project** is a workspace inside a tenant and the RBAC unit. Roles are ordered
  ``viewer < editor < runner < admin``: editors change datasets and specs, runners may
  also *spend compute* (runs, scores), admins manage members, endpoints and plugins.
* :data:`PERMISSIONS` is the whole permission matrix; :func:`can` is the pure check every
  component uses. :class:`AuthContext` combines it with token scopes, tenant status and the
  operator flag (:meth:`AuthContext.allows`).
* :func:`resolve_project_scope` turns the ``X-Mapi-Project`` header, path parameters and a
  token's binding into the ``(tenant, project)`` a request acts in.

Pure Pydantic and stdlib: the server, the web app (through OpenAPI) and tests share it.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, Field

from mapi_spec._dto import EXAMPLE_TS, TENANT_ID_RE, Id, ResponseModel, TenantId, dto_examples
from mapi_spec._types import StrictModel
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import ExecutorCaps, Isolation
from mapi_spec.tokens import scope_allows

__all__ = [
    "DEFAULT_RESIDENCY",
    "MULTI_REGIONS",
    "OPERATOR_PERMISSIONS",
    "PERMISSIONS",
    "PROJECT_HEADER",
    "TIER_PLACEMENT",
    "Action",
    "AuthContext",
    "InternalService",
    "Level",
    "Membership",
    "PlacementRequirement",
    "Principal",
    "PrincipalKind",
    "Project",
    "ProjectRole",
    "ProjectSelector",
    "ResidencyRegion",
    "ResourceKind",
    "Role",
    "Rule",
    "ServicePrincipal",
    "Tenant",
    "TenantStatus",
    "TokenPrincipal",
    "TrustTier",
    "UserPrincipal",
    "can",
    "effective_role",
    "parse_project_selector",
    "placement_allows",
    "region_satisfies",
    "required_rule",
    "residency_of_region",
    "resolve_project_scope",
]

PROJECT_HEADER: Final = "X-Mapi-Project"
"""Selects the project of a request: ``<tenant_id>/<project_id>`` (or a bare project id
when the tenant is unambiguous). See :func:`resolve_project_scope`."""


# ====================================================================================== tenants


class TrustTier(StrEnum):
    """How much the platform trusts a tenant's code and data (plan §2, §7, §9)."""

    INTERNAL = "internal"
    VERIFIED = "verified"
    PUBLIC = "public"
    SELF_HOSTED_TRUSTED = "self-hosted-trusted"


class TenantStatus(StrEnum):
    """``suspended`` tenants are read-only; ``deleting`` tenants are inaccessible."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETING = "deleting"

    @property
    def allows_reads(self) -> bool:
        return self is not TenantStatus.DELETING

    @property
    def allows_writes(self) -> bool:
        """Creating, changing or running anything (including spending compute)."""
        return self is TenantStatus.ACTIVE


ResidencyRegion = Annotated[
    str,
    Field(
        pattern=r"^(?:[a-z]{2,6}|[a-z]+-[a-z]+[0-9]{1,2})$",
        examples=["eu", "us", "europe-west1"],
        description="A multi-region (eu, us, asia) or one cloud region (europe-west1)",
    ),
]
DEFAULT_RESIDENCY: Final = "us"
MULTI_REGIONS: Final[dict[str, tuple[str, ...]]] = {
    "eu": ("europe-",),
    "us": ("us-", "northamerica-"),
    "asia": ("asia-",),
}
"""Region-name prefixes each multi-region covers."""


def residency_of_region(region: str) -> str:
    """The broadest residency ``region`` satisfies: its multi-region (:data:`MULTI_REGIONS`,
    e.g. ``europe-west4`` → ``eu``), else the region itself. A deployment's default
    residency: tenants created without one can always run in its region."""
    for residency, prefixes in MULTI_REGIONS.items():
        if region.startswith(prefixes):
            return residency
    return region


def region_satisfies(residency: str, region: str) -> bool:
    """Can data with ``residency`` be processed in ``region``?

    A single-region residency needs exactly that region; a multi-region residency
    (:data:`MULTI_REGIONS`) any region with a matching prefix. ``local`` (an executor with
    no cloud region, e.g. ``local-process``) never satisfies a residency other than itself.
    """
    if residency == region:
        return True
    prefixes = MULTI_REGIONS.get(residency)
    return prefixes is not None and region.startswith(prefixes)


@dataclass(frozen=True, slots=True)
class PlacementRequirement:
    """What an executor must provide before it may run a tier's work (plan §8.4)."""

    min_isolation: Isolation
    egress_enforced: bool
    dns_enforced: bool


TIER_PLACEMENT: Final[dict[TrustTier, PlacementRequirement]] = {
    TrustTier.INTERNAL: PlacementRequirement(Isolation.PROCESS, False, False),
    TrustTier.SELF_HOSTED_TRUSTED: PlacementRequirement(Isolation.PROCESS, False, False),
    TrustTier.VERIFIED: PlacementRequirement(Isolation.GVISOR, True, True),
    TrustTier.PUBLIC: PlacementRequirement(Isolation.GVISOR, True, True),
}
"""Placement is enforced in dispatcher code, never by tenant configuration. Shared,
multi-tenant tiers (``verified``, ``public``) need isolation ≥ gVisor with egress and DNS
enforced, exactly :meth:`ExecutorCaps.allows_public_tenants`."""


def placement_allows(
    tier: TrustTier,
    caps: ExecutorCaps,
    *,
    residency: str | None = None,
    region: str | None = None,
) -> bool:
    """May an executor with ``caps`` run work of a ``tier`` tenant (optionally in ``region``,
    for data that must stay in ``residency``)?"""
    req = TIER_PLACEMENT[tier]
    if caps.isolation < req.min_isolation:
        return False
    if req.egress_enforced and not caps.egress_enforced:
        return False
    if req.dns_enforced and not caps.dns_enforced:
        return False
    if residency is not None:
        if region is None or not region_satisfies(residency, region):
            return False
        if caps.regions and region not in caps.regions:
            return False
    return True


class Tenant(ResponseModel):
    """The billing, isolation and quota boundary."""

    id: TenantId
    name: str = Field(..., min_length=1, max_length=200)
    trust_tier: TrustTier
    status: TenantStatus = TenantStatus.ACTIVE
    status_reason: str | None = Field(None, max_length=500)
    data_residency: ResidencyRegion = DEFAULT_RESIDENCY
    created_at: AwareDatetime
    updated_at: AwareDatetime | None = None

    model_config = dto_examples(
        {
            "id": "acme",
            "name": "Acme Retail",
            "trust_tier": "verified",
            "status": "active",
            "data_residency": "eu",
            "created_at": EXAMPLE_TS,
        }
    )


class Project(ResponseModel):
    """A workspace inside a tenant (M1: only ``default``)."""

    # The id is a slug unique within the tenant: it appears in references such as
    # ``dataset://growth/eu_media_daily``. (The docstring above is the published schema
    # description and is kept as released in v0.2.)
    id: TenantId
    tenant_id: TenantId
    name: str = Field(..., max_length=200)
    description: str | None = Field(None, max_length=4000)
    created_at: AwareDatetime

    model_config = dto_examples(
        {
            "id": "default",
            "tenant_id": "default",
            "name": "Default project",
            "created_at": EXAMPLE_TS,
        }
    )


# ======================================================================================== roles


class ProjectRole(StrEnum):
    """Project roles, ordered ``viewer < editor < runner < admin`` (each includes the last)."""

    VIEWER = "viewer"
    EDITOR = "editor"
    RUNNER = "runner"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _ROLE_ORDER.index(self)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ProjectRole):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, ProjectRole):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, ProjectRole):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, ProjectRole):
            return NotImplemented
        return self.rank >= other.rank


_ROLE_ORDER: Final = (ProjectRole.VIEWER, ProjectRole.EDITOR, ProjectRole.RUNNER, ProjectRole.ADMIN)

Role = ProjectRole
"""Alias used in this module's vocabulary (``mapi_spec.Role`` is the *variable* role)."""


class Action(StrEnum):
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    RUN = "run"
    """Spend compute through an existing resource (score a model or endpoint, render)."""
    CANCEL = "cancel"
    PROMOTE = "promote"
    MANAGE = "manage"
    """Act on other principals' resources, or change a tenant's tier/status (operators)."""


class ResourceKind(StrEnum):
    TENANT = "tenant"
    TENANT_MEMBER = "tenant_member"
    PROJECT = "project"
    MEMBER = "member"
    TOKEN = "token"
    CATALOG = "catalog"
    DATASET = "dataset"
    SPEC = "spec"
    RUN = "run"
    MODEL = "model"
    ENDPOINT = "endpoint"
    PLOT = "plot"
    PLUGIN = "plugin"
    USAGE = "usage"
    """Cost and quota of one project."""
    BILLING = "billing"
    """Cost and quota of the whole tenant."""
    AUDIT = "audit"
    QUOTA = "quota"
    KILL_SWITCH = "kill_switch"
    EXECUTOR = "executor"
    EVENT = "event"
    """Run events pushed to ``/internal/events``."""


class Level(StrEnum):
    """Which grant a permission is checked against."""

    PROJECT = "project"
    """The effective role in the project (max of tenant-wide and project membership)."""
    TENANT = "tenant"
    """Only a tenant-wide membership counts."""
    PLATFORM = "platform"
    """Operators only; no project or tenant role grants it."""
    INTERNAL = "internal"
    """Internal services only (Pub/Sub push, orchestrator)."""


@dataclass(frozen=True, slots=True)
class Rule:
    min_role: ProjectRole | None
    level: Level


_V, _E, _R, _A = ProjectRole.VIEWER, ProjectRole.EDITOR, ProjectRole.RUNNER, ProjectRole.ADMIN
_P, _T = Level.PROJECT, Level.TENANT
_OP = Rule(None, Level.PLATFORM)
_RK, _AC = ResourceKind, Action

PERMISSIONS: Final[dict[tuple[ResourceKind, Action], Rule]] = {
    (_RK.TENANT, _AC.READ): Rule(_V, _T),
    (_RK.TENANT, _AC.UPDATE): Rule(_A, _T),
    (_RK.TENANT, _AC.CREATE): _OP,
    (_RK.TENANT, _AC.DELETE): _OP,
    (_RK.TENANT, _AC.MANAGE): _OP,
    (_RK.TENANT_MEMBER, _AC.READ): Rule(_V, _T),
    (_RK.TENANT_MEMBER, _AC.CREATE): Rule(_A, _T),
    (_RK.TENANT_MEMBER, _AC.UPDATE): Rule(_A, _T),
    (_RK.TENANT_MEMBER, _AC.DELETE): Rule(_A, _T),
    (_RK.PROJECT, _AC.READ): Rule(_V, _P),
    (_RK.PROJECT, _AC.CREATE): Rule(_A, _T),
    (_RK.PROJECT, _AC.UPDATE): Rule(_A, _P),
    (_RK.PROJECT, _AC.DELETE): Rule(_A, _T),
    (_RK.MEMBER, _AC.READ): Rule(_V, _P),
    (_RK.MEMBER, _AC.CREATE): Rule(_A, _P),
    (_RK.MEMBER, _AC.UPDATE): Rule(_A, _P),
    (_RK.MEMBER, _AC.DELETE): Rule(_A, _P),
    (_RK.TOKEN, _AC.READ): Rule(_V, _P),
    (_RK.TOKEN, _AC.CREATE): Rule(_V, _P),
    (_RK.TOKEN, _AC.DELETE): Rule(_V, _P),
    (_RK.TOKEN, _AC.MANAGE): Rule(_A, _P),
    (_RK.CATALOG, _AC.READ): Rule(_V, _P),
    (_RK.DATASET, _AC.READ): Rule(_V, _P),
    (_RK.DATASET, _AC.CREATE): Rule(_E, _P),
    (_RK.DATASET, _AC.UPDATE): Rule(_E, _P),
    (_RK.DATASET, _AC.DELETE): Rule(_A, _P),
    (_RK.SPEC, _AC.READ): Rule(_V, _P),
    (_RK.SPEC, _AC.CREATE): Rule(_E, _P),
    (_RK.SPEC, _AC.UPDATE): Rule(_E, _P),
    (_RK.SPEC, _AC.DELETE): Rule(_A, _P),
    (_RK.RUN, _AC.READ): Rule(_V, _P),
    (_RK.RUN, _AC.CREATE): Rule(_R, _P),
    (_RK.RUN, _AC.CANCEL): Rule(_R, _P),
    (_RK.MODEL, _AC.READ): Rule(_V, _P),
    (_RK.MODEL, _AC.RUN): Rule(_R, _P),
    (_RK.MODEL, _AC.DELETE): Rule(_A, _P),
    (_RK.ENDPOINT, _AC.READ): Rule(_V, _P),
    (_RK.ENDPOINT, _AC.RUN): Rule(_R, _P),
    (_RK.ENDPOINT, _AC.CREATE): Rule(_A, _P),
    (_RK.ENDPOINT, _AC.UPDATE): Rule(_A, _P),
    (_RK.ENDPOINT, _AC.PROMOTE): Rule(_A, _P),
    (_RK.ENDPOINT, _AC.DELETE): Rule(_A, _P),
    (_RK.PLOT, _AC.READ): Rule(_V, _P),
    (_RK.PLOT, _AC.CREATE): Rule(_E, _P),
    (_RK.PLOT, _AC.RUN): Rule(_R, _P),
    (_RK.PLUGIN, _AC.READ): Rule(_V, _P),
    (_RK.PLUGIN, _AC.CREATE): Rule(_A, _P),
    (_RK.PLUGIN, _AC.DELETE): Rule(_A, _P),
    (_RK.USAGE, _AC.READ): Rule(_V, _P),
    (_RK.BILLING, _AC.READ): Rule(_A, _T),
    (_RK.AUDIT, _AC.READ): Rule(_A, _T),
    (_RK.QUOTA, _AC.READ): _OP,
    (_RK.QUOTA, _AC.UPDATE): _OP,
    (_RK.QUOTA, _AC.DELETE): _OP,
    (_RK.KILL_SWITCH, _AC.READ): _OP,
    (_RK.KILL_SWITCH, _AC.UPDATE): _OP,
    (_RK.EXECUTOR, _AC.READ): _OP,
    (_RK.EXECUTOR, _AC.UPDATE): _OP,
    (_RK.EVENT, _AC.CREATE): Rule(None, Level.INTERNAL),
}
"""The complete matrix. A pair that is not listed is denied to everyone."""

OPERATOR_PERMISSIONS: Final[frozenset[tuple[ResourceKind, Action]]] = frozenset(
    {k for k, rule in PERMISSIONS.items() if rule.level is Level.PLATFORM}
    | {
        (r, Action.READ)
        for r in (
            ResourceKind.TENANT,
            ResourceKind.TENANT_MEMBER,
            ResourceKind.PROJECT,
            ResourceKind.MEMBER,
            ResourceKind.TOKEN,
            ResourceKind.RUN,
            ResourceKind.USAGE,
            ResourceKind.BILLING,
            ResourceKind.AUDIT,
            ResourceKind.CATALOG,
        )
    }
)
"""What platform operators may do. Operators read metadata (tenants, members, token
listings, runs, usage, audit) but never tenant content (datasets, specs, models, plots)."""


def required_rule(action: Action | str, resource: ResourceKind | str) -> Rule | None:
    """The rule for ``(resource, action)``, or ``None`` if nobody may do it."""
    try:
        return PERMISSIONS.get((ResourceKind(resource), Action(action)))
    except ValueError:
        return None


def can(role: ProjectRole | str | None, action: Action | str, resource: ResourceKind | str) -> bool:
    """Does ``role`` permit ``action`` on ``resource``?

    ``role`` must be the role at the rule's :class:`Level` (see :func:`effective_role`):
    the tenant-wide role for tenant-level rules, the effective project role otherwise.
    Platform and internal rules are never granted by a role.
    """
    rule = required_rule(action, resource)
    if rule is None or rule.min_role is None or role is None:
        return False
    try:
        return ProjectRole(role) >= rule.min_role
    except ValueError:
        return False


# ================================================================================= memberships


class PrincipalKind(StrEnum):
    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    """A non-human member (owns service tokens; never logs in)."""
    TOKEN = "token"
    SERVICE = "service"
    """An internal platform service (dispatcher, events, ...)."""


class Membership(ResponseModel):
    """A role grant. ``project_id=None`` grants the role on the tenant and every project."""

    tenant_id: TenantId
    project_id: TenantId | None = None
    principal_id: Id
    principal_kind: Literal[PrincipalKind.USER, PrincipalKind.SERVICE_ACCOUNT] = PrincipalKind.USER
    role: ProjectRole
    email: str | None = Field(None, max_length=320)
    display_name: str | None = Field(None, max_length=200)
    created_at: AwareDatetime
    created_by: Id | None = None

    model_config = dto_examples(
        {
            "tenant_id": "acme",
            "project_id": "growth",
            "principal_id": "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
            "principal_kind": "user",
            "role": "runner",
            "email": "ana@acme.example",
            "display_name": "Ana Analyst",
            "created_at": EXAMPLE_TS,
            "created_by": "usr_01jaf3a0b0c0d0e0f0g0h0j0k0",
        },
        {
            "tenant_id": "acme",
            "project_id": None,
            "principal_id": "usr_01jaf3a0b0c0d0e0f0g0h0j0k0",
            "role": "admin",
            "email": "it@acme.example",
            "created_at": EXAMPLE_TS,
        },
    )


def effective_role(
    memberships: Iterable[Membership],
    principal_id: str,
    tenant_id: str,
    project_id: str | None = None,
) -> ProjectRole | None:
    """The highest role ``principal_id`` holds: tenant-wide grants always count, project
    grants only when ``project_id`` is given and matches."""
    best: ProjectRole | None = None
    for m in memberships:
        if m.principal_id != principal_id or m.tenant_id != tenant_id:
            continue
        if m.project_id is not None and m.project_id != project_id:
            continue
        if best is None or m.role > best:
            best = m.role
    return best


# ================================================================================== principals


class InternalService(StrEnum):
    API = "api"
    DISPATCHER = "dispatcher"
    RECONCILER = "reconciler"
    FINALIZER = "finalizer"
    EVENTS = "events"
    PUBSUB_PUSH = "pubsub-push"
    """Google Pub/Sub delivering to ``/internal/events`` (OIDC-authenticated)."""


class UserPrincipal(StrictModel):
    """A person authenticated by OIDC. ``(issuer, subject)`` is the stable identity; the
    platform id ``usr_…`` is assigned at first login."""

    kind: Literal["user"] = "user"
    id: Id = Field(..., pattern=r"^usr_[0-9a-z]{26}$")
    issuer: str = Field(..., pattern=r"^https://[^\s]{1,500}$")
    subject: str = Field(..., min_length=1, max_length=255)
    email: str | None = Field(None, max_length=320)
    email_verified: bool = False
    name: str | None = Field(None, max_length=200)


class TokenPrincipal(StrictModel):
    """A request authenticated with an API token (acts as ``owner_id``, narrowed by scopes)."""

    kind: Literal["token"] = "token"
    id: Id = Field(..., pattern=r"^tok_[0-9a-z]{26}$")
    prefix: str = Field(..., pattern=r"^[0-9a-hjkmnp-tv-z]{8}$")
    owner_id: Id = Field(..., pattern=r"^(?:usr|sa)_[0-9a-z]{26}$")
    owner_kind: Literal[PrincipalKind.USER, PrincipalKind.SERVICE_ACCOUNT] = PrincipalKind.USER
    tenant_id: TenantId
    project_id: TenantId
    scopes: tuple[str, ...] = Field(..., min_length=1)

    @property
    def is_service_token(self) -> bool:
        return self.owner_kind is PrincipalKind.SERVICE_ACCOUNT


class ServicePrincipal(StrictModel):
    """An internal platform component (bypasses RLS through its own database role)."""

    kind: Literal["service"] = "service"
    id: str = Field(..., pattern=r"^svc_[a-z][a-z0-9-]{1,40}$")
    service: InternalService


Principal = Annotated[
    UserPrincipal | TokenPrincipal | ServicePrincipal, Field(discriminator="kind")
]


class AuthContext(StrictModel):
    """Who is calling, in which tenant/project, with which grants (one per request).

    ``role`` is the effective project role and ``tenant_role`` the tenant-wide role, both
    resolved by the server from memberships (for a token: its owner's memberships *now*).
    ``scopes`` is ``None`` for browser sessions (no narrowing) and the token's scopes else.
    """

    principal: Principal
    tenant_id: TenantId | None = None
    project_id: TenantId | None = None
    role: ProjectRole | None = None
    tenant_role: ProjectRole | None = None
    scopes: frozenset[str] | None = None
    operator: bool = False
    tenant_status: TenantStatus = TenantStatus.ACTIVE
    trust_tier: TrustTier | None = None
    request_id: str | None = Field(None, max_length=128)

    @property
    def principal_id(self) -> str:
        return self.principal.id

    @property
    def is_internal(self) -> bool:
        return isinstance(self.principal, ServicePrincipal)

    def allows(
        self,
        action: Action | str,
        resource: ResourceKind | str,
        *,
        resource_name: str | None = None,
    ) -> bool:
        """The single authorization decision (pure; no I/O).

        1. internal services may do exactly the ``internal`` rules;
        2. operators may do :data:`OPERATOR_PERMISSIONS` (regardless of tenant status);
        3. otherwise the tenant must allow it (suspended: reads only; deleting: nothing),
           the role at the rule's level must satisfy :func:`can`, and a token's scopes
           must cover it (:func:`mapi_spec.tokens.scope_allows`).
        """
        rule = required_rule(action, resource)
        if rule is None:
            return False
        act, res = Action(action), ResourceKind(resource)
        if rule.level is Level.INTERNAL:
            return self.is_internal
        if self.is_internal:
            return False
        if self.operator and (res, act) in OPERATOR_PERMISSIONS:
            return True
        if rule.level is Level.PLATFORM:
            return False
        if not (
            self.tenant_status.allows_writes
            or (act is Action.READ and self.tenant_status.allows_reads)
        ):
            return False
        role = (
            self.tenant_role
            if rule.level is Level.TENANT
            else _max_role(self.role, self.tenant_role)
        )
        if not can(role, act, res):
            return False
        if self.scopes is not None:
            return scope_allows(self.scopes, act.value, res.value, resource_name=resource_name)
        return True

    def require(
        self,
        action: Action | str,
        resource: ResourceKind | str,
        *,
        resource_name: str | None = None,
    ) -> None:
        """Raise ``FORBIDDEN`` unless :meth:`allows`."""
        if not self.allows(action, resource, resource_name=resource_name):
            raise MapiError(
                f"not allowed to {Action(action).value} {ResourceKind(resource).value}",
                code=ErrorCode.FORBIDDEN,
            )


def _max_role(a: ProjectRole | None, b: ProjectRole | None) -> ProjectRole | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


# ============================================================================ project selection


@dataclass(frozen=True, slots=True)
class ProjectSelector:
    """A parsed ``X-Mapi-Project`` value; ``tenant_id`` is ``None`` for a bare project id."""

    tenant_id: str | None
    project_id: str

    def __str__(self) -> str:
        return self.project_id if self.tenant_id is None else f"{self.tenant_id}/{self.project_id}"


def _check_slug(value: str, what: str) -> str:
    if not re.fullmatch(TENANT_ID_RE, value):
        raise MapiError(
            f"{PROJECT_HEADER}: {what} must be a lowercase slug",
            code=ErrorCode.SCHEMA_INVALID,
        )
    return value


def parse_project_selector(value: str) -> ProjectSelector:
    """Parse ``"<tenant>/<project>"`` or ``"<project>"`` (raises ``SCHEMA_INVALID``)."""
    text = value.strip()
    tenant, sep, project = text.partition("/")
    if not sep:
        return ProjectSelector(None, _check_slug(tenant, "the project id"))
    return ProjectSelector(
        _check_slug(tenant, "the tenant id"), _check_slug(project, "the project id")
    )


def resolve_project_scope(
    *,
    header: str | None = None,
    path_tenant: str | None = None,
    path_project: str | None = None,
    token_binding: tuple[str, str] | None = None,
    member_tenants: Collection[str] = (),
    default: tuple[str, str] | None = None,
) -> ProjectSelector:
    """Decide which ``(tenant, project)`` a project-scoped request acts in.

    Precedence: path parameters, then the ``X-Mapi-Project`` header, then the token's
    binding, then ``default`` (the single-tenant deployment profile only).

    * path and header that disagree → ``SCHEMA_INVALID``;
    * a token used outside its binding → ``FORBIDDEN`` (tokens are bound to one project);
    * a bare project id is qualified with the path tenant, the token's tenant, or the only
      tenant the principal belongs to; otherwise → ``SCHEMA_INVALID``;
    * nothing selects a project and there is no default → ``SCHEMA_INVALID``.

    Membership is checked afterwards; a project the caller cannot see is ``NOT_FOUND``
    (not ``FORBIDDEN``), so ids cannot be probed.
    """
    sel_header = parse_project_selector(header) if header else None
    tenant: str | None = path_tenant
    project: str | None = path_project
    if sel_header is not None:
        if project is not None and sel_header.project_id != project:
            raise MapiError(
                f"{PROJECT_HEADER} names a different project than the path",
                code=ErrorCode.SCHEMA_INVALID,
            )
        if tenant is not None and sel_header.tenant_id not in (None, tenant):
            raise MapiError(
                f"{PROJECT_HEADER} names a different tenant than the path",
                code=ErrorCode.SCHEMA_INVALID,
            )
        project = sel_header.project_id
        tenant = tenant or sel_header.tenant_id
    if project is None:
        if token_binding is not None:
            return ProjectSelector(*token_binding)
        if default is not None:
            return ProjectSelector(*default)
        raise MapiError(
            f"select a project with the {PROJECT_HEADER} header (<tenant>/<project>)",
            code=ErrorCode.SCHEMA_INVALID,
        )
    if tenant is None:
        if token_binding is not None:
            tenant = token_binding[0]
        elif len(set(member_tenants)) == 1:
            tenant = next(iter(member_tenants))
        elif default is not None and not member_tenants:
            tenant = default[0]
        else:
            raise MapiError(
                f"{PROJECT_HEADER} must be qualified as <tenant>/<project>",
                code=ErrorCode.SCHEMA_INVALID,
            )
    if token_binding is not None and (tenant, project) != tuple(token_binding):
        raise MapiError("this token is bound to a different project", code=ErrorCode.FORBIDDEN)
    return ProjectSelector(tenant, project)
