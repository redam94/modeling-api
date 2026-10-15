"""The process-wide service container shared by the API and the orchestrator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from mapi_spec.storage import Buckets, ObjectStore

from mapi_server.db import Database
from mapi_server.services.catalog import CatalogService
from mapi_server.services.notify import NotificationHub
from mapi_server.services.runs import Scope
from mapi_server.services.signing import ManifestSigner
from mapi_server.settings import Settings

if TYPE_CHECKING:
    from mapi_server.services.pubsub import PushVerifier

__all__ = ["FinalizeQueue", "ServerContext"]


class FinalizeQueue(Protocol):
    """Where the reconciler hands FINALIZING runs (procrastinate in production)."""

    async def defer_finalize(self, run_id: str, attempt: int) -> None: ...


@dataclass
class ServerContext:
    settings: Settings
    db: Database
    store: ObjectStore
    catalogs: CatalogService
    signer: ManifestSigner
    hub: NotificationHub
    queue: FinalizeQueue | None = None
    on_shutdown: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    push_verifier: PushVerifier | None = None
    """Pub/Sub push OIDC verifier (built on first use when push auth is configured)."""

    @property
    def buckets(self) -> Buckets:
        return self.settings.buckets

    @property
    def scope(self) -> Scope:
        """M1 is single-tenant: every request acts in the configured default scope."""
        return Scope(self.settings.tenant_id, self.settings.project_id)
