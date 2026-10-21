"""The automatic layout 1 → layout 2 upgrade of dashboards (v0.17, plan §10.1).

On release every layout 1 dashboard moves to layout 2 without an analyst pressing a button.
:func:`auto_upgrade_v1` is one idempotent pass; the worker runs it once per process start
(the dashboard loop's start-up, registered by WP8), guarded by a Postgres advisory lock so
concurrent workers upgrade each dashboard once, and controlled by the
``dashboard_auto_upgrade`` setting (``MAPI_DASHBOARD_AUTO_UPGRADE``, default true).

For each dashboard that is not archived:

* a **published** layout 1 revision is converted with
  :func:`~mapi_spec.dashboard_presets.upgrade_v1` against the source catalog of the SAME
  model version; with no blocking issue a new published revision holds the layout 2 spec
  (the same model version and endpoint pin, the same ``run_as`` publisher, ``created_by``
  :data:`~mapi_spec.audit.SYSTEM_ACTOR_ID`) and a baseline readout is queued (actor
  ``system``; the dashboard loop submits it as the publisher). The revision's pictures are
  queued as the publisher when they still may run;
* a layout 1 **draft** becomes a layout 2 draft the same way (its etag moves on);
* stored rows are never rewritten: old revisions and readouts stay layout 1 and History
  shows them with the layout 1 renderer;
* when the conversion has blocking issues (or the source catalog cannot be read) the
  dashboard stays on layout 1, ``dashboards.problem`` becomes
  :data:`~mapi_spec.dashboards.UPGRADE_NEEDS_REVIEW` and the issues are audited (codes and
  pointers); an editor finishes it with ``POST …:upgrade``. A transient error (storage, the
  database) changes nothing: the next start tries again.

Every upgrade is audited as ``DASHBOARD_UPGRADED`` by the platform (``actor_kind`` service).
:func:`review_issues` recomputes the blocking issues for the builder's banner.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from mapi_spec.api import new_id
from mapi_spec.audit import SYSTEM_ACTOR_ID, AuditAction, AuditOutcome, make_audit_event
from mapi_spec.dashboard_presets import upgrade_v1
from mapi_spec.dashboard_v2 import DashboardSpecV2, parse_dashboard_spec
from mapi_spec.dashboards import UPGRADE_NEEDS_REVIEW, DashboardSpec, dashboard_spec_hash
from mapi_spec.errors import ERROR_INFO, ErrorCode, Issue, MapiError, Severity
from mapi_spec.tenancy import PrincipalKind, ResourceKind
from pydantic import JsonValue, ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.audit import write_audit
from mapi_server.services.runs import now

__all__ = [
    "ADVISORY_LOCK_KEY",
    "Conversion",
    "UpgradeReport",
    "auto_upgrade_enabled",
    "auto_upgrade_v1",
    "convert",
    "review_issues",
    "run_auto_upgrade",
]

log = logging.getLogger(__name__)

ADVISORY_LOCK_KEY: Final = 0x6D617069_64736875  # "mapidshu": one pass at a time, cluster-wide
"""The transaction-scoped advisory lock of the pass (``pg_try_advisory_xact_lock``)."""
_MAX_AUDIT_ISSUES: Final = 20
_LOCK_TIMEOUT_MS: Final = 10_000

CatalogOf = Callable[[ServerContext, AsyncSession, m.ModelVersion], Awaitable[Any]]
"""Reads the source catalog of a model version (tests replace it)."""


@dataclass
class UpgradeReport:
    """What one pass did (dashboard ids). ``ran`` is false when the pass was turned off or
    another worker held the lock."""

    ran: bool = False
    published: list[str] = field(default_factory=list)
    drafts: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)
    retry: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Conversion:
    """One layout 1 spec converted: the layout 2 spec (``None`` when blocked), its notes and
    issues (every severity), and whether the failure was transient."""

    spec: DashboardSpecV2 | None
    notes: list[str]
    issues: list[Issue]
    transient: bool = False

    @property
    def blocked(self) -> bool:
        return self.spec is None or any(i.severity is Severity.ERROR for i in self.issues)


def auto_upgrade_enabled(ctx: ServerContext) -> bool:
    """The ``dashboard_auto_upgrade`` setting (true when the setting does not exist yet)."""
    return bool(getattr(ctx.settings, "dashboard_auto_upgrade", True))


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, MapiError):
        return ERROR_INFO[exc.code].status >= 500
    return not isinstance(exc, ValueError | ValidationError)


def _blocked(code: ErrorCode, pointer: str, message: str) -> Issue:
    return Issue(code=code, pointer=pointer, message=message[:2000])


class CatalogNotReady(RuntimeError):
    """The image catalog of the running release is not stored yet (its describe run is
    queued at start-up): a transient state, never a reason to pick another preset."""


async def _default_catalog(ctx: ServerContext, session: AsyncSession, mv: m.ModelVersion) -> Any:
    from mapi_server.services.dashboards.sources import source_catalog

    if ctx.catalogs is not None and await ctx.catalogs.get(session) is None:
        # without the plugin manifests the catalog has no family: the upgrade would pick
        # ``generic`` (or flag a media planner) for good. Wait for the catalog instead.
        raise CatalogNotReady("the image catalog of this release is not read yet")
    return await source_catalog(ctx, session, mv)


async def convert(
    ctx: ServerContext,
    session: AsyncSession,
    spec: DashboardSpec,
    mv: m.ModelVersion | None,
    *,
    catalog_of: CatalogOf | None = None,
) -> Conversion:
    """Convert ``spec`` on ``mv`` (``None``: the source no longer resolves) and check the
    result against the version (:func:`~mapi_server.services.dashboards.sources.
    check_version_v2`). Never raises for the dashboard's own problems; a transient error
    comes back as ``transient`` (and blocked)."""
    from mapi_server.services.dashboards.sources import check_version_v2

    if mv is None:
        issue = _blocked(ErrorCode.REF_UNRESOLVED, "/source", "the model version is gone")
        return Conversion(None, [], [issue])
    try:
        catalog = await (catalog_of or _default_catalog)(ctx, session, mv)
    except Exception as exc:
        if _transient(exc):
            log.warning("dashboard upgrade: the catalog of %s cannot be read now", mv.id,
                        exc_info=True)  # fmt: skip
            msg = "the model's sources cannot be read now"
            return Conversion(None, [], [_blocked(ErrorCode.INTERNAL_ERROR, "/source", msg)],
                              transient=True)  # fmt: skip
        code = exc.code if isinstance(exc, MapiError) else ErrorCode.SCHEMA_INVALID
        msg = f"the model's sources cannot be read: {exc}"
        return Conversion(None, [], [_blocked(code, "/source", msg)])
    try:
        new, notes = upgrade_v1(spec, catalog)
    except (ValueError, ValidationError) as exc:
        msg = f"the spec cannot be converted to layout 2: {str(exc)[:300]}"
        return Conversion(None, [], [_blocked(ErrorCode.SCHEMA_INVALID, "", msg)])
    try:
        issues = await check_version_v2(ctx, session, new, mv, catalog=catalog)
    except Exception as exc:
        if not _transient(exc):
            raise
        log.warning("dashboard upgrade: %s cannot be checked now", mv.id, exc_info=True)
        msg = "the model version cannot be checked now"
        return Conversion(None, notes, [_blocked(ErrorCode.INTERNAL_ERROR, "/source", msg)],
                          transient=True)  # fmt: skip
    return Conversion(new, notes, issues)


# ====================================================================================== pass


def _audit_event(
    row: m.Dashboard,
    outcome: AuditOutcome,
    details: dict[str, JsonValue],
    code: ErrorCode | None = None,
) -> Any:
    return make_audit_event(
        action=AuditAction.DASHBOARD_UPGRADED,
        resource_kind=ResourceKind.DASHBOARD,
        outcome=outcome,
        ts=now(),
        fingerprint_key=b"",
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        actor_kind=PrincipalKind.SERVICE,
        actor_id=SYSTEM_ACTOR_ID,
        resource_id=row.id,
        code=code,
        details=details,
    )


def _issue_refs(issues: list[Issue]) -> list[JsonValue]:
    """Issues as the audit keeps them: codes and pointers only."""
    blocking = [i for i in issues if i.severity is Severity.ERROR] or issues
    out: list[JsonValue] = []
    for i in blocking[:_MAX_AUDIT_ISSUES]:
        out.append({"code": i.code.value, "pointer": i.pointer or ""})
    return out


async def _last_upgrade_failed(session: AsyncSession, row: m.Dashboard) -> bool:
    """Whether the dashboard's latest upgrade audit is a refusal (it was flagged)."""
    outcome = (
        await session.execute(
            select(m.AuditEvent.outcome)
            .where(
                m.AuditEvent.action == AuditAction.DASHBOARD_UPGRADED.value,
                m.AuditEvent.resource_id == row.id,
            )
            .order_by(m.AuditEvent.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return outcome == AuditOutcome.ERROR.value


async def _flag(
    ctx: ServerContext, session: AsyncSession, row: m.Dashboard, issues: list[Issue]
) -> None:
    """Flag the part that stays on layout 1 (audited once, when the flag is set). Another
    problem the dashboard holds (a newer endpoint version, paused inputs) keeps its words:
    the flag is set only over no problem, and the builder still offers the upgrade of a
    layout 1 spec."""
    if row.problem == UPGRADE_NEEDS_REVIEW or (
        row.problem is not None and await _last_upgrade_failed(session, row)
    ):
        return
    if row.problem is None:
        row.problem = UPGRADE_NEEDS_REVIEW
    row.updated_at = now()
    await session.flush()
    first = next((i for i in issues if i.severity is Severity.ERROR), None)
    ev = _audit_event(
        row,
        AuditOutcome.ERROR,
        {"draft": row.status == "draft", "issues": _issue_refs(issues)},
        code=first.code if first is not None else None,
    )
    await write_audit(ctx.db, [ev], session=session)


async def _new_revision(
    session: AsyncSession, row: m.Dashboard, old: m.DashboardRevision, spec: DashboardSpecV2
) -> m.DashboardRevision:
    from mapi_server.services.dashboards.crud import _next_seq

    seq = await _next_seq(session, m.DashboardRevision, row.id)
    rev = m.DashboardRevision(
        id=new_id("dsr"),
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        dashboard_id=row.id,
        seq=seq,
        spec=spec.model_dump(mode="json"),
        spec_hash=dashboard_spec_hash(spec),
        model_version_id=old.model_version_id,
        model_ref=old.model_ref,
        endpoint_alias=old.endpoint_alias,
        endpoint_revision=old.endpoint_revision,
        created_by=SYSTEM_ACTOR_ID,
        created_at=now(),
    )
    session.add(rev)
    await session.flush()
    return rev


async def _pictures(
    ctx: ServerContext, session: AsyncSession, row: m.Dashboard, rev: m.DashboardRevision,
    spec: DashboardSpecV2,
) -> None:  # fmt: skip
    """Queue the new revision's pictures as the publisher (skipped when the publisher may
    not run any more: the readout then shows the pictures as unavailable).

    The pass holds the dashboard row lock here, so this never calls ``owner_context``: its
    pause write runs in a second transaction on the same row and would wait on this one
    for ever. The publisher's rights are read without writing; the dashboard loop pauses
    the dashboard when it next submits as the publisher."""
    from mapi_server.services.dashboards.assets import picture_panels, queue_pictures
    from mapi_server.services.dashboards.runas import _may_run, owner_auth

    if not picture_panels(spec):
        return
    auth = await owner_auth(ctx, row.tenant_id, row.project_id, row.run_as)
    if auth is None or not _may_run(auth):
        return
    await queue_pictures(ctx.for_request(auth), session, rev, spec)


async def _draft_binding(
    session: AsyncSession, row: m.Dashboard, spec: DashboardSpec
) -> m.ModelVersion | None:
    from mapi_server.services.dashboards.crud import binding_of

    issues: list[Issue] = []
    binding = await binding_of(session, row.tenant_id, row.project_id, spec.source, issues)
    return binding.model_version if binding is not None else None


async def _upgrade_one(
    ctx: ServerContext,
    session: AsyncSession,
    row: m.Dashboard,
    report: UpgradeReport,
    catalog_of: CatalogOf | None,
) -> None:
    from mapi_server.services.dashboards.crud import _queue_baseline, next_etag

    rev = (
        await session.get(m.DashboardRevision, row.published_revision_id)
        if row.published_revision_id
        else None
    )
    pub_v1 = rev if rev is not None and rev.dashboard_id == row.id else None
    pub_spec = parse_dashboard_spec(pub_v1.spec) if pub_v1 is not None else None
    if not isinstance(pub_spec, DashboardSpec):
        pub_v1, pub_spec = None, None
    draft = parse_dashboard_spec(row.draft_spec)
    draft_v1 = draft if isinstance(draft, DashboardSpec) else None
    if pub_v1 is None and draft_v1 is None:
        return
    published: Conversion | None = None
    drafted: Conversion | None = None
    if pub_v1 is not None and pub_spec is not None:
        mv = await session.get(m.ModelVersion, pub_v1.model_version_id)
        published = await convert(ctx, session, pub_spec, mv, catalog_of=catalog_of)
    if draft_v1 is not None:
        if published is not None and pub_spec is not None and draft_v1 == pub_spec:
            drafted = published
        else:
            mv = await _draft_binding(session, row, draft_v1)
            drafted = await convert(ctx, session, draft_v1, mv, catalog_of=catalog_of)
    done = [c for c in (published, drafted) if c is not None]
    if len(done) == 2 and done[0] is done[1]:  # the draft is the published spec
        done = done[:1]
    if any(c.transient for c in done):
        report.retry.append(row.id)
        return
    if published is not None and published.blocked:
        await _flag(ctx, session, row, list(published.issues))
        report.needs_review.append(row.id)
        return
    # the published revision converts: it moves on even when an unfinished layout 1 draft
    # does not (the draft stays as it is, flagged for the editor)
    draft_blocked = drafted is not None and drafted is not published and drafted.blocked
    if draft_blocked and published is None:
        assert drafted is not None
        await _flag(ctx, session, row, list(drafted.issues))
        report.needs_review.append(row.id)
        return
    ts = now()
    details: dict[str, JsonValue] = {"from_revision": None, "to_revision": None, "draft": False}
    new_rev: m.DashboardRevision | None = None
    if published is not None and published.spec is not None and pub_v1 is not None:
        new_rev = await _new_revision(session, row, pub_v1, published.spec)
        row.published_revision_id = new_rev.id
        await _queue_baseline(
            session, row, new_rev, None, actor_kind="system",
            title=f"New layout on {ts:%d/%m/%Y}",
        )  # fmt: skip
        details.update(from_revision=pub_v1.seq, to_revision=new_rev.seq,
                       preset=published.spec.preset, notes=list(published.notes[:20]))  # fmt: skip
        report.published.append(row.id)
    if drafted is not None and drafted.spec is not None and not draft_blocked:
        row.draft_spec = drafted.spec.model_dump(mode="json")
        row.draft_etag = next_etag(row.draft_etag)
        details["draft"] = True
        details.setdefault("preset", drafted.spec.preset)
        details.setdefault("notes", list(drafted.notes[:20]))
        report.drafts.append(row.id)
    if row.problem == UPGRADE_NEEDS_REVIEW:
        row.problem = None
    row.updated_at = ts
    await session.flush()
    if new_rev is not None and published is not None and published.spec is not None:
        await _pictures(ctx, session, row, new_rev, published.spec)
    await write_audit(ctx.db, [_audit_event(row, AuditOutcome.SUCCESS, details)], session=session)
    if draft_blocked:
        assert drafted is not None
        await _flag(ctx, session, row, list(drafted.issues))
        report.needs_review.append(row.id)


async def auto_upgrade_v1(
    ctx: ServerContext,
    *,
    enabled: bool | None = None,
    catalog_of: CatalogOf | None = None,
) -> UpgradeReport:
    """One idempotent upgrade pass over every tenant (plan §10.1). Returns at once when
    ``enabled`` (default: :func:`auto_upgrade_enabled`) is false or another worker holds
    :data:`ADVISORY_LOCK_KEY`. Each dashboard is upgraded in its own savepoint: one that
    fails unexpectedly is logged and retried on the next start, the others go on."""
    report = UpgradeReport()
    if not (auto_upgrade_enabled(ctx) if enabled is None else enabled):
        return report
    async with ctx.db.session() as s:
        got = (
            await s.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        ).scalar_one()
        if not got:
            log.info("dashboard upgrade: another worker runs the pass")
            return report
        # a lock the pass waits on (a dashboard row an API request holds) never stalls the
        # worker's start: the dashboard fails this pass and is retried
        await s.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT_MS}ms'"))
        report.ran = True
        ids = [
            str(i)
            for i in (
                await s.execute(
                    select(m.Dashboard.id)
                    .where(m.Dashboard.status != "archived")
                    .order_by(m.Dashboard.id)
                )
            ).scalars()
        ]
        for dashboard_id in ids:
            try:
                async with s.begin_nested():
                    row = (
                        await s.execute(
                            select(m.Dashboard).where(m.Dashboard.id == dashboard_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()  # fmt: skip
                    if row is None or row.status == "archived":
                        continue
                    await _upgrade_one(ctx, s, row, report, catalog_of)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dashboard %s: the layout 2 upgrade failed", dashboard_id)
                report.retry.append(dashboard_id)
    if report.published or report.drafts or report.needs_review:
        log.info(
            "dashboard upgrade: %d published, %d drafts, %d need review, %d retry later",
            len(report.published), len(report.drafts), len(report.needs_review),
            len(report.retry),
        )  # fmt: skip
    return report


async def run_auto_upgrade(ctx: ServerContext) -> UpgradeReport:
    """The start-up hook (WP8 registers it in the worker): :func:`auto_upgrade_v1` that
    never raises (a failed pass is logged; the next start tries again)."""
    try:
        return await auto_upgrade_v1(ctx)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("the dashboard layout 2 upgrade pass failed")
        return UpgradeReport()


async def review_issues(
    ctx: ServerContext,
    session: AsyncSession,
    row: m.Dashboard,
    *,
    catalog_of: CatalogOf | None = None,
) -> list[Issue]:
    """What stops the upgrade of a dashboard flagged ``upgrade_needs_review`` now (the
    builder's banner): the blocking issues of its layout 1 revision and of its layout 1
    draft (each part converts on its own: a published conversion with only notes never
    hides what stops the draft)."""
    if row.problem != UPGRADE_NEEDS_REVIEW:
        return []
    out: list[Issue] = []
    rev = (
        await session.get(m.DashboardRevision, row.published_revision_id)
        if row.published_revision_id
        else None
    )
    if rev is not None:
        spec = parse_dashboard_spec(rev.spec)
        if isinstance(spec, DashboardSpec):
            mv = await session.get(m.ModelVersion, rev.model_version_id)
            out += (await convert(ctx, session, spec, mv, catalog_of=catalog_of)).issues
    draft = parse_dashboard_spec(row.draft_spec)
    blocking = [i for i in out if i.severity is Severity.ERROR]
    published = parse_dashboard_spec(rev.spec) if rev is not None else None
    if isinstance(draft, DashboardSpec) and draft != published:
        mv = await _draft_binding(session, row, draft)
        found = (await convert(ctx, session, draft, mv, catalog_of=catalog_of)).issues
        seen = {(i.code, i.pointer, i.message) for i in blocking}
        blocking += [
            i for i in found
            if i.severity is Severity.ERROR and (i.code, i.pointer, i.message) not in seen
        ]  # fmt: skip
    return blocking[:50]
