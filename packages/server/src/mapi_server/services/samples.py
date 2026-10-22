"""Sample projects: a real small fit per tenant, one click away (v0.18 D20).

A sample (:data:`mapi_spec.samples.SAMPLES`) is installed once per tenant, in a project of
its own (``sample``; ``sample-2`` … ``sample-9`` when that id is taken), and runs down the
same path a user's upload takes: our synthetic table is stored, ingested, turned into the
Guided draft's spec and fitted with a short profile. Nothing is precomputed: the model
version comes from a finalized, signed, sandboxed run like any other.

**Life cycle** (one ``tenant_samples`` row per tenant and sample, :data:`SAMPLE_TRANSITIONS`):

* :func:`install` (the request; an editor somewhere in the tenant, since it spends their
  compute) creates the project (``projects.kind = 'sample'``), gives the installer a
  ``runner`` grant there when they need one and stores the row ``provisioning``. Two
  installs at once make one row: the second caller joins.
* The sample loop (:class:`~mapi_server.services.samples_loop.SampleLoop`) calls
  :func:`advance`, which acts **as the installer** (a synthetic request of theirs, built from
  their grants now, like a dashboard's publisher; :func:`owner_auth`): ``provisioning``
  stores the table content-addressed under ``t/{tenant}/samples/`` in the quarantine
  bucket, creates the dataset and its version and submits the ingest run (``ingesting``);
  once the version is ready it stores the spec and submits the fit with the sample's profile
  and labels (``fitting``); once the fit finished it records the model version and seeds
  the project's library with the sample's three documents (``ready``). Admission, quotas
  and attribution apply as for the installer's own request.
* Anything refused or failed on the way (a quota, an ingest or fit failure, an installer
  who lost the right to start runs) makes the sample ``failed`` with a plain
  :class:`~mapi_spec.errors.Problem`; :func:`retry` (the installer or a tenant admin, at most
  ``MAPI_SAMPLE_MAX_ATTEMPTS`` attempts in all) starts it again from ``provisioning``, reusing
  what the failed attempt left (the dataset version, the spec).
* :func:`install` on an installed sample **joins** it: a project membership of the
  caller's best role in the tenant, capped at ``runner`` (audited as ``member.add`` with the
  sample, and ``sample.join``).
* :func:`retire` (tenant admins) hides the sample for good: the row becomes ``retired``,
  the project ``sample_retired`` and every grant the sample made is removed. Nothing is
  deleted.

``MAPI_SAMPLES_ENABLED=false`` hides samples platform-wide: the list is empty and every
action is ``SAMPLES_DISABLED`` (404); the loop leaves rows alone.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Final

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction, AuditEvent
from mapi_spec.errors import ErrorCode, MapiError, Problem
from mapi_spec.executor import RunKind
from mapi_spec.loader import dump_yaml
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from mapi_spec.samples import (
    SAMPLE_PROJECT_ID,
    SAMPLES,
    SampleDef,
    SampleInfo,
    SampleState,
    can_transition,
    sample_draft,
    sample_project_ids,
)
from mapi_spec.tenancy import Action, ProjectRole, ResourceKind, effective_role
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError as DBIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import Credential, RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, NotFoundError
from mapi_server.services.audit import write_audit
from mapi_server.services.dashboards.runas import owner_auth
from mapi_server.services.datasets import admit_upload, create_dataset_row, create_version
from mapi_server.services.endpoints import put_content_addressed
from mapi_server.services.library import create_item_system
from mapi_server.services.onboarding import get_state
from mapi_server.services.quotas import global_policy, tenant_policy
from mapi_server.services.runs import Scope, now
from mapi_server.services.specs import create_spec_row, validate_document
from mapi_server.services.submit import submit_spec_run

__all__ = [
    "LOOP_STATES",
    "SAMPLE_GRANT_BY",
    "SAMPLE_OBJECT_PREFIX",
    "advance",
    "install",
    "list_samples",
    "retire",
    "retry",
    "sample_bytes",
]

log = logging.getLogger(__name__)

SAMPLE_GRANT_BY: Final = "sample"
"""``memberships.created_by`` of the grants a sample makes (the installer's and the joins);
:func:`retire` removes exactly these."""
SAMPLE_OBJECT_PREFIX: Final = "samples"
"""The sample tables live at ``t/{tenant}/samples/{sha256}.csv`` in the quarantine bucket."""
LOOP_STATES: Final[tuple[SampleState, ...]] = ("provisioning", "ingesting", "fitting")
"""The states :func:`advance` moves on (``ready``, ``failed`` and ``retired`` wait for a
person)."""
JOIN_ROLE_CAP: Final = ProjectRole.RUNNER
"""The highest role a sample grant gives (the installer gets exactly this one)."""
INSTALL_ROLE: Final = ProjectRole.EDITOR
"""Installing spends compute: the caller needs this role in some project of the tenant."""

_PEOPLE: Final = (Credential.SESSION, Credential.LOCAL)
_PROGRESS: Final[dict[str, float]] = {
    "provisioning": 0.05,
    "ingesting": 0.15,
    "ready": 1.0,
}
_FIT_PROGRESS: Final[dict[RunState, float]] = {
    RunState.CREATED: 0.3,
    RunState.QUEUED: 0.3,
    RunState.DISPATCHING: 0.35,
    RunState.PROVISIONING: 0.4,
    RunState.RUNNING: 0.6,
    RunState.STALLED: 0.6,
    RunState.FINALIZING: 0.9,
}
_MAX_STEPS: Final = 3
"""Steps :func:`advance` may take in one call (a retry whose dataset version is ready goes
from ``provisioning`` to ``fitting`` at once)."""


# ===================================================================================== caller


@dataclass(slots=True)
class _Caller:
    """A signed-in member of the tenant acting on its samples."""

    auth: RequestAuth
    tenant_id: str
    user_id: str
    best_role: ProjectRole
    tenant_admin: bool
    granted: dict[str, ProjectRole] = field(default_factory=dict)
    """Grants this request made (the request's memberships were read before them)."""

    def role_in(self, project_id: str | None) -> ProjectRole | None:
        if project_id is None:
            return None
        held = effective_role(self.auth.memberships, self.user_id, self.tenant_id, project_id)
        new = self.granted.get(project_id)
        if new is not None and (held is None or new > held):
            return new
        return held

    def sees(self, project_id: str | None) -> bool:
        role = self.role_in(project_id)
        return role is not None and role >= ProjectRole.VIEWER


def _caller(auth: RequestAuth, tenant_id: str) -> _Caller:
    """The caller, after checking they are a signed-in person with at least the viewer role
    somewhere in the tenant (a sample grant is a person's; API tokens cannot hold one)."""
    if auth.credential not in _PEOPLE or auth.token is not None:
        raise MapiError(
            "sample projects belong to signed-in people; API tokens cannot install or join them",
            code=ErrorCode.FORBIDDEN,
        )
    roles = [mm.role for mm in auth.memberships if mm.tenant_id == tenant_id]
    tenant_role = auth.context.tenant_role if auth.context is not None else None
    if tenant_role is not None and auth.context is not None and auth.context.tenant_id == tenant_id:
        roles.append(tenant_role)
    best = max(roles, default=None)
    user_id = auth.user_id
    if user_id is None or best is None or best < ProjectRole.VIEWER:
        raise MapiError(
            "you need at least the viewer role in this tenant", code=ErrorCode.FORBIDDEN
        )
    admin = tenant_role is ProjectRole.ADMIN or any(
        mm.tenant_id == tenant_id and mm.project_id is None and mm.role is ProjectRole.ADMIN
        for mm in auth.memberships
    )
    return _Caller(auth, tenant_id, user_id, best, admin)


def _enabled(ctx: ServerContext) -> None:
    if not ctx.settings.samples_enabled:
        raise MapiError(
            "sample projects are turned off on this platform", code=ErrorCode.SAMPLES_DISABLED
        )


def _definition(sample_id: str) -> SampleDef:
    sample = SAMPLES.get(sample_id)
    if sample is None:
        raise NotFoundError("sample", sample_id)
    return sample


# ======================================================================================= rows


async def _row(
    session: AsyncSession, tenant_id: str, sample_id: str, *, lock: bool = False
) -> m.TenantSample | None:
    stmt = select(m.TenantSample).where(
        m.TenantSample.tenant_id == tenant_id, m.TenantSample.sample_id == sample_id
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


def _move(row: m.TenantSample, state: SampleState) -> None:
    """Set ``row.state`` (only along :data:`SAMPLE_TRANSITIONS`)."""
    old: SampleState = row.state  # type: ignore[assignment]
    if not can_transition(old, state):
        raise ValueError(f"a sample cannot go from {old} to {state}")
    row.state = state
    row.updated_at = now()


def _fail(row: m.TenantSample, problem: Problem) -> None:
    _move(row, "failed")
    row.problem = problem.model_dump(mode="json", exclude_none=True)


def _problem(row: m.TenantSample) -> Problem | None:
    if not row.problem:
        return None
    try:
        return Problem.model_validate(row.problem)
    except ValidationError:  # pragma: no cover - only this module writes it
        return Problem.from_code(ErrorCode.INTERNAL_ERROR, "the sample failed")


async def _progress(session: AsyncSession, row: m.TenantSample) -> float | None:
    if row.state in _PROGRESS:
        return _PROGRESS[row.state]
    if row.state == "fitting" and row.run_id is not None:
        run = await session.get(m.Run, row.run_id)
        if run is not None:
            return _FIT_PROGRESS.get(RunState(run.state), 0.95)
    return None


async def _info(
    ctx: ServerContext,
    session: AsyncSession,
    sample: SampleDef,
    row: m.TenantSample | None,
    caller: _Caller,
    *,
    hidden: bool = False,
) -> SampleInfo:
    """What ``caller`` sees of ``sample``: the ids of the sample's project and what it
    holds only when they are a member of it."""
    info = SampleInfo(
        id=sample.id,
        title=sample.title,
        summary=sample.summary,
        minutes=sample.minutes,
        hidden=hidden,
    )
    if row is None:
        return info.model_copy(update={"can_install": caller.best_role >= INSTALL_ROLE})
    member = caller.sees(row.project_id)
    retired = row.state == "retired"
    update: dict[str, object] = {
        "state": row.state,
        "progress": await _progress(session, row),
        "problem": _problem(row),
        "attempts": row.attempts,
        "can_install": not retired and not member,
        "can_retry": (
            row.state == "failed"
            and row.attempts < ctx.settings.sample_max_attempts
            and (caller.tenant_admin or row.created_by == caller.user_id)
        ),
        "can_retire": caller.tenant_admin and not retired,
    }
    if member:
        update |= {
            "project_id": row.project_id,
            "spec_id": row.spec_id,
            "run_id": row.run_id,
            "model_id": row.model_id,
            "model_version": row.model_version,
        }
    return info.model_copy(update=update)


async def _hidden(session: AsyncSession, tenant_id: str, user_id: str) -> frozenset[str]:
    state = await get_state(session, tenant_id, user_id)
    return frozenset(state.hidden_samples)


# =================================================================================== requests


async def list_samples(
    ctx: ServerContext, session: AsyncSession, auth: RequestAuth, tenant_id: str
) -> list[SampleInfo]:
    """Every sample as the caller sees it (``[]`` when samples are turned off)."""
    caller = _caller(auth, tenant_id)
    if not ctx.settings.samples_enabled:
        return []
    hidden = await _hidden(session, tenant_id, caller.user_id)
    out: list[SampleInfo] = []
    for sample in SAMPLES.values():
        row = await _row(session, tenant_id, sample.id)
        out.append(await _info(ctx, session, sample, row, caller, hidden=sample.id in hidden))
    return out


async def install(
    ctx: ServerContext, session: AsyncSession, auth: RequestAuth, tenant_id: str, sample_id: str
) -> tuple[SampleInfo, bool]:
    """Install ``sample_id`` in the tenant, or join the one installed. Returns the sample as
    the caller now sees it and whether this call installed it (the loop does the rest)."""
    _enabled(ctx)
    sample = _definition(sample_id)
    caller = _caller(auth, tenant_id)
    row = await _row(session, tenant_id, sample_id, lock=True)
    if row is None:
        if caller.best_role < INSTALL_ROLE:
            raise MapiError(
                "installing the sample starts a model fit: you need the editor role in a "
                "project of this tenant. Ask an editor or an admin to install it, then join it",
                code=ErrorCode.FORBIDDEN,
            )
        created = await _insert_row(session, tenant_id, sample_id, caller.user_id)
        if created is not None:
            await _provision_project(ctx, session, sample, created, caller)
            return await _info(ctx, session, sample, created, caller), True
        # another install won the race: join it
        row = await _row(session, tenant_id, sample_id, lock=True)
        assert row is not None
    if row.state == "retired":
        raise Conflict(f"the sample {sample_id!r} was retired by a tenant admin")
    if not caller.sees(row.project_id):
        await _join(ctx, session, sample, row, caller)
    return await _info(ctx, session, sample, row, caller), False


async def _insert_row(
    session: AsyncSession, tenant_id: str, sample_id: str, user_id: str
) -> m.TenantSample | None:
    """The new ``provisioning`` row, or ``None`` when a concurrent install made it first (the
    insert waits for that transaction, then does nothing)."""
    ts = now()
    inserted = (
        await session.execute(
            insert(m.TenantSample)
            .values(
                tenant_id=tenant_id,
                sample_id=sample_id,
                state="provisioning",
                attempts=1,
                created_by=user_id,
                created_at=ts,
                updated_at=ts,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "sample_id"])
            .returning(m.TenantSample.sample_id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return None
    return await _row(session, tenant_id, sample_id, lock=True)


async def _provision_project(
    ctx: ServerContext,
    session: AsyncSession,
    sample: SampleDef,
    row: m.TenantSample,
    caller: _Caller,
) -> None:
    """The sample's project (the first free id of :func:`sample_project_ids`), the
    installer's grant and the audit rows."""
    candidates = sample_project_ids()
    taken = set(
        (
            await session.execute(
                select(m.Project.id).where(
                    m.Project.tenant_id == row.tenant_id, m.Project.id.in_(candidates)
                )
            )
        ).scalars()
    )
    project_id = next((p for p in candidates if p not in taken), None)
    if project_id is None:
        raise Conflict(
            "every project id the sample can use is taken "
            f"({candidates[0]} … {candidates[-1]}); rename one of those projects first"
        )
    session.add(
        m.Project(
            tenant_id=row.tenant_id,
            id=project_id,
            project_id=project_id,
            name=sample.project_name,
            description=sample.summary,
            kind="sample",
            created_at=now(),
        )
    )
    await session.flush()
    row.project_id = project_id
    events = [
        caller.auth.audit(
            ctx,
            AuditAction.SAMPLE_INSTALL,
            ResourceKind.PROJECT,
            resource_id=project_id,
            tenant_id=row.tenant_id,
            project_id=project_id,
            details={"sample": sample.id, "attempt": row.attempts},
        )
    ]
    events += await _grant(ctx, session, sample, row, caller, JOIN_ROLE_CAP)
    await session.flush()
    await write_audit(ctx.db, events, session=session)


async def _grant(
    ctx: ServerContext,
    session: AsyncSession,
    sample: SampleDef,
    row: m.TenantSample,
    caller: _Caller,
    role: ProjectRole,
) -> list[AuditEvent]:
    """Give the caller ``role`` in the sample's project unless they hold it there already
    (a tenant-wide grant counts). Returns the ``member.add`` audit row, if any."""
    assert row.project_id is not None
    held = caller.role_in(row.project_id)
    if held is not None and held >= role:
        return []
    principal = caller.auth.principal
    grant = m.Membership(
        id=new_id("mem"),
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        principal_id=caller.user_id,
        principal_kind="user",
        role=role.value,
        capabilities=[],
        email=getattr(principal, "email", None),
        display_name=getattr(principal, "name", None),
        created_at=now(),
        created_by=SAMPLE_GRANT_BY,
    )
    try:
        async with session.begin_nested():
            session.add(grant)
    except DBIntegrityError:  # a project grant exists already (e.g. two joins at once)
        return []
    caller.granted[row.project_id] = role
    return [
        caller.auth.audit(
            ctx,
            AuditAction.MEMBER_ADD,
            ResourceKind.MEMBER,
            resource_id=caller.user_id,
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            details={"role": role.value, "project_id": row.project_id, "sample": sample.id},
        )
    ]


async def _join(
    ctx: ServerContext,
    session: AsyncSession,
    sample: SampleDef,
    row: m.TenantSample,
    caller: _Caller,
) -> None:
    role = min(caller.best_role, JOIN_ROLE_CAP)
    events = await _grant(ctx, session, sample, row, caller, role)
    if not events:
        return
    events.append(
        caller.auth.audit(
            ctx,
            AuditAction.SAMPLE_JOIN,
            ResourceKind.PROJECT,
            resource_id=row.project_id,
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            details={"sample": sample.id, "role": role.value},
        )
    )
    await write_audit(ctx.db, events, session=session)


async def retry(
    ctx: ServerContext, session: AsyncSession, auth: RequestAuth, tenant_id: str, sample_id: str
) -> SampleInfo:
    """Start a ``failed`` sample again (its installer or a tenant admin). The caller becomes
    the installer: the next runs are theirs."""
    _enabled(ctx)
    sample = _definition(sample_id)
    caller = _caller(auth, tenant_id)
    row = await _row(session, tenant_id, sample_id, lock=True)
    if row is None:
        raise NotFoundError("installed sample", sample_id)
    if not (caller.tenant_admin or row.created_by == caller.user_id):
        raise MapiError(
            "only the person who installed the sample or a tenant admin can retry it",
            code=ErrorCode.FORBIDDEN,
        )
    if row.state != "failed":
        raise Conflict(f"the sample is {row.state}; only a failed sample can be retried")
    limit = ctx.settings.sample_max_attempts
    if row.attempts >= limit:
        raise Conflict(
            f"the sample failed {row.attempts} times, the most it may try ({limit}); a tenant "
            "admin can retire it"
        )
    if caller.best_role < INSTALL_ROLE:
        raise MapiError(
            "retrying the sample starts a model fit: you need the editor role in a project of "
            "this tenant",
            code=ErrorCode.FORBIDDEN,
        )
    _move(row, "provisioning")
    row.attempts += 1
    row.problem = None
    row.run_id = None
    row.created_by = caller.user_id
    events = await _grant(ctx, session, sample, row, caller, JOIN_ROLE_CAP)
    events.append(
        caller.auth.audit(
            ctx,
            AuditAction.SAMPLE_RETRY,
            ResourceKind.PROJECT,
            resource_id=row.project_id,
            tenant_id=tenant_id,
            project_id=row.project_id,
            details={"sample": sample.id, "attempt": row.attempts},
        )
    )
    await session.flush()
    await write_audit(ctx.db, events, session=session)
    return await _info(ctx, session, sample, row, caller)


async def retire(
    ctx: ServerContext, session: AsyncSession, auth: RequestAuth, tenant_id: str, sample_id: str
) -> SampleInfo:
    """Retire an installed sample (tenant admins): it stays where it is, hidden, and every
    grant the sample made is removed. Retiring a retired sample changes nothing."""
    _enabled(ctx)
    sample = _definition(sample_id)
    caller = _caller(auth, tenant_id)
    if not caller.tenant_admin:
        raise MapiError("only a tenant admin can retire a sample", code=ErrorCode.FORBIDDEN)
    row = await _row(session, tenant_id, sample_id, lock=True)
    if row is None:
        raise NotFoundError("installed sample", sample_id)
    if row.state == "retired":
        return await _info(ctx, session, sample, row, caller)
    _move(row, "retired")
    events: list[AuditEvent] = []
    removed: list[str] = []
    if row.project_id is not None:
        project = await session.get(m.Project, (tenant_id, row.project_id), with_for_update=True)
        if project is not None:
            project.kind = "sample_retired"
        removed = sorted(
            str(p)
            for p in (
                await session.execute(
                    delete(m.Membership)
                    .where(
                        m.Membership.tenant_id == tenant_id,
                        m.Membership.project_id == row.project_id,
                        m.Membership.created_by == SAMPLE_GRANT_BY,
                    )
                    .returning(m.Membership.principal_id)
                    .execution_options(synchronize_session=False)
                )
            ).scalars()
        )
        events += [
            auth.audit(
                ctx,
                AuditAction.MEMBER_REMOVE,
                ResourceKind.MEMBER,
                resource_id=principal_id,
                tenant_id=tenant_id,
                project_id=row.project_id,
                details={"project_id": row.project_id, "sample": sample.id},
            )
            for principal_id in removed
        ]
    events.append(
        auth.audit(
            ctx,
            AuditAction.SAMPLE_RETIRE,
            ResourceKind.PROJECT,
            resource_id=row.project_id,
            tenant_id=tenant_id,
            project_id=row.project_id,
            details={"sample": sample.id, "grants_removed": len(removed)},
        )
    )
    await session.flush()
    await write_audit(ctx.db, events, session=session)
    return await _info(ctx, session, sample, row, caller)


# ======================================================================================= loop


@cache
def sample_bytes(sample_id: str) -> bytes:
    """The table of ``sample_id`` as shipped with the server, checked against its
    ``data_sha256`` (``INTERNAL_ERROR`` when the installed file was changed)."""
    sample = _definition(sample_id)
    data = resources.files("mapi_server").joinpath("samples", "data", sample.data_file).read_bytes()
    if "sha256:" + hashlib.sha256(data).hexdigest() != sample.data_sha256:
        raise MapiError(
            f"the table of the {sample_id} sample does not match its checksum; reinstall the "
            "server package",
            code=ErrorCode.INTERNAL_ERROR,
        )
    return data


async def advance(ctx: ServerContext, session: AsyncSession, row: m.TenantSample) -> bool:
    """Move one sample on as far as it can go now (the loop calls it with the row locked).
    Returns whether anything changed. A refusal or a failure on the way makes the sample
    ``failed`` with a plain problem; the work of the failed step is rolled back."""
    sample = SAMPLES.get(row.sample_id)
    if sample is None:  # a sample a later release removed: leave its row alone
        return False
    changed = False
    for _ in range(_MAX_STEPS):
        if row.state not in LOOP_STATES:
            break
        try:
            async with session.begin_nested():
                moved = await _step(ctx, session, sample, row)
        except MapiError as exc:
            _fail(row, _plain(exc))
            moved = True
        changed |= moved
        if not moved:
            break
    if changed:
        await session.flush()
    return changed


def _plain(exc: MapiError) -> Problem:
    problem = exc.to_problem()
    return problem.model_copy(update={"trace_id": None, "instance": None})


async def _installer(ctx: ServerContext, row: m.TenantSample) -> ServerContext:
    """A context acting as the installer in the sample's project, from their grants now
    (``FORBIDDEN`` when they can no longer start runs there: they left, or lost the role)."""
    assert row.project_id is not None
    auth = await owner_auth(ctx, row.tenant_id, row.project_id, row.created_by)
    if auth is None or not (
        auth.allows(Action.CREATE, ResourceKind.RUN) and auth.allows(Action.RUN, ResourceKind.MODEL)
    ):
        raise MapiError(
            "the person who installed the sample can no longer start runs in its project; "
            "a tenant admin can retry the sample",
            code=ErrorCode.FORBIDDEN,
        )
    return ctx.for_request(auth)


async def _step(
    ctx: ServerContext, session: AsyncSession, sample: SampleDef, row: m.TenantSample
) -> bool:
    if row.state == "provisioning":
        return await _provision_data(ctx, session, sample, row)
    if row.state == "ingesting":
        return await _after_ingest(ctx, session, sample, row)
    if row.state == "fitting":
        return await _after_fit(ctx, session, sample, row)
    return False


async def _dataset(session: AsyncSession, scope: Scope, sample: SampleDef) -> m.Dataset:
    """The sample's dataset in its project (created on the first attempt), row-locked."""
    found = (
        await session.execute(
            select(m.Dataset)
            .where(
                m.Dataset.tenant_id == scope.tenant_id,
                m.Dataset.project_id == scope.project_id,
                m.Dataset.name == sample.dataset,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if found is not None:
        return found
    return await create_dataset_row(
        session,
        scope,
        name=sample.dataset,
        description=sample.summary,
        labels=sample.labels,
    )


async def _provision_data(
    ctx: ServerContext, session: AsyncSession, sample: SampleDef, row: m.TenantSample
) -> bool:
    """``provisioning → ingesting``: the table stored and its ingest run submitted, or the
    ready version a failed attempt left reused."""
    octx = await _installer(ctx, row)
    ds = await _dataset(session, octx.scope, sample)
    if ds.latest_version is not None:  # an earlier attempt's version is ready: reuse it
        _move(row, "ingesting")
        row.dataset_id, row.dataset_version = ds.id, ds.latest_version
        return True
    data = sample_bytes(sample.id)
    await admit_upload(octx, session, row.tenant_id, len(data))
    sha = hashlib.sha256(data).hexdigest()
    name = f"t/{row.tenant_id}/{SAMPLE_OBJECT_PREFIX}/{sha}.csv"
    meta = await put_content_addressed(
        octx, ctx.buckets.quarantine, name, data, content_type="text/csv"
    )
    ts = now()
    upload = m.Upload(
        id=new_id("upl"),
        tenant_id=ds.tenant_id,
        project_id=ds.project_id,
        dataset_id=ds.id,
        filename=sample.data_file,
        content_type="text/csv",
        size_bytes=len(data),
        status="uploaded",
        bucket=meta.bucket,
        object_name=meta.name,
        upload={},
        expires_at=ts,
        created_at=ts,
    )
    session.add(upload)
    await session.flush()
    draft, _ = sample_draft(sample, project=ds.project_id)
    dv = await create_version(
        session,
        octx,
        ds,
        [(upload, meta.generation)],
        mapping=draft.mapping.model_dump(mode="json"),
        message=f"The {sample.title.lower()} sample's data",
    )
    _move(row, "ingesting")
    row.dataset_id, row.dataset_version = ds.id, dv.version
    return True


async def _after_ingest(
    ctx: ServerContext, session: AsyncSession, sample: SampleDef, row: m.TenantSample
) -> bool:
    """``ingesting → fitting`` once the version is ready: the spec stored (or reused) and
    the fit submitted as the installer."""
    assert row.dataset_id is not None and row.dataset_version is not None
    dv = await session.get(m.DatasetVersion, (row.dataset_id, row.dataset_version))
    if dv is None:
        raise NotFoundError("sample dataset version", f"{row.dataset_id}@v{row.dataset_version}")
    if dv.status == "ingesting":
        return False
    if dv.status != "ready":
        raise MapiError(
            "the sample's data could not be read into a dataset; retry the sample",
            code=await _run_code(session, dv.ingest_run_id, ErrorCode.INTERNAL_ERROR),
        )
    octx = await _installer(ctx, row)
    spec = await _spec(session, octx, sample)
    run = await submit_spec_run(
        session,
        octx,
        kind=RunKind.FIT,
        spec_id=spec.id,
        revision=spec.latest_revision,
        resource_class=await _fit_class(ctx, session, sample, row.tenant_id),
        labels=dict(sample.labels),
    )
    _move(row, "fitting")
    row.spec_id, row.run_id = spec.id, run.id
    return True


async def _fit_class(
    ctx: ServerContext, session: AsyncSession, sample: SampleDef, tenant_id: str
) -> ResourceClass | None:
    """The resource class of the sample's fit: the spec's own, lowered to the largest class
    the tenant's quotas allow (a public-tier tenant runs it on ``S``: slower, not refused).
    ``None`` keeps the spec's class."""
    _, document = sample_draft(sample, project=SAMPLE_PROJECT_ID)
    wanted = ResourceClass(str(document.get("execution", {}).get("resource_class", "M")))
    tenant = await session.get(m.Tenant, tenant_id)
    async with ctx.db.session() as system:
        policies = [await global_policy(system)]
        if tenant is not None:
            policies.append(await tenant_policy(system, tenant_id, tenant.trust_tier))
    caps = [p.limits.max_resource_class for p in policies if p.limits.max_resource_class]
    cap = min(caps, key=lambda c: c.rank, default=None)
    return cap if cap is not None and cap.rank < wanted.rank else None


async def _spec(session: AsyncSession, octx: ServerContext, sample: SampleDef) -> m.Spec:
    """The sample's spec in its project: the Guided draft with the sample's fit profile and
    labels, stored on the first attempt (a later attempt fits the stored one again)."""
    scope = octx.scope
    _, document = sample_draft(sample, project=scope.project_id)
    name = str(document["metadata"]["name"])
    found = (
        await session.execute(
            select(m.Spec).where(
                m.Spec.tenant_id == scope.tenant_id,
                m.Spec.project_id == scope.project_id,
                m.Spec.name == name,
            )
        )
    ).scalar_one_or_none()
    if found is not None:
        return found
    yaml = dump_yaml(document)
    v = await validate_document(session, scope, octx.catalogs, text=yaml)
    spec, _ = await create_spec_row(
        session, scope, v, yaml=yaml, message=f"The {sample.title.lower()} sample"
    )
    return spec


async def _after_fit(
    ctx: ServerContext, session: AsyncSession, sample: SampleDef, row: m.TenantSample
) -> bool:
    """``fitting → ready`` once the fit made a model version (the library seeded), or
    ``failed`` when it ended without one."""
    assert row.run_id is not None
    run = await session.get(m.Run, row.run_id)
    if run is None:
        raise NotFoundError("sample fit run", row.run_id)
    state = RunState(run.state)
    if not state.terminal:
        return False
    mv = (
        await session.get(m.ModelVersion, run.output_model_version_id)
        if run.output_model_version_id is not None
        else None
    )
    if mv is None:
        raise MapiError(
            "the sample's model fit did not finish; retry the sample",
            code=_code_of(run.problem, ErrorCode.INTERNAL_ERROR),
        )
    _move(row, "ready")
    row.model_id, row.model_version = mv.model_id, mv.version
    row.problem = None
    await _seed_library(ctx, session, sample, row)
    return True


async def _seed_library(
    ctx: ServerContext, session: AsyncSession, sample: SampleDef, row: m.TenantSample
) -> None:
    """The sample's library documents in its project (``created_by="system"``). A seed
    never fails a sample: a document that cannot be stored is logged and skipped."""
    assert row.project_id is not None
    snapshot = await ctx.catalogs.get(session)
    for doc in sample.library_seed:
        try:
            async with session.begin_nested():
                await create_item_system(
                    session,
                    row.tenant_id,
                    row.project_id,
                    doc,
                    catalog=snapshot.catalog if snapshot is not None else None,
                    catalog_digest=snapshot.digest if snapshot is not None else None,
                    max_items=ctx.settings.library_items_per_tenant,
                )
        except Exception:
            log.exception(
                "sample %s of tenant %s: a library seed document was not stored",
                sample.id,
                row.tenant_id,
            )


def _code_of(problem: object, default: ErrorCode) -> ErrorCode:
    """The code of a stored run problem (``default`` when there is none)."""
    if isinstance(problem, dict):
        try:
            return ErrorCode(str(problem.get("code")))
        except ValueError:
            pass
    return default


async def _run_code(session: AsyncSession, run_id: str | None, default: ErrorCode) -> ErrorCode:
    run = await session.get(m.Run, run_id) if run_id is not None else None
    return _code_of(run.problem if run is not None else None, default)
