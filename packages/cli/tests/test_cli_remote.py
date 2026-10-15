"""Remote commands against a respx-mocked API (``--url``/``--remote``)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import respx
import xarray as xr
from mapi_sdk.wire import write_datatree_zip
from mapi_spec.api import (
    CatalogResponse,
    Dataset,
    DatasetVersion,
    LogLine,
    Run,
    RunEventView,
    ScoreCallResult,
    SpecDoc,
    SpecRevision,
    UploadSession,
    ValidateResponse,
    encode_sse,
)
from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.storage import crc32c, crc32c_to_base64
from typer.testing import Result

Invoke = Callable[..., Result]
AsJson = Callable[[Result], Any]
Ex = Callable[..., dict[str, Any]]

URL = "http://api.test"
V1 = f"{URL}/v1"
STORAGE = "http://storage.test"


def page(items: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {"items": items, "next_cursor": cursor}


def run_json(example: Ex, state: str = "running", **changes: Any) -> dict[str, Any]:
    return example(Run, 0, state=state, **changes)


def sse(*frames: str) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content="".join(frames).encode()
    )


# ----------------------------------------------------------------------------------- runs


def test_runs_ls_and_get(mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson) -> None:
    route = api.get(f"{V1}/runs").respond(200, json=page([run_json(example)]))
    res = mapi("--url", URL, "runs", "ls", "--state", "running", "-n", "5")
    assert res.exit_code == 0, res.output
    assert "run_01jaf3q2x8m9v7k5h4g3f2e1d0" in res.stdout and "40%" in res.stdout
    params = route.calls.last.request.url.params
    assert params["state"] == "running" and params["limit"] == "5"
    failed = example(Run, 1)
    api.get(f"{V1}/runs/{failed['id']}").respond(200, json=failed)
    res = mapi("--url", URL, "runs", "get", failed["id"])
    assert res.exit_code == 0
    assert "RESOURCE_EXHAUSTED" in res.stderr and "class+1" not in res.stderr
    assert "retry at a larger resource class" in res.stderr
    js = mapi("--url", URL, "--json", "runs", "get", failed["id"])
    assert as_json(js)["failure_class"] == "resource_exhausted"


def test_runs_logs_strip_terminal_escapes(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    evil = example(LogLine, line="\x1b[2J\x1b[31mpwned\x1b[0m [red]x[/red]")
    api.get(f"{V1}/runs/run_01/logs").respond(200, json=page([evil]))
    res = mapi("--url", URL, "runs", "logs", "run_01", "--stream", "stderr")
    assert res.exit_code == 0
    assert "\x1b" not in res.stdout and "pwned [red]x[/red]" in res.stdout


def test_runs_watch_follows_sse(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    run = Run.model_validate(run_json(example))
    done = Run.model_validate(run_json(example, "succeeded", output_model_version_id="mv_01"))
    ev = example(RunEventView)
    progress = RunEventView.model_validate(
        ev | {"id": 18, "type": "progress", "payload": {"phase": "sample", "fraction": 0.5}}
    )
    log = RunEventView.model_validate(
        ev | {"id": 19, "type": "log", "payload": {"stream": "stderr", "line": "hello"}}
    )
    api.get(f"{V1}/runs/{run.id}").mock(
        side_effect=[
            httpx.Response(200, json=run.model_dump(mode="json")),
            httpx.Response(200, json=done.model_dump(mode="json")),
        ]
    )
    api.get(f"{V1}/runs/{run.id}/events").mock(
        return_value=sse(
            encode_sse("snapshot", run),
            encode_sse("progress", progress, id=18),
            encode_sse("log", log, id=19),
            encode_sse("end", done),
        )
    )
    res = mapi("--url", URL, "runs", "watch", run.id)
    assert res.exit_code == 0, res.output
    assert "hello" in res.stderr and "mv_01" in res.stdout


def test_runs_watch_json_lines(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    run = Run.model_validate(run_json(example))
    failed = Run.model_validate(example(Run, 1))
    api.get(f"{V1}/runs/{run.id}").respond(200, json=failed.model_dump(mode="json"))
    route = api.get(f"{V1}/runs/{run.id}/events").mock(
        return_value=sse(encode_sse("snapshot", run), encode_sse("end", failed))
    )
    res = mapi("--url", URL, "--json", "runs", "watch", run.id, "--from", "7")
    assert res.exit_code == 1  # the run failed
    events = [json.loads(line)["event"] for line in res.stdout.splitlines()]
    assert events == ["snapshot", "end"]
    assert route.calls.last.request.headers["Last-Event-ID"] == "7"


def test_runs_cancel_and_retry(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.post(f"{V1}/runs/run_01:cancel").respond(202, json=run_json(example, "cancelling"))
    retry = api.post(f"{V1}/runs/run_01:retry").respond(201, json=run_json(example, "created"))
    assert "cancelling" in mapi("--url", URL, "runs", "cancel", "run_01").stdout
    res = mapi("--url", URL, "runs", "retry", "run_01", "--resource-class", "L")
    assert res.exit_code == 0, res.output
    assert json.loads(retry.calls.last.request.content) == {"resource_class": "L"}


def test_api_errors_and_unreachable_api(mapi: Invoke, api: respx.MockRouter) -> None:
    api.get(f"{V1}/runs/nope").respond(
        404, json=Problem.from_code(ErrorCode.NOT_FOUND, "run nope not found").model_dump()
    )
    res = mapi("--url", URL, "runs", "get", "nope")
    assert res.exit_code == 1 and "NOT_FOUND" in res.stderr and "run nope not found" in res.stderr
    js = mapi("--url", URL, "--json", "runs", "get", "nope")
    assert json.loads(js.stdout)["error"]["code"] == "NOT_FOUND"
    api.get(f"{V1}/runs").mock(side_effect=httpx.ConnectError("refused"))
    down = mapi("--url", URL, "runs", "ls")
    assert down.exit_code == 1 and "INFRA_ERROR" in down.stderr


# ------------------------------------------------------------------------------- datasets


def test_datasets_ls_and_upload(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, as_json: AsJson
) -> None:
    ds = example(Dataset)
    api.get(f"{V1}/datasets").respond(200, json=page([ds]))
    res = mapi("--url", URL, "datasets", "ls")
    assert res.exit_code == 0 and "eu_media_daily" in res.stdout

    csv = tmp_path / "media.csv"
    content = b"market,date,revenue,spend\nde,2024-01-01,1,2\n"
    csv.write_bytes(content)
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(
        "format: csv\n"
        "dims: {market: {column: market, spec: {kind: categorical}},"
        " date: {column: date, spec: {kind: time, policy: extend, freq: D}}}\n"
        "vars: {revenue: {column: revenue, role: target}}\n"
    )
    sess = example(UploadSession, size_bytes=len(content))
    sess["upload"] = sess["upload"] | {"url": f"{STORAGE}/up?sig=1", "max_bytes": None}
    api.post(f"{V1}/datasets/{ds['id']}/uploads").respond(201, json=sess)
    put = api.put(f"{STORAGE}/up").respond(200, json={"crc32c": crc32c_to_base64(crc32c(content))})
    version = api.post(f"{V1}/datasets/{ds['id']}/versions").respond(
        202, json=example(DatasetVersion, status="ingesting")
    )
    api.get(f"{V1}/datasets/{ds['id']}/versions/3").respond(200, json=example(DatasetVersion))
    res = mapi(
        "--url",
        URL,
        "--json",
        "datasets",
        "upload",
        str(csv),
        "--name",
        "eu_media_daily",
        "--mapping",
        str(mapping),
    )
    assert res.exit_code == 0, res.output
    assert as_json(res)["status"] == "ready"
    assert put.calls.last.request.content == content
    assert json.loads(version.calls.last.request.content)["mapping"]["format"] == "csv"

    no_map = mapi("--url", URL, "datasets", "upload", str(csv), "--name", "eu_media_daily")
    assert no_map.exit_code == 1 and "IngestMapping" in no_map.stderr


# ------------------------------------------------------------------------- check/catalog


def test_check_remote(mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "glm", "-o", str(spec)).exit_code == 0
    route = api.post(f"{V1}/specs:validate").respond(200, json=example(ValidateResponse))
    res = mapi(
        "--url",
        URL,
        "check",
        str(spec),
        "--remote",
        "@",
        "--dataset",
        "dataset://default/radon_homes@v1",
    )
    assert res.exit_code == 1 and "LEAKAGE_RISK" in res.stdout
    body = json.loads(route.calls.last.request.content)
    assert body["dataset"] == "dataset://default/radon_homes@v1" and "yaml" in body
    ok = example(ValidateResponse, valid=True, issues=[])
    api.post("http://other.test/v1/specs:validate").respond(200, json=ok)
    assert mapi("check", str(spec), "--remote", "http://other.test").exit_code == 0


def test_catalog_remote(mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson) -> None:
    api.get(f"{V1}/catalog").respond(200, json=example(CatalogResponse))
    res = mapi("--url", URL, "--json", "catalog", "steps", "--remote", "@")
    assert res.exit_code == 0, res.output
    doc = as_json(res)
    assert doc["digest"] == "sha256:" + "cd" * 32 and doc["steps"][0]["id"] == "core.log1p"


# --------------------------------------------------------------------------- fit/score


def _remote_fit_routes(api: respx.MockRouter, example: Ex, *, final_state: str) -> respx.Route:
    ds = example(Dataset, name="radon_homes")
    api.get(f"{V1}/datasets").respond(200, json=page([ds]))
    sess = example(UploadSession)
    sess["upload"] = sess["upload"] | {"url": f"{STORAGE}/up?sig=1", "max_bytes": None}
    api.post(f"{V1}/datasets/{ds['id']}/uploads").respond(201, json=sess)
    api.put(f"{STORAGE}/up").respond(200)
    api.post(f"{V1}/datasets/{ds['id']}/versions").respond(
        202, json=example(DatasetVersion, status="ingesting")
    )
    api.get(f"{V1}/datasets/{ds['id']}/versions/3").respond(200, json=example(DatasetVersion))
    api.get(f"{V1}/specs").respond(200, json=page([]))
    api.post(f"{V1}/specs").respond(201, json=example(SpecDoc) | {"latest": example(SpecRevision)})
    created = run_json(example, "queued")
    fit = api.post(f"{V1}/runs").respond(201, json=created)
    final = Run.model_validate(
        example(Run, 1)
        if final_state == "failed"
        else run_json(example, final_state, output_model_version_id="mv_01")
    )
    api.get(f"{V1}/runs/{created['id']}/events").mock(
        return_value=sse(
            encode_sse("snapshot", Run.model_validate(created)), encode_sse("end", final)
        )
    )
    return fit


def test_fit_remote(mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "glm", "-o", str(spec)).exit_code == 0
    data = tmp_path / "data.zarr.zip"
    data.write_bytes(b"PK-bytes")
    fit = _remote_fit_routes(api, example, final_state="succeeded")
    res = mapi(
        "--url",
        URL,
        "fit",
        str(spec),
        "--data",
        str(data),
        "--remote",
        "@",
        "--resource-class",
        "M",
    )
    assert res.exit_code == 0, res.output
    body = json.loads(fit.calls.last.request.content)
    assert body["kind"] == "fit" and body["resource_class"] == "M"
    assert "mv_01" in res.stdout and "uploading" in res.stderr


def test_fit_remote_failure_exits_1(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "glm", "-o", str(spec)).exit_code == 0
    _remote_fit_routes(api, example, final_state="failed")
    res = mapi("--url", URL, "fit", str(spec), "--remote", "@")
    assert res.exit_code == 1 and "RESOURCE_EXHAUSTED" in res.stderr


def test_fit_remote_rejects_invalid_revisions(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "glm", "-o", str(spec)).exit_code == 0
    issue = {"code": "LEAKAGE_RISK", "pointer": "/pipeline/0", "message": "leaks"}
    rev = example(SpecRevision, valid=False, issues=[issue])
    api.get(f"{V1}/specs").respond(200, json=page([]))
    api.post(f"{V1}/specs").respond(201, json=example(SpecDoc) | {"latest": rev})
    runs = api.post(f"{V1}/runs").respond(201, json=run_json(example))
    res = mapi("--url", URL, "fit", str(spec), "--remote", "@")
    assert res.exit_code == 1 and "LEAKAGE_RISK" in res.stdout
    assert runs.call_count == 0


def test_score_remote(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path, as_json: AsJson
) -> None:
    tree = xr.DataTree.from_dict({"summary": xr.Dataset({"y_hat": (("t",), np.ones(3))})})
    data = write_datatree_zip(tree)
    assert data is not None
    link = {
        "name": "score.zarr.zip",
        "url": f"{STORAGE}/score.zarr.zip?sig=1",
        "expires_at": "2026-10-15T09:45:00Z",
        "size": len(data),
        "crc32c": crc32c_to_base64(crc32c(data)),
        "generation": 2,
    }
    route = api.post(f"{V1}/models/mdl_1/versions/2:score").respond(
        200, json=example(ScoreCallResult) | {"outputs": [link]}
    )
    api.get(f"{STORAGE}/score.zarr.zip").respond(200, content=data)
    req = tmp_path / "req.yaml"
    req.write_text("apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: model://default/m@v2\n")
    out = tmp_path / "out.zarr.zip"
    res = mapi(
        "--url",
        URL,
        "--json",
        "score",
        str(req),
        "--remote",
        "@",
        "--model",
        "mdl_1",
        "--version",
        "2",
        "--out",
        str(out),
        "--wait",
        "30",
    )
    assert res.exit_code == 0, res.output
    assert as_json(res)["outputs"][0]["name"] == "score.zarr.zip"
    assert out.read_bytes() == data
    assert route.calls.last.request.headers["Prefer"] == "wait=30"
    human = mapi(
        "--url", URL, "score", str(req), "--remote", "@", "--model", "mdl_1", "--version", "2"
    )
    assert human.exit_code == 0 and "/summary" in human.stdout
