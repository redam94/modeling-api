"""Identity builders for multi-tenant tests: tenants, users, grants, sessions and tokens.

Rows are written through the platform role (like the identity service does); requests then
go through the real authentication dependency.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from mapi_server.auth.identity import create_session
from mapi_server.auth.request import CSRF_HEADER, session_cookie
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import now
from mapi_server.services.tenancy import ensure_platform_tenant, grant
from mapi_spec.api import new_id
from mapi_spec.tenancy import PROJECT_HEADER, ProjectRole
from mapi_spec.tokens import generate_token, hash_token
from sqlalchemy.dialects.postgresql import insert

TEST_ISSUER = "https://idp.test"


async def make_tenant(
    ctx: ServerContext,
    tenant_id: str,
    *,
    tier: str = "verified",
    projects: Sequence[str] = ("main",),
    status: str = "active",
) -> None:
    async with ctx.db.session() as s:
        await s.execute(
            insert(m.Tenant)
            .values(
                id=tenant_id,
                name=tenant_id.title(),
                trust_tier=tier,
                status=status,
                data_residency="us",
            )
            .on_conflict_do_nothing()
        )
        for p in projects:
            await ensure_platform_tenant(s, tenant_id, p)


async def make_user(ctx: ServerContext, email: str, *, operator: bool = False) -> m.User:
    async with ctx.db.session() as s:
        user = m.User(
            id=new_id("usr"),
            issuer=TEST_ISSUER,
            subject=email,
            email=email,
            email_verified=True,
            name=email.split("@")[0].title(),
            operator=operator,
            status="active",
            created_at=now(),
        )
        s.add(user)
    return user


async def grant_role(
    ctx: ServerContext,
    user: m.User,
    tenant_id: str,
    project_id: str | None,
    role: ProjectRole | str,
) -> None:
    async with ctx.db.session() as s:
        await grant(
            s, tenant_id=tenant_id, project_id=project_id, user=user, role=ProjectRole(role)
        )


async def make_token(
    ctx: ServerContext,
    user: m.User,
    tenant_id: str,
    project_id: str,
    scopes: Sequence[str],
    *,
    rate_limit_per_minute: int | None = None,
    vcpu_seconds_budget: float | None = None,
    budget_period: str = "month",
    expires_in: timedelta = timedelta(days=30),
    revoked: bool = False,
) -> str:
    """Insert a token row and return the secret."""
    parsed = generate_token()
    ts = now()
    async with ctx.db.session() as s:
        s.add(
            m.ApiToken(
                id=new_id("tok"),
                tenant_id=tenant_id,
                project_id=project_id,
                owner_id=user.id,
                owner_kind="user",
                name="test",
                prefix=parsed.prefix,
                secret_hash=hash_token(parsed, ctx.secrets.token_pepper),
                scopes=list(scopes),
                rate_limit_per_minute=rate_limit_per_minute,
                vcpu_seconds_budget=vcpu_seconds_budget,
                budget_period=budget_period,
                created_at=ts - timedelta(seconds=1),
                expires_at=ts + expires_in,
                revoked_at=ts if revoked else None,
            )
        )
    return str(parsed)


@dataclass
class Caller:
    """An HTTP client view that authenticates as one principal in one project."""

    client: httpx.AsyncClient
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)

    def _merge(self, kw: dict[str, Any]) -> dict[str, Any]:
        headers = {**self.headers, **(kw.pop("headers", None) or {})}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        kw["headers"] = headers
        return kw

    async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        return await self.client.request(method, url, **self._merge(kw))

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("POST", url, **kw)

    async def put(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("PUT", url, **kw)

    async def patch(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kw)

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kw)

    def in_project(self, tenant_id: str, project_id: str) -> Caller:
        return Caller(
            self.client,
            {**self.headers, PROJECT_HEADER: f"{tenant_id}/{project_id}"},
            dict(self.cookies),
        )


async def session_caller(
    ctx: ServerContext,
    client: httpx.AsyncClient,
    user: m.User,
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    csrf: bool = True,
) -> Caller:
    """A browser-session caller (cookie + CSRF header) for ``user``."""
    async with ctx.db.session() as s:
        row = await s.get(m.User, user.id)
        assert row is not None
        value, sess = await create_session(s, row)
        token = sess.csrf_token
    headers: dict[str, str] = {}
    if csrf:
        headers[CSRF_HEADER] = token
    if tenant_id is not None and project_id is not None:
        headers[PROJECT_HEADER] = f"{tenant_id}/{project_id}"
    cookie = session_cookie(ctx.settings.secure_cookies).name
    return Caller(client, headers, {cookie: value})


def token_caller(client: httpx.AsyncClient, secret: str, **headers: str) -> Caller:
    return Caller(client, {"Authorization": f"Bearer {secret}", **headers})
