"""Per-request authentication and authorization (one FastAPI dependency for every route).

:func:`authenticate` runs before every handler:

1. looks up the route's :class:`~mapi_spec.api.RouteAccess` by ``operation_id``
   (:data:`mapi_spec.api.ROUTE_ACCESS`, plus :data:`SERVER_ROUTE_ACCESS` for server-only
   routes); an operation without a rule is refused (fail closed);
2. resolves the credential: ``Authorization: Bearer mapi_…`` (an API token: checksum,
   constant-time hash comparison, expiry, revocation, the owner still active), else the
   session cookie (absolute and idle expiry; cookie-authenticated mutations must pass the
   CSRF check), else, only where the profile allows it, the local administrator;
3. resolves the tenant/project (:func:`mapi_spec.tenancy.resolve_project_scope`) and the
   caller's roles from memberships *now* (a token acts as its owner, narrowed by scopes); a
   tenant or project the caller cannot see is ``NOT_FOUND``;
4. checks the route's permission with :meth:`mapi_spec.tenancy.AuthContext.require`
   (plus unconditional secondary checks), then the request-rate limits at token, tenant and
   global level (``RATE_LIMITED`` with ``Retry-After``).

These lookups run on the platform role (the tenant is not known yet); the handler then gets
a tenant session pinned to the resolved tenant (row-level security).
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from fastapi import Request
from mapi_spec.api import ROUTE_ACCESS, TOKEN_TENANT_ROUTES, RouteAccess, RouteScope
from mapi_spec.audit import AuditAction, AuditEvent, AuditOutcome, make_audit_event
from mapi_spec.auth import CookieSpec
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.quotas import GLOBAL_SUBJECT, QuotaPolicy
from mapi_spec.tenancy import (
    OPERATOR_PERMISSIONS,
    PROJECT_HEADER,
    Action,
    AuthContext,
    Membership,
    PrincipalKind,
    ProjectRole,
    ResourceKind,
    TenantStatus,
    TokenPrincipal,
    TrustTier,
    UserPrincipal,
    effective_role,
    resolve_project_scope,
)
from mapi_spec.tokens import (
    TokenRecord,
    TokenStatus,
    bearer_token,
    hash_token,
    looks_like_token,
    parse_token,
    verify_token_hash,
)
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.identity import (
    LOCAL_USER_ID,
    AccountDisabled,
    claim_or_create_user,
    is_operator,
    load_session,
    local_principal,
    memberships_of,
    token_record,
    user_principal,
)
from mapi_server.auth.oidc import OidcError
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError, ProblemWithHeaders, unauthenticated
from mapi_server.services.audit import write_audit
from mapi_server.services.quotas import global_policy, rate_limit, tenant_policy, token_policy
from mapi_server.services.runs import Scope, now

__all__ = [
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "DEV_CSRF_COOKIE",
    "SAFE_METHODS",
    "SERVER_ROUTE_ACCESS",
    "Credential",
    "RequestAuth",
    "access_for",
    "authenticate",
    "csrf_cookie",
    "session_cookie",
]

log = logging.getLogger(__name__)

CSRF_HEADER: Final = "X-CSRF-Token"
"""Cookie-authenticated mutations must echo the session's CSRF token in this header."""
CSRF_COOKIE: Final = "__Host-mapi_csrf"
DEV_CSRF_COOKIE: Final = "mapi_csrf"
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
TOKEN_TOUCH_INTERVAL: Final = timedelta(minutes=1)

_RK, _AC = ResourceKind, Action
_PUBLIC = RouteAccess(RouteScope.PUBLIC)

SERVER_ROUTE_ACCESS: Final[dict[str, RouteAccess]] = {
    "diff_spec_revisions": RouteAccess(RouteScope.PROJECT, _RK.SPEC, _AC.READ),
    "evaluate_model": RouteAccess(RouteScope.PROJECT, _RK.RUN, _AC.CREATE, ((_RK.MODEL, _AC.RUN),)),
    "redirect_dataset_view": RouteAccess(RouteScope.PROJECT, _RK.DATASET, _AC.READ),
    "redirect_model_ui": RouteAccess(RouteScope.PROJECT, _RK.MODEL, _AC.READ),
    "devidp_discovery": _PUBLIC,
    "devidp_jwks": _PUBLIC,
    "devidp_authorize": _PUBLIC,
    "devidp_token": _PUBLIC,
    # Prometheus scrape: authenticated by its own bearer (MAPI_METRICS_TOKEN), like pushes
    "metrics": RouteAccess(RouteScope.INTERNAL),
}
"""Routes the server serves beyond :data:`mapi_spec.api.ENDPOINTS` (same semantics)."""

_CONDITIONAL_ALSO: Final = frozenset({"create_run"})
"""Routes whose secondary checks depend on the body (applied by the handler)."""


def access_for(operation_id: str) -> RouteAccess:
    """The access rule of an operation; unknown operations are refused (fail closed)."""
    rule = ROUTE_ACCESS.get(operation_id) or SERVER_ROUTE_ACCESS.get(operation_id)
    if rule is None:
        raise MapiError(f"no access rule for {operation_id!r}", code=ErrorCode.FORBIDDEN)
    return rule


def session_cookie(secure: bool) -> CookieSpec:
    return CookieSpec.session(secure=secure)


def csrf_cookie(secure: bool) -> CookieSpec:
    """Readable by the SPA (not ``HttpOnly``) so it can echo it in :data:`CSRF_HEADER`."""
    spec = CookieSpec.session(secure=secure)
    return CookieSpec(
        CSRF_COOKIE if secure else DEV_CSRF_COOKIE,
        spec.max_age,
        http_only=False,
        secure=secure,
        same_site="Strict",
    )


class Credential(StrEnum):
    ANONYMOUS = "anonymous"
    """A public route (no credential looked at)."""
    LOCAL = "local"
    """No credential, single-tenant profile: the local administrator."""
    SESSION = "session"
    TOKEN = "token"
    SERVICE = "service"


@dataclass
class RequestAuth:
    """Who is calling, where, with which grants (``request.state.auth``)."""

    operation_id: str
    access: RouteAccess
    credential: Credential
    context: AuthContext | None = None
    scope: Scope | None = None
    tenant_id: str | None = None
    token: TokenRecord | None = None
    memberships: tuple[Membership, ...] = ()
    session_key: str | None = None
    csrf_token: str | None = None
    request_id: str = ""
    client_ip: str | None = None
    user_agent: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def principal(self) -> UserPrincipal | TokenPrincipal | None:
        p = self.context.principal if self.context else None
        return p if isinstance(p, UserPrincipal | TokenPrincipal) else None

    @property
    def principal_id(self) -> str | None:
        return self.context.principal_id if self.context else None

    @property
    def user_id(self) -> str | None:
        """The acting person or service account (a token's owner)."""
        p = self.principal
        if isinstance(p, TokenPrincipal):
            return p.owner_id
        return p.id if p is not None else None

    @property
    def operator(self) -> bool:
        return bool(self.context and self.context.operator)

    @property
    def actor_kind(self) -> PrincipalKind | None:
        if self.context is None:
            return None
        return PrincipalKind(self.context.principal.kind)

    def require(
        self,
        action: Action | str,
        resource: ResourceKind | str,
        *,
        resource_name: str | None = None,
    ) -> None:
        if self.context is None:
            raise unauthenticated()
        self.context.require(action, resource, resource_name=resource_name)

    def allows(
        self,
        action: Action | str,
        resource: ResourceKind | str,
        *,
        resource_name: str | None = None,
    ) -> bool:
        return bool(
            self.context and self.context.allows(action, resource, resource_name=resource_name)
        )

    def audit(
        self,
        ctx: ServerContext,
        action: AuditAction,
        resource_kind: ResourceKind,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        *,
        resource_id: str | None = None,
        code: ErrorCode | None = None,
        details: dict[str, JsonValue] | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> AuditEvent:
        """An audit event for this caller (tenant/project default to the request's)."""
        return make_audit_event(
            action=action,
            resource_kind=resource_kind,
            outcome=outcome,
            ts=now(),
            fingerprint_key=ctx.secrets.audit_key,
            tenant_id=tenant_id if tenant_id is not None else self.tenant_id,
            project_id=project_id
            if project_id is not None
            else (self.scope.project_id if self.scope else None),
            actor_kind=self.actor_kind,
            actor_id=self.principal_id,
            token_id=self.token.id if self.token else None,
            operator=self.operator,
            resource_id=resource_id,
            code=code,
            client_ip=self.client_ip,
            user_agent=self.user_agent,
            request_id=self.request_id or None,
            details=details,
        )


@dataclass
class _Resolved:
    credential: Credential
    principal: UserPrincipal | TokenPrincipal
    operator: bool = False
    token: TokenRecord | None = None
    session: m.WebSession | None = None


class _AuthFailure(Exception):
    """Raised inside the lookup transaction; carries audit rows to write after rollback."""

    def __init__(self, error: MapiError, audit: list[AuditEvent]) -> None:
        super().__init__(error.detail)
        self.error = error
        self.audit = audit


def _request_id(request: Request) -> str:
    rid = request.headers.get("x-request-id", "")
    if rid and len(rid) <= 128 and rid.isprintable():
        return rid
    return "req-" + secrets.token_hex(8)


def _token_failure(
    ctx: ServerContext, request: Request, row: m.ApiToken, reason: str
) -> _AuthFailure:
    ev = make_audit_event(
        action=AuditAction.TOKEN_AUTH_FAILED,
        resource_kind=ResourceKind.TOKEN,
        outcome=AuditOutcome.DENIED,
        ts=now(),
        fingerprint_key=ctx.secrets.audit_key,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        resource_id=row.id,
        code=ErrorCode.UNAUTHENTICATED,
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        details={"reason": reason, "prefix": row.prefix},
    )
    return _AuthFailure(unauthenticated(f"the API token is {reason}"), [ev])


async def _from_token(
    s: AsyncSession, ctx: ServerContext, request: Request, raw: str, at: datetime
) -> _Resolved:
    try:
        parsed = parse_token(raw)
    except MapiError:
        raise unauthenticated("malformed API token") from None
    pepper = ctx.secrets.token_pepper
    row = (
        await s.execute(select(m.ApiToken).where(m.ApiToken.prefix == parsed.prefix))
    ).scalar_one_or_none()
    if row is None:
        hash_token(parsed, pepper)  # the same work as a known prefix (timing)
        raise unauthenticated("invalid API token")
    if not verify_token_hash(parsed, row.secret_hash, pepper):
        raise _token_failure(ctx, request, row, "invalid")
    record = token_record(row)
    status = record.status_at(at)
    if status is not TokenStatus.ACTIVE:
        raise _token_failure(ctx, request, row, status.value)
    if row.owner_kind == PrincipalKind.USER.value:
        owner = await s.get(m.User, row.owner_id)
        if owner is None or owner.status != "active":
            raise _token_failure(ctx, request, row, "orphaned")
    if row.last_used_at is None or at - row.last_used_at >= TOKEN_TOUCH_INTERVAL:
        row.last_used_at = at
    principal = TokenPrincipal(
        id=row.id,
        prefix=row.prefix,
        owner_id=row.owner_id,
        owner_kind=PrincipalKind(row.owner_kind),
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        scopes=tuple(record.scopes),
    )
    return _Resolved(Credential.TOKEN, principal, operator=False, token=record)


async def _from_id_token(s: AsyncSession, ctx: ServerContext, raw: str) -> _Resolved:
    """An OIDC ID token from a public client (``mapi login``) of the configured issuer: the
    same person as a browser sign-in (Me.auth is ``session``), without a cookie or CSRF."""
    audiences = ctx.settings.oidc_cli_client_ids
    if ctx.oidc is None or not audiences:
        raise unauthenticated("this server accepts API tokens (mapi_…) as bearers only")
    try:
        ident = await ctx.oidc.verify_bearer(raw, audiences=audiences)
    except OidcError as exc:
        log.info("bearer ID token refused: %s", exc.reason)
        raise unauthenticated("the ID token was refused; sign in again") from None
    user = (
        await s.execute(
            select(m.User).where(m.User.issuer == ident.issuer, m.User.subject == ident.subject)
        )
    ).scalar_one_or_none()
    operators = ctx.settings.effective_operator_emails()
    if user is None:  # first use: claim an invitation or create the user, like /auth/callback
        try:
            user = await claim_or_create_user(s, ident, operator_emails=operators)
        except AccountDisabled:
            raise unauthenticated("this account is disabled") from None
    elif user.status != "active":
        raise unauthenticated("this account is disabled")
    return _Resolved(Credential.SESSION, user_principal(user), is_operator(user, operators))


def _check_csrf(ctx: ServerContext, request: Request, sess: m.WebSession) -> None:
    """Cookie-authenticated mutations: same-origin ``Origin`` and the session's CSRF token."""
    if request.method.upper() in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") not in ctx.settings.allowed_origins():
        raise MapiError("cross-origin request refused", code=ErrorCode.FORBIDDEN)
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise MapiError("cross-site request refused", code=ErrorCode.FORBIDDEN)
    presented = request.headers.get(CSRF_HEADER, "")
    if not presented or not hmac.compare_digest(presented.encode(), sess.csrf_token.encode()):
        raise MapiError(
            f"cookie-authenticated requests must send the {CSRF_HEADER} header",
            code=ErrorCode.FORBIDDEN,
        )


async def _credentials(
    s: AsyncSession, ctx: ServerContext, request: Request, at: datetime
) -> _Resolved:
    header = request.headers.get("authorization")
    raw = bearer_token(header)
    if header is not None and raw is None:
        raise unauthenticated("unsupported Authorization scheme (use Bearer mapi_…)")
    if raw is not None:
        if not looks_like_token(raw) and raw.count(".") == 2:
            return await _from_id_token(s, ctx, raw)
        return await _from_token(s, ctx, request, raw, at)
    cookie = request.cookies.get(session_cookie(ctx.settings.secure_cookies).name)
    if cookie:
        loaded = await load_session(s, cookie, at=at)
        if loaded is None:
            raise unauthenticated("the session has expired; sign in again")
        sess, user = loaded
        _check_csrf(ctx, request, sess)
        operator = is_operator(user, ctx.settings.effective_operator_emails())
        return _Resolved(Credential.SESSION, user_principal(user), operator, session=sess)
    if not ctx.settings.requires_auth:
        return _Resolved(Credential.LOCAL, local_principal(), operator=True)
    raise unauthenticated()


def _local_memberships(ctx: ServerContext, at: datetime) -> list[Membership]:
    return [
        Membership(
            tenant_id=ctx.settings.tenant_id,
            project_id=None,
            principal_id=LOCAL_USER_ID,
            role=ProjectRole.ADMIN,
            created_at=at,
        )
    ]


async def _resolve(
    s: AsyncSession, ctx: ServerContext, request: Request, auth: RequestAuth, at: datetime
) -> RequestAuth:
    settings = ctx.settings
    who = await _credentials(s, ctx, request, at)
    auth.credential = who.credential
    auth.token = who.token
    if who.session is not None:
        auth.session_key = who.session.id
        auth.csrf_token = who.session.csrf_token
    token = who.token
    member_id = token.owner_id if token is not None else who.principal.id
    memberships = await memberships_of(
        s, member_id, tenant_ids=[token.tenant_id] if token is not None else None
    )
    if who.credential is Credential.LOCAL and not memberships:
        memberships = _local_memberships(ctx, at)
    auth.memberships = tuple(memberships)
    scopes = frozenset(token.scopes) if token is not None else None
    access = auth.access
    tenant: m.Tenant | None = None
    common: dict[str, Any] = {
        "principal": who.principal,
        "scopes": scopes,
        "operator": who.operator,
        "request_id": auth.request_id,
    }
    if access.scope is RouteScope.TENANT:
        tenant_id = str(request.path_params.get("tenant_id", ""))
        if token is not None and token.tenant_id != tenant_id:
            raise MapiError("this token is bound to a different tenant", code=ErrorCode.FORBIDDEN)
        if token is not None and auth.operation_id not in TOKEN_TENANT_ROUTES:
            raise MapiError(
                "API tokens are bound to one project; tenant administration needs a "
                "signed-in session",
                code=ErrorCode.FORBIDDEN,
            )
        tenant = await s.get(m.Tenant, tenant_id) if tenant_id else None
        member = any(mm.tenant_id == tenant_id for mm in memberships)
        if tenant is None or not (member or who.operator):
            raise NotFoundError("tenant", tenant_id)
        auth.tenant_id = tenant.id
        auth.context = AuthContext(
            **common,
            tenant_id=tenant.id,
            tenant_role=effective_role(memberships, member_id, tenant.id, None),
            tenant_status=TenantStatus(tenant.status),
            trust_tier=TrustTier(tenant.trust_tier),
        )
    elif access.scope is RouteScope.PROJECT:
        path_tenant = request.path_params.get("tenant_id")
        path_project = request.path_params.get("project_id")
        selector = resolve_project_scope(
            header=request.headers.get(PROJECT_HEADER),
            path_tenant=str(path_tenant) if path_tenant else None,
            path_project=str(path_project) if path_project else None,
            token_binding=(token.tenant_id, token.project_id) if token is not None else None,
            member_tenants={mm.tenant_id for mm in memberships},
            default=None if settings.multi_tenant else (settings.tenant_id, settings.project_id),
        )
        assert selector.tenant_id is not None
        tenant = await s.get(m.Tenant, selector.tenant_id)
        project = await s.get(m.Project, (selector.tenant_id, selector.project_id))
        role = effective_role(memberships, member_id, selector.tenant_id, selector.project_id)
        operator_may = (
            who.operator
            and access.resource is not None
            and access.action is not None
            and (access.resource, access.action) in OPERATOR_PERMISSIONS
        )
        if tenant is None or project is None or not (role is not None or operator_may):
            raise NotFoundError("project", str(selector))
        auth.tenant_id = tenant.id
        auth.scope = Scope(tenant.id, project.id)
        auth.context = AuthContext(
            **common,
            tenant_id=tenant.id,
            project_id=project.id,
            role=role,
            tenant_role=effective_role(memberships, member_id, tenant.id, None),
            tenant_status=TenantStatus(tenant.status),
            trust_tier=TrustTier(tenant.trust_tier),
        )
    else:  # AUTHENTICATED, PLATFORM
        auth.context = AuthContext(**common)
    if access.resource is not None and access.action is not None:
        named = request.path_params.get(access.name_param) if access.name_param else None
        auth.context.require(
            access.action, access.resource, resource_name=str(named) if named else None
        )
        if auth.operation_id not in _CONDITIONAL_ALSO:
            for resource, action in access.also:
                auth.context.require(action, resource)
    if settings.rate_limits_enabled:
        levels: list[tuple[QuotaPolicy, str]] = []
        if token is not None:
            levels.append((await token_policy(s, token), f"token:{token.id}"))
        if tenant is not None:
            levels.append(
                (await tenant_policy(s, tenant.id, tenant.trust_tier), f"tenant:{tenant.id}")
            )
        levels.append((await global_policy(s), GLOBAL_SUBJECT))
        decision = await rate_limit(s, levels, now=at)
        if not decision.allowed:
            assert decision.code is not None
            raise ProblemWithHeaders(
                decision.message or "too many requests",
                code=decision.code,
                headers=decision.headers(),
            )
    return auth


async def authenticate(request: Request) -> RequestAuth:
    """The app-wide dependency (cached per request; also ``request.state.auth``)."""
    cached = getattr(request.state, "auth", None)
    if isinstance(cached, RequestAuth):
        return cached
    ctx: ServerContext = request.app.state.ctx
    route = request.scope.get("route")
    op = str(getattr(route, "operation_id", None) or getattr(route, "name", "") or "")
    access = access_for(op)
    auth = RequestAuth(
        operation_id=op,
        access=access,
        credential=Credential.ANONYMOUS,
        request_id=_request_id(request),
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    if access.scope in (RouteScope.PUBLIC, RouteScope.INTERNAL):
        request.state.auth = auth
        return auth
    try:
        async with ctx.db.session() as s:
            await _resolve(s, ctx, request, auth, now())
    except _AuthFailure as failure:
        await write_audit(ctx.db, failure.audit)
        raise failure.error from None
    except MapiError as exc:
        denied = _DENIED_AUDIT.get(auth.operation_id)
        if denied is not None and exc.code is ErrorCode.FORBIDDEN:
            action, resource = denied
            await write_audit(
                ctx.db, [auth.audit(ctx, action, resource, AuditOutcome.DENIED, code=exc.code)]
            )
        raise
    request.state.auth = auth
    return auth


_DENIED_AUDIT: Final[dict[str, tuple[AuditAction, ResourceKind]]] = {
    "admin_create_tenant": (AuditAction.TENANT_CREATE, ResourceKind.TENANT),
    "admin_update_tenant": (AuditAction.TENANT_UPDATE, ResourceKind.TENANT),
    "admin_put_quota": (AuditAction.QUOTA_UPDATE, ResourceKind.QUOTA),
    "admin_delete_quota": (AuditAction.QUOTA_DELETE, ResourceKind.QUOTA),
    "admin_put_kill_switches": (AuditAction.KILL_SWITCH_UPDATE, ResourceKind.KILL_SWITCH),
    "create_token": (AuditAction.TOKEN_CREATE, ResourceKind.TOKEN),
    "revoke_token": (AuditAction.TOKEN_REVOKE, ResourceKind.TOKEN),
    "add_member": (AuditAction.MEMBER_ADD, ResourceKind.MEMBER),
    "add_tenant_member": (AuditAction.MEMBER_ADD, ResourceKind.TENANT_MEMBER),
}
"""Privileged operations whose *refusals* are audited (a signal of probing or misuse)."""
