"""Mediated compute: a dashboard's runs act as its publisher (v0.16, plan D7).

A client user or a share link never holds ``(run, create)`` or ``(model, run)``. The exact
what-ifs, actuals scores and refreshes of a dashboard are submitted server-side as the
dashboard's ``run_as`` owner (the runner who published it): :func:`owner_context` builds a
synthetic :class:`~mapi_server.auth.request.RequestAuth` for that user from their memberships
*now* and returns a :class:`~mapi_server.context.ServerContext` bound to it, so admission
(quotas, kill switches, placement), run attribution (``runs.created_by``) and audit work as
for a request of the owner.

Every submit re-checks the rule: when the owner lost the runner role (or left the project,
or the tenant is not active) the dashboard's ``live_inputs`` become ``paused`` and the submit
is refused with ``DASHBOARD_LIVE_PAUSED``; existing readouts still show.
"""

from __future__ import annotations

from typing import Final

from mapi_spec.api import RouteAccess, RouteScope
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import (
    Action,
    AuthContext,
    Membership,
    ProjectRole,
    ResourceKind,
    TenantStatus,
    TrustTier,
    effective_role,
)
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.identity import (
    LOCAL_USER_ID,
    local_principal,
    memberships_of,
    user_principal,
)
from mapi_server.auth.request import Credential, RequestAuth, held_capabilities
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import Scope, now

__all__ = ["OWNER_OPERATION", "PAUSED_PROBLEM", "owner_auth", "owner_context", "paused_error"]

OWNER_OPERATION: Final = "dashboard_owner_run"
"""The ``operation_id`` of the synthetic owner request (it shows in audit rows)."""
PAUSED_PROBLEM: Final = "live_inputs_paused"
"""``dashboards.problem`` once the owner lost the right to run (for the analyst)."""

_ACCESS: Final = RouteAccess(RouteScope.PROJECT)


def paused_error() -> MapiError:
    return MapiError(
        "the dashboard's live inputs are paused: the analyst who published it can no longer "
        "start runs in this project. Ask your analyst to publish it again",
        code=ErrorCode.DASHBOARD_LIVE_PAUSED,
    )


async def owner_auth(
    ctx: ServerContext, tenant_id: str, project_id: str, user_id: str | None
) -> RequestAuth | None:
    """A :class:`RequestAuth` for ``user_id`` acting in the project, from their grants now
    (``None`` when the user is unknown, never signed in, or holds no grant there)."""
    if not user_id:
        return None
    async with ctx.db.session() as s:  # identity tables: the platform (gateway) role
        grants: list[Membership] = await memberships_of(s, user_id, tenant_ids=[tenant_id])
        tenant = await s.get(m.Tenant, tenant_id)
        if tenant is None:
            return None
        if user_id == LOCAL_USER_ID and not ctx.settings.multi_tenant:
            principal = local_principal()
            if not grants:  # the single-tenant local admin (never a stored grant)
                grants = [
                    Membership(
                        tenant_id=tenant_id,
                        project_id=None,
                        principal_id=LOCAL_USER_ID,
                        role=ProjectRole.ADMIN,
                        created_at=now(),
                    )
                ]
        else:
            user = await s.get(m.User, user_id)
            if user is None or user.issuer is None or user.subject is None:
                return None
            if user.status != "active":
                return None
            principal = user_principal(user)
        role = effective_role(grants, user_id, tenant_id, project_id)
        if role is None:
            return None
        context = AuthContext(
            principal=principal,
            tenant_id=tenant_id,
            project_id=project_id,
            role=role,
            tenant_role=effective_role(grants, user_id, tenant_id, None),
            capabilities=held_capabilities(grants, user_id, tenant_id, project_id),
            tenant_status=TenantStatus(tenant.status),
            trust_tier=TrustTier(tenant.trust_tier),
        )
    return RequestAuth(
        operation_id=OWNER_OPERATION,
        access=_ACCESS,
        credential=Credential.SESSION,
        context=context,
        scope=Scope(tenant_id, project_id),
        tenant_id=tenant_id,
        memberships=tuple(grants),
    )


def _may_run(auth: RequestAuth | None) -> bool:
    return auth is not None and (
        auth.allows(Action.CREATE, ResourceKind.RUN) and auth.allows(Action.RUN, ResourceKind.MODEL)
    )


async def owner_context(
    ctx: ServerContext, session: AsyncSession, dashboard: m.Dashboard
) -> ServerContext:
    """The context the dashboard's runs are submitted in (its ``run_as`` owner), or
    ``DASHBOARD_LIVE_PAUSED`` after marking the dashboard paused.

    The pause is written in its own transaction (the refusal rolls the caller's back, and
    the analyst must still see why), so call this *before* the caller locks the dashboard
    row. ``session`` is the caller's: its copy of the row is updated to match."""
    auth = await owner_auth(ctx, dashboard.tenant_id, dashboard.project_id, dashboard.run_as)
    if not _may_run(auth):
        if dashboard.live_inputs != "paused":
            ts = now()
            async with ctx.db.tenant_session(dashboard.tenant_id) as own:
                await own.execute(
                    update(m.Dashboard)
                    .where(m.Dashboard.id == dashboard.id)
                    .values(live_inputs="paused", problem=PAUSED_PROBLEM, updated_at=ts)
                )
            dashboard.live_inputs, dashboard.problem, dashboard.updated_at = (
                "paused", PAUSED_PROBLEM, ts
            )  # fmt: skip
        raise paused_error()
    assert auth is not None
    return ctx.for_request(auth)
