"""M4 upload helpers: DataTree and raw-HDF5 uploads, multi-file versions (``upload_files``)
and upload inspection (``inspect``/``inspect_file``), sync and async, against respx."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import respx
import xarray as xr
from mapi_client import AssemblyFile, AsyncClient, Client, ClientConfig, InspectedUpload
from mapi_client._m4_common import assembly_for, plan_assembly
from mapi_client.transfer import default_mapping, detect_format, mapping_reads, upload_source
from mapi_sdk.wire import read_datatree_zip
from mapi_spec.api import (
    Dataset,
    DatasetVersion,
    IngestMapping,
    Run,
    UploadInspection,
    UploadSession,
)
from mapi_spec.errors import ErrorCode, MapiError, Problem
from pydantic import ValidationError

from .conftest import STORAGE
from .conftest import _example as ex

BASE = "http://api.test"
V1 = f"{BASE}/v1"
DS = ex(Dataset)["id"]
PUT_URL = f"{STORAGE}/upload/quarantine"
Sleeps = list[float]


def _hdf5_mapping() -> IngestMapping:
    return IngestMapping.model_validate(IngestMapping.model_json_schema()["examples"][4])


def _session(upload_id: str, filename: str, size: int, ctype: str) -> dict[str, Any]:
    data = ex(UploadSession, id=upload_id, filename=filename, size_bytes=size, content_type=ctype)
    data["upload"] = data["upload"] | {
        "url": f"{PUT_URL}?sig={upload_id}",
        "max_bytes": 10_000_000,
        "headers": {"content-type": ctype},
    }
    return data


def _sessions(api: respx.MockRouter) -> respx.Route:
    """Every POST …/uploads answers a new session (upl_0, upl_1, …) for the asked file."""
    counter = iter(range(100))

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        upl = f"upl_{next(counter)}"
        return httpx.Response(
            201, json=_session(upl, body["filename"], body["size_bytes"], body["content_type"])
        )

    return api.post(f"{V1}/datasets/{DS}/uploads").mock(side_effect=answer)


def _not_ready() -> httpx.Response:
    problem = Problem.from_code(ErrorCode.NOT_READY, "inspection is running")
    return httpx.Response(409, json=problem.model_dump(mode="json"))


def _run(state: str, **changes: Any) -> dict[str, Any]:
    return ex(Run, kind="inspect", state=state, **changes)


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def sleeps() -> Sleeps:
    return []


@pytest.fixture
def client(api: respx.MockRouter, sleeps: Sleeps) -> Iterator[Client]:
    with Client(config=ClientConfig(base_url=BASE, max_retries=0), sleep=sleeps.append) as c:
        yield c


@pytest.fixture
async def aclient(api: respx.MockRouter) -> Any:
    async def nosleep(_: float) -> None:
        return None

    async with AsyncClient(config=ClientConfig(base_url=BASE, max_retries=0), sleep=nosleep) as c:
        yield c


def _tree() -> xr.DataTree:
    root = xr.Dataset({"y": (("t",), np.arange(3.0))}, coords={"t": np.arange(3)})
    psf = xr.Dataset({"kernel": (("r", "c"), np.ones((2, 2)))})
    return xr.DataTree.from_dict({"/": root, "/psf": psf})


# ----------------------------------------------------------------------------- transfer


@pytest.mark.parametrize("name", ["cube.h5", "cube.HDF5", "swath.he5"])
def test_hdf5_suffixes(name: str) -> None:
    assert detect_format(name) == ("hdf5", "application/x-hdf5")


def test_hdf5_needs_a_mapping_and_netcdf_hdf5_cross_read() -> None:
    with pytest.raises(ValueError, match="Hdf5Mapping"):
        default_mapping("hdf5")
    assert mapping_reads("netcdf", "hdf5") and mapping_reads("hdf5", "netcdf")
    assert mapping_reads("mff", "csv") and not mapping_reads("hdf5", "csv")
    assert not mapping_reads("mff", "hdf5")


def test_datatree_source_is_a_tree_zarr_zip() -> None:
    src = upload_source(_tree())
    assert (src.filename, src.format, src.content_type) == (
        "data.tree.zip",
        "zarr_zip",
        "application/zip",
    )
    assert src.data is not None
    back = read_datatree_zip(src.data)
    assert back["/psf"]["kernel"].shape == (2, 2)
    assert upload_source(_tree(), filename="cube.zarr.zip").filename == "cube.zarr.zip"


# ----------------------------------------------------------------------- single uploads


def test_upload_hdf5_requires_a_mapping_before_any_request(
    client: Client, api: respx.MockRouter, tmp_path: Path
) -> None:
    h5 = tmp_path / "cube.h5"
    h5.write_bytes(b"\x89HDF\r\n\x1a\n" + b"\0" * 8)
    with pytest.raises(ValueError, match="hdf5"):
        client.datasets.upload(h5, dataset_id=DS)
    assert not api.calls
    _sessions(api)
    put = api.put(PUT_URL).respond(200)
    version = api.post(f"{V1}/datasets/{DS}/versions").respond(
        202, json=ex(DatasetVersion, status="ingesting")
    )
    client.datasets.upload(h5, dataset_id=DS, mapping=_hdf5_mapping(), wait=False)
    assert put.calls.last.request.headers["content-type"] == "application/x-hdf5"
    body = json.loads(version.calls.last.request.content)
    assert body["mapping"]["format"] == "hdf5" and body["mapping"]["hdf5"]["vars"]
    # a NetCDF-4 / xarray-written .h5 file may be read as netcdf
    client.datasets.upload(h5, dataset_id=DS, mapping=IngestMapping(format="netcdf"), wait=False)
    assert json.loads(version.calls.last.request.content)["mapping"] == {
        "format": "netcdf",
        "layout": "dense",
        "dims": {},
        "vars": {},
    }


def test_upload_datatree(client: Client, api: respx.MockRouter) -> None:
    sessions = _sessions(api)
    put = api.put(PUT_URL).respond(200)
    version = api.post(f"{V1}/datasets/{DS}/versions").respond(
        202, json=ex(DatasetVersion, status="ingesting")
    )
    client.datasets.upload(_tree(), dataset_id=DS, wait=False)
    assert json.loads(sessions.calls.last.request.content)["filename"] == "data.tree.zip"
    assert read_datatree_zip(put.calls.last.request.content)["/"]["y"].size == 3
    assert json.loads(version.calls.last.request.content)["mapping"]["format"] == "zarr_zip"


# --------------------------------------------------------------------- multi-file versions


def _nc(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"CDF\x01" + name.encode())
    return path


def test_plan_assembly_from_simple_arguments(tmp_path: Path) -> None:
    a, b = _nc(tmp_path, "2024.nc"), _nc(tmp_path, "2025.nc")
    plan = plan_assembly([a, b], concat="year", labels=[2024, 2025], join="outer")
    got = assembly_for(plan, ["upl_a", "upl_b"])
    assert [
        (s.upload_id, s.combine, s.concat_dim, s.concat_label, s.join) for s in got.sources
    ] == [
        ("upl_a", "concat", "year", 2024, "outer"),
        ("upl_b", "concat", "year", 2025, "outer"),
    ]
    merged = plan_assembly([a, b], merge=True, node="/obs")
    assert {(p.source.node, p.source.combine) for p in merged} == {("/obs", "merge")}
    psf = AssemblyFile(_nc(tmp_path, "psf.nc"), node="/instrument")
    mixed = plan_assembly([a, psf])
    assert [(p.source.node, p.source.combine) for p in mixed] == [
        ("/", "node"),
        ("/instrument", "node"),
    ]
    inferred = plan_assembly([AssemblyFile(a, concat_dim="year", concat_label="2024")])
    assert inferred[0].source.combine == "concat"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"concat": "year"}, "one label per file"),
        ({"concat": "year", "labels": [1]}, "one label per file"),
        ({"labels": [1, 2]}, "only used with concat"),
        ({"concat": "year", "labels": [1, 2], "merge": True}, "not both"),
        ({}, "combine them with 'concat' or 'merge'"),
        ({"concat": "year", "labels": [1, 1]}, "unique"),
    ],
)
def test_plan_assembly_refuses_bad_arguments(
    tmp_path: Path, kwargs: dict[str, Any], match: str
) -> None:
    files = [_nc(tmp_path, "a.nc"), _nc(tmp_path, "b.nc")]
    with pytest.raises((ValueError, ValidationError), match=match):
        plan_assembly(files, **kwargs)


def test_plan_assembly_mappings(tmp_path: Path) -> None:
    csv = tmp_path / "a.csv"
    csv.write_text("x\n1\n")
    with pytest.raises(ValueError, match=r"a\.csv: csv uploads need an IngestMapping"):
        plan_assembly([csv])
    with pytest.raises(ValueError, match="does not match"):
        plan_assembly([AssemblyFile(csv, mapping=IngestMapping(format="netcdf"))])
    with pytest.raises(ValueError, match="at least one"):
        plan_assembly([])
    # the shared mapping applies where it reads the file, the rest default
    nc = _nc(tmp_path, "b.nc")
    shared = IngestMapping.model_validate(IngestMapping.model_json_schema()["examples"][0])
    plan = plan_assembly([csv, AssemblyFile(nc, node="/b")], mapping=shared)
    assert [p.source.mapping.format for p in plan] == ["csv", "netcdf"]


def test_upload_files_concat_end_to_end(
    client: Client, api: respx.MockRouter, tmp_path: Path, sleeps: Sleeps
) -> None:
    a, b = _nc(tmp_path, "2024.nc"), _nc(tmp_path, "2025.nc")
    sessions = _sessions(api)
    put = api.put(PUT_URL).respond(200)
    version = api.post(f"{V1}/datasets/{DS}/versions").respond(
        202, json=ex(DatasetVersion, 1, status="ingesting")
    )
    api.get(f"{V1}/datasets/{DS}/versions/4").respond(200, json=ex(DatasetVersion, 1))
    out = client.datasets.upload_files(
        [a, b], dataset_id=DS, concat="year", labels=[2024, 2025], message="cubes"
    )
    assert out.layout == "tree"
    assert [json.loads(c.request.content)["filename"] for c in sessions.calls] == [
        "2024.nc",
        "2025.nc",
    ]
    assert [c.request.content for c in put.calls] == [a.read_bytes(), b.read_bytes()]
    body = json.loads(version.calls.last.request.content)
    assert set(body) == {"assembly", "message"}
    assert [
        (s["upload_id"], s["combine"], s["concat_dim"], s["concat_label"])
        for s in body["assembly"]["sources"]
    ] == [("upl_0", "concat", "year", 2024), ("upl_1", "concat", "year", 2025)]
    assert version.calls.last.request.headers["Idempotency-Key"]


def test_upload_files_checks_before_uploading(
    client: Client, api: respx.MockRouter, tmp_path: Path
) -> None:
    files = [_nc(tmp_path, "a.nc"), _nc(tmp_path, "b.nc")]
    with pytest.raises(ValidationError):
        client.datasets.upload_files(files, dataset_id=DS)
    with pytest.raises(ValueError, match="exactly one of dataset"):
        client.datasets.upload_files(files, merge=True)
    assert not api.calls


# --------------------------------------------------------------------------- inspection

UPLOAD = f"{V1}/datasets/{DS}/uploads/upl_0"


def test_inspect_polls_until_ready(client: Client, api: respx.MockRouter, sleeps: Sleeps) -> None:
    run = _run("running")
    post = api.post(f"{UPLOAD}/inspect").respond(202, json=run)
    api.get(f"{UPLOAD}/inspection").mock(
        side_effect=[_not_ready(), _not_ready(), httpx.Response(200, json=ex(UploadInspection))]
    )
    runs = api.get(f"{V1}/runs/{run['id']}").respond(200, json=run)
    found = client.datasets.inspect(DS, "upl_0", poll_interval=0.5)
    assert found.arrays[0].path == "/observations/image"
    assert post.call_count == 1 and runs.call_count == 2 and sleeps == [0.5, 0.75]


def test_inspect_raises_the_failed_run_and_times_out(client: Client, api: respx.MockRouter) -> None:
    failed = ex(Run, 1, kind="inspect")
    api.post(f"{UPLOAD}/inspect").respond(202, json=_run("running"))
    api.get(f"{UPLOAD}/inspection").mock(return_value=_not_ready())
    api.get(f"{V1}/runs/{_run('running')['id']}").respond(200, json=failed)
    with pytest.raises(MapiError) as exc:
        client.datasets.inspect(DS, "upl_0")
    assert exc.value.code is not ErrorCode.NOT_READY
    api.get(f"{V1}/runs/{_run('running')['id']}").respond(200, json=_run("running"))
    with pytest.raises(TimeoutError, match="upl_0"):
        client.datasets.inspect(DS, "upl_0", timeout=0.0)


def test_inspect_does_not_swallow_other_errors(client: Client, api: respx.MockRouter) -> None:
    api.post(f"{UPLOAD}/inspect").respond(202, json=_run("running"))
    api.get(f"{V1}/runs/{_run('running')['id']}").respond(200, json=_run("running"))
    problem = Problem.from_code(ErrorCode.NOT_FOUND, "no such upload")
    api.get(f"{UPLOAD}/inspection").respond(404, json=problem.model_dump(mode="json"))
    with pytest.raises(MapiError) as exc:
        client.datasets.inspect(DS, "upl_0")
    assert exc.value.code is ErrorCode.NOT_FOUND


def test_inspect_raises_the_run_error_when_it_failed_between_polls(
    client: Client, api: respx.MockRouter
) -> None:
    # the run was running when polled, then failed: the inspection answers 404 and the
    # caller gets the run's own error, not NOT_FOUND
    api.post(f"{UPLOAD}/inspect").respond(202, json=_run("running"))
    failed = ex(Run, 1, kind="inspect")
    runs = api.get(f"{V1}/runs/{_run('running')['id']}").respond(200, json=failed)
    problem = Problem.from_code(ErrorCode.NOT_FOUND, "upload inspection")
    api.get(f"{UPLOAD}/inspection").respond(404, json=problem.model_dump(mode="json"))
    with pytest.raises(MapiError) as exc:
        client.datasets.inspect(DS, "upl_0")
    assert exc.value.code is not ErrorCode.NOT_FOUND and runs.call_count == 1


async def test_async_inspect_raises_the_run_error_when_it_failed_between_polls(
    aclient: AsyncClient, api: respx.MockRouter
) -> None:
    api.post(f"{UPLOAD}/inspect").respond(202, json=_run("running"))
    api.get(f"{V1}/runs/{_run('running')['id']}").respond(200, json=ex(Run, 1, kind="inspect"))
    problem = Problem.from_code(ErrorCode.NOT_FOUND, "upload inspection")
    api.get(f"{UPLOAD}/inspection").respond(404, json=problem.model_dump(mode="json"))
    with pytest.raises(MapiError) as exc:
        await aclient.datasets.inspect(DS, "upl_0")
    assert exc.value.code is not ErrorCode.NOT_FOUND


def test_inspect_file_stages_without_ingesting(
    client: Client, api: respx.MockRouter, tmp_path: Path
) -> None:
    h5 = tmp_path / "cube.h5"
    h5.write_bytes(b"\x89HDF\r\n\x1a\n")
    _sessions(api)
    api.put(PUT_URL).respond(200)
    versions = api.post(f"{V1}/datasets/{DS}/versions")
    api.post(f"{UPLOAD}/inspect").respond(200, json=_run("succeeded"))
    api.get(f"{UPLOAD}/inspection").respond(200, json=ex(UploadInspection))
    found = client.datasets.inspect_file(h5, dataset_id=DS)
    assert isinstance(found, InspectedUpload)
    assert (found.upload_id, found.dataset_id) == ("upl_0", ex(UploadSession)["dataset_id"])
    assert found.inspection.format == "hdf5" and not versions.called


# -------------------------------------------------------------------------------- async


async def test_async_upload_files_and_inspect(
    aclient: AsyncClient, api: respx.MockRouter, tmp_path: Path
) -> None:
    _sessions(api)
    api.put(PUT_URL).respond(200)
    version = api.post(f"{V1}/datasets/{DS}/versions").respond(
        202, json=ex(DatasetVersion, 1, status="ingesting")
    )
    api.get(f"{V1}/datasets/{DS}/versions/4").respond(200, json=ex(DatasetVersion, 1))
    files: list[Any] = [_nc(tmp_path, "a.nc"), _tree()]
    out = await aclient.datasets.upload_files(files, dataset_id=DS, merge=True, join="inner")
    assert out.version == 4
    sources = json.loads(version.calls.last.request.content)["assembly"]["sources"]
    assert [(s["combine"], s["join"]) for s in sources] == [("merge", "inner")] * 2

    run = _run("running")
    uploads = rf"{V1}/datasets/{DS}/uploads/upl_\d+"
    api.post(url__regex=rf"{uploads}/inspect$").respond(202, json=run)
    api.get(url__regex=rf"{uploads}/inspection$").mock(
        side_effect=[
            _not_ready(),
            httpx.Response(200, json=ex(UploadInspection)),
            httpx.Response(200, json=ex(UploadInspection)),
        ]
    )
    api.get(f"{V1}/runs/{run['id']}").respond(200, json=run)
    found = await aclient.datasets.inspect(DS, "upl_0")
    assert found.groups[0] == "/"
    staged = await aclient.datasets.inspect_file(_tree(), dataset_id=DS)
    assert staged.upload.filename == "data.tree.zip" and staged.upload_id == "upl_2"
