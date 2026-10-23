"""Review of uploaded code: ask for approval, approve or reject, yank (v0.19 D13-D15).

**Approval is a second person's decision (binding).** ``:approve`` and ``:reject`` need a web
session (API tokens are refused by ``TOKEN_NEVER``, the single-tenant local administrator has
no session), a tenant admin, a sign-in at most ``MAPI_PLUGIN_APPROVE_MAX_AUTH_AGE_S`` ago
(else ``REAUTH_REQUIRED``), someone other than the uploader (``SELF_APPROVAL_FORBIDDEN``),
whose membership the uploader did not create in the last
``MAPI_PLUGIN_APPROVER_MIN_MEMBERSHIP_AGE_DAYS`` days, and the digests the reviewer saw
(the wheel's SHA-256, the exports' ``describe_digest`` and the base image digest: any change
is ``PLUGIN_STATE_CONFLICT``). The row is locked, so of two racing decisions one wins and the
other gets ``PLUGIN_STATE_CONFLICT``.

**Yank** (the uploader for their own versions, else a tenant admin; never a token) takes the
version out of every catalog view and every admission and dispatch at once (the gate reads
the state); ``security`` also cancels the queued and running runs pinned to it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction, AuditOutcome
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.governance import check_approver
from mapi_spec.plugins import PluginDecision, PluginYank
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.tenancy import Action, ResourceKind
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import Credential, RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.audit import add_audit, write_audit
from mapi_server.services.plugins.gate import describe_for
from mapi_server.services.plugins.notify import notify_plugin_event
from mapi_server.services.plugins.store import load_version, set_state
from mapi_server.services.plugins.uploaders import caller_id, is_tenant_admin, refuse_tokens
from mapi_server.services.runs import apply_transition, now

__all__ = ["cancel_pinned_runs", "decide", "request_approval", "yank"]

_SKIP_CANCEL = frozenset(
    {s.value for s in RunState if s.terminal}
    | {RunState.FINALIZING.value, RunState.CANCELLING.value}
)


def _conflict(message: str) -> MapiError:
    return MapiError(message, code=ErrorCode.PLUGIN_STATE_CONFLICT)


async def request_approval(
    session: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    version_id: str,
    *,
    plugin_id: str | None = None,
) -> m.PluginVersion:
    """``kit_passed → awaiting_approval`` by the uploader or a tenant admin (a
    ``plugins:publish`` token of either may do it). Mails and webhooks the tenant admins,
    except the uploader."""
    tenant_id = ctx.scope.tenant_id
    version = await load_version(session, tenant_id, plugin_id, version_id, lock=True)
    user = caller_id(auth)
    if version.uploaded_by != user and not is_tenant_admin(auth):
        raise MapiError(
            "Only the person who uploaded this version or a tenant admin can ask for its review.",
            code=ErrorCode.FORBIDDEN,
        )
    if version.state != "kit_passed":
        raise _conflict(
            f"Only a version that passed its tests can be sent for review; this one is "
            f"{version.state.replace('_', ' ')}."
        )
    ts = now()
    set_state(version, "awaiting_approval", now=ts)
    version.requested_by = user
    version.requested_at = ts
    await session.flush()
    await add_audit(
        session,
        [
            auth.audit(
                ctx,
                AuditAction.PLUGIN_APPROVAL_REQUEST,
                ResourceKind.PLUGIN,
                resource_id=version.id,
                details={"plugin_id": version.plugin_id, "plugin_version_id": version.id},
            )
        ],
    )
    await notify_plugin_event(
        session,
        ctx,
        version,
        "plugin.version.awaiting_approval",
        to="admins",
        exclude=(version.uploaded_by,),
    )
    return version


async def _require_recent_session(ctx: ServerContext, auth: RequestAuth) -> str:
    """A web session signed in at most ``plugin_approve_max_auth_age_s`` ago; returns the
    person. A token or the single-tenant local administrator has no session (``FORBIDDEN``,
    before the age check); an older sign-in is ``REAUTH_REQUIRED``."""
    if auth.credential is not Credential.SESSION or auth.session_key is None:
        raise MapiError(
            "Approving or rejecting uploaded code needs a person signed in to the web app. API "
            "tokens and the local administrator cannot do it.",
            code=ErrorCode.FORBIDDEN,
        )
    async with ctx.db.session() as s:  # web_sessions is a platform table
        signed_in = (
            await s.execute(
                select(m.WebSession.created_at).where(m.WebSession.id == auth.session_key)
            )
        ).scalar_one_or_none()
    max_age = timedelta(seconds=int(ctx.settings.plugin_approve_max_auth_age_s))
    if signed_in is None or now() - signed_in > max_age:
        raise MapiError(
            "Sign in again to approve or reject uploaded code (the sign-in must be less than "
            f"{int(max_age.total_seconds() // 60)} minutes old).",
            code=ErrorCode.REAUTH_REQUIRED,
        )
    return caller_id(auth)


async def _sock_puppet(
    session: AsyncSession, ctx: ServerContext, *, tenant_id: str, approver: str, uploader: str
) -> bool:
    """Did the uploader create one of the approver's memberships in the last
    ``plugin_approver_min_membership_age_days`` days?"""
    days = int(ctx.settings.plugin_approver_min_membership_age_days)
    since = now() - timedelta(days=days)
    row = (
        await session.execute(
            select(m.Membership.id).where(
                m.Membership.tenant_id == tenant_id,
                m.Membership.principal_id == approver,
                m.Membership.created_by == uploader,
                m.Membership.created_at > since,
            )
        )
    ).first()
    return row is not None


async def decide(
    session: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    version_id: str,
    body: PluginDecision,
    *,
    decision: Literal["approve", "reject"],
    plugin_id: str | None = None,
) -> m.PluginVersion:
    """A second tenant admin approves (``approved``, tenant-wide) or rejects (``rejected``;
    a note is required) a version waiting for review. Mails and webhooks every tenant admin
    and the uploader."""
    tenant_id = ctx.scope.tenant_id
    approver = await _require_recent_session(ctx, auth)
    if not auth.allows(Action.APPROVE, ResourceKind.PLUGIN):
        raise MapiError("Only a tenant admin can approve uploaded code.", code=ErrorCode.FORBIDDEN)
    if decision == "reject" and not body.note:
        raise MapiError(
            "Say why the version is not approved (note).",
            code=ErrorCode.SCHEMA_INVALID,
            issues=[
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/note",
                    message="A rejection needs a note.",
                )
            ],
        )
    version = await load_version(session, tenant_id, plugin_id, version_id, lock=True)
    check_approver(
        submitter_id=version.uploaded_by,
        approver_id=approver,
        is_reviewer=True,  # the tenant-admin check above; the reviewer capability is for models
        subject="this plugin version",
    )
    if await _sock_puppet(
        session, ctx, tenant_id=tenant_id, approver=approver, uploader=version.uploaded_by
    ):
        days = int(ctx.settings.plugin_approver_min_membership_age_days)
        await write_audit(
            ctx.db,
            [
                auth.audit(
                    ctx,
                    AuditAction.PLUGIN_APPROVE
                    if decision == "approve"
                    else AuditAction.PLUGIN_REJECT,
                    ResourceKind.PLUGIN,
                    AuditOutcome.DENIED,
                    resource_id=version.id,
                    code=ErrorCode.FORBIDDEN,
                    details={
                        "plugin_version_id": version.id,
                        "approver_id": approver,
                        "uploader_id": version.uploaded_by,
                        "reason": "membership_created_by_uploader",
                    },
                )
            ],
        )
        raise MapiError(
            f"The person who uploaded this version added you to the workspace less than {days} "
            "days ago, so you cannot review it. Ask another tenant admin.",
            code=ErrorCode.FORBIDDEN,
        )
    if version.state != "awaiting_approval":
        raise _conflict(
            f"This version is {version.state.replace('_', ' ')}, not waiting for a review. "
            "Reload the page."
        )
    describe = await describe_for(session, version.id, ctx.catalogs.image_digest)
    if (
        describe is None
        or body.overlay_sha256 != version.upload_sha256
        or body.describe_digest != describe.describe_digest
        or body.base_image_digest != describe.base_image_digest
    ):
        raise _conflict(
            "The version changed after the page was loaded (its file, its exports or the base "
            "image). Reload the page and review it again."
        )
    ts = now()
    session.add(
        m.PluginApproval(
            id=new_id("pap"),
            tenant_id=tenant_id,
            version_id=version.id,
            overlay_sha256=version.upload_sha256,
            describe_digest=describe.describe_digest,
            base_image_digest=describe.base_image_digest,
            decision=decision,
            approver_id=approver,
            uploader_id=version.uploaded_by,
            note=body.note,
            created_at=ts,
        )
    )
    set_state(
        version,
        "approved" if decision == "approve" else "rejected",
        now=ts,
        retention=timedelta(days=int(ctx.settings.plugin_retention_days)),
    )
    await session.flush()
    await add_audit(
        session,
        [
            auth.audit(
                ctx,
                AuditAction.PLUGIN_APPROVE if decision == "approve" else AuditAction.PLUGIN_REJECT,
                ResourceKind.PLUGIN,
                resource_id=version.id,
                details={
                    "plugin_id": version.plugin_id,
                    "plugin_version_id": version.id,
                    "approver_id": approver,
                    "uploader_id": version.uploaded_by,
                    "overlay_sha256": version.upload_sha256,
                    "describe_digest": describe.describe_digest,
                    "base_image_digest": describe.base_image_digest,
                },
            )
        ],
    )
    await notify_plugin_event(
        session,
        ctx,
        version,
        "plugin.version.approved" if decision == "approve" else "plugin.version.rejected",
        to="admins_and_uploader",
    )
    return version


async def cancel_pinned_runs(session: AsyncSession, version_id: str) -> list[str]:
    """Move every unfinished run pinned to ``version_id`` to ``cancelling`` (in the caller's
    tenant session; ``finalizing`` runs are left alone, their execution is over). The
    reconciler stops the executions and confirms ``cancelled``."""
    runs = (
        await session.execute(
            select(m.Run)
            .where(
                m.Run.overlays.contains([{"plugin_version_id": version_id}]),
                m.Run.state.not_in(_SKIP_CANCEL),
            )
            .order_by(m.Run.id)
            .with_for_update()
        )
    ).scalars()
    cancelled: list[str] = []
    for run in list(runs):
        await apply_transition(session, run, RunTrigger.CANCEL, Actor.API)
        cancelled.append(run.id)
    return cancelled


async def yank(
    session: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    version_id: str,
    body: PluginYank,
    *,
    plugin_id: str | None = None,
) -> m.PluginVersion:
    """Withdraw a version (``kit_passed``, ``awaiting_approval`` or ``approved``): the
    uploader for their own version, else a tenant admin; never an API token. ``security``
    also cancels the runs pinned to it. Mails and webhooks every tenant admin and the
    uploader."""
    refuse_tokens(auth, "withdraw uploaded code")
    tenant_id = ctx.scope.tenant_id
    version = await load_version(session, tenant_id, plugin_id, version_id, lock=True)
    user = caller_id(auth)
    if version.uploaded_by != user and not is_tenant_admin(auth):
        raise MapiError(
            "Only the person who uploaded this version or a tenant admin can withdraw it.",
            code=ErrorCode.FORBIDDEN,
        )
    if version.state not in ("kit_passed", "awaiting_approval", "approved"):
        raise _conflict(
            f"This version is {version.state.replace('_', ' ')}; only a version that passed "
            "its tests can be withdrawn."
        )
    ts = now()
    set_state(
        version,
        "yanked",
        now=ts,
        retention=timedelta(days=int(ctx.settings.plugin_retention_days)),
    )
    version.yanked_by = user
    version.yank_reason = body.reason
    version.yank_note = body.note
    await session.flush()
    cancelled = await cancel_pinned_runs(session, version.id) if body.reason == "security" else []
    await add_audit(
        session,
        [
            auth.audit(
                ctx,
                AuditAction.PLUGIN_YANK,
                ResourceKind.PLUGIN,
                resource_id=version.id,
                details={
                    "plugin_id": version.plugin_id,
                    "plugin_version_id": version.id,
                    "reason": body.reason,
                    "cancelled_runs": len(cancelled),
                },
            )
        ],
    )
    await notify_plugin_event(
        session, ctx, version, "plugin.version.yanked", to="admins_and_uploader"
    )
    return version
