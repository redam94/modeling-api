"""``/v1/tenants/{tenant_id}/samples``: one-click sample projects (v0.18 D20).

Installing needs an editor somewhere in the tenant (it spends compute) and makes the
``sample`` project (``sample-2`` when that id is taken) with a ``runner`` grant for the
installer; a second install joins (a project grant of the caller's best role, capped at
``runner``). Two installs at once make one row. Retry is for the installer or a tenant admin,
within the attempt limit; retire (tenant admins) removes the sample's grants. Samples turned
off: an empty list and ``SAMPLES_DISABLED``. Every action is audited.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.app import create_app
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.samples import SAMPLE_GRANT_BY
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import (
    grant_role,
    make_tenant,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_spec.audit import AuditAction
from mapi_spec.samples import SampleInfo
from sqlalchemy import select, update

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

BASE = "/v1/tenants/acme/samples"
GEO = f"{BASE}/geo_mmm"
GUIDED_CATALOG = Path(__file__).parent / "fixtures" / "catalog_guided.json"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"profile": Profile.MULTI_TENANT, "catalog_path": GUIDED_CATALOG}
    )


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
        yield c


async def _people(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    await make_tenant(ctx, "acme", projects=("growth",))
    await make_tenant(ctx, "other", projects=("main",))
    out: dict[str, Any] = {}
    for role in ("viewer", "editor", "runner"):
        u = await make_user(ctx, f"{role}@acme.test")
        await grant_role(ctx, u, "acme", "growth", role)
        out[role] = await session_caller(ctx, client, u)
        out[f"{role}_user"] = u
    admin = await make_user(ctx, "admin@acme.test")
    await grant_role(ctx, admin, "acme", None, "admin")
    out["admin"] = await session_caller(ctx, client, admin)
    out["admin_user"] = admin
    outsider = await make_user(ctx, "someone@other.test")
    await grant_role(ctx, outsider, "other", "main", "editor")
    out["outsider"] = await session_caller(ctx, client, outsider)
    return out


def _info(r: httpx.Response, status: int = 200) -> SampleInfo:
    assert r.status_code == status, r.text
    return SampleInfo.model_validate(r.json())


def _code(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    assert r.json()["code"] == code, r.text


async def _sample_row(ctx: ServerContext, tenant_id: str = "acme") -> m.TenantSample | None:
    async with ctx.db.session() as s:
        return (
            await s.execute(select(m.TenantSample).where(m.TenantSample.tenant_id == tenant_id))
        ).scalar_one_or_none()


async def _grants(ctx: ServerContext, project_id: str) -> dict[str, tuple[str, str | None]]:
    async with ctx.db.session() as s:
        rows = (
            await s.execute(
                select(m.Membership).where(
                    m.Membership.tenant_id == "acme", m.Membership.project_id == project_id
                )
            )
        ).scalars()
        return {r.principal_id: (r.role, r.created_by) for r in rows}


async def _audits(ctx: ServerContext, action: AuditAction) -> list[m.AuditEvent]:
    async with ctx.db.session() as s:
        return list(
            (
                await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == action.value))
            ).scalars()
        )


async def test_the_list_offers_install_to_editors_only(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    r = await who["editor"].get(BASE)
    assert r.status_code == 200, r.text
    [geo] = [SampleInfo.model_validate(x) for x in r.json()]
    assert geo.id == "geo_mmm" and geo.state is None and geo.can_install
    assert not geo.can_retry and not geo.can_retire and not geo.hidden
    [geo] = [SampleInfo.model_validate(x) for x in (await who["viewer"].get(BASE)).json()]
    assert not geo.can_install  # a viewer joins an installed sample, never installs one
    [geo] = [SampleInfo.model_validate(x) for x in (await who["admin"].get(BASE)).json()]
    assert geo.can_install and not geo.can_retire  # nothing to retire yet
    # people of another tenant and API tokens see nothing
    assert (await who["outsider"].get(BASE)).status_code in (403, 404)
    secret = await make_token(ctx, who["editor_user"], "acme", "growth", ["models:read"])
    assert (await token_caller(client, secret).get(BASE)).status_code in (401, 403)


async def test_a_viewer_cannot_install(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    who = await _people(ctx, client)
    _code(await who["viewer"].post(f"{GEO}:install"), 403, "FORBIDDEN")
    assert await _sample_row(ctx) is None
    _code(await who["editor"].post(f"{BASE}/no_such:install"), 404, "NOT_FOUND")


async def test_install_makes_the_project_and_a_runner_grant_then_others_join(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    info = _info(await who["editor"].post(f"{GEO}:install"), 202)
    assert info.state == "provisioning" and info.project_id == "sample" and info.attempts == 1
    assert not info.can_install  # the installer is a member now
    async with ctx.db.session() as s:
        project = await s.get(m.Project, ("acme", "sample"))
        assert project is not None and project.kind == "sample"
        assert project.name == "Sample: marketing mix"
    row = await _sample_row(ctx)
    assert row is not None and row.created_by == who["editor_user"].id
    grants = await _grants(ctx, "sample")
    assert grants == {who["editor_user"].id: ("runner", SAMPLE_GRANT_BY)}
    [installed] = await _audits(ctx, AuditAction.SAMPLE_INSTALL)
    assert installed.actor_id == who["editor_user"].id and installed.project_id == "sample"
    [added] = await _audits(ctx, AuditAction.MEMBER_ADD)
    assert added.details["sample"] == "geo_mmm" and added.details["role"] == "runner"

    # a viewer joins as a viewer (no second install, no second fit)
    joined = _info(await who["viewer"].post(f"{GEO}:install"), 200)
    assert joined.project_id == "sample" and joined.state == "provisioning"
    # a runner joins as a runner; a tenant admin already sees the project (no grant)
    _info(await who["runner"].post(f"{GEO}:install"), 200)
    admin_view = _info(await who["admin"].post(f"{GEO}:install"), 200)
    assert admin_view.project_id == "sample" and admin_view.can_retire
    grants = await _grants(ctx, "sample")
    assert grants[who["viewer_user"].id] == ("viewer", SAMPLE_GRANT_BY)
    assert grants[who["runner_user"].id] == ("runner", SAMPLE_GRANT_BY)
    assert who["admin_user"].id not in grants
    joins = await _audits(ctx, AuditAction.SAMPLE_JOIN)
    assert sorted(j.details["role"] for j in joins) == ["runner", "viewer"]
    # joining again changes nothing
    _info(await who["viewer"].post(f"{GEO}:install"), 200)
    assert len(await _audits(ctx, AuditAction.SAMPLE_JOIN)) == 2
    assert len(await _audits(ctx, AuditAction.SAMPLE_INSTALL)) == 1
    # another tenant has no sample
    async with ctx.db.session() as s:
        assert (
            await s.execute(select(m.TenantSample).where(m.TenantSample.tenant_id == "other"))
        ).first() is None


async def test_an_existing_sample_project_id_moves_the_sample_to_sample_2(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await make_tenant(ctx, "acme", projects=("growth", "sample"))
    who = await _people(ctx, client)
    info = _info(await who["editor"].post(f"{GEO}:install"), 202)
    assert info.project_id == "sample-2"
    async with ctx.db.session() as s:
        mine = await s.get(m.Project, ("acme", "sample"))
        assert mine is not None and mine.kind == "standard"  # untouched


async def test_two_installs_at_once_make_one_row(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    other_editor = await make_user(ctx, "editor2@acme.test")
    await grant_role(ctx, other_editor, "acme", "growth", "editor")
    second = await session_caller(ctx, client, other_editor)
    a, b = await asyncio.gather(who["editor"].post(f"{GEO}:install"), second.post(f"{GEO}:install"))
    assert sorted((a.status_code, b.status_code)) == [200, 202], (a.text, b.text)
    async with ctx.db.session() as s:
        rows = list((await s.execute(select(m.TenantSample))).scalars())
        projects = list(
            (await s.execute(select(m.Project).where(m.Project.kind == "sample"))).scalars()
        )
    assert len(rows) == 1 and len(projects) == 1
    assert len(await _audits(ctx, AuditAction.SAMPLE_INSTALL)) == 1
    assert set(await _grants(ctx, "sample")) == {who["editor_user"].id, other_editor.id}


async def test_retire_hides_the_sample_and_removes_its_grants(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    _info(await who["editor"].post(f"{GEO}:install"), 202)
    _info(await who["viewer"].post(f"{GEO}:install"), 200)
    # a grant someone gave by hand in the sample project stays
    await grant_role(ctx, who["runner_user"], "acme", "sample", "editor")
    _code(await who["editor"].post(f"{GEO}:retire"), 403, "FORBIDDEN")
    info = _info(await who["admin"].post(f"{GEO}:retire"))
    assert info.state == "retired" and not info.can_retire and not info.can_install
    assert await _grants(ctx, "sample") == {who["runner_user"].id: ("editor", None)}
    async with ctx.db.session() as s:
        project = await s.get(m.Project, ("acme", "sample"))
        assert project is not None and project.kind == "sample_retired"  # never deleted
    [retired] = await _audits(ctx, AuditAction.SAMPLE_RETIRE)
    assert retired.details["grants_removed"] == 2
    assert len(await _audits(ctx, AuditAction.MEMBER_REMOVE)) == 2
    # retired is final: no join, no install, retiring again changes nothing
    _code(await who["editor"].post(f"{GEO}:install"), 409, "CONFLICT")
    assert _info(await who["admin"].post(f"{GEO}:retire")).state == "retired"
    assert len(await _audits(ctx, AuditAction.SAMPLE_RETIRE)) == 1


async def test_retry_is_for_the_installer_or_a_tenant_admin_within_the_limit(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    _info(await who["editor"].post(f"{GEO}:install"), 202)
    _code(await who["editor"].post(f"{GEO}:retry"), 409, "CONFLICT")  # not failed
    problem = {"type": "about:blank", "title": "Quota exceeded", "status": 429,
               "code": "QUOTA_EXCEEDED", "detail": "too many runs"}  # fmt: skip
    async with ctx.db.session() as s:
        await s.execute(update(m.TenantSample).values(state="failed", problem=problem))
    [geo] = [SampleInfo.model_validate(x) for x in (await who["editor"].get(BASE)).json()]
    assert geo.state == "failed" and geo.can_retry
    assert geo.problem is not None and geo.problem.code == "QUOTA_EXCEEDED"
    _info(await who["viewer"].post(f"{GEO}:install"), 200)
    _code(await who["viewer"].post(f"{GEO}:retry"), 403, "FORBIDDEN")

    again = _info(await who["editor"].post(f"{GEO}:retry"), 202)
    assert again.state == "provisioning" and again.attempts == 2 and again.problem is None
    [retried] = await _audits(ctx, AuditAction.SAMPLE_RETRY)
    assert retried.details["attempt"] == 2

    # a tenant admin may retry too and becomes the installer; the third attempt is the last
    async with ctx.db.session() as s:
        await s.execute(update(m.TenantSample).values(state="failed", problem=problem))
    _info(await who["admin"].post(f"{GEO}:retry"), 202)
    row = await _sample_row(ctx)
    assert row is not None and row.attempts == 3 and row.created_by == who["admin_user"].id
    async with ctx.db.session() as s:
        await s.execute(update(m.TenantSample).values(state="failed", problem=problem))
    _code(await who["admin"].post(f"{GEO}:retry"), 409, "CONFLICT")
    [geo] = [SampleInfo.model_validate(x) for x in (await who["admin"].get(BASE)).json()]
    assert not geo.can_retry and geo.can_retire


async def test_hidden_samples_come_from_the_onboarding_state(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    who = await _people(ctx, client)
    r = await who["viewer"].patch(
        "/v1/tenants/acme/me/onboarding", json={"hide_samples": ["geo_mmm"]}
    )
    assert r.status_code == 200, r.text
    [geo] = [SampleInfo.model_validate(x) for x in (await who["viewer"].get(BASE)).json()]
    assert geo.hidden
    [geo] = [SampleInfo.model_validate(x) for x in (await who["editor"].get(BASE)).json()]
    assert not geo.hidden  # per person


@pytest.mark.parametrize("action", [":install", ":retry", ":retire"])
async def test_samples_turned_off_list_nothing_and_refuse_every_action(
    ctx: ServerContext, client: httpx.AsyncClient, action: str
) -> None:
    who = await _people(ctx, client)
    ctx.settings = ctx.settings.model_copy(update={"samples_enabled": False})
    r = await who["admin"].get(BASE)
    assert r.status_code == 200 and r.json() == []
    _code(await who["admin"].post(f"{GEO}{action}"), 404, "SAMPLES_DISABLED")
    assert await _sample_row(ctx) is None
