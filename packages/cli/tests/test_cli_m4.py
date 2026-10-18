"""M4 CLI: multi-file ``mapi datasets upload`` (--node/--concat/--merge/--join),
``mapi datasets inspect`` and ``mapi datasets ingest`` against a respx-mocked API."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mapi_cli.multifile import assembly_files, parse_concat, parse_label
from mapi_spec.api import (
    Dataset,
    DatasetVersion,
    IngestMapping,
    Run,
    UploadInspection,
    UploadSession,
)
from mapi_spec.errors import ErrorCode, Problem
from typer.testing import Result

Invoke = Callable[..., Result]
AsJson = Callable[[Result], Any]
Ex = Callable[..., dict[str, Any]]

URL = "http://api.test"
V1 = f"{URL}/v1"
STORAGE = "http://storage.test"


def _routes(api: respx.MockRouter, example: Ex) -> tuple[str, respx.Route, respx.Route]:
    ds = example(Dataset)
    api.get(f"{V1}/datasets").respond(200, json={"items": [ds], "next_cursor": None})
    counter = iter(range(100))

    def session(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data = example(
            UploadSession,
            id=f"upl_{next(counter)}",
            filename=body["filename"],
            size_bytes=body["size_bytes"],
            content_type=body["content_type"],
        )
        data["upload"] = data["upload"] | {"url": f"{STORAGE}/up?sig=1", "max_bytes": None}
        return httpx.Response(201, json=data)

    uploads = api.post(f"{V1}/datasets/{ds['id']}/uploads").mock(side_effect=session)
    api.put(f"{STORAGE}/up").respond(200)
    version = api.post(f"{V1}/datasets/{ds['id']}/versions").respond(
        202, json=example(DatasetVersion, 1, status="ingesting")
    )
    api.get(f"{V1}/datasets/{ds['id']}/versions/4").respond(200, json=example(DatasetVersion, 1))
    return ds["id"], uploads, version


def _files(tmp_path: Path, *names: str) -> list[str]:
    out = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"CDF\x01" + name.encode())
        out.append(str(path))
    return out


def _sources(route: respx.Route) -> list[dict[str, Any]]:
    body = json.loads(route.calls.last.request.content)
    assert "upload_id" not in body and "mapping" not in body
    sources: list[dict[str, Any]] = body["assembly"]["sources"]
    return sources


# ------------------------------------------------------------------------------ grammar


def test_labels_and_concat_parsing() -> None:
    assert [parse_label(x) for x in ("2024", "0.5", "north", "inf", " 7 ")] == [
        2024,
        0.5,
        "north",
        "inf",
        7,
    ]
    assert parse_concat(["year=2024,2025"]) == ("year", [2024, 2025])
    assert parse_concat(["year=2024", "year=2025"]) == ("year", [2024, 2025])
    for bad in (["2024"], ["=1"], ["year=1", "month=2"], ["year=1,"]):
        with pytest.raises(ValueError):
            parse_concat(bad)


def test_assembly_files_grammar(tmp_path: Path) -> None:
    a, b, c = (Path(p) for p in _files(tmp_path, "a.nc", "b.nc", "c.nc"))
    items, shared = assembly_files(
        [a, b, c], nodes=["/", "/", "/instrument"], concat=["year=2024", "year=2025"]
    )
    assert shared is None
    assert [(i.node, i.combine, i.concat_dim, i.concat_label) for i in items] == [
        ("/", "concat", "year", 2024),
        ("/", "concat", "year", 2025),
        ("/instrument", "node", None, None),
    ]
    merged, _ = assembly_files([a, b], merge=True, join="outer")
    assert [(i.combine, i.join) for i in merged] == [("merge", "outer")] * 2
    m = IngestMapping(format="netcdf")
    _, one = assembly_files([a, b], merge=True, mappings=[m])
    assert one is m
    per, none = assembly_files([a, b], merge=True, mappings=[m, m])
    assert none is None and [i.mapping for i in per] == [m, m]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({}, "combine them with --concat"),
        ({"concat": ["y=1,2"], "merge": True}, "not both"),
        ({"join": "outer"}, "--join only applies"),
        ({"concat": ["y=1"]}, "one label per combined file"),
        ({"nodes": ["/", "/a", "/b"]}, "once per file"),
        ({"nodes": ["/a", "/b"], "concat": ["y=1,2"]}, "at least two files"),
        ({"nodes": ["/a", "/b"], "merge": True}, "at least two files"),
    ],
)
def test_assembly_files_refusals(tmp_path: Path, kwargs: dict[str, Any], match: str) -> None:
    files = [Path(p) for p in _files(tmp_path, "a.nc", "b.nc")]
    with pytest.raises(ValueError, match=match):
        assembly_files(files, **kwargs)


# ---------------------------------------------------------------------- datasets upload


def test_upload_several_files_with_concat(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, as_json: AsJson
) -> None:
    _, uploads, version = _routes(api, example)
    files = _files(tmp_path, "2024.nc", "2025.nc")
    res = mapi(
        "--url", URL, "--json", "datasets", "upload", *files, "--name", "eu_media_daily",
        "--concat", "year=2024,2025", "-m", "two years",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert as_json(res)["layout"] == "tree"
    assert [json.loads(c.request.content)["filename"] for c in uploads.calls] == [
        "2024.nc",
        "2025.nc",
    ]
    assert [(s["upload_id"], s["concat_dim"], s["concat_label"]) for s in _sources(version)] == [
        ("upl_0", "year", 2024),
        ("upl_1", "year", 2025),
    ]
    assert json.loads(version.calls.last.request.content)["message"] == "two years"


def test_upload_nodes_and_merge(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    _, _, version = _routes(api, example)
    a, b, psf = _files(tmp_path, "a.nc", "b.nc", "psf.nc")
    res = mapi(
        "--url", URL, "datasets", "upload", a, b, psf, "--name", "eu_media_daily",
        "--node", "/obs", "--node", "/obs", "--node", "/instrument",
        "--merge", "--join", "outer", "--no-wait",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert "layout" in res.stdout and "tree" in res.stdout
    assert [(s["node"], s["combine"], s.get("join")) for s in _sources(version)] == [
        ("/obs", "merge", "outer"),
        ("/obs", "merge", "outer"),
        ("/instrument", "node", "exact"),
    ]
    assert "at node /instrument (node)" in res.stderr


def test_one_file_at_a_node_is_an_assembly(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    _, _, version = _routes(api, example)
    (psf,) = _files(tmp_path, "psf.nc")
    res = mapi(
        "--url", URL, "datasets", "upload", psf, "--name", "eu_media_daily", "--node", "/instrument",
        "--no-wait",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert [s["node"] for s in _sources(version)] == ["/instrument"]


def test_upload_per_file_mappings(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    _, _, version = _routes(api, example)
    a, b = _files(tmp_path, "a.nc", "b.nc")
    m1, m2 = tmp_path / "m1.yaml", tmp_path / "m2.yaml"
    m1.write_text("format: netcdf\nvars: {flux: {column: f}}\n")
    m2.write_text("format: netcdf\n")
    res = mapi(
        "--url", URL, "datasets", "upload", a, b, "--name", "eu_media_daily", "--merge",
        "--mapping", str(m1), "--mapping", str(m2), "--no-wait",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert [sorted(s["mapping"].get("vars", {})) for s in _sources(version)] == [["flux"], []]


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ([], "combine them with --concat"),
        (["--concat", "year=2024"], "one label per combined file"),
        (["--merge", "--concat", "year=1,2"], "not both"),
        (["--join", "inner"], "--join only applies"),
        (["--node", "/", "--node", "/", "--node", "/"], "once per file"),
        (["--merge", "--mapping", "M", "--mapping", "M", "--mapping", "M"], "once per file"),
    ],
)
def test_upload_flag_errors_send_nothing(
    mapi: Invoke,
    api: respx.MockRouter,
    example: Ex,
    tmp_path: Path,
    args: list[str],
    match: str,
) -> None:
    _, uploads, version = _routes(api, example)
    mapping = tmp_path / "m.yaml"
    mapping.write_text("format: netcdf\n")
    args = [str(mapping) if a == "M" else a for a in args]
    files = _files(tmp_path, "a.nc", "b.nc")
    res = mapi("--url", URL, "datasets", "upload", *files, "--name", "eu_media_daily", *args)
    assert res.exit_code == 1 and "invalid input" in res.stderr
    assert match in " ".join(res.stderr.split())
    assert not uploads.called and not version.called


def test_raw_hdf5_needs_a_mapping(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    _, uploads, version = _routes(api, example)
    h5 = tmp_path / "cube.h5"
    h5.write_bytes(b"\x89HDF\r\n\x1a\n")
    res = mapi("--url", URL, "datasets", "upload", str(h5), "--name", "eu_media_daily")
    assert res.exit_code == 1 and "Hdf5Mapping" in " ".join(res.stderr.split())
    assert not uploads.called
    mapping = tmp_path / "h5.json"
    mapping.write_text(json.dumps(IngestMapping.model_json_schema()["examples"][4]))
    res = mapi(
        "--url", URL, "datasets", "upload", str(h5), "--name", "eu_media_daily", "--mapping", str(mapping),
        "--no-wait",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    body = json.loads(version.calls.last.request.content)
    assert set(body) == {"upload_id", "mapping"} and body["mapping"]["format"] == "hdf5"


# --------------------------------------------------------------------- inspect / ingest


def _inspect_routes(api: respx.MockRouter, example: Ex, ds_id: str) -> respx.Route:
    run = example(Run, 0, kind="inspect", state="running")
    uploads = rf"{V1}/datasets/{ds_id}/uploads/upl_\d+"
    post = api.post(url__regex=rf"{uploads}/inspect$").respond(202, json=run)
    problem = Problem.from_code(ErrorCode.NOT_READY, "running").model_dump(mode="json")
    api.get(url__regex=rf"{uploads}/inspection$").mock(
        side_effect=[
            httpx.Response(409, json=problem),
            httpx.Response(200, json=example(UploadInspection)),
        ]
    )
    api.get(f"{V1}/runs/{run['id']}").respond(200, json=run)
    return post


def test_inspect_a_local_file(
    mapi: Invoke,
    api: respx.MockRouter,
    example: Ex,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.sleep", lambda s: None)
    ds_id, uploads, version = _routes(api, example)
    post = _inspect_routes(api, example, ds_id)
    h5 = tmp_path / "cube.h5"
    h5.write_bytes(b"\x89HDF\r\n\x1a\n")
    res = mapi("--url", URL, "datasets", "inspect", "eu_media_daily", str(h5))
    assert res.exit_code == 0, res.output
    assert "/observations/image" in res.stdout and "upl_0" in res.stdout
    assert "hdf5" in res.stdout
    assert uploads.called and post.called and not version.called


def test_inspect_an_upload_id_as_json(
    mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson, monkeypatch: Any
) -> None:
    monkeypatch.setattr("time.sleep", lambda s: None)
    ds_id, uploads, _ = _routes(api, example)
    _inspect_routes(api, example, ds_id)
    res = mapi("--url", URL, "--json", "datasets", "inspect", "eu_media_daily", "upl_7")
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    assert doc["upload_id"] == "upl_7" and doc["inspection"]["arrays"][0]["dtype"]
    assert not uploads.called
    missing = mapi("--url", URL, "datasets", "inspect", "eu_media_daily", "nope.h5")
    assert missing.exit_code == 1 and "neither a file nor an upload id" in missing.stderr
    unknown = mapi("--url", URL, "datasets", "inspect", "no_such_dataset", "upl_7")
    assert unknown.exit_code == 1 and "no dataset called" in unknown.stderr


def test_ingest_a_staged_upload(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, as_json: AsJson
) -> None:
    _, _, version = _routes(api, example)
    api.get(f"{V1}/datasets/{example(Dataset)['id']}/versions/4").respond(
        200, json=example(DatasetVersion, 1)
    )
    mapping = tmp_path / "h5.json"
    mapping.write_text(json.dumps(IngestMapping.model_json_schema()["examples"][4]))
    res = mapi(
        "--url", URL, "--json", "datasets", "ingest", "eu_media_daily", "upl_7",
        "--mapping", str(mapping),
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert as_json(res)["version"] == 4
    body = json.loads(version.calls.last.request.content)
    assert body["upload_id"] == "upl_7" and body["mapping"]["hdf5"]["vars"]
    assert version.calls.last.request.headers["Idempotency-Key"]
