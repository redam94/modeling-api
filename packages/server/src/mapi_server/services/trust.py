"""The pluggable parts of the trust lifecycle, built once per process.

:class:`TrustServices` holds what sign-up, verification and tenant deletion delegate to:

* the :class:`~mapi_server.services.mail.Mailer` (``MAPI_MAILER``);
* the disposable-e-mail policy (:class:`mapi_spec.trust.DisposableEmailPolicy`): the
  built-in list plus ``MAPI_SIGNUP_BLOCKED_DOMAINS`` and ``MAPI_SIGNUP_BLOCKLIST_PATH``;
* the bot check of sign-ups (:class:`SignupChallenge`; none by default);
* the identity verification provider (:class:`IdentityVerifier`; operators record
  verification by hand until a vendor is integrated, M5);
* the hooks of tenant deletion: :class:`TenantRevoker` (revoke a component's credentials
  or links of a tenant when its deletion starts) and :class:`DeletionNotifier` (deliver the
  ``tenant.deletion.completed`` webhook). Components that own such resources (webhooks,
  embeds) register theirs with :meth:`TrustServices.add_revoker` /
  :attr:`TrustServices.notifier`.

``trust_services(ctx)`` returns the process's instance (built on first use).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mapi_spec.trust import DEFAULT_DISPOSABLE_DOMAINS, BlocklistEmailPolicy, DisposableEmailPolicy
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.services.mail import Mailer, build_mailer
from mapi_server.settings import Settings

if TYPE_CHECKING:
    from mapi_server.context import ServerContext

__all__ = [
    "AllowAllChallenge",
    "DeletionNotifier",
    "FakeIdentityVerifier",
    "IdentityCheck",
    "IdentityVerifier",
    "ManualIdentityVerifier",
    "NoWebhookNotifier",
    "SignupChallenge",
    "TenantRevoker",
    "TrustServices",
    "load_email_policy",
    "trust_services",
]

log = logging.getLogger(__name__)


# ================================================================================ sign-up


@runtime_checkable
class SignupChallenge(Protocol):
    """A bot check of a sign-up (e.g. a Turnstile or reCAPTCHA token verifier)."""

    async def verify(self, token: str | None, *, client_ip: str | None) -> bool: ...


class AllowAllChallenge:
    """No bot check (the rate limits and the Sybil heuristics still apply)."""

    async def verify(self, token: str | None, *, client_ip: str | None) -> bool:
        return True


def load_email_policy(settings: Settings) -> DisposableEmailPolicy:
    """The built-in disposable domains, plus the configured ones and the blocklist file."""
    domains = set(DEFAULT_DISPOSABLE_DOMAINS)
    domains.update(d.strip().lower() for d in settings.signup_blocked_domains if d.strip())
    path: Path | None = settings.signup_blocklist_path
    if path is not None:
        for line in path.read_text("utf-8").splitlines():
            entry = line.split("#", 1)[0].strip().lower()
            if entry:
                domains.add(entry)
    return BlocklistEmailPolicy(frozenset(domains))


# ========================================================================== verification


@dataclass(frozen=True, slots=True)
class IdentityCheck:
    """A provider's answer about one tenant's identity."""

    verified: bool
    method: str
    """A :class:`mapi_spec.trust.VerificationMethod` value (``card``, ``document``, ...)."""
    reference: str | None = None
    """The provider's own id of the check (kept for disputes; no personal data)."""
    reason: str = ""


@runtime_checkable
class IdentityVerifier(Protocol):
    """An identity-verification provider (card or document check) for one tenant."""

    name: str

    async def check(self, tenant_id: str, evidence: Mapping[str, str]) -> IdentityCheck: ...


class ManualIdentityVerifier:
    """No provider: operators record verification (``PUT /v1/admin/tenants/{id}/verification``)."""

    name = "manual"

    async def check(self, tenant_id: str, evidence: Mapping[str, str]) -> IdentityCheck:
        raise NotImplementedError(
            "no identity-verification provider is integrated (M5); an operator records "
            "verification with PUT /v1/admin/tenants/{tenant_id}/verification"
        )


class FakeIdentityVerifier:
    """A deterministic provider for tests and local drills: ``evidence["result"] == "pass"``
    verifies (method ``document``), anything else does not. It records every check."""

    name = "fake"

    def __init__(self) -> None:
        self.checks: list[tuple[str, dict[str, str]]] = []

    async def check(self, tenant_id: str, evidence: Mapping[str, str]) -> IdentityCheck:
        self.checks.append((tenant_id, dict(evidence)))
        ok = evidence.get("result") == "pass"
        return IdentityCheck(
            verified=ok,
            method="document",
            reference=f"fake-{len(self.checks)}",
            reason="document check passed" if ok else "document check failed",
        )


# ============================================================================== deletion


@runtime_checkable
class TenantRevoker(Protocol):
    """Revokes one kind of credential or link of a tenant whose deletion starts.

    Runs on the platform role inside the transaction that moves the tenant to ``deleting``;
    returns how many items it revoked. It must be idempotent (a retried start runs it again).
    """

    name: str

    async def revoke(self, session: AsyncSession, tenant_id: str, *, now: datetime) -> int: ...


@runtime_checkable
class DeletionNotifier(Protocol):
    """Delivers ``tenant.deletion.completed`` (plan §8.2 Integrations; the webhooks component).

    ``capture`` runs when the deletion starts, *before* the tenant's webhooks are purged, and
    returns what ``deliver`` needs later (stored with the deletion, JSON). ``deliver`` runs
    once every store is purged and returns how many deliveries it attempted.
    """

    name: str

    async def capture(self, session: AsyncSession, tenant_id: str) -> list[JsonValue]: ...

    async def deliver(
        self, tenant_id: str, deletion_id: str, captured: Sequence[JsonValue], *, now: datetime
    ) -> int: ...


class NoWebhookNotifier:
    """No webhook service is wired into this process: nothing is captured or delivered."""

    name = "none"

    async def capture(self, session: AsyncSession, tenant_id: str) -> list[JsonValue]:
        return []

    async def deliver(
        self, tenant_id: str, deletion_id: str, captured: Sequence[JsonValue], *, now: datetime
    ) -> int:
        return 0


# ============================================================================== container


@dataclass
class TrustServices:
    mailer: Mailer
    email_policy: DisposableEmailPolicy
    challenge: SignupChallenge = field(default_factory=AllowAllChallenge)
    identity: IdentityVerifier = field(default_factory=ManualIdentityVerifier)
    notifier: DeletionNotifier = field(default_factory=NoWebhookNotifier)
    revokers: list[TenantRevoker] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Settings) -> TrustServices:
        return cls(mailer=build_mailer(settings), email_policy=load_email_policy(settings))

    def add_revoker(self, revoker: TenantRevoker) -> None:
        if any(r.name == revoker.name for r in self.revokers):
            raise ValueError(f"a revoker named {revoker.name!r} is already registered")
        self.revokers.append(revoker)

    def revoker_names(self) -> Iterable[str]:
        return (r.name for r in self.revokers)


def trust_services(ctx: ServerContext) -> TrustServices:
    """The trust services of ``ctx`` (built with the context; request views share it)."""
    if ctx.trust is not None:
        return ctx.trust
    built = TrustServices.from_settings(ctx.settings)
    ctx.trust = built
    return built
