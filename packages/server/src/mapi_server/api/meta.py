"""Health, identity, projects, catalog and JSON Schemas."""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Path
from fastapi.responses import JSONResponse
from mapi_spec.api import (
    CatalogResponse,
    Health,
    HealthCheck,
    Page,
    Project,
)
from mapi_spec.catalog import ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry
from mapi_spec.jsonschema_export import export_schemas
from mapi_spec.refs import Version
from sqlalchemy import text

import mapi_server
from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Limit,
    Session,
    SystemSession,
    decode_cursor,
    encode_cursor,
)
from mapi_server.api.tenancy import project_dto, visible_projects
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError

__all__ = ["router"]

router = APIRouter()


@router.get("/healthz", response_model=Health, tags=["health"], operation_id="health")
async def healthz() -> Health:
    """Process liveness (never touches dependencies)."""
    return Health(status="ok", version=mapi_server.__version__)


@router.get(
    "/readyz",
    response_model=Health,
    tags=["health"],
    operation_id="ready",
    responses={503: {"model": Health}},
)
async def readyz(ctx: Ctx) -> JSONResponse:
    """Postgres and the object store are reachable (503 with the same body if not)."""
    checks: dict[str, HealthCheck] = {}
    t = time.perf_counter()
    try:
        async with ctx.db.session() as s:
            await s.execute(text("SELECT 1"))
        checks["postgres"] = HealthCheck(status="ok", latency_ms=(time.perf_counter() - t) * 1e3)
    except Exception as exc:
        checks["postgres"] = HealthCheck(status="fail", detail=type(exc).__name__)
    t = time.perf_counter()
    try:
        await ctx.store.list(ctx.buckets.runs, prefix="", max_results=1)
        checks["object_store"] = HealthCheck(
            status="ok", latency_ms=(time.perf_counter() - t) * 1e3
        )
    except Exception as exc:
        checks["object_store"] = HealthCheck(status="fail", detail=type(exc).__name__)
    t = time.perf_counter()
    try:
        async with ctx.db.session() as s:
            snap = await ctx.catalogs.get(s)
        checks["catalog"] = HealthCheck(
            status="ok" if snap else "degraded",
            detail=None if snap else "no catalog for this image digest yet",
            latency_ms=(time.perf_counter() - t) * 1e3,
        )
    except Exception as exc:
        checks["catalog"] = HealthCheck(status="fail", detail=type(exc).__name__)
    hard_fail = any(checks[k].status == "fail" for k in ("postgres", "object_store"))
    overall = (
        "fail"
        if hard_fail
        else ("degraded" if any(c.status != "ok" for c in checks.values()) else "ok")
    )
    body = Health(status=overall, version=mapi_server.__version__, checks=checks)
    return JSONResponse(body.model_dump(mode="json"), status_code=503 if hard_fail else 200)


@router.get(
    "/v1/projects", response_model=Page[Project], tags=["projects"], operation_id="list_projects"
)
async def list_projects(
    auth: Auth, system: SystemSession, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[Project]:
    """Every project the caller can act in, across its tenants (ordered by tenant, id)."""
    rows = [p for p, role in await visible_projects(system, auth) if role is not None]
    if auth.token is not None:  # a token sees only the project it is bound to
        rows = [
            p for p in rows if (p.tenant_id, p.id) == (auth.token.tenant_id, auth.token.project_id)
        ]
    after = decode_cursor(cursor)
    if after is not None:
        key = (str(after[0]), str(after[1]))
        rows = [p for p in rows if (p.tenant_id, p.id) > key]
    rows.sort(key=lambda p: (p.tenant_id, p.id))
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[Project](
        items=[project_dto(p) for p in rows],
        next_cursor=encode_cursor([rows[-1].tenant_id, rows[-1].id]) if more and rows else None,
    )


@router.get(
    "/v1/projects/{project_id}",
    response_model=Project,
    tags=["projects"],
    operation_id="get_project",
)
async def get_project(ctx: Ctx, session: Session, project_id: str) -> Project:
    """The selected project (its tenant comes from ``X-Mapi-Project``, the token, or the
    caller's only tenant)."""
    p = await session.get(m.Project, (ctx.scope.tenant_id, ctx.scope.project_id))
    if p is None:
        raise NotFoundError("project", project_id)
    return project_dto(p)


# ------------------------------------------------------------------------------- catalog


@router.get(
    "/v1/catalog", response_model=CatalogResponse, tags=["catalog"], operation_id="get_catalog"
)
async def get_catalog(ctx: Ctx, session: SystemSession) -> CatalogResponse:
    snap = await ctx.catalogs.require(session)
    c = snap.catalog
    return CatalogResponse(
        digest=snap.digest,
        models=c.models,
        steps=c.steps,
        plots=c.plots,
        generated_at=snap.generated_at,
    )


def _page_list(items: list[Any], limit: int, cursor: str | None) -> tuple[list[Any], str | None]:
    start = int(decode_cursor(cursor) or 0)
    chunk = items[start : start + limit]
    nxt = encode_cursor(start + limit) if start + limit < len(items) else None
    return chunk, nxt


@router.get(
    "/v1/catalog/models",
    response_model=Page[ModelCatalogEntry],
    tags=["catalog"],
    operation_id="list_catalog_models",
)
async def list_catalog_models(
    ctx: Ctx, session: SystemSession, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[ModelCatalogEntry]:
    snap = await ctx.catalogs.require(session)
    items = sorted(snap.catalog.models, key=lambda e: (e.id, Version.parse(e.version)))
    chunk, nxt = _page_list(items, limit, cursor)
    return Page[ModelCatalogEntry](items=chunk, next_cursor=nxt)


@router.get(
    "/v1/catalog/models/{model_id}/{version}",
    response_model=ModelCatalogEntry,
    tags=["catalog"],
    operation_id="get_catalog_model",
)
async def get_catalog_model(
    ctx: Ctx, session: SystemSession, model_id: str, version: str
) -> ModelCatalogEntry:
    snap = await ctx.catalogs.require(session)
    for entry in snap.catalog.models:
        if entry.id == model_id and entry.version == version:
            return entry
    raise NotFoundError("catalog model", f"{model_id}@{version}")


@router.get(
    "/v1/catalog/steps",
    response_model=Page[StepCatalogEntry],
    tags=["catalog"],
    operation_id="list_catalog_steps",
)
async def list_catalog_steps(
    ctx: Ctx, session: SystemSession, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[StepCatalogEntry]:
    snap = await ctx.catalogs.require(session)
    items = sorted(snap.catalog.steps, key=lambda e: (e.id, Version.parse(e.version)))
    chunk, nxt = _page_list(items, limit, cursor)
    return Page[StepCatalogEntry](items=chunk, next_cursor=nxt)


@router.get(
    "/v1/catalog/plots",
    response_model=Page[PlotCatalogEntry],
    tags=["catalog"],
    operation_id="list_catalog_plots",
)
async def list_catalog_plots(
    ctx: Ctx, session: SystemSession, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[PlotCatalogEntry]:
    snap = await ctx.catalogs.require(session)
    items = sorted(snap.catalog.plots, key=lambda e: (e.id, Version.parse(e.version)))
    chunk, nxt = _page_list(items, limit, cursor)
    return Page[PlotCatalogEntry](items=chunk, next_cursor=nxt)


@lru_cache(maxsize=1)
def _schemas() -> dict[str, dict[str, Any]]:
    return export_schemas()


@router.get(
    "/v1/schemas/{name}",
    tags=["catalog"],
    operation_id="get_schema",
    response_model=dict[str, Any],
)
async def get_schema(
    name: Annotated[str, Path(pattern=r"^[A-Za-z0-9_.\-]{1,128}$")],
) -> JSONResponse:
    """An exported JSON Schema, e.g. ``modelspec.json`` (drives Monaco and JSON Forms)."""
    schemas = _schemas()
    if name not in schemas:
        raise NotFoundError("schema", name)
    return JSONResponse(schemas[name], media_type="application/schema+json")
