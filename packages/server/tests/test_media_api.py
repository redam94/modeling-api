"""Run media over HTTP (v0.14): list (with and without signed links), series, one item,
API writes of images and tables (dedupe, replace, audit), the body-size guard, refusals,
budgets and permissions."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest
from mapi_server.api.media import MAX_MEDIA_BODY_BYTES
from mapi_server.auth.identity import LOCAL_USER_ID
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services import media as media_mod
from mapi_server.services.runs import Scope, create_run
from mapi_server_testkit.identity import (
    grant_role,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_server_testkit.media import b64, png_bytes, table_bytes
from mapi_spec.executor import RunKind
from mapi_spec.resources import ResourceClass
from mapi_spec.storage import run_media_store_key
from mapi_spec.tracking import TrackingLimits
from sqlalchemy import select


async def _run(ctx: ServerContext) -> str:
    async with ctx.db.session() as s:
        run = await create_run(
            s,
            Scope("default", "default"),
            kind=RunKind.FIT,
            resource_class=ResourceClass.S,
            timeout_s=600,
            payload={},
        )
        return run.id


def _image(
    step: int | None = 0, *, key: str = "samples", shade: int = 0, **kw: Any
) -> dict[str, Any]:
    return {
        "key": key,
        "kind": "image",
        "content_type": "image/png",
        "data": b64(png_bytes(4, 3, shade=shade)),
        "step": step,
        **kw,
    }


async def test_log_list_and_get_media(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    run_id = await _run(ctx)
    for step in range(3):
        r = await client.post(
            f"/v1/runs/{run_id}/media",
            json=_image(step, shade=step, labels={"split": "val"}, caption=f"step {step}"),
        )
        assert r.status_code == 201, r.text
    first = r.json()
    assert first["kind"] == "image" and (first["width"], first["height"]) == (4, 3)
    assert first["sha256"] == "sha256:" + hashlib.sha256(png_bytes(4, 3, shade=2)).hexdigest()
    assert first["source"] == "api" and first["url"] and first["expires_at"]
    table = {
        "key": "counts",
        "kind": "table",
        "content_type": "application/json",
        "data": b64(table_bytes()),
    }
    r = await client.post(f"/v1/runs/{run_id}/media", json=table)
    assert r.status_code == 201 and r.json()["step"] is None
    # the same bytes again: stored once (the same item comes back)
    again = await client.post(f"/v1/runs/{run_id}/media", json=table)
    assert again.status_code == 201 and again.json()["id"] == r.json()["id"]

    page = (await client.get(f"/v1/runs/{run_id}/media?limit=2")).json()
    assert [(i["key"], i["step"]) for i in page["items"]] == [("counts", None), ("samples", 0)]
    assert all(i["url"] for i in page["items"])
    rest = (await client.get(f"/v1/runs/{run_id}/media?cursor={page['next_cursor']}")).json()
    assert [i["step"] for i in rest["items"]] == [1, 2] and rest["next_cursor"] is None
    bare = (await client.get(f"/v1/runs/{run_id}/media?links=false&kind=image")).json()
    assert len(bare["items"]) == 3 and all("url" not in i for i in bare["items"])
    only = (await client.get(f"/v1/runs/{run_id}/media?key=counts")).json()["items"]
    assert [i["kind"] for i in only] == ["table"]

    keys = (await client.get(f"/v1/runs/{run_id}/media-keys")).json()["items"]
    assert [(k["key"], k["labels"], k["kind"], k["count"]) for k in keys] == [
        ("counts", {}, "table", 1),
        ("samples", {"split": "val"}, "image", 3),
    ]
    assert (keys[0]["first_step"], keys[0]["last_step"]) == (None, None)
    assert (keys[1]["first_step"], keys[1]["last_step"]) == (0, 2)

    one = await client.get(f"/v1/runs/{run_id}/media/{first['id']}")
    assert one.status_code == 200 and one.json()["caption"] == "step 2" and one.json()["url"]
    assert (await client.get(f"/v1/runs/{run_id}/media/rm_nope")).status_code == 404
    assert (await client.get("/v1/runs/run_nope/media")).status_code == 404
    stored = await ctx.store.get(ctx.buckets.bundles, _object_name(ctx, run_id, first["id"]))
    assert stored == png_bytes(4, 3, shade=2)

    async with ctx.db.session() as s:
        audits = list(
            (
                await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == "run.log"))
            ).scalars()
        )
    assert len(audits) == 5 and all(a.details["media"] == 1 for a in audits)
    assert {a.details["kind"] for a in audits} == {"image", "table"}


def _object_name(ctx: ServerContext, run_id: str, media_id: str) -> str:
    from mapi_spec.storage import run_media_store_key

    sha = hashlib.sha256(png_bytes(4, 3, shade=2)).hexdigest()
    return run_media_store_key("default", run_id, f"{sha}.png")


async def test_new_bytes_at_the_same_step_replace_the_item(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    run_id = await _run(ctx)
    a = (await client.post(f"/v1/runs/{run_id}/media", json=_image(0))).json()
    b = (await client.post(f"/v1/runs/{run_id}/media", json=_image(0, shade=5))).json()
    assert a["sha256"] != b["sha256"]
    items = (await client.get(f"/v1/runs/{run_id}/media")).json()["items"]
    assert [i["sha256"] for i in items] == [b["sha256"]]
    # the replaced bytes are deleted: repeated replacements do not grow the stored bytes
    for shade in (6, 7, 8):
        r = await client.post(f"/v1/runs/{run_id}/media", json=_image(0, shade=shade))
        assert r.status_code == 201
    page = await ctx.store.list(ctx.buckets.bundles, prefix=f"t/default/run-media/{run_id}/")
    sha = hashlib.sha256(png_bytes(4, 3, shade=8)).hexdigest()
    assert [o.name for o in page.items] == [run_media_store_key("default", run_id, f"{sha}.png")]


async def test_media_bodies_are_checked_before_and_while_reading(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    run_id = await _run(ctx)
    big = b"x" * (MAX_MEDIA_BODY_BYTES + 1)
    r = await client.post(
        f"/v1/runs/{run_id}/media", content=big, headers={"content-type": "application/json"}
    )
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE"
    bad = await client.post(f"/v1/runs/{run_id}/media", json={**_image(), "data": "%%%%"})
    assert bad.status_code == 422 and bad.json()["code"] == "SCHEMA_INVALID"
    gif = b64(b"GIF89a" + b"\x00" * 40)
    bad = await client.post(f"/v1/runs/{run_id}/media", json={**_image(), "data": gif})
    assert bad.status_code == 422
    assert any(e["pointer"].startswith("/body") for e in bad.json()["errors"])
    wrong_kind = await client.post(f"/v1/runs/{run_id}/media", json={**_image(), "kind": "table"})
    assert wrong_kind.status_code == 422
    not_json = await client.post(
        f"/v1/runs/{run_id}/media", content=b"{", headers={"content-type": "application/json"}
    )
    assert not_json.status_code == 422
    assert (await client.get(f"/v1/runs/{run_id}/media")).json()["items"] == []


async def test_media_over_the_budget_is_refused(
    client: httpx.AsyncClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media_mod, "TrackingLimits", lambda: TrackingLimits(media_items=1))
    run_id = await _run(ctx)
    assert (await client.post(f"/v1/runs/{run_id}/media", json=_image(0))).status_code == 201
    # replacing the one item fits; a second item does not
    r = await client.post(f"/v1/runs/{run_id}/media", json=_image(0, shade=3))
    assert r.status_code == 201
    r = await client.post(f"/v1/runs/{run_id}/media", json=_image(1))
    assert r.json()["code"] == "QUOTA_EXCEEDED"


async def test_writes_need_run_update_and_reads_run_read(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    run_id = await _run(ctx)
    viewer = await make_user(ctx, "viewer@default.test")
    await grant_role(ctx, viewer, "default", "default", "viewer")
    vc = await session_caller(ctx, client, viewer)
    assert (await vc.get(f"/v1/runs/{run_id}/media")).status_code == 200
    assert (await vc.get(f"/v1/runs/{run_id}/media-keys")).status_code == 200
    assert (await vc.post(f"/v1/runs/{run_id}/media", json=_image())).status_code == 403
    async with ctx.db.session() as s:
        owner = await s.get(m.User, LOCAL_USER_ID)
        assert owner is not None
    read_only = token_caller(
        client, await make_token(ctx, owner, "default", "default", ["models:read"])
    )
    assert (await read_only.get(f"/v1/runs/{run_id}/media")).status_code == 200
    assert (await read_only.post(f"/v1/runs/{run_id}/media", json=_image())).status_code == 403
    writer = token_caller(
        client, await make_token(ctx, owner, "default", "default", ["runs:write"])
    )
    r = await writer.post(f"/v1/runs/{run_id}/media", json=_image())
    assert r.status_code == 201, r.text
