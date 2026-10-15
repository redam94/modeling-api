"""Tenancy on *every* route (M1 and M2 alike), driven by the server's own route table.

For each route the app serves, by its access rule (:data:`mapi_spec.api.ROUTE_ACCESS` plus
:data:`mapi_server.auth.request.SERVER_ROUTE_ACCESS`):

* without credentials, a multi-tenant server answers 401 unless the route is public or
  authenticates itself (``/internal/events``, ``/metrics``);
* a signed-in member of tenant ``alpha`` naming tenant ``beta``'s project (header, path or
  token binding) never gets a 2xx: the tenant is unknown to them (404) or the action is not
  theirs (403), before any handler code runs;
* an API token bound to ``alpha/main`` cannot be pointed at another project.

New routes are covered automatically: a route without a rule cannot even start the app.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from mapi_server.app import create_app, iter_api_routes
from mapi_server.auth.request import access_for
from mapi_server.context import ServerContext
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import (
    grant_role,
    make_tenant,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_spec.api import RouteScope

pytestmark = pytest.mark.integration

_PARAM = re.compile(r"\{([a-z_]+)(?::[a-z]+)?\}")
_VALUES = {
    "tenant_id": "beta",
    "project_id": "main",
    "scope": "tenant",
    "subject_id": "beta",
    "version": "1",
    "revision": "1",
    "name": "x",
    "path": "x.json",
    "alias": "x",
}
SELF_AUTHENTICATED = frozenset({"ingest_event", "metrics"})


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


def _routes(app: FastAPI) -> Iterator[tuple[str, str, str]]:
    for route, template in iter_api_routes(app):
        op = route.operation_id or route.name
        for method in sorted(route.methods or ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            path = _PARAM.sub(lambda mt: _VALUES.get(mt.group(1), "zz_1"), template)
            yield op, method, path


def _app(settings: Settings, ctx: ServerContext) -> FastAPI:
    return create_app(settings, ctx=ctx, manage_context=False)


async def test_every_non_public_route_requires_credentials(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    checked = 0
    for op, method, path in _routes(_app(ctx.settings, ctx)):
        access = access_for(op)
        if access.scope is RouteScope.PUBLIC or op in SELF_AUTHENTICATED:
            continue
        r = await client.request(method, path, json={})
        assert r.status_code == 401, f"{method} {path} ({op}) -> {r.status_code}"
        checked += 1
    assert checked > 60  # every M1 and M2 route that needs a caller


async def test_no_route_serves_another_tenants_project(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await make_tenant(ctx, "alpha")
    await make_tenant(ctx, "beta")
    ana = await make_user(ctx, "ana@alpha.test")
    await grant_role(ctx, ana, "alpha", None, "admin")  # the strongest tenant role
    in_beta = (await session_caller(ctx, client, ana)).in_project("beta", "main")
    secret = await make_token(ctx, ana, "alpha", "main", ["admin"])
    bound = token_caller(client, secret, **{"X-Mapi-Project": "beta/main"})
    checked = 0
    for op, method, path in _routes(_app(ctx.settings, ctx)):
        access = access_for(op)
        if access.scope not in (RouteScope.PROJECT, RouteScope.TENANT):
            continue
        for who, caller in (("session", in_beta), ("token", bound)):
            r = await caller.request(method, path, json={})
            assert r.status_code in (403, 404), (
                f"{who}: {method} {path} ({op}) reached beta -> {r.status_code} {r.text[:200]}"
            )
        checked += 1
    assert checked > 40
