"""FastAPI application factory and the service container lifecycle."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mapi_spec.api import IDEMPOTENCY_KEY_HEADER, LAST_EVENT_ID_HEADER, PREFER_HEADER
from mapi_spec.capabilities import Diagnostic
from mapi_spec.run import (
    CheckpointPayload,
    CompletePayload,
    ErrorPayload,
    LogPayload,
    MetricPayload,
    ProgressPayload,
    StatePayload,
    WarningPayload,
)
from mapi_spec.spec import CONTRACT_VERSION
from mapi_spec.storage import ObjectStore
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema
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

__all__ = [
    "EVENT_PAYLOAD_MODELS",
    "build_context",
    "create_app",
    "ensure_scope",
    "load_signer",
    "openapi_document",
    "startup",
]

EVENT_PAYLOAD_MODELS: tuple[type[BaseModel], ...] = (
    StatePayload,
    ProgressPayload,
    LogPayload,
    WarningPayload,
    CheckpointPayload,
    MetricPayload,
    CompletePayload,
    ErrorPayload,
    Diagnostic,
)
"""``payload`` schemas of run events (SSE frames and the events page), published as OpenAPI
components so generated clients can type them (FastAPI cannot infer them from routes)."""


def _with_event_payloads(doc: dict[str, Any]) -> dict[str, Any]:
    _, top = models_json_schema(
        [(model, "serialization") for model in EVENT_PAYLOAD_MODELS],
        ref_template="#/components/schemas/{model}",
    )
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})
    for name, schema in sorted(top.get("$defs", {}).items()):
        schemas.setdefault(name, schema)
    return doc


log = logging.getLogger(__name__)


def load_signer(settings: Settings) -> BundleSigner:
    """The bundle-signing key at ``settings.key_path`` (created if allowed and missing)."""
    try:
        return BundleSigner.from_file(settings.key_path, create=settings.signing_key_create)
    except FileNotFoundError:
        raise RuntimeError(
            f"no bundle-signing key at {settings.key_path} and MAPI_SIGNING_KEY_CREATE is off: "
            "run `mapi-server migrate` first (it creates the key), or check that the key "
            "volume is mounted and MAPI_SIGNING_KEY_PATH points into it"
        ) from None


def build_context(
    settings: Settings,
    *,
    store: ObjectStore | None = None,
    signer: BundleSigner | None = None,
    queue: FinalizeQueue | None = None,
    database_url: str | None = None,
) -> ServerContext:
    """Wire the process-wide services (no I/O besides starting an embedded Postgres)."""
    signer = signer or load_signer(settings)
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
        signer=signer,
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
    base_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = _with_event_payloads(base_openapi())
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
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
