"""FastAPI application factory and the service container lifecycle."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mapi_spec.api import IDEMPOTENCY_KEY_HEADER, LAST_EVENT_ID_HEADER, PREFER_HEADER
from mapi_spec.spec import CONTRACT_VERSION
from mapi_spec.storage import ObjectStore
from sqlalchemy.dialects.postgresql import insert

import mapi_server
from mapi_server.api import datasets, internal, meta, models, runs, specs
from mapi_server.context import FinalizeQueue, ServerContext
from mapi_server.db import Database, resolve_database_url
from mapi_server.db import models as m
from mapi_server.errors import install_error_handlers
from mapi_server.services.catalog import CatalogService
from mapi_server.services.notify import NotificationHub
from mapi_server.services.signing import BundleSigner
from mapi_server.settings import Settings
from mapi_server.storage import build_object_store

__all__ = ["build_context", "create_app", "ensure_scope", "startup"]

log = logging.getLogger(__name__)


def build_context(
    settings: Settings,
    *,
    store: ObjectStore | None = None,
    signer: BundleSigner | None = None,
    queue: FinalizeQueue | None = None,
    database_url: str | None = None,
) -> ServerContext:
    """Wire the process-wide services (no I/O besides starting an embedded Postgres)."""
    url = database_url or resolve_database_url(settings)
    db = Database(url, pool_size=settings.db_pool_size, echo=settings.db_echo)
    owned_store = store is None
    ctx = ServerContext(
        settings=settings,
        db=db,
        store=store or build_object_store(settings),
        catalogs=CatalogService(
            settings.resolve_image_digest(), catalog_path=settings.catalog_path
        ),
        signer=signer or BundleSigner.from_file(settings.key_path),
        hub=NotificationHub(db.url),
        queue=queue,
    )
    aclose = getattr(ctx.store, "aclose", None)
    if owned_store and callable(aclose):
        ctx.on_shutdown.append(aclose)
    return ctx


async def ensure_scope(ctx: ServerContext) -> None:
    """Create the single M1 tenant and project if they do not exist (idempotent)."""
    sc = ctx.scope
    async with ctx.db.session() as s:
        await s.execute(
            insert(m.Tenant)
            .values(id=sc.tenant_id, name=sc.tenant_id.capitalize(), trust_tier="internal")
            .on_conflict_do_nothing()
        )
        await s.execute(
            insert(m.Project)
            .values(
                tenant_id=sc.tenant_id,
                id=sc.project_id,
                project_id=sc.project_id,
                name=f"{sc.project_id.capitalize()} project",
            )
            .on_conflict_do_nothing()
        )


async def startup(ctx: ServerContext, *, listen: bool = True) -> None:
    await ensure_scope(ctx)
    async with ctx.db.session() as s:
        await ctx.catalogs.load_file(
            s, tenant_id=ctx.scope.tenant_id, project_id=ctx.scope.project_id
        )
    if listen:
        await ctx.hub.start()


async def shutdown(ctx: ServerContext) -> None:
    await ctx.hub.stop()
    for fn in reversed(ctx.on_shutdown):
        await fn()
    await ctx.db.dispose()


def create_app(
    settings: Settings | None = None,
    *,
    ctx: ServerContext | None = None,
    manage_context: bool | None = None,
) -> FastAPI:
    """The API app. With ``ctx`` the caller owns its lifecycle unless ``manage_context``."""
    settings = settings or (ctx.settings if ctx else Settings())
    owns = manage_context if manage_context is not None else ctx is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        context = ctx or build_context(settings)
        app.state.ctx = context
        if owns:
            await startup(context)
        try:
            yield
        finally:
            if owns:
                await shutdown(context)

    app = FastAPI(
        title="modeling-api",
        version=mapi_server.__version__,
        summary="Typed xarray modelling runtime: control plane API (v1)",
        description=(
            f"Contract version {CONTRACT_VERSION}. Errors are RFC 7807 problem documents "
            "(`application/problem+json`) with stable `code`s. M1 is single-tenant and "
            "unauthenticated (listens on 127.0.0.1)."
        ),
        lifespan=lifespan,
        openapi_tags=[
            {"name": n}
            for n in (
                "health",
                "identity",
                "projects",
                "catalog",
                "datasets",
                "specs",
                "runs",
                "models",
                "plots",
                "internal",
            )
        ],
    )
    if ctx is not None:
        app.state.ctx = ctx
    install_error_handlers(app, debug=settings.debug)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin.rstrip("/")],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            IDEMPOTENCY_KEY_HEADER,
            LAST_EVENT_ID_HEADER,
            PREFER_HEADER,
        ],
        expose_headers=["Preference-Applied", "Location"],
    )
    for router in (
        meta.router,
        datasets.router,
        specs.router,
        runs.router,
        models.router,
        models.plots_router,
        internal.router,
    ):
        app.include_router(router)
    return app


def openapi_document(settings: Settings | None = None) -> dict[str, Any]:
    """The OpenAPI 3.1 document (no database or storage needed)."""
    app = create_app(settings or Settings(database_url="postgresql://unused/unused"))
    return app.openapi()
