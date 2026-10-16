"""Guided mode across tenants and roles: the persona lock (admins lock; members below admin
then store Guided specs only), role detection never reading another tenant's dataset
profiles (API and row-level security), and token scopes on the guided operations."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from mapi_spec.api import new_id
from sqlalchemy import select

pytestmark = pytest.mark.integration

GUIDED_CATALOG = Path(__file__).parent / "fixtures" / "catalog_guided.json"

COLUMNS = [
    {"name": "Week", "dtype": "date", "n_rows": 520, "n_distinct": 104, "detected_freq": "W-SUN"},
    {"name": "Region", "dtype": "string", "n_rows": 520, "n_distinct": 5},
    {"name": "Revenue", "dtype": "number", "n_rows": 520, "min": 10.0},
    {"name": "TV_Spend", "dtype": "number", "n_rows": 520, "min": 0.0},
]
ANSWERS = {
    "template_id": "geo_mmm",
    "name": "eu-geo-mmm",
    "dataset": "media",
    "answers": {"kpi": "Revenue", "date": "Week", "geo": "Region", "spend": ["TV_Spend"]},
    "columns": COLUMNS,
}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"profile": Profile.MULTI_TENANT, "catalog_path": GUIDED_CATALOG}
    )


async def _people(ctx: ServerContext, client: httpx.AsyncClient) -> tuple[Any, Any]:
    await make_tenant(ctx, "acme", projects=("growth",))
    ada = await make_user(ctx, "ada@acme.test")
    eve = await make_user(ctx, "eve@acme.test")
    await grant_role(ctx, ada, "acme", "growth", "admin")
    await grant_role(ctx, eve, "acme", "growth", "editor")
    admin = await session_caller(ctx, client, ada, tenant_id="acme", project_id="growth")
    editor = await session_caller(ctx, client, eve, tenant_id="acme", project_id="growth")
    return admin, editor


def _yaml(doc: dict[str, Any]) -> str:
    return json.dumps(doc)  # JSON is YAML 1.2


async def test_the_guided_lock(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    admin, editor = await _people(ctx, client)
    project = "/v1/tenants/acme/projects/growth"
    assert (await editor.patch(project, json={"guided_locked": True})).status_code == 403
    r = await admin.patch(project, json={"guided_locked": True})
    assert r.status_code == 200 and r.json()["guided_locked"] is True
    assert (await editor.get("/v1/projects/growth")).json()["guided_locked"] is True
    async with ctx.db.session() as s:
        actions = [
            (e.action, e.details)
            for e in (await s.execute(select(m.AuditEvent))).scalars()
            if e.action == "project.guided_lock"
        ]
    assert actions == [("project.guided_lock", {"locked": True})]

    # a Guided draft is fine for the editor ...
    draft = (await editor.post("/v1/guided/drafts", json=ANSWERS)).json()
    saved = await editor.post("/v1/specs", json={"yaml": draft["yaml"]})
    assert saved.status_code == 201, saved.text
    spec_id = saved.json()["id"]
    basic = copy.deepcopy(draft["spec"])
    basic["model"]["settings"]["pooling"] = "complete"
    rev = await editor.post(
        f"/v1/specs/{spec_id}/revisions", json={"yaml": _yaml(basic), "parent_revision": 1}
    )
    assert rev.status_code == 201, rev.text

    # ... a spec that is not Guided, or a changed advanced setting, is not
    plain = copy.deepcopy(draft["spec"])
    plain["metadata"] = {"name": "expert-spec", "project": "growth"}
    r = await editor.post("/v1/specs", json={"yaml": _yaml(plain)})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    assert r.json()["errors"][0]["pointer"] == "/metadata/labels/guided"
    fit = copy.deepcopy(basic)
    fit["model"]["fit"] = {"mode": "evaluate_then_refit", "draws": 50}
    body = {"yaml": _yaml(fit), "parent_revision": 2}
    r = await editor.post(f"/v1/specs/{spec_id}/revisions", json=body)
    assert r.status_code == 403 and [e["pointer"] for e in r.json()["errors"]] == ["/model/fit"]
    assert r.json()["errors"][0]["suggested_action"]
    # admins are not held by the lock
    assert (await admin.post(f"/v1/specs/{spec_id}/revisions", json=body)).status_code == 201
    assert (await admin.post("/v1/specs", json={"yaml": _yaml(plain)})).status_code == 201
    # validation is never refused
    v = await editor.post("/v1/specs:validate", json={"document": plain})
    assert v.status_code == 200

    # unlocked again: the editor writes any spec
    assert (await admin.patch(project, json={"guided_locked": False})).status_code == 200
    plain["metadata"]["name"] = "expert-spec-2"
    assert (await editor.post("/v1/specs", json={"yaml": _yaml(plain)})).status_code == 201


async def test_creating_a_locked_project(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    await make_tenant(ctx, "acme", projects=("growth",))
    ada = await make_user(ctx, "ada@acme.test")
    await grant_role(ctx, ada, "acme", None, "admin")
    admin = await session_caller(ctx, client, ada)
    r = await admin.post(
        "/v1/tenants/acme/projects",
        json={"id": "analysts", "name": "Analysts", "guided_locked": True},
    )
    assert r.status_code == 201 and r.json()["guided_locked"] is True


# ================================================================================= isolation


async def _dataset(ctx: ServerContext, tenant: str, columns: list[dict[str, Any]]) -> None:
    async with ctx.db.session() as s:  # the platform role, as the finalizer writes
        ds = m.Dataset(
            id=new_id("ds"), tenant_id=tenant, project_id="main", name="media", labels={},
            latest_version=1, next_version=2,
        )  # fmt: skip
        s.add(ds)
        await s.flush()
        s.add(
            m.DatasetVersion(
                dataset_id=ds.id, version=1, tenant_id=tenant, project_id="main",
                status="ready", sha256="sha256:" + tenant[0] * 64, size_bytes=10,
                data_schema={"dims": {}, "vars": {}}, objects={}, issues=[], columns=columns,
                ready_at=datetime.now(UTC),
            )
        )  # fmt: skip


async def test_detection_reads_only_the_callers_tenant(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await make_tenant(ctx, "alpha")
    await make_tenant(ctx, "beta")
    await _dataset(ctx, "alpha", [*COLUMNS[:3], {**COLUMNS[3], "name": "alpha_tv_spend"}])
    await _dataset(ctx, "beta", [*COLUMNS[:3], {**COLUMNS[3], "name": "beta_radio_spend"}])
    ana = await make_user(ctx, "ana@alpha.test")
    bob = await make_user(ctx, "bob@beta.test")
    await grant_role(ctx, ana, "alpha", None, "viewer")
    await grant_role(ctx, bob, "beta", None, "viewer")
    a = await session_caller(ctx, client, ana, tenant_id="alpha", project_id="main")
    b = await session_caller(ctx, client, bob, tenant_id="beta", project_id="main")
    body = {"template_id": "geo_mmm", "dataset": "dataset://main/media@latest"}

    async def spend(caller: Any) -> Any:
        r = await caller.post("/v1/guided/detect-roles", json=body)
        assert r.status_code == 200, r.text
        return next(q["answer"] for q in r.json()["questions"] if q["question_id"] == "spend")

    assert await spend(a) == ["alpha_tv_spend"]
    assert await spend(b) == ["beta_radio_spend"]
    # alpha's member pointed at beta's project never gets beta's profiles
    r = await a.in_project("beta", "main").post("/v1/guided/detect-roles", json=body)
    assert r.status_code in (403, 404)
    # and row-level security keeps the stored profiles apart below the API too
    async with ctx.db.tenant_session("alpha") as s:
        rows = (await s.execute(select(m.DatasetVersion))).scalars().all()
    assert [r.tenant_id for r in rows] == ["alpha"]
    assert rows[0].columns is not None and rows[0].columns[3]["name"] == "alpha_tv_spend"


@pytest.mark.parametrize(
    ("scope", "detect", "draft"),
    [("models:read", 200, 200), ("score:*", 403, 403), ("datasets:write", 200, 403)],
)
async def test_token_scopes_on_guided_operations(
    ctx: ServerContext, client: httpx.AsyncClient, scope: str, detect: int, draft: int
) -> None:
    await make_tenant(ctx, "acme")
    owner = await make_user(ctx, "own@acme.test")
    await grant_role(ctx, owner, "acme", None, "admin")
    tok = token_caller(client, await make_token(ctx, owner, "acme", "main", [scope]))
    detect_body = {"template_id": "geo_mmm", "columns": COLUMNS}
    assert (await tok.post("/v1/guided/detect-roles", json=detect_body)).status_code == detect
    assert (await tok.post("/v1/guided/drafts", json=ANSWERS)).status_code == draft
    assert (await tok.get("/v1/guided/templates")).status_code == 200  # any principal
