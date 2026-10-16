"""The asynchronous client mirrors the synchronous one."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import respx
import xarray as xr
from mapi_client import AsyncClient, ClientConfig, RunEnd, RunEventFrame, TransportError
from mapi_sdk.wire import write_datatree_zip
from mapi_spec.api import (
    Dataset,
    DatasetVersion,
    Run,
    RunEventView,
    ScoreCallResult,
    SpecDoc,
    SpecRevision,
    UploadSession,
    encode_sse,
)
from mapi_spec.errors import ErrorCode, IntegrityFailure, MapiError, Problem
from mapi_spec.storage import crc32c, crc32c_to_base64

BASE = "http://api.test"
STORAGE = "http://storage.test"
V1 = f"{BASE}/v1"
Ex = Callable[..., dict[str, Any]]


class AsyncSleeps(list[float]):
    async def __call__(self, seconds: float) -> None:
        self.append(seconds)


@pytest.fixture
def asleeps() -> AsyncSleeps:
    return AsyncSleeps()


@pytest.fixture
async def aclient(api: respx.MockRouter, asleeps: AsyncSleeps) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(config=ClientConfig(base_url=BASE), sleep=asleeps) as c:
        yield c


def run_json(example: Ex, state: str = "running") -> dict[str, Any]:
    return example(Run, 0, state=state)


async def test_send_retries_and_maps_errors(
    aclient: AsyncClient, api: respx.MockRouter, example: Ex, asleeps: AsyncSleeps
) -> None:
    api.get(f"{V1}/runs/run_01").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=run_json(example))]
    )
    assert (await aclient.runs.get("run_01")).state.value == "running"
    assert len(asleeps) == 1
    api.get(f"{V1}/runs/nope").respond(
        404, json=Problem.from_code(ErrorCode.NOT_FOUND, "no").model_dump(mode="json")
    )
    with pytest.raises(MapiError) as err:
        await aclient.runs.get("nope")
    assert err.value.code is ErrorCode.NOT_FOUND
    api.get(f"{V1}/runs/down").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(TransportError):
        await aclient.runs.get("down")


async def test_async_pagination(aclient: AsyncClient, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/specs").mock(
        side_effect=[
            httpx.Response(200, json={"items": [example(SpecDoc)], "next_cursor": "c"}),
            httpx.Response(200, json={"items": [example(SpecDoc, name="b")]}),
        ]
    )
    names = [s.name async for s in aclient.specs.list()]
    assert names == ["eu-geo-mmm", "b"]


async def test_async_wait_and_stream(
    aclient: AsyncClient, api: respx.MockRouter, example: Ex, asleeps: AsyncSleeps
) -> None:
    api.get(f"{V1}/runs/run_01").mock(
        side_effect=[
            httpx.Response(200, json=run_json(example, "queued")),
            httpx.Response(200, json=run_json(example, "succeeded")),
        ]
    )
    assert (await aclient.runs.wait("run_01")).state.value == "succeeded"

    run = Run.model_validate(run_json(example))
    ev = RunEventView.model_validate(example(RunEventView))
    sse = "text/event-stream"
    route = api.get(f"{V1}/runs/run_01/events").mock(
        side_effect=[
            httpx.ReadTimeout("dropped"),
            httpx.Response(
                200,
                headers={"content-type": sse},
                content=(
                    encode_sse("snapshot", run)
                    + encode_sse("state", ev, id=ev.id)
                    + encode_sse("end", run)
                ).encode(),
            ),
        ]
    )
    items = [i async for i in aclient.runs.stream_events("run_01", last_event_id=3)]
    assert isinstance(items[1], RunEventFrame) and isinstance(items[-1], RunEnd)
    assert all(c.request.headers["Last-Event-ID"] == "3" for c in route.calls)


async def test_async_upload_and_score(
    aclient: AsyncClient, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    f = tmp_path / "d.zarr.zip"
    payload = b"PK-zip-bytes"
    f.write_bytes(payload)
    ds = example(Dataset)
    sess = example(UploadSession, size_bytes=len(payload), content_type="application/zip")
    sess["upload"] = sess["upload"] | {"url": f"{STORAGE}/up?sig=1", "max_bytes": None}
    api.get(f"{V1}/datasets").respond(200, json={"items": [ds]})
    api.post(f"{V1}/datasets/{ds['id']}/uploads").respond(201, json=sess)
    put = api.put(f"{STORAGE}/up").respond(200, json={"crc32c": crc32c_to_base64(crc32c(payload))})
    api.post(f"{V1}/datasets/{ds['id']}/versions").respond(
        202, json=example(DatasetVersion, status="ingesting")
    )
    api.get(f"{V1}/datasets/{ds['id']}/versions/3").respond(200, json=example(DatasetVersion))
    out = await aclient.datasets.upload(f, dataset=ds["name"])
    assert out.status.value == "ready"
    assert put.calls.last.request.content == payload
    assert "Authorization" not in put.calls.last.request.headers

    tree = xr.DataTree.from_dict({"summary": xr.Dataset({"y": (("t",), np.zeros(2))})})
    data = write_datatree_zip(tree)
    assert data is not None
    link = {
        "name": "score.zarr.zip",
        "url": f"{STORAGE}/score.zarr.zip?sig=1",
        "expires_at": "2026-10-15T09:45:00Z",
        "size": len(data),
        "crc32c": crc32c_to_base64(crc32c(data)),
        "generation": 1,
    }
    score = api.post(f"{V1}/models/mdl_1/versions/1:score").respond(
        200, json=example(ScoreCallResult) | {"outputs": [link]}
    )
    api.get(f"{STORAGE}/score.zarr.zip").respond(200, content=data)
    res = await aclient.models.score("mdl_1", 1, {"model": "model://default/eu@v1"})
    assert res.tree is not None and res.tree["summary"]["y"].shape == (2,)
    assert json.loads(score.calls.last.request.content)["request"]["mode"] == "predict"

    with pytest.raises(IntegrityFailure):
        from mapi_spec.api import ArtifactLink

        await aclient.download(ArtifactLink.model_validate(link | {"size": len(data) + 1}))


async def test_async_spec_upsert(aclient: AsyncClient, api: respx.MockRouter, example: Ex) -> None:
    yaml = "apiVersion: mapi/v1\nkind: ModelSpec\nmetadata: {name: eu-geo-mmm, project: x}\n"
    doc = example(SpecDoc)
    api.get(f"{V1}/specs").respond(200, json={"items": [doc]})
    rev = api.post(f"{V1}/specs/{doc['id']}/revisions").respond(201, json=example(SpecRevision))
    assert (await aclient.specs.upsert(yaml)).revision == 2
    assert json.loads(rev.calls.last.request.content)["parent_revision"] == 2


async def test_async_score_outlasts_its_read_timeout(
    api: respx.MockRouter, example: Ex, asleeps: AsyncSleeps
) -> None:
    """The long-poll's read timeout covers Prefer: wait, and a read timeout means "still
    running" (the same Idempotency-Key is replayed), not a TransportError."""
    done = example(ScoreCallResult) | {"outputs": []}
    reads: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        reads.append(request.extensions["timeout"]["read"])
        if len(reads) <= 3:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=done)

    route = api.post(f"{V1}/models/mdl_1/versions/1:score").mock(side_effect=respond)
    config = ClientConfig(base_url=BASE, timeout=1.5, max_retries=2)
    async with AsyncClient(config=config, sleep=asleeps) as c:
        res = await c.models.score("mdl_1", 1, {"model": "model://default/eu@v1"}, timeout=600)
    assert res.done and route.call_count == 4
    assert min(reads) >= 60 + 1.5
    assert len({call.request.headers["Idempotency-Key"] for call in route.calls}) == 1


async def test_async_reviewer_capability(
    aclient: AsyncClient, api: respx.MockRouter, example: Ex
) -> None:
    from mapi_spec.api import Membership

    usr = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"
    upd = api.patch(f"{V1}/tenants/acme/projects/growth/members/{usr}").respond(
        200, json=example(Membership)
    )
    await aclient.tenants.set_capabilities("acme", usr, ["reviewer"], project_id="growth")
    assert json.loads(upd.calls.last.request.content) == {"capabilities": ["reviewer"]}
    add = api.post(f"{V1}/tenants/acme/projects/growth/members").respond(
        201, json=example(Membership)
    )
    await aclient.tenants.add_member(
        "acme", "editor", project_id="growth", principal_id=usr, capabilities=["reviewer"]
    )
    assert json.loads(add.calls.last.request.content) == {
        "principal_id": usr,
        "role": "editor",
        "capabilities": ["reviewer"],
    }
