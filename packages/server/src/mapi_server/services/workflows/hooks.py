"""Extension points the governance component plugs into (resolved in the control plane).

``governance.policy`` nodes need the policies in force for the model they check, which live
in the governance component's tables. It registers a :data:`PolicyResolver` here (and,
for ``submit: true``, a :data:`ValidationSubmitter` that records the evidence on a
``ModelValidation``) when the app and the orchestrator start. Without a resolver no policy
applies (the node says so); a node that names a policy then fails with ``POLICY_INVALID``.
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


_HOOKS = GovernanceHooks()


def governance_hooks() -> GovernanceHooks:
    """The registered hooks (process-wide)."""
    return _HOOKS


def set_policy_resolver(fn: PolicyResolver | None) -> None:
    _HOOKS.resolver = fn


def set_validation_submitter(fn: ValidationSubmitter | None) -> None:
    _HOOKS.submitter = fn
