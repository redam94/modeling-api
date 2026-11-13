"""Plugins, uploads and versions: the one state writer, claims, upload tickets, ``:complete``
and the REST views (v0.19 D3-D5, D8, I-7).

:func:`set_state` is the only code that changes ``plugin_versions.state`` (the review routes
here, and the pipeline's finalizers and sweeps): it refuses an edge outside
:data:`~mapi_spec.plugins.PLUGIN_TRANSITIONS` and keeps ``scope``, ``decided_at``,
``yanked_at``, ``purged_at`` and ``purge_after`` in step with the state. The caller holds the
row ``FOR UPDATE``.

An upload is a wheel posted to the quarantine bucket under a POST policy for exactly one
key. ``:complete`` checks the object's size, then streams it once to compute its SHA-256 (as
bytes: the control plane never opens the archive), records the version and starts the
sandboxed ``plugin_inspect`` run (``services.plugins.pipeline``).

Uploaded strings (titles, About text, kit messages) are stored and returned as data; the
spec DTOs normalize them and the web renders them as text.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, Literal, cast

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction
from mapi_spec.errors import ErrorCode, Issue, MapiError, Problem
from mapi_spec.plugins import (
    MOUNTABLE_STATES,
    PLUGIN_ID_PREFIX,
    PLUGIN_UPLOAD_ID_PREFIX,
    PLUGIN_VERSION_ID_PREFIX,
    ExportKind,
    Plugin,
    PluginClaim,
    PluginDecisionRecord,
    PluginExports,
    PluginInspection,
    PluginKitReport,
    PluginUploadComplete,
    PluginUploadTicket,
    PluginUploadTicketRequest,
    PluginVersion,
    PluginVersionState,
    PluginVersionSummary,
    check_transition,
    upload_dist_name,
    upload_namespace,
)
from mapi_spec.storage import NotFound, plugin_quarantine_key, plugin_quarantine_prefix
from mapi_spec.tenancy import ResourceKind
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError, ProblemWithHeaders
from mapi_server.services.audit import add_audit
from mapi_server.services.plugins import pipeline
from mapi_server.services.plugins.gate import describe_for
from mapi_server.services.plugins.uploaders import (
    caller_id,
    ensure_settings,
    is_tenant_admin,
    require_can_upload,
    require_plugins_available,
)
from mapi_server.services.runs import now

__all__ = [
    "PLUGIN_RETENTION",
    "RETAINED_STATES",
    "claim_plugin",
    "complete_upload",
    "create_ticket",
    "list_plugins",
    "list_versions",
    "load_plugin",
    "load_version",
    "parse_wheel_filename",
    "plugin_dto",
    "plugin_view",
    "set_state",
    "summary_dto",
    "version_dto",
    "version_visible",
]

PLUGIN_RETENTION: Final = timedelta(days=30)
"""Default time a refused, failed, rejected or yanked version's wheel is kept (default f);
the pipeline's callers pass ``MAPI_PLUGIN_RETENTION_DAYS``."""
RETAINED_STATES: Final[frozenset[PluginVersionState]] = frozenset(
    {"inspect_failed", "kit_failed", "rejected", "yanked"}
)
"""States whose wheel is purged after the retention (``purge_after`` is set on entry)."""
HASH_CHUNK: Final = 8 * 1024 * 1024
UPLOAD_WINDOW: Final = timedelta(days=1)

_WHEEL: Final = re.compile(
    r"^(?P<dist>[A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?)-(?P<version>[0-9][A-Za-z0-9.+!]*)"
    r"(?:-(?P<build>[0-9][A-Za-z0-9_.]*))?-(?P<tag>[^-]+-[^-]+-[^-]+)\.whl$"
)
_PEP503: Final = re.compile(r"[-_.]+")


# ================================================================================ state writer


def _scope(state: PluginVersionState) -> Literal["private", "tenant", "none"]:
    if state == "approved":
        return "tenant"
    if state in MOUNTABLE_STATES:
        return "private"
    return "none"


def set_state(
    version: m.PluginVersion,
    to: PluginVersionState,
    *,
    now: datetime,
    retention: timedelta = PLUGIN_RETENTION,
) -> None:
    """Move ``version`` to ``to`` (``PLUGIN_STATE_CONFLICT`` for an edge outside
    :data:`~mapi_spec.plugins.PLUGIN_TRANSITIONS`) and keep the derived columns in step:
    ``scope``; ``decided_at`` on approve/reject; ``yanked_at`` on yank (the caller sets
    ``yank_reason``, ``yanked_by`` and ``yank_note`` in the same flush); ``purged_at`` on
    purge; ``purge_after = now + retention`` on entry to a retained state. The caller holds
    the row ``FOR UPDATE``."""
    check_transition(cast(PluginVersionState, version.state), to)
    version.state = to
    version.scope = _scope(to)
    version.updated_at = now
    if to in ("approved", "rejected"):
        version.decided_at = now
    if to == "yanked":
        version.yanked_at = now
    if to == "purged":
        version.purged_at = now
    if to in RETAINED_STATES:
        version.purge_after = now + retention


# ===================================================================================== loading


async def load_plugin(session: AsyncSession, tenant_id: str, plugin_id: str) -> m.Plugin:
    row = await session.get(m.Plugin, plugin_id)
    if row is None or row.tenant_id != tenant_id:
        raise NotFoundError("plugin", plugin_id)
    return row


async def load_version(
    session: AsyncSession,
    tenant_id: str,
    plugin_id: str | None,
    version_id: str,
    *,
    lock: bool = False,
) -> m.PluginVersion:
    """A version of the tenant (of ``plugin_id`` when given; ``NOT_FOUND`` otherwise),
    locked ``FOR UPDATE`` with ``lock``."""
    if lock:  # FOR UPDATE, and the locked row's values even if this session loaded it
        row = await session.get(
            m.PluginVersion, version_id, with_for_update=True, populate_existing=True
        )
    else:
        row = await session.get(m.PluginVersion, version_id)
    if (
        row is None
        or row.tenant_id != tenant_id
        or (plugin_id is not None and row.plugin_id != plugin_id)
    ):
        raise NotFoundError("plugin version", version_id)
    return row


def version_visible(version: m.PluginVersion, auth: RequestAuth) -> bool:
    """Tenant admins and the uploader see every version; the rest of the tenant sees the
    approved ones and the withdrawn ones that were approved once (so a failing run can be
    explained). Private (unapproved) versions of other people stay hidden."""
    if is_tenant_admin(auth) or version.uploaded_by == auth.user_id:
        return True
    if version.state == "approved":
        return True
    return version.state == "yanked" and version.decided_at is not None


# ========================================================================================= DTOs


def summary_dto(
    version: m.PluginVersion, describe: m.PluginDescribe | None
) -> PluginVersionSummary:
    exports: dict[ExportKind, int] = {}
    if describe is not None:
        exports = PluginExports.model_validate(describe.exports).counts()
    return PluginVersionSummary.model_validate(
        {
            "id": version.id,
            "plugin_id": version.plugin_id,
            "version": version.version,
            "state": version.state,
            "scope": version.scope,
            "uploaded_by": version.uploaded_by,
            "uploaded_at": version.uploaded_at,
            "overlay_sha256": version.upload_sha256,
            "describe_digest": describe.describe_digest if describe is not None else None,
            "exports": exports,
        }
    )


def _urls(exports: PluginExports) -> list[str]:
    """Every About reference URL of the exports, once each, in order (for the reviewer)."""
    out: list[str] = []
    groups: Iterable[Sequence[Any]] = (
        exports.models,
        exports.steps,
        exports.plots,
        exports.workflow_nodes,
        exports.workflow_templates,
    )
    for group in groups:
        for entry in group:
            about = getattr(entry, "about", None)
            for ref in getattr(about, "references", None) or ():
                url = getattr(ref, "url", None)
                if url and url not in out:
                    out.append(url)
    return out


async def _decisions(session: AsyncSession, version: m.PluginVersion) -> list[PluginDecisionRecord]:
    rows = list(
        (
            await session.execute(
                select(m.PluginApproval)
                .where(
                    m.PluginApproval.tenant_id == version.tenant_id,
                    m.PluginApproval.version_id == version.id,
                )
                .order_by(m.PluginApproval.created_at, m.PluginApproval.id)
            )
        ).scalars()
    )
    names: dict[str, str | None] = {}
    if rows:
        members = (
            await session.execute(
                select(m.Membership.principal_id, m.Membership.display_name).where(
                    m.Membership.tenant_id == version.tenant_id,
                    m.Membership.principal_id.in_({r.approver_id for r in rows}),
                )
            )
        ).all()
        for principal, name in members:
            names.setdefault(principal, name)
    return [
        PluginDecisionRecord.model_validate(
            {
                "decision": r.decision,
                "approver_id": r.approver_id,
                "approver_name": names.get(r.approver_id),
                "note": r.note,
                "overlay_sha256": r.overlay_sha256,
                "describe_digest": r.describe_digest,
                "base_image_digest": r.base_image_digest,
                "decided_at": r.created_at,
            }
        )
        for r in rows
    ]


async def version_dto(
    session: AsyncSession, ctx: ServerContext, version: m.PluginVersion
) -> PluginVersion:
    """Everything the version page and the reviewer panel show: inspect facts first, the
    describe result for the current base image (else the newest), the decisions."""
    describe = await describe_for(session, version.id, ctx.catalogs.image_digest)
    summary = summary_dto(version, describe)
    exports = PluginExports.model_validate(describe.exports) if describe is not None else None
    return PluginVersion.model_validate(
        {
            **summary.model_dump(mode="json"),
            "inspection": (
                PluginInspection.model_validate(version.inspection)
                if version.inspection is not None
                else None
            ),
            "kit": PluginKitReport.model_validate(describe.kit) if describe is not None else None,
            "exports_detail": exports,
            "issues": [Issue.model_validate(i) for i in (describe.issues if describe else [])],
            "base_image_digest": describe.base_image_digest if describe is not None else None,
            "approvals": await _decisions(session, version),
            "problem": (
                Problem.model_validate(version.problem) if version.problem is not None else None
            ),
            "yank_reason": version.yank_reason,
            "yank_note": version.yank_note,
            "purge_after": version.purge_after,
            "urls": _urls(exports) if exports is not None else [],
        }
    )


def plugin_dto(plugin: m.Plugin, latest: PluginVersionSummary | None, versions: int) -> Plugin:
    return Plugin.model_validate(
        {
            "id": plugin.id,
            "namespace": plugin.namespace,
            "short_name": plugin.short_name,
            "title": plugin.title,
            "created_by": plugin.created_by,
            "created_at": plugin.created_at,
            "updated_at": plugin.updated_at,
            "latest_version": latest,
            "versions": versions,
        }
    )


async def _visible_versions_of(
    session: AsyncSession, auth: RequestAuth, plugin_ids: Sequence[str]
) -> dict[str, list[m.PluginVersion]]:
    """The versions of ``plugin_ids`` the caller may see, newest first per plugin."""
    if not plugin_ids:
        return {}
    rows = (
        await session.execute(
            select(m.PluginVersion)
            .where(m.PluginVersion.plugin_id.in_(plugin_ids))
            .order_by(m.PluginVersion.uploaded_at.desc(), m.PluginVersion.id.desc())
        )
    ).scalars()
    out: dict[str, list[m.PluginVersion]] = {pid: [] for pid in plugin_ids}
    for v in rows:
        if version_visible(v, auth):
            out[v.plugin_id].append(v)
    return out


async def plugin_view(
    session: AsyncSession, ctx: ServerContext, auth: RequestAuth, plugin: m.Plugin
) -> Plugin:
    versions = (await _visible_versions_of(session, auth, [plugin.id]))[plugin.id]
    latest = None
    if versions:
        newest = versions[0]
        latest = summary_dto(
            newest, await describe_for(session, newest.id, ctx.catalogs.image_digest)
        )
    return plugin_dto(plugin, latest, len(versions))


async def list_plugins(
    session: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    *,
    state: PluginVersionState | None,
    mine: bool,
    after: str | None,
    limit: int,
) -> tuple[list[Plugin], str | None]:
    """The tenant's plugins by namespace. A plugin is listed for tenant admins, its claimer
    and anyone who can see one of its versions; ``state`` keeps plugins with a visible
    version in that state, ``mine`` the caller's claims. Returns the page and the last
    namespace when there are more."""
    tenant_id = auth.tenant_id or ""
    stmt = select(m.Plugin).where(m.Plugin.tenant_id == tenant_id)
    if mine:
        stmt = stmt.where(m.Plugin.created_by == auth.user_id)
    if after is not None:
        stmt = stmt.where(m.Plugin.namespace > str(after))
    stmt = stmt.order_by(m.Plugin.namespace)
    out: list[Plugin] = []
    last: str | None = None
    batch = max(limit * 2, 50)
    offset = 0
    while len(out) <= limit:
        plugins = list((await session.execute(stmt.offset(offset).limit(batch))).scalars())
        if not plugins:
            break
        offset += len(plugins)
        visible = await _visible_versions_of(session, auth, [p.id for p in plugins])
        for p in plugins:
            versions = visible[p.id]
            if state is not None and not any(v.state == state for v in versions):
                continue
            if not versions and not (is_tenant_admin(auth) or p.created_by == auth.user_id):
                continue
            if len(out) == limit:
                return out, last
            latest = None
            if versions:
                latest = summary_dto(
                    versions[0],
                    await describe_for(session, versions[0].id, ctx.catalogs.image_digest),
                )
            out.append(plugin_dto(p, latest, len(versions)))
            last = p.namespace
        if len(plugins) < batch:
            break
    return out, None


async def list_versions(
    session: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    plugin: m.Plugin,
    *,
    after: Sequence[str] | None,
    limit: int,
) -> tuple[list[PluginVersionSummary], list[str] | None]:
    """The versions of a plugin the caller may see, newest first, and the cursor key of the
    last one when there are more (``[uploaded_at ISO, id]``)."""
    versions = (await _visible_versions_of(session, auth, [plugin.id]))[plugin.id]
    if after is not None:
        if len(after) != 2:
            raise MapiError("the cursor is invalid", code=ErrorCode.SCHEMA_INVALID,
                            pointer="/query/cursor")  # fmt: skip
        key = (str(after[0]), str(after[1]))
        versions = [v for v in versions if (v.uploaded_at.isoformat(), v.id) < key]
    page = versions[:limit]
    items = [
        summary_dto(v, await describe_for(session, v.id, ctx.catalogs.image_digest)) for v in page
    ]
    more = len(versions) > limit and bool(page)
    return items, ([page[-1].uploaded_at.isoformat(), page[-1].id] if more else None)


# ======================================================================================= claims


async def claim_plugin(
    session: AsyncSession, ctx: ServerContext, auth: RequestAuth, body: PluginClaim
) -> m.Plugin:
    """Claim ``short_name`` in the tenant: namespace ``t_<tenant code>_<short_name>`` (the
    tenant code is assigned on the first claim). ``PLUGIN_NAMESPACE_TAKEN`` when the name is
    already claimed in the tenant."""
    scope = ctx.scope
    await require_can_upload(session, ctx, auth, project_id=scope.project_id)
    by = caller_id(auth)
    settings = await ensure_settings(session, scope.tenant_id, by=by)
    taken = PluginNamespaceTaken(body.short_name)
    exists = (
        await session.execute(
            select(m.Plugin.id).where(
                m.Plugin.tenant_id == scope.tenant_id, m.Plugin.short_name == body.short_name
            )
        )
    ).first()
    if exists is not None:
        raise taken
    ts = now()
    plugin = m.Plugin(
        id=new_id(PLUGIN_ID_PREFIX.rstrip("_")),
        tenant_id=scope.tenant_id,
        namespace=upload_namespace(settings.tenant_code, body.short_name),
        short_name=body.short_name,
        title=body.title,
        created_by=by,
        created_at=ts,
        updated_at=ts,
    )
    try:
        async with session.begin_nested():
            session.add(plugin)
            await session.flush()
    except IntegrityError:  # a concurrent claim of the same name won
        raise taken from None
    await add_audit(
        session,
        [
            auth.audit(
                ctx,
                AuditAction.PLUGIN_CLAIM,
                ResourceKind.PLUGIN,
                resource_id=plugin.id,
                details={"plugin_id": plugin.id, "namespace": plugin.namespace},
            )
        ],
    )
    return plugin


class PluginNamespaceTaken(MapiError):
    default_code = ErrorCode.PLUGIN_NAMESPACE_TAKEN

    def __init__(self, short_name: str) -> None:
        super().__init__(
            f"The plugin name {short_name!r} is already used in this workspace. Choose another "
            "name, or upload a new version of that plugin.",
            pointer="/short_name",
        )


# ======================================================================================= uploads


def _refused(message: str, check: str, pointer: str = "") -> MapiError:
    """``PLUGIN_REFUSED`` at the upload stage (details in the issue's ``context``)."""
    return MapiError(
        message,
        code=ErrorCode.PLUGIN_REFUSED,
        issues=[
            Issue(
                code=ErrorCode.PLUGIN_REFUSED,
                pointer=pointer,
                message=message,
                context={"stage": "upload", "checks": [check]},
            )
        ],
    )


def parse_wheel_filename(filename: str, *, namespace: str) -> str:
    """The version in a wheel file name ``{dist}-{version}-py3-none-any.whl`` whose
    distribution normalizes (PEP 503) to the plugin's namespace; ``PLUGIN_REFUSED`` else."""
    if not filename.endswith(".whl"):
        raise _refused("Upload a wheel. `mapi plugin push` builds one for you.", "wheel_tag",
                       "/filename")  # fmt: skip
    match = _WHEEL.fullmatch(filename)
    if match is None:
        raise _refused(
            "The file name is not a wheel name ({distribution}-{version}-py3-none-any.whl).",
            "wheel_tag",
            "/filename",
        )
    if match["tag"] != "py3-none-any":
        raise _refused(
            f"The wheel tag is {match['tag']!r}. Uploads must be pure Python wheels "
            "(py3-none-any).",
            "wheel_tag",
            "/filename",
        )
    expected = upload_dist_name(namespace)
    if _PEP503.sub("-", match["dist"]).lower() != expected:
        raise _refused(
            f"The distribution name of the wheel must be {expected!r} (the plugin's namespace "
            f"{namespace!r}).",
            "dist_name",
            "/filename",
        )
    version = match["version"]
    if len(version) > 64:
        raise _refused("The version is longer than 64 characters.", "metadata_invalid",
                       "/filename")  # fmt: skip
    return version


async def _version_exists(session: AsyncSession, plugin_id: str, version: str) -> bool:
    row = (
        await session.execute(
            select(m.PluginVersion.id).where(
                m.PluginVersion.plugin_id == plugin_id, m.PluginVersion.version == version
            )
        )
    ).first()
    return row is not None


def _version_exists_error(namespace: str, version: str) -> MapiError:
    return MapiError(
        f"Version {version} of {namespace} was already uploaded. Raise the version number "
        "and build the wheel again.",
        code=ErrorCode.PLUGIN_VERSION_EXISTS,
    )


async def _rate_check(session: AsyncSession, ctx: ServerContext, tenant_id: str, user: str) -> None:
    """At most ``MAPI_PLUGIN_UPLOADS_PER_USER_PER_DAY`` tickets per person in 24 hours."""
    cap = ctx.settings.plugin_uploads_per_user_per_day
    since = now() - UPLOAD_WINDOW
    count, oldest = (
        await session.execute(
            select(func.count(), func.min(m.PluginUpload.created_at)).where(
                m.PluginUpload.tenant_id == tenant_id,
                m.PluginUpload.created_by == user,
                m.PluginUpload.created_at > since,
            )
        )
    ).one()
    if int(count) < cap:
        return
    retry = max(1, int(((oldest or now()) + UPLOAD_WINDOW - now()).total_seconds()) + 1)
    raise ProblemWithHeaders(
        f"You can upload at most {cap} files in 24 hours. Try again later.",
        code=ErrorCode.RATE_LIMITED,
        headers={"Retry-After": str(retry)},
    )


async def create_ticket(
    session: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    plugin_id: str,
    body: PluginUploadTicketRequest,
) -> PluginUploadTicket:
    """An upload ticket: a create-only POST policy for exactly one quarantine key, valid
    ``MAPI_PLUGIN_TICKET_TTL_S``. Refuses a file above the cap (``PAYLOAD_TOO_LARGE``), a
    file name that is not this plugin's pure wheel (``PLUGIN_REFUSED``), a version that
    exists (``PLUGIN_VERSION_EXISTS``) and the 21st ticket of a person in a day
    (``RATE_LIMITED``)."""
    scope = ctx.scope
    await require_can_upload(session, ctx, auth, project_id=scope.project_id)
    plugin = await load_plugin(session, scope.tenant_id, plugin_id)
    max_bytes = int(ctx.settings.plugin_upload_max_bytes)
    if body.size > max_bytes:
        raise MapiError(
            f"The wheel is {body.size} bytes; uploads are limited to {max_bytes} bytes.",
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            pointer="/size",
        )
    version = parse_wheel_filename(body.filename, namespace=plugin.namespace)
    if await _version_exists(session, plugin.id, version):
        raise _version_exists_error(plugin.namespace, version)
    user = caller_id(auth)
    await _rate_check(session, ctx, scope.tenant_id, user)
    upload_id = new_id(PLUGIN_UPLOAD_ID_PREFIX.rstrip("_"))
    ttl = timedelta(seconds=int(ctx.settings.plugin_ticket_ttl_s))
    key = plugin_quarantine_key(scope.tenant_id, upload_id)
    policy = await ctx.store.post_policy(
        ctx.buckets.quarantine,
        plugin_quarantine_prefix(scope.tenant_id, upload_id),
        expires_in=ttl,
        max_bytes=max_bytes,
        min_bytes=1,
        create_only=True,
    )
    # the policy is prefix-based: the form names the one key `:complete` reads
    policy = policy.model_copy(update={"fields": {**policy.fields, "key": key}})
    session.add(
        m.PluginUpload(
            id=upload_id,
            tenant_id=scope.tenant_id,
            plugin_id=plugin.id,
            project_id=scope.project_id,
            created_by=user,
            filename=body.filename,
            declared_size=body.size,
            object_key=key,
            state="pending",
            expires_at=policy.expires_at,
            created_at=now(),
        )
    )
    await session.flush()
    return PluginUploadTicket(
        upload_id=upload_id, post=policy, expires_at=policy.expires_at, max_bytes=max_bytes
    )


async def _sha256(ctx: ServerContext, bucket: str, key: str, generation: int, size: int) -> str:
    """SHA-256 of one object generation, streamed in ranges (bytes only, never parsed)."""
    digest = hashlib.sha256()
    start = 0
    while start < size:
        end = min(start + HASH_CHUNK, size) - 1
        chunk = await ctx.store.get(bucket, key, generation=generation, byte_range=(start, end))
        if not chunk:
            break
        digest.update(chunk)
        start += len(chunk)
    if start != size:
        raise MapiError(
            "The uploaded file changed while it was read. Upload it again.",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    return "sha256:" + digest.hexdigest()


async def complete_upload(
    session: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    plugin_id: str,
    upload_id: str,
    body: PluginUploadComplete,
) -> m.PluginVersion:
    """The wheel is in quarantine: check its size, hash it, record the version and start the
    ``plugin_inspect`` run. Only the person who got the ticket completes it.

    ``PRECONDITION_FAILED`` when the ticket expired or nothing was uploaded,
    ``PLUGIN_REFUSED`` (stage ``upload``) when the size is not the declared one or above the
    cap, ``PLUGIN_VERSION_EXISTS`` for a version uploaded meanwhile, and the admission errors
    of the inspect run (``UNTRUSTED_PLACEMENT_UNAVAILABLE`` when no executor may run uploaded
    code); nothing is recorded then and the same ticket can be completed again.
    """
    scope = ctx.scope
    require_plugins_available(ctx, auth)
    plugin = await load_plugin(session, scope.tenant_id, plugin_id)
    upload = await session.get(m.PluginUpload, upload_id, with_for_update=True)
    user = caller_id(auth)
    if (
        upload is None
        or upload.tenant_id != scope.tenant_id
        or upload.plugin_id != plugin.id
        or upload.created_by != user
    ):
        raise NotFoundError("upload", upload_id)
    await require_can_upload(session, ctx, auth, project_id=body.project)
    if upload.state != "pending":
        raise MapiError(
            "This upload was already completed. Ask for a new upload ticket.",
            code=ErrorCode.PRECONDITION_FAILED,
        )
    ts = now()
    if upload.expires_at <= ts:
        raise MapiError(
            "The upload ticket expired. Ask for a new one and upload the file again.",
            code=ErrorCode.PRECONDITION_FAILED,
        )
    bucket = ctx.buckets.quarantine
    try:
        meta = await ctx.store.stat(bucket, upload.object_key)
    except NotFound:
        raise MapiError(
            "The file is not uploaded yet. Post it with the upload ticket, then complete the "
            "upload.",
            code=ErrorCode.PRECONDITION_FAILED,
        ) from None
    max_bytes = int(ctx.settings.plugin_upload_max_bytes)
    if meta.size > max_bytes:
        raise _refused(f"The wheel is larger than {max_bytes} bytes.", "archive_size")
    if meta.size != upload.declared_size:
        raise _refused(
            f"The uploaded file has {meta.size} bytes, not the {upload.declared_size} bytes the "
            "ticket was asked for. Ask for a new ticket and upload the file again.",
            "archive_size",
        )
    version_str = parse_wheel_filename(upload.filename, namespace=plugin.namespace)
    if await _version_exists(session, plugin.id, version_str):
        raise _version_exists_error(plugin.namespace, version_str)
    sha = await _sha256(ctx, bucket, upload.object_key, meta.generation, meta.size)
    version = m.PluginVersion(
        id=new_id(PLUGIN_VERSION_ID_PREFIX.rstrip("_")),
        tenant_id=scope.tenant_id,
        plugin_id=plugin.id,
        version=version_str,
        state="uploaded",
        scope="none",
        project_id=body.project,
        uploaded_by=user,
        uploaded_at=ts,
        upload_sha256=sha,
        size_bytes=meta.size,
        quarantine_key=upload.object_key,
        quarantine_generation=meta.generation,
        requested_by=user if body.request_approval else None,
        requested_at=ts if body.request_approval else None,
        updated_at=ts,
    )
    try:
        async with session.begin_nested():
            session.add(version)
            await session.flush()
    except IntegrityError:  # a concurrent `:complete` of the same version won
        raise _version_exists_error(plugin.namespace, version_str) from None
    upload.state = "completed"
    plugin.updated_at = ts
    await session.flush()
    # moves the version to `inspecting` (the fallback below keeps that true either way)
    await pipeline.start_inspect(session, ctx, version, created_by=user)
    if version.state == "uploaded":
        set_state(version, "inspecting", now=ts)
    await session.flush()
    await add_audit(
        session,
        [
            auth.audit(
                ctx,
                AuditAction.PLUGIN_PUBLISH,
                ResourceKind.PLUGIN,
                resource_id=version.id,
                details={
                    "plugin_id": plugin.id,
                    "plugin_version_id": version.id,
                    "namespace": plugin.namespace,
                    "version": version.version,
                    "upload_sha256": sha,
                    "size_bytes": meta.size,
                    "project_id": body.project,
                    "request_approval": body.request_approval,
                },
            )
        ],
    )
    return version
