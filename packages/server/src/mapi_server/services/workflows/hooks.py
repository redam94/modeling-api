"""Extension points the governance component plugs into (resolved in the control plane).

``governance.policy`` nodes need the policies in force for the model they check, which live
in the governance tables. The defaults are the governance service's
(:func:`mapi_server.services.governance.workflow_policy_resolver` and, for ``submit: true``,
:func:`~mapi_server.services.governance.workflow_validation_submitter`, which records the
evidence as a ``ModelValidation``). Tests and embedders may replace them; with the resolver
set to ``None`` no policy applies (the node says so) and a node that names a policy fails
with ``POLICY_INVALID``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mapi_spec.governance import GovernancePolicy
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m

if TYPE_CHECKING:
    from mapi_server.context import ServerContext

__all__ = [
    "DEFAULT_RESOLVER",
    "DEFAULT_SUBMITTER",
    "GovernanceHooks",
    "PolicyResolver",
    "ValidationSubmitter",
    "governance_hooks",
    "set_policy_resolver",
    "set_validation_submitter",
]

PolicyResolver = Callable[
    ["AsyncSession", "ServerContext", m.ModelVersion, str | None],
    Awaitable[Sequence[GovernancePolicy]],
]
"""``(session, ctx, model version, policy id or None) -> policies in force``."""

ValidationSubmitter = Callable[
    ["AsyncSession", "ServerContext", m.ModelVersion, dict[str, object], str],
    Awaitable[str | None],
]
"""``(session, ctx, model version, evidence payload, workflow run id) -> validation id``."""


@dataclass
class GovernanceHooks:
    resolver: PolicyResolver | None = None
    submitter: ValidationSubmitter | None = None
    notes: list[str] = field(default_factory=list)


async def _default_resolver(
    session: AsyncSession, ctx: ServerContext, mv: m.ModelVersion, policy_id: str | None
) -> Sequence[GovernancePolicy]:
    from mapi_server.services.governance import workflow_policy_resolver

    return await workflow_policy_resolver(session, ctx, mv, policy_id)


async def _default_submitter(
    session: AsyncSession,
    ctx: ServerContext,
    mv: m.ModelVersion,
    evidence: dict[str, object],
    workflow_run_id: str,
) -> str | None:
    from mapi_server.services.governance import workflow_validation_submitter

    return await workflow_validation_submitter(session, ctx, mv, evidence, workflow_run_id)


DEFAULT_RESOLVER: PolicyResolver = _default_resolver
DEFAULT_SUBMITTER: ValidationSubmitter = _default_submitter

_HOOKS = GovernanceHooks(resolver=DEFAULT_RESOLVER, submitter=DEFAULT_SUBMITTER)


def governance_hooks() -> GovernanceHooks:
    """The registered hooks (process-wide)."""
    return _HOOKS


def set_policy_resolver(fn: PolicyResolver | None) -> None:
    _HOOKS.resolver = fn


def set_validation_submitter(fn: ValidationSubmitter | None) -> None:
    _HOOKS.submitter = fn
