"""The Guided persona lock on spec writes (``Project.guided_locked``, M3).

A project admin can lock a project to Guided mode. The web app then shows members below
admin the Guided screens only, and the API holds their spec writes to the same line:
``POST /v1/specs`` and ``POST /v1/specs/{id}/revisions`` accept only Guided specs whose
``advanced`` and ``hidden`` fields keep their template's (or parent revision's) values -
the rules of :func:`mapi_spec.guided.guided_lock_issues`. Refusals are ``403 FORBIDDEN``
with one issue per offending JSON pointer. Admins, ``POST /v1/specs:validate``, runs and
scoring are not affected.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.guided import guided_lock_issues
from mapi_spec.tenancy import Action, ResourceKind
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import RequestAuth
from mapi_server.db import models as m
from mapi_server.services.runs import Scope

__all__ = ["enforce_guided_lock", "is_guided_locked"]


async def is_guided_locked(session: AsyncSession, scope: Scope) -> bool:
    project = await session.get(m.Project, (scope.tenant_id, scope.project_id))
    return bool(project is not None and project.guided_locked)


async def enforce_guided_lock(
    session: AsyncSession,
    scope: Scope,
    auth: RequestAuth | None,
    document: Mapping[str, Any],
    parent: Mapping[str, Any] | None = None,
) -> None:
    """Raise ``FORBIDDEN`` when a member below project admin writes a spec that breaks
    the lock of a Guided-locked project (no-op for admins and unlocked projects)."""
    if auth is not None and auth.allows(Action.UPDATE, ResourceKind.PROJECT):
        return
    if not await is_guided_locked(session, scope):
        return
    issues = guided_lock_issues(document, parent)
    if issues:
        raise MapiError(
            f"this project is locked to Guided mode: {issues[0].message}"[:2000],
            code=ErrorCode.FORBIDDEN,
            pointer=issues[0].pointer,
            issues=issues,
        )
