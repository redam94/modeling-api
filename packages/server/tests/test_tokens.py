"""API tokens: minting (secret once, HMAC at rest), bearer authentication, expiry and
revocation, project binding, role narrowing, request-rate limits, and scope enforcement on
every project-, tenant- and platform-scoped endpoint of :data:`mapi_spec.api.ENDPOINTS`."""

from __future__ import annotations

import re
from datetime import timedelta

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import (
    grant_role,
    make_tenant,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_spec.api import (
    ENDPOINTS,
    TOKEN_TENANT_ROUTES,
    ApiToken,
    Me,
    Page,
    RouteScope,
    TokenCreated,
    route_access,
)
from mapi_spec.tenancy import PROJECT_HEADER
from mapi_spec.tokens import TOKEN_ALPHABET, generate_token, scope_allows
from sqlalchemy import select

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


async def _acme(ctx: ServerContext, role: str = "admin") -> m.User:
    await make_tenant(ctx, "acme", projects=("growth", "other"))
    ana = await make_user(ctx, "ana@acme.test")
    await grant_role(ctx, ana, "acme", "growth", role)
    return ana


async def test_mint_list_use_and_revoke(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    ana = await _acme(ctx)
    web = await session_caller(ctx, client, ana, tenant_id="acme", project_id="growth")
    r = await web.post(
        "/v1/tokens", json={"name": "ci", "scopes": ["models:read"], "expires_in_days": 7}
    )
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    created = TokenCreated.model_validate(r.json())
    assert "secret" not in repr(created) or "<redacted>" in repr(created)
    secret = created.secret
    assert created.token.display == f"mapi_{created.token.prefix}_…"
    listing = Page[ApiToken].model_validate((await web.get("/v1/tokens")).json())
    assert [t.id for t in listing.items] == [created.token.id]
    assert secret not in (await web.get("/v1/tokens")).text
    assert secret not in (await web.get(f"/v1/tokens/{created.token.id}")).text
    async with ctx.db.session() as s:
        row = await s.get(m.ApiToken, created.token.id)
        assert row is not None and re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", row.secret_hash)
        assert secret.split("_")[2] not in str(row.__dict__)

    tok = token_caller(client, secret)
    me = Me.model_validate((await tok.get("/v1/me")).json())
    assert me.auth == "token" and me.token is not None and me.token.id == created.token.id
    assert [(t.tenant.id, [p.project_id for p in t.projects]) for t in me.tenants] == [
        ("acme", ["growth"])
    ]
    assert (await tok.get("/v1/datasets")).status_code == 200  # the token's own project
    projects = (await tok.get("/v1/projects")).json()["items"]
    assert [(p["tenant_id"], p["id"]) for p in projects] == [("acme", "growth")]
    assert len((await web.get("/v1/projects")).json()["items"]) == 1  # ana: growth only
    r = await tok.post("/v1/datasets", json={"name": "x"})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"  # models:read only
    r = await tok.get("/v1/datasets", headers={PROJECT_HEADER: "acme/other"})
    assert r.status_code == 403 and "different project" in r.json()["detail"]

    assert (await web.delete(f"/v1/tokens/{created.token.id}")).status_code == 204
    assert (await web.delete(f"/v1/tokens/{created.token.id}")).status_code == 204  # idempotent
    r = await tok.get("/v1/datasets")
    assert r.status_code == 401 and "revoked" in r.json()["detail"]
    async with ctx.db.session() as s:
        actions = [e.action for e in (await s.execute(select(m.AuditEvent))).scalars()]
    assert {"token.create", "token.revoke", "token.auth_failed"} <= set(actions)


async def test_malformed_wrong_expired_and_orphaned_tokens(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ana = await _acme(ctx)
    secret = await make_token(ctx, ana, "acme", "growth", ["models:read"])
    ok = token_caller(client, secret)
    assert (await ok.get("/v1/datasets")).status_code == 200
    # one changed character: caught by the checksum, no database lookup needed
    i = 12
    typo = secret[:i] + ("a" if secret[i] != "a" else "b") + secret[i + 1 :]
    r = await token_caller(client, typo).get("/v1/datasets")
    assert r.status_code == 401 and r.json()["detail"] == "malformed API token"
    assert typo not in r.text and secret not in r.text
    # a well-formed token with the right prefix but another secret
    forged = str(generate_token(prefix=secret.split("_")[1]))
    r = await token_caller(client, forged).get("/v1/datasets")
    assert r.status_code == 401 and r.json()["detail"] == "the API token is invalid"
    # unknown prefix
    r = await token_caller(client, str(generate_token())).get("/v1/datasets")
    assert r.status_code == 401
    # expired
    old = await make_token(ctx, ana, "acme", "growth", ["models:read"],
                           expires_in=timedelta(milliseconds=-500))  # fmt: skip
    r = await token_caller(client, old).get("/v1/datasets")
    assert r.status_code == 401 and "expired" in r.json()["detail"]
    # other schemes are refused rather than ignored
    r = await client.get("/v1/datasets", headers={"Authorization": "Basic YTpi"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    # the owner loses the project: the token loses it too (it acts as its owner *now*)
    async with ctx.db.session() as s:
        for row in (await s.execute(select(m.Membership))).scalars():
            await s.delete(row)
    r = await ok.get("/v1/datasets")
    assert r.status_code == 404
    async with ctx.db.session() as s:
        failed = [
            e
            for e in (await s.execute(select(m.AuditEvent))).scalars()
            if e.action == "token.auth_failed"
        ]
    assert {e.details["reason"] for e in failed} >= {"invalid", "expired"}
    assert all(e.tenant_id == "acme" and e.ip_hash for e in failed)


async def test_scopes_never_widen_the_owners_role(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    viewer = await _acme(ctx, role="viewer")
    secret = await make_token(ctx, viewer, "acme", "growth", ["admin"])
    tok = token_caller(client, secret)
    assert (await tok.get("/v1/runs")).status_code == 200
    r = await tok.post("/v1/datasets", json={"name": "x"})
    assert r.status_code == 403  # a viewer's token cannot write, whatever its scopes
    r = await tok.post("/v1/runs", json={"kind": "fit", "spec_id": "spc_x", "revision": 1})
    assert r.status_code == 403


async def test_token_rate_limit(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    ana = await _acme(ctx)
    secret = await make_token(ctx, ana, "acme", "growth", ["models:read"],
                              rate_limit_per_minute=3)  # fmt: skip
    tok = token_caller(client, secret)
    codes = [(await tok.get("/v1/datasets")).status_code for _ in range(3)]
    assert codes == [200, 200, 200]
    r = await tok.get("/v1/datasets")
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED"
    assert 1 <= int(r.headers["retry-after"]) <= 60
    # other principals are not affected by this token's budget
    web = await session_caller(ctx, client, ana, tenant_id="acme", project_id="growth")
    assert (await web.get("/v1/datasets")).status_code == 200


async def test_tokens_cannot_mint_tokens(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    """A child token would drop its parent's budget, rate limit and expiry and outlive its
    revocation: even an ``admin`` token cannot mint one."""
    ana = await _acme(ctx)
    secret = await make_token(ctx, ana, "acme", "growth", ["admin"],
                              rate_limit_per_minute=5, vcpu_seconds_budget=10.0,
                              expires_in=timedelta(hours=1))  # fmt: skip
    tok = token_caller(client, secret)
    r = await tok.post(
        "/v1/tokens", json={"name": "child", "scopes": ["admin"], "expires_in_days": 365}
    )
    assert r.status_code == 403, r.text
    async with ctx.db.session() as s:
        assert len(list((await s.execute(select(m.ApiToken))).scalars())) == 1
        denied = [
            e for e in (await s.execute(select(m.AuditEvent))).scalars()
            if e.action == "token.create"
        ]  # fmt: skip
    assert [e.outcome for e in denied] == ["denied"]
    # the token may still list and revoke tokens; a session still mints
    assert (await tok.get("/v1/tokens")).status_code == 200
    web = await session_caller(ctx, client, ana, tenant_id="acme", project_id="growth")
    r = await web.post("/v1/tokens", json={"name": "ci", "scopes": ["models:read"]})
    assert r.status_code == 201, r.text


async def test_project_bound_token_cannot_administer_the_tenant(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    """A token is bound to one project: even an ``admin`` token of a tenant admin cannot add
    tenant admins, delete other projects or read tenant-wide audit and billing."""
    owner = await make_user(ctx, "owner@acme.test")
    await make_tenant(ctx, "acme", projects=("growth", "secret"))
    await grant_role(ctx, owner, "acme", None, "admin")
    tok = token_caller(client, await make_token(ctx, owner, "acme", "growth", ["admin"]))
    r = await tok.post("/v1/tenants/acme/members", json={"email": "evil@x.test", "role": "admin"})
    assert r.status_code == 403, r.text
    assert "signed-in session" in r.json()["detail"]
    assert (await tok.delete("/v1/tenants/acme/projects/secret")).status_code == 403
    assert (await tok.get("/v1/tenants/acme/audit")).status_code == 403
    assert (await tok.get("/v1/tenants/acme/usage")).status_code == 403
    assert (await tok.get("/v1/tenants/acme/members")).status_code == 403
    assert (await tok.patch("/v1/tenants/acme", json={"name": "x"})).status_code == 403
    # read-only views of its own tenant stay open, narrowed to the token's project
    assert (await tok.get("/v1/tenants/acme")).status_code == 200
    r = await tok.get("/v1/tenants/acme/projects")
    assert r.status_code == 200 and [p["id"] for p in r.json()["items"]] == ["growth"]
    async with ctx.db.session() as s:
        users = [u.email for u in (await s.execute(select(m.User))).scalars()]
        projects = [p.id for p in (await s.execute(select(m.Project))).scalars()]
    assert "evil@x.test" not in users and "secret" in projects
    # the same person in a browser session still can
    web = await session_caller(ctx, client, owner)
    assert (await web.get("/v1/tenants/acme/audit")).status_code == 200


# =================================================================== scopes on every endpoint

_PARAMS = {
    "tenant_id": "acme",
    "project_id": "growth",
    "token_id": "tok_" + "0" * 26,
    "principal_id": "usr_" + "0" * 26,
    "version": "1",
    "revision": "1",
    "view": "preview",
    "scope": "tenant",
    "subject_id": "acme",
    "name": "executor-x",
}


def _path(template: str) -> str:
    return re.sub(r"\{(\w+)(?::path)?\}", lambda mo: _PARAMS.get(mo[1], "missing"), template)


_SCOPES = ["models:read", "runs:create", "datasets:write", "plugins:publish", "score:*", "admin"]


@pytest.mark.parametrize("scope", _SCOPES)
async def test_token_scopes_gate_every_endpoint(
    ctx: ServerContext, client: httpx.AsyncClient, scope: str
) -> None:
    """Every operation is refused (403) unless the token's scope covers its permission (and
    the owner is a tenant admin, so only the scope decides). Platform operations and tenant
    administration are never open to tokens; public and "any principal" routes never answer
    403."""
    owner = await make_user(ctx, "owner@acme.test")
    await make_tenant(ctx, "acme", projects=("growth", "scratch"))
    await grant_role(ctx, owner, "acme", None, "admin")
    tok = token_caller(client, await make_token(ctx, owner, "acme", "growth", [scope]))
    checked = 0
    for e in ENDPOINTS:
        access = route_access(e.operation_id)
        if access.scope in (RouteScope.PUBLIC, RouteScope.INTERNAL):
            continue
        body = {} if e.method in ("POST", "PUT", "PATCH") else None
        path = _path(e.path)
        r = await tok.request(e.method, path, json=body)
        if access.scope is RouteScope.PLATFORM:
            assert r.status_code == 403, (scope, e.operation_id, r.status_code)
            continue
        if access.scope is RouteScope.TENANT and e.operation_id not in TOKEN_TENANT_ROUTES:
            assert r.status_code == 403, (scope, e.operation_id, r.status_code)
            checked += 1
            continue
        if access.resource is None or access.action is None:
            assert r.status_code != 403, (scope, e.operation_id, r.text)
            continue
        needs = [(access.resource, access.action)]
        if e.operation_id != "create_run":  # its secondary check depends on the body
            needs += list(access.also)
        allowed = all(scope_allows([scope], a.value, res.value) for res, a in needs)
        if allowed:
            assert r.status_code != 403, (scope, e.operation_id, r.text)
        else:
            assert r.status_code == 403, (scope, e.operation_id, r.status_code, r.text)
        checked += 1
    assert checked >= 50


def test_token_alphabet_matches_ids() -> None:
    assert set(TOKEN_ALPHABET) <= set("0123456789abcdefghjkmnpqrstvwxyz")
