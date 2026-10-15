"""The process-wide service container shared by the API and the orchestrator.

Request handlers get a *per-request view* of it (:meth:`ServerContext.for_request`) that
carries the authenticated caller, so ``ctx.scope`` is the tenant/project the request acts in.
The orchestrator uses the container directly: its ``scope`` is the platform scope (the
configured default tenant and project), and it reads and writes through the platform role.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from mapi_spec.cost import DEFAULT_PRICE_TABLE, PriceTable
from mapi_spec.storage import Buckets, ObjectStore
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import Database
from mapi_server.services.catalog import CatalogService
from mapi_server.services.notify import NotificationHub
from mapi_server.services.runs import Scope
from mapi_server.services.secrets import ServerSecrets
from mapi_server.services.signing import BundleSigner
from mapi_server.settings import Settings

if TYPE_CHECKING:
    from mapi_server.auth.oidc import JwksCache, OidcClient
    from mapi_server.auth.request import RequestAuth
    from mapi_server.devidp import DevIdP

__all__ = ["FinalizeQueue", "NoProjectScope", "ServerContext"]


class FinalizeQueue(Protocol):
    """Where the reconciler hands FINALIZING runs (procrastinate in production)."""

    async def defer_finalize(self, run_id: str, attempt: int) -> None: ...


class NoProjectScope(RuntimeError):
    """A handler asked for ``ctx.scope`` on a route that does not act in one project."""


@dataclass
class ServerContext:
    settings: Settings
    db: Database
    store: ObjectStore
    catalogs: CatalogService
    signer: BundleSigner
    hub: NotificationHub
    queue: FinalizeQueue | None = None
    on_shutdown: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    secrets: ServerSecrets = field(default_factory=ServerSecrets.generate)
    price_table: PriceTable = DEFAULT_PRICE_TABLE
    oidc: OidcClient | None = None
    devidp: DevIdP | None = None
    push_jwks: JwksCache | None = None
    """Google's signing keys for Pub/Sub push tokens (when push OIDC is configured)."""
    request: RequestAuth | None = None
    """The authenticated caller (request views only; ``None`` in the orchestrator)."""

    @property
    def buckets(self) -> Buckets:
        return self.settings.buckets

    @property
    def platform_scope(self) -> Scope:
        """The configured default tenant/project (single-tenant installs; platform runs)."""
        return Scope(self.settings.tenant_id, self.settings.project_id)

    @property
    def scope(self) -> Scope:
        """The project a request acts in (the platform scope outside requests)."""
        if self.request is None:
            return self.platform_scope
        if self.request.scope is None:
            raise NoProjectScope(f"{self.request.operation_id} does not act in a project")
        return self.request.scope

    def data_session(self) -> AbstractAsyncContextManager[AsyncSession]:
        """A new transaction for tenant data: pinned to the request's tenant (row-level
        security) in a request view, the platform role in the orchestrator."""
        if self.request is None:
            return self.db.session()
        if not self.request.tenant_id:
            raise NoProjectScope(f"{self.request.operation_id} does not act in a tenant")
        return self.db.tenant_session(self.request.tenant_id)

    def for_request(self, auth: RequestAuth) -> ServerContext:
        """A shallow copy bound to one request's caller (services are shared)."""
        return replace(self, request=auth)
