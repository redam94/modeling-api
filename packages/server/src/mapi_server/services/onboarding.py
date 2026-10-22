"""Onboarding progress: the first-run checklist and the guided tours of one member (v0.18 D19).

One ``onboarding_progress`` row per tenant and principal holds an
:class:`~mapi_spec.onboarding.OnboardingState` (``state`` JSON, ``version`` column). The row
is created on the first write; a member without one reads the fresh state at version 0.

* :func:`get_state` reads the caller's state (leniently: :func:`mapi_spec.onboarding.load_state`).
* :func:`apply_patch` merges a PATCH under an optional ``If-Match`` (the :func:`etag` of the
  state read): a stale precondition is ``PRECONDITION_FAILED`` (412). The merge itself is
  :func:`mapi_spec.onboarding.apply_patch`, so a completed step never reverts; a PATCH that
  changes nothing writes nothing and keeps the version.
* :func:`reset` starts the checklist and the tours again (the only way a step reverts).

Progress is UX state, not a security control: it gates nothing and is not audited. Every
function runs in the caller's tenant session (row-level security); the row is locked for the
read-modify-write, so two tabs that PATCH at once both land.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.onboarding import (
    OnboardingPatch,
    OnboardingState,
    load_state,
    reset_state,
    state_document,
)
from mapi_spec.onboarding import apply_patch as merge_patch
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.services.runs import now

__all__ = ["apply_patch", "etag", "etag_matches", "get_state", "reset"]

_WEAK: Final = "W/"


def etag(state: OnboardingState) -> str:
    """The strong ETag of a state: its version, quoted (``"4"``)."""
    return f'"{state.version}"'


def etag_matches(if_match: str, state: OnboardingState) -> bool:
    """Does an ``If-Match`` header name ``state``? ``*`` and a list of tags are accepted; a
    weak tag compares by its value (the version is the only validator)."""
    current = etag(state)
    for raw in if_match.split(","):
        tag = raw.strip()
        if tag == "*":
            return True
        if tag.startswith(_WEAK):
            tag = tag[len(_WEAK) :]
        if tag == current:
            return True
    return False


async def _row(
    session: AsyncSession, tenant_id: str, principal_id: str, *, lock: bool
) -> m.OnboardingProgress | None:
    stmt = select(m.OnboardingProgress).where(
        m.OnboardingProgress.tenant_id == tenant_id,
        m.OnboardingProgress.principal_id == principal_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def _locked_row(
    session: AsyncSession, tenant_id: str, principal_id: str
) -> m.OnboardingProgress:
    """The caller's row, created at version 0 when missing, locked for update."""
    row = await _row(session, tenant_id, principal_id, lock=True)
    if row is not None:
        return row
    await session.execute(
        insert(m.OnboardingProgress)
        .values(tenant_id=tenant_id, principal_id=principal_id, state={}, version=0)
        .on_conflict_do_nothing(index_elements=["tenant_id", "principal_id"])
    )
    row = await _row(session, tenant_id, principal_id, lock=True)
    assert row is not None  # inserted above, or by a concurrent first write
    return row


def _state_of(row: m.OnboardingProgress | None) -> OnboardingState:
    if row is None:
        return OnboardingState()
    return load_state(row.state, version=row.version)


def _store(row: m.OnboardingProgress, state: OnboardingState, at: datetime) -> None:
    row.state = dict(state_document(state))
    row.version = state.version
    row.updated_at = at


async def get_state(session: AsyncSession, tenant_id: str, principal_id: str) -> OnboardingState:
    """The caller's progress in the tenant (version 0 and empty before the first write)."""
    return _state_of(await _row(session, tenant_id, principal_id, lock=False))


async def apply_patch(
    session: AsyncSession,
    tenant_id: str,
    principal_id: str,
    patch: OnboardingPatch,
    *,
    if_match: str | None = None,
    at: datetime | None = None,
) -> OnboardingState:
    """Merge ``patch`` into the caller's progress and store it.

    ``if_match`` (optional; the merge never loses a completed step, so a client may PATCH
    blind) must name the current version, else ``PRECONDITION_FAILED``. Raises what
    :func:`mapi_spec.onboarding.apply_patch` raises (``SCHEMA_INVALID``,
    ``PAYLOAD_TOO_LARGE``)."""
    ts = at or now()
    row = await _locked_row(session, tenant_id, principal_id)
    state = _state_of(row)
    if if_match is not None and not etag_matches(if_match, state):
        raise MapiError(
            f"the onboarding progress changed since you read it (it is now at version "
            f"{state.version}); read it again and send If-Match with its ETag",
            code=ErrorCode.PRECONDITION_FAILED,
        )
    merged = merge_patch(state, patch, now=ts)
    if merged.version != state.version:
        _store(row, merged, ts)
        await session.flush()
    return merged


async def reset(
    session: AsyncSession, tenant_id: str, principal_id: str, *, at: datetime | None = None
) -> OnboardingState:
    """Start the checklist and every tour again (tips come back on; the hidden samples and
    the first welcome are kept). The version always goes up."""
    ts = at or now()
    row = await _locked_row(session, tenant_id, principal_id)
    state = reset_state(_state_of(row))
    _store(row, state, ts)
    await session.flush()
    return state
