"""M4 dataset resources (multi-file versions, upload inspection) against a respx-mocked API,
sync and async. Responses are the contract's own examples."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from mapi_client import AsyncClient, Client, ClientConfig
from mapi_spec.api import DatasetVersion, IngestAssembly, IngestMapping, Run, UploadInspection
from mapi_spec.errors import ErrorCode, MapiError, Problem
from pydantic import ValidationError

from .conftest import _example as ex

BASE = "http://api.test"
V1 = f"{BASE}/v1"
HEADER = "X-Mapi-Project"
DS = "ds_01jaf3a1b2c3d4e5f6g7h8j9k0"
UPL = "upl_01jaf3d1e2f3g4h5j6k7m8n9p0"
UPLOAD = f"{V1}/datasets/{DS}/uploads/{UPL}"


def _cfg() -> ClientConfig:
    return ClientConfig(
        base_url=BASE, project="acme/growth", token="t0k", allow_insecure_token=True, max_retries=0
    )


@pytest.fixture
def scoped(api: respx.MockRouter) -> Iterator[Client]:
    with Client(config=_cfg(), sleep=lambda s: None) as c:
        yield c


@pytest.fixture
async def ascoped(api: respx.MockRouter) -> Any:
    async def nosleep(_: float) -> None:
        return None

    async with AsyncClient(config=_cfg(), sleep=nosleep) as c:
        yield c


def _assembly() -> IngestAssembly:
    return IngestAssembly.model_validate(IngestAssembly.model_json_schema()["examples"][0])


def test_create_version_from_an_assembly(api: respx.MockRouter, scoped: Client) -> None:
    route = api.post(f"{V1}/datasets/{DS}/versions").respond(
        202, json=ex(DatasetVersion, 1, status="ingesting")
    )
    dv = scoped.datasets.create_version(DS, assembly=_assembly(), message="cubes")
    assert dv.layout == "tree" and dv.tree_schema is not None
    body = json.loads(route.calls.last.request.content)
    assert set(body) == {"assembly", "message"}
    assert body["assembly"]["sources"][0]["upload_id"] == UPL
    with pytest.raises(ValidationError, match="not several"):
        scoped.datasets.create_version(
            DS, upload_id=UPL, mapping=IngestMapping(format="netcdf"), assembly=_assembly()
        )
    # single-file versions are sent exactly as before
    scoped.datasets.create_version(DS, upload_id=UPL, mapping=IngestMapping(format="netcdf"))
    assert set(json.loads(route.calls.last.request.content)) == {"upload_id", "mapping"}


def test_inspect_upload_and_read_the_inspection(api: respx.MockRouter, scoped: Client) -> None:
    post = api.post(f"{UPLOAD}/inspect").respond(202, json=ex(Run, kind="inspect"))
    run = scoped.datasets.inspect_upload(DS, UPL, idempotency_key="k1")
    assert run.kind.value == "inspect"
    request = post.calls.last.request
    assert request.headers["Idempotency-Key"] == "k1" and request.headers[HEADER] == "acme/growth"
    get = api.get(f"{UPLOAD}/inspection")
    get.respond(
        409,
        json=Problem.from_code(ErrorCode.NOT_READY, "still running").model_dump(mode="json"),
    )
    with pytest.raises(MapiError) as exc:
        scoped.datasets.upload_inspection(DS, UPL)
    assert exc.value.code is ErrorCode.NOT_READY
    get.mock(return_value=httpx.Response(200, json=ex(UploadInspection)))
    found = scoped.datasets.upload_inspection(DS, UPL)
    assert found.format == "hdf5" and found.arrays[0].path == "/observations/image"


async def test_async_inspection_and_assembly(api: respx.MockRouter, ascoped: AsyncClient) -> None:
    api.post(f"{UPLOAD}/inspect").respond(200, json=ex(Run, kind="inspect", state="succeeded"))
    api.get(f"{UPLOAD}/inspection").respond(200, json=ex(UploadInspection))
    api.post(f"{V1}/datasets/{DS}/versions").respond(202, json=ex(DatasetVersion, 1))
    assert (await ascoped.datasets.inspect_upload(DS, UPL)).state.value == "succeeded"
    assert (await ascoped.datasets.upload_inspection(DS, UPL)).groups[0] == "/"
    dv = await ascoped.datasets.create_version(DS, assembly=_assembly())
    assert dv.sources[0] == UPL
