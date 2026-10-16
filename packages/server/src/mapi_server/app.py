"""FastAPI application factory and the service container lifecycle."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import fastapi.routing
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.routing import APIRoute
from mapi_spec.api import IDEMPOTENCY_KEY_HEADER, LAST_EVENT_ID_HEADER, PREFER_HEADER
from mapi_spec.capabilities import Diagnostic
from mapi_spec.errors import MapiError
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
from mapi_spec.tenancy import PROJECT_HEADER
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema
from starlette.types import ASGIApp, Receive, Scope, Send

import mapi_server
from mapi_server.api import (
    admin,
    datasets,
    guided,
    internal,
    meta,
    models,
    prior_translations,
    runs,
    specs,
    tenancy,
    tokens,
    usage,
)
from mapi_server.api import auth as auth_routes
from mapi_server.auth.oidc import HttpOidcProvider, OidcClient
from mapi_server.auth.request import CSRF_HEADER, access_for, authenticate
from mapi_server.context import FinalizeQueue, ServerContext
from mapi_server.db import Database, acting_as_gateway, resolve_database_url
from mapi_server.devidp import DevIdP, mount_devidp
from mapi_server.errors import install_error_handlers
from mapi_server.services.catalog import CatalogService
from mapi_server.services.cost import PRICE_TABLE_INFO_KEY, load_price_table
from mapi_server.services.notify import NotificationHub
from mapi_server.services.secrets import ServerSecrets, load_server_secrets
from mapi_server.services.signing import LocalSigner, ManifestSigner
from mapi_server.services.tenancy import (
    ensure_local_admin,
    ensure_platform_tenant,
    invite_admins,
    seed_dev_accounts,
)
from mapi_server.settings import Profile, Settings, SignerKind
from mapi_server.storage import build_object_store

__all__ = [
    "EVENT_PAYLOAD_MODELS",
    "build_context",
    "build_oidc",
    "check_route_access",
    "create_app",
    "ensure_scope",
    "iter_api_routes",
    "load_signer",
    "openapi_document",
    "prepare_signer",
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


def load_signer(settings: Settings) -> ManifestSigner:
    """The bundle signer ``MAPI_SIGNER`` selects.

    ``local``: the P-256 key at ``settings.key_path`` (created if allowed and missing).
    ``kms``: the Cloud KMS key version ``MAPI_KMS_KEY_VERSION`` (plus the trusted older
    versions); its public keys are loaded by :func:`startup`.
    """
    if settings.signer is SignerKind.KMS:
        from mapi_server.services.kms import KmsSigner

        assert settings.kms_key_version is not None  # enforced by Settings
        return KmsSigner.create(
            settings.kms_key_version, trusted_versions=settings.kms_trusted_key_versions
        )
    try:
        return LocalSigner.from_file(
            settings.key_path,
            create=settings.signing_key_create,
            trusted_paths=settings.signing_trusted_key_paths,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"no bundle-signing key at {settings.key_path} and MAPI_SIGNING_KEY_CREATE is off: "
            "run `mapi-server migrate` first (it creates the key), or check that the key "
            "volume is mounted and MAPI_SIGNING_KEY_PATH points into it"
        ) from None


def build_oidc(settings: Settings, devidp: DevIdP | None) -> OidcClient | None:
    """The relying party for ``/auth/*``: the dev IdP, a configured issuer, or none."""
    if devidp is not None:
        return OidcClient(
            devidp,
            client_id=devidp.client_id,
            redirect_uri=settings.redirect_uri(),
            algorithms=("RS256",),
            allow_http=True,
        )
    if settings.oidc_issuer and settings.oidc_client_id:
        return OidcClient(
            HttpOidcProvider(
                settings.oidc_issuer,
                client_secret=settings.oidc_client_secret,
                discovery_url=settings.oidc_discovery_url,
            ),
            client_id=settings.oidc_client_id,
            redirect_uri=settings.redirect_uri(),
            algorithms=settings.oidc_algorithms,
            extra_params=settings.oidc_extra_params,
        )
    return None


def build_context(
    settings: Settings,
    *,
    store: ObjectStore | None = None,
    signer: ManifestSigner | None = None,
    queue: FinalizeQueue | None = None,
    database_url: str | None = None,
    secrets: ServerSecrets | None = None,
) -> ServerContext:
    """Wire the process-wide services (no I/O besides starting an embedded Postgres and
    creating missing key files)."""
    signer = signer or load_signer(settings)
    url = database_url or resolve_database_url(settings)
    price_table = load_price_table(settings.price_table_path)
    db = Database.from_settings(settings, url, session_info={PRICE_TABLE_INFO_KEY: price_table})
    if settings.tracing_enabled:
        from mapi_server.observability.tracing import instrument_engine

        instrument_engine(db.system_engine)
        instrument_engine(db.gateway_engine)
        instrument_engine(db.app_engine)
    owned_store = store is None
    devidp = (
        DevIdP(
            users=settings.devidp_users,
            base_url=settings.api_url,
            redirect_uri=settings.redirect_uri(),
            public_clients=settings.oidc_cli_client_ids,
        )
        if settings.devidp_enabled
        else None
    )
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
        secrets=secrets or load_server_secrets(settings),
        price_table=price_table,
        devidp=devidp,
        oidc=build_oidc(settings, devidp),
    )
    aclose = getattr(ctx.store, "aclose", None)
    if owned_store and callable(aclose):
        ctx.on_shutdown.append(aclose)
    provider_close = getattr(getattr(ctx.oidc, "provider", None), "aclose", None)
    if callable(provider_close):
        ctx.on_shutdown.append(provider_close)
    return ctx


async def ensure_scope(ctx: ServerContext) -> None:
    """Provision the platform tenant/project (and, by profile, invited accounts)."""
    sc = ctx.platform_scope
    settings = ctx.settings
    async with ctx.db.session() as s:
        await ensure_platform_tenant(
            s, sc.tenant_id, sc.project_id, residency=settings.effective_default_residency()
        )
        if not settings.multi_tenant:
            await ensure_local_admin(s, sc.tenant_id)
        if settings.devidp_enabled:
            await seed_dev_accounts(
                s, settings.devidp_users, tenant_id=sc.tenant_id, project_id=sc.project_id
            )
        if settings.default_tenant_admins:
            await invite_admins(s, settings.default_tenant_admins, tenant_id=sc.tenant_id)


async def prepare_signer(signer: ManifestSigner) -> None:
    """Load what a remote signer needs to verify locally (KMS public keys)."""
    prepare = getattr(signer, "prepare", None)
    if prepare is not None:
        await prepare()


async def startup(ctx: ServerContext, *, listen: bool = True) -> None:
    from mapi_server.observability import metrics as obs_metrics

    obs_metrics.METRICS.bind_sse_gauge(ctx.hub.subscriber_count)
    await prepare_signer(ctx.signer)
    await ensure_scope(ctx)
    async with ctx.db.session() as s:
        await ctx.catalogs.load_file(
            s, tenant_id=ctx.platform_scope.tenant_id, project_id=ctx.platform_scope.project_id
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
        if owns:  # the API process: its platform lookups are the gateway role's
            with acting_as_gateway():
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
            "(`application/problem+json`) with stable `code`s. Programs authenticate with "
            "`Authorization: Bearer mapi_…` API tokens, browsers with the BFF session "
            f"(`/auth/login`); project-scoped routes act in the project named by the "
            f"`{PROJECT_HEADER}: <tenant>/<project>` header (or the token's project). The "
            "single-tenant profile accepts credential-less requests on loopback as the "
            "local administrator."
        ),
        lifespan=lifespan,
        dependencies=[Depends(authenticate)],
        openapi_tags=[
            {"name": n}
            for n in (
                "health",
                "identity",
                "auth",
                "tenants",
                "projects",
                "tokens",
                "usage",
                "admin",
                "catalog",
                "datasets",
                "specs",
                "guided",
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
    _install_observability(app, settings, ctx)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin.rstrip("/")],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            IDEMPOTENCY_KEY_HEADER,
            LAST_EVENT_ID_HEADER,
            PREFER_HEADER,
            PROJECT_HEADER,
            CSRF_HEADER,
        ],
        expose_headers=["Preference-Applied", "Location", "Retry-After", CSRF_HEADER],
    )
    # Outermost: DNS rebinding makes a hostile page same-origin with 127.0.0.1, so CORS never
    # applies; the Host header is the only tell (plan §9). 400 for any unexpected host.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts())
    # Every request's platform sessions run as the gateway role, never the bypass role
    # (plan §9): what a handler reads before (or besides) its tenant stays under RLS.
    app.add_middleware(GatewayRoleMiddleware)
    for router in (
        meta.router,
        auth_routes.router,
        tenancy.router,
        tokens.router,
        usage.router,
        admin.router,
        datasets.router,
        specs.router,
        guided.router,
        runs.router,
        models.router,
        models.plots_router,
        prior_translations.router,
        internal.router,
    ):
        app.include_router(router)
    if settings.devidp_enabled:
        mount_devidp(app, settings)
    check_route_access(app)
    return app


class GatewayRoleMiddleware:
    """Runs each request in :func:`~mapi_server.db.acting_as_gateway` (pure ASGI, so the
    context covers the whole response, streamed bodies included)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        with acting_as_gateway():
            await self.app(scope, receive, send)


def iter_api_routes(app: FastAPI) -> Iterator[tuple[APIRoute, str]]:
    """Every API route of ``app`` with its full path, including those of included routers
    (FastAPI >= 0.140 keeps included routers as branches in ``app.routes``)."""
    iterate = getattr(fastapi.routing, "iter_route_contexts", None)
    if iterate is None:  # older FastAPI: included routes are flattened into app.routes
        for route in app.routes:
            if isinstance(route, APIRoute):
                yield route, route.path
        return
    for context in iterate(app.routes):
        original = context.original_route
        if isinstance(original, APIRoute):
            yield original, context.path or original.path


def check_route_access(app: FastAPI) -> None:
    """Every route must have an access rule (fail closed at start-up, not per request)."""
    missing = []
    for route, path in iter_api_routes(app):
        op = route.operation_id or route.name
        try:
            access_for(op)
        except MapiError:
            missing.append(f"{sorted(route.methods or ())} {path} ({op})")
    if missing:
        raise RuntimeError(f"routes without an access rule: {missing}")


def _install_observability(app: FastAPI, settings: Settings, ctx: ServerContext | None) -> None:
    """Metrics endpoint, request middleware, OTel instrumentation and the SSE-client gauge.

    Logging and the tracer provider are configured once per process at the entry point
    (``mapi-server api``/``worker``); here we only attach per-app pieces so that tests which
    build apps directly still get ``/metrics`` and request metrics.
    """
    from mapi_server.observability import metrics as obs_metrics
    from mapi_server.observability.middleware import (
        ObservabilityMiddleware,
        add_metrics_endpoint,
    )
    from mapi_server.observability.tracing import instrument_app

    if settings.metrics_enabled:
        add_metrics_endpoint(
            app,
            token=settings.metrics_token,
            require_token=settings.profile is Profile.MULTI_TENANT,
        )
    app.add_middleware(ObservabilityMiddleware, fastapi_app=app)
    if settings.tracing_enabled:
        instrument_app(app)
    if ctx is not None:
        obs_metrics.METRICS.bind_sse_gauge(ctx.hub.subscriber_count)


def openapi_document(settings: Settings | None = None) -> dict[str, Any]:
    """The OpenAPI 3.1 document (no database or storage needed)."""
    app = create_app(settings or Settings(database_url="postgresql://unused/unused"))
    return app.openapi()
