"""The synchronous client against a respx-mocked API and storage service."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import respx
import xarray as xr
from mapi_client import (
    Client,
    ClientConfig,
    RunEnd,
    RunEventFrame,
    RunSnapshot,
    TransportError,
)
from mapi_sdk.wire import read_zarr_zip, write_datatree_zip
from mapi_spec.api import (
    ArtifactLink,
    CatalogResponse,
    Dataset,
    DatasetVersion,
    IngestMapping,
    LogLine,
    Page,
    PlotValidateResponse,
    Run,
    RunEventView,
    ScoreCallResult,
    SpecDoc,
    SpecRevision,
    UploadSession,
    ValidateResponse,
    encode_sse,
)
from mapi_spec.errors import (
    ErrorCode,
    IntegrityFailure,
    MapiError,
    Problem,
    ResourceExhausted,
    ValidationFailed,
)
from mapi_spec.storage import crc32c, crc32c_to_base64

BASE = "http://api.test"
STORAGE = "http://storage.test"
V1 = f"{BASE}/v1"
Ex = Callable[..., dict[str, Any]]


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content)


def run_json(example: Ex, state: str = "running", **changes: Any) -> dict[str, Any]:
    data = example(Run, 0, state=state, **changes)
    if state in ("succeeded", "failed", "partial", "cancelled", "rejected"):
        data["finished_at"] = "2026-10-15T09:40:00Z"
    return data


def page(items: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {"items": items, "next_cursor": cursor}


# ------------------------------------------------------------------------- basics


def test_health_and_ready(client: Client, api: respx.MockRouter, example: Ex) -> None:
    from mapi_spec.api import Health

    api.get(f"{BASE}/healthz").respond(200, json=example(Health))
    ready = example(Health, status="fail")
    api.get(f"{BASE}/readyz").respond(503, json=ready)
    assert client.health().status == "ok"
    assert client.ready().status == "fail"  # 503 carries the Health body


def test_bearer_token_goes_to_the_api_only(
    api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    get_run = api.get(f"{V1}/runs/run_01").respond(200, json=run_json(example))
    data = b"hello"
    link = ArtifactLink(
        name="a.json",
        url=f"{STORAGE}/a.json?sig=1",
        expires_at="2026-10-15T09:45:00Z",
        size=len(data),
        crc32c=crc32c_to_base64(crc32c(data)),
        generation=1,
    )
    dl = api.get(f"{STORAGE}/a.json").respond(200, content=data)
    cfg = ClientConfig(base_url=BASE.replace("api.test", "127.0.0.1"), token="s3cret")
    with Client(config=cfg.with_overrides(allow_insecure_token=True, base_url=BASE)) as c:
        c.runs.get("run_01")
        assert c.download(link) == data
    assert get_run.calls.last.request.headers["Authorization"] == "Bearer s3cret"
    assert "Authorization" not in dl.calls.last.request.headers
    assert get_run.calls.last.request.headers["User-Agent"].startswith("mapi-client/")


def test_no_auth_header_without_token(client: Client, api: respx.MockRouter, example: Ex) -> None:
    route = api.get(f"{V1}/runs/run_01").respond(200, json=run_json(example))
    client.runs.get("run_01")
    assert "Authorization" not in route.calls.last.request.headers


# ------------------------------------------------------------------------ retries


def test_get_is_retried_on_503_with_retry_after(
    client: Client, api: respx.MockRouter, example: Ex, sleeps: list[float]
) -> None:
    route = api.get(f"{V1}/runs/run_01").mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "2"}),
            httpx.Response(200, json=run_json(example)),
        ]
    )
    assert client.runs.get("run_01").id.startswith("run_")
    assert route.call_count == 2 and sleeps == [2.0]


def test_post_without_key_is_not_retried(client: Client, api: respx.MockRouter) -> None:
    route = api.post(f"{V1}/specs").respond(503)
    with pytest.raises(MapiError) as err:
        client.specs.create("apiVersion: mapi/v1\n")
    assert err.value.code is ErrorCode.INFRA_ERROR
    assert route.call_count == 1


def test_post_with_idempotency_key_is_retried_with_the_same_key(
    client: Client, api: respx.MockRouter, example: Ex
) -> None:
    route = api.post(f"{V1}/runs").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(201, json=run_json(example))]
    )
    client.runs.create({"kind": "fit", "spec_id": "spc_1", "revision": 2})
    keys = [c.request.headers["Idempotency-Key"] for c in route.calls]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert body_of(route.calls.last.request) == {
        "kind": "fit",
        "spec_id": "spc_1",
        "revision": 2,
        "params": {},
        "labels": {},
    }


def test_idempotent_replay_200_is_accepted(
    client: Client, api: respx.MockRouter, example: Ex
) -> None:
    route = api.post(f"{V1}/runs").respond(200, json=run_json(example))
    client.runs.create({"kind": "fit", "spec_id": "spc_1", "revision": 2}, idempotency_key="k-1")
    assert route.calls.last.request.headers["Idempotency-Key"] == "k-1"
    with pytest.raises(ValueError):
        client.runs.create({"kind": "fit", "spec_id": "s", "revision": 1}, idempotency_key="a b")


def test_transport_errors_exhaust_retries(
    client: Client, api: respx.MockRouter, sleeps: list[float]
) -> None:
    route = api.get(f"{V1}/runs/run_01").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TransportError):
        client.runs.get("run_01")
    assert route.call_count == 3 and len(sleeps) == 2


# --------------------------------------------------------------------- pagination


def test_pagination_follows_cursors_lazily(
    client: Client, api: respx.MockRouter, example: Ex
) -> None:
    a, b = example(Dataset, name="a"), example(Dataset, name="b")
    route = api.get(f"{V1}/datasets").mock(
        side_effect=[
            httpx.Response(200, json=page([a], "c1")),
            httpx.Response(200, json=page([b])),
        ]
    )
    pager = client.datasets.list(limit=1)
    assert route.call_count == 0
    assert [d.name for d in pager] == ["a", "b"]
    assert route.calls[0].request.url.params["limit"] == "1"
    assert route.calls[1].request.url.params["cursor"] == "c1"


def test_take_stops_early(client: Client, api: respx.MockRouter, example: Ex) -> None:
    route = api.get(f"{V1}/models").respond(200, json=page([], None))
    assert client.models.list().take(5) == []
    assert route.call_count == 1


def test_cursor_loops_are_detected(client: Client, api: respx.MockRouter, example: Ex) -> None:
    from mapi_client import UnexpectedResponse

    api.get(f"{V1}/specs").respond(200, json=page([example(SpecDoc)], "same"))
    with pytest.raises(UnexpectedResponse, match="twice"):
        list(client.specs.list())


def test_run_filters_and_logs(client: Client, api: respx.MockRouter, example: Ex) -> None:
    runs = api.get(f"{V1}/runs").respond(200, json=page([run_json(example)]))
    logs = api.get(f"{V1}/runs/run_01/logs").respond(200, json=page([example(LogLine)]))
    assert len(list(client.runs.list(kind="fit", state="running"))) == 1
    assert runs.calls.last.request.url.params["state"] == "running"
    lines = list(client.runs.logs("run_01", stream="stderr", attempt=1))
    assert lines[0].stream == "stderr"
    assert logs.calls.last.request.url.params["stream"] == "stderr"


def test_event_pages_continue_after_the_last_id(
    client: Client, api: respx.MockRouter, example: Ex
) -> None:
    ev = example(RunEventView)
    route = api.get(f"{V1}/runs/run_01/events").mock(
        side_effect=[
            httpx.Response(200, json=page([ev | {"id": 5}], "opaque")),
            httpx.Response(200, json=page([ev | {"id": 9}])),
        ]
    )
    ids = [e.id for e in client.runs.events("run_01", after=2)]
    assert ids == [5, 9]
    assert route.calls[0].request.url.params["after"] == "2"
    assert route.calls[1].request.url.params["after"] == "5"
    assert route.calls[0].request.headers["Accept"] == "application/json"


# --------------------------------------------------------------------------- runs


def test_wait_polls_until_terminal(
    client: Client, api: respx.MockRouter, example: Ex, sleeps: list[float]
) -> None:
    api.get(f"{V1}/runs/run_01").mock(
        side_effect=[
            httpx.Response(200, json=run_json(example, "queued")),
            httpx.Response(200, json=run_json(example, "running")),
            httpx.Response(200, json=run_json(example, "succeeded")),
        ]
    )
    seen: list[str] = []
    run = client.runs.wait("run_01", on_update=lambda r: seen.append(r.state.value))
    assert run.state.value == "succeeded"
    assert seen == ["queued", "running", "succeeded"]
    assert sleeps == [1.0, 1.5]


def test_wait_raises_the_run_problem(client: Client, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/runs/run_01").respond(200, json=example(Run, 1))
    with pytest.raises(ResourceExhausted, match="exit 137"):
        client.runs.wait("run_01", raise_on_failure=True)
    assert client.runs.wait("run_01").state.value == "failed"


def test_wait_times_out(client: Client, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/runs/run_01").respond(200, json=run_json(example, "running"))
    with pytest.raises(TimeoutError):
        client.runs.wait("run_01", timeout=0.5, poll_interval=1.0)


def test_cancel_resume_retry(client: Client, api: respx.MockRouter, example: Ex) -> None:
    cancel = api.post(f"{V1}/runs/run_01:cancel").respond(202, json=run_json(example, "cancelling"))
    api.post(f"{V1}/runs/run_01:resume").respond(202, json=run_json(example, "queued"))
    retry = api.post(f"{V1}/runs/run_01:retry").respond(201, json=run_json(example, "created"))
    assert client.runs.cancel("run_01").state.value == "cancelling"
    assert client.runs.resume("run_01").state.value == "queued"
    client.runs.retry("run_01", resource_class="L", reason="oom")
    assert body_of(retry.calls.last.request) == {"resource_class": "L", "reason": "oom"}
    assert "Idempotency-Key" in retry.calls.last.request.headers
    assert cancel.call_count == 1


# ---------------------------------------------------------------------------- SSE


def _sse(*frames: str) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content="".join(frames).encode()
    )


def _live(kind: str, seq: int) -> str:
    data = (
        f'{{"run_id":"run_01","attempt":1,"seq":{seq},"ts":"2026-10-15T00:00:00Z",'
        f'"type":"{kind}","payload":{{"fraction":0.5}}}}'
    )
    return f"event: {kind}\ndata: {data}\n\n"


def test_stream_resumes_with_last_event_id(
    client: Client, api: respx.MockRouter, example: Ex, sleeps: list[float]
) -> None:
    run = Run.model_validate(run_json(example))
    done = Run.model_validate(run_json(example, "succeeded"))
    ev = example(RunEventView)

    def event(i: int, kind: str = "state") -> str:
        view = RunEventView.model_validate(ev | {"id": i, "type": kind})
        return encode_sse(kind, view, id=i)

    route = api.get(f"{V1}/runs/run_01/events").mock(
        side_effect=[
            _sse(
                encode_sse("snapshot", run, retry_ms=250),
                event(1),
                ": keepalive\n\n",
                event(2, "progress"),
                _live("progress", 5),  # live frames carry no id and never move the cursor
            ),  # connection drops before 'end'
            _sse(
                encode_sse("snapshot", run), event(2), event(3, "complete"), encode_sse("end", done)
            ),
        ]
    )
    items = list(client.runs.stream_events("run_01"))
    kinds = [type(i).__name__ for i in items]
    assert kinds == [
        "RunSnapshot",
        "RunEventFrame",
        "RunEventFrame",
        "RunLiveFrame",
        "RunSnapshot",
        "RunEventFrame",
        "RunEnd",
    ]
    ids = [i.id for i in items if isinstance(i, RunEventFrame)]
    assert ids == [1, 2, 3]  # the overlapping replay of 2 was dropped
    first, second = (c.request for c in route.calls)
    assert first.headers["Accept"] == "text/event-stream"
    assert "Last-Event-ID" not in first.headers
    assert second.headers["Last-Event-ID"] == "2"
    assert sleeps == [0.25]  # the server's retry: hint
    assert isinstance(items[0], RunSnapshot) and isinstance(items[-1], RunEnd)
    assert items[-1].run.state.value == "succeeded"


def test_stream_gives_up_after_max_reconnects(
    client: Client, api: respx.MockRouter, example: Ex
) -> None:
    api.get(f"{V1}/runs/run_01/events").mock(side_effect=httpx.ReadTimeout("silent"))
    with pytest.raises(TransportError, match="ended before the run did"):
        list(client.runs.stream_events("run_01", max_reconnects=2))


def test_stream_errors_are_not_retried(client: Client, api: respx.MockRouter) -> None:
    api.get(f"{V1}/runs/nope/events").respond(
        404, json=Problem.from_code(ErrorCode.NOT_FOUND, "no run").model_dump(mode="json")
    )
    with pytest.raises(MapiError) as err:
        list(client.runs.stream_events("nope"))
    assert err.value.code is ErrorCode.NOT_FOUND


def test_stream_rejects_non_sse_bodies(client: Client, api: respx.MockRouter) -> None:
    from mapi_client import UnexpectedResponse

    api.get(f"{V1}/runs/run_01/events").respond(200, json={"items": []})
    with pytest.raises(UnexpectedResponse, match="event stream"):
        list(client.runs.stream_events("run_01"))


# ------------------------------------------------------------------------ datasets


def _session(example: Ex, size: int, ctype: str) -> dict[str, Any]:
    data = example(UploadSession, size_bytes=size, content_type=ctype)
    data["upload"] = data["upload"] | {
        "url": f"{STORAGE}/upload/quarantine?sig=abc",
        "max_bytes": 10_000_000,
        "headers": {"content-type": ctype, "x-goog-if-generation-match": "0"},
    }
    return data


def test_upload_csv_end_to_end(
    client: Client, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    csv = tmp_path / "eu_media.csv"
    content = b"market,date,revenue,spend\nde,2024-01-01,1.0,2.0\n"
    csv.write_bytes(content)
    ds = example(Dataset)
    ds_id = ds["id"]
    api.get(f"{V1}/datasets").respond(200, json=page([]))
    create = api.post(f"{V1}/datasets").respond(201, json=ds)
    session = api.post(f"{V1}/datasets/{ds_id}/uploads").respond(
        201, json=_session(example, len(content), "text/csv")
    )
    put = api.put(f"{STORAGE}/upload/quarantine").respond(
        200, json={"name": "t/default/uploads/x", "crc32c": crc32c_to_base64(crc32c(content))}
    )
    ingesting = example(DatasetVersion, status="ingesting", sha256=None)
    version = api.post(f"{V1}/datasets/{ds_id}/versions").respond(202, json=ingesting)
    api.get(f"{V1}/datasets/{ds_id}/versions/3").mock(
        side_effect=[
            httpx.Response(200, json=ingesting),
            httpx.Response(200, json=example(DatasetVersion)),
        ]
    )
    mapping = IngestMapping.model_validate(IngestMapping.model_json_schema()["examples"][0])
    out = client.datasets.upload(csv, dataset="eu_media_daily", mapping=mapping, message="q3")
    assert out.status.value == "ready" and out.version == 3
    assert body_of(create.calls.last.request)["name"] == "eu_media_daily"
    assert body_of(session.calls.last.request) == {
        "filename": "eu_media.csv",
        "content_type": "text/csv",
        "size_bytes": len(content),
    }
    put_req = put.calls.last.request
    assert put_req.content == content
    assert put_req.headers["x-goog-if-generation-match"] == "0"
    assert put_req.headers["content-type"] == "text/csv"
    assert "Authorization" not in put_req.headers
    vbody = body_of(version.calls.last.request)
    assert vbody["upload_id"] == example(UploadSession)["id"]
    assert vbody["mapping"]["format"] == "csv" and vbody["message"] == "q3"
    assert "Idempotency-Key" in version.calls.last.request.headers


def test_upload_xarray_dataset_as_zarr_zip(
    client: Client, api: respx.MockRouter, example: Ex
) -> None:
    ds = example(Dataset)
    data = xr.Dataset({"y": (("t",), np.arange(4.0))}, coords={"t": np.arange(4)})
    api.get(f"{V1}/datasets").respond(200, json=page([ds]))
    session = api.post(f"{V1}/datasets/{ds['id']}/uploads").respond(
        201, json=_session(example, 1, "application/zip")
    )
    put = api.put(f"{STORAGE}/upload/quarantine").respond(200)
    version = api.post(f"{V1}/datasets/{ds['id']}/versions").respond(
        202, json=example(DatasetVersion, status="ingesting")
    )
    out = client.datasets.upload(data, dataset=ds["name"], wait=False)
    assert out.status.value == "ingesting"
    assert body_of(session.calls.last.request)["filename"] == "data.zarr.zip"
    assert read_zarr_zip(put.calls.last.request.content)["y"].values.tolist() == [0, 1, 2, 3]
    assert body_of(version.calls.last.request)["mapping"] == {
        "format": "zarr_zip",
        "layout": "dense",
        "dims": {},
        "vars": {},
    }


def test_upload_failure_surfaces_the_ingest_problem(
    client: Client, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    f = tmp_path / "d.zarr.zip"
    f.write_bytes(b"PK..")
    ds = example(Dataset)
    api.post(f"{V1}/datasets/{ds['id']}/uploads").respond(
        201, json=_session(example, 4, "application/zip")
    )
    api.put(f"{STORAGE}/upload/quarantine").respond(200)
    failed = example(DatasetVersion, status="failed")
    api.post(f"{V1}/datasets/{ds['id']}/versions").respond(202, json=failed)
    api.get(f"{V1}/datasets/{ds['id']}/versions/3").respond(200, json=failed)
    problem = Problem.from_code(ErrorCode.USER_CODE_ERROR, "zarr store could not be decoded")
    api.get(f"{V1}/runs/{failed['ingest_run_id']}").respond(
        200, json=run_json(example, "failed", kind="ingest", problem=problem.model_dump())
    )
    with pytest.raises(MapiError, match="could not be decoded") as err:
        client.datasets.upload(f, dataset_id=ds["id"])
    assert err.value.code is ErrorCode.USER_CODE_ERROR


def test_upload_failure_with_issues_only(
    client: Client, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    f = tmp_path / "d.nc"
    f.write_bytes(b"CDF\x01")
    ds = example(Dataset)
    api.post(f"{V1}/datasets/{ds['id']}/uploads").respond(
        201, json=_session(example, 4, "application/x-netcdf")
    )
    api.put(f"{STORAGE}/upload/quarantine").respond(200)
    issue = {"code": "SCHEMA_INVALID", "pointer": "/vars/y", "message": "y has NaNs"}
    failed = example(DatasetVersion, status="failed", ingest_run_id=None, issues=[issue])
    api.post(f"{V1}/datasets/{ds['id']}/versions").respond(202, json=failed)
    api.get(f"{V1}/datasets/{ds['id']}/versions/3").respond(200, json=failed)
    with pytest.raises(ValidationFailed, match="y has NaNs"):
        client.datasets.upload(f, dataset_id=ds["id"])


def test_upload_detects_corruption(
    client: Client, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    f = tmp_path / "d.zarr.zip"
    f.write_bytes(b"PK....")
    ds = example(Dataset)
    api.post(f"{V1}/datasets/{ds['id']}/uploads").respond(
        201, json=_session(example, 6, "application/zip")
    )
    api.put(f"{STORAGE}/upload/quarantine").respond(200, json={"crc32c": "AAAAAA=="})
    with pytest.raises(IntegrityFailure, match="corrupted in transit"):
        client.datasets.upload(f, dataset_id=ds["id"])


def test_upload_argument_checks(client: Client, tmp_path: Path) -> None:
    f = tmp_path / "d.csv"
    f.write_text("a\n1\n")
    with pytest.raises(ValueError, match="exactly one"):
        client.datasets.upload(f)
    with pytest.raises(ValueError, match="IngestMapping"):
        client.datasets.upload(f, dataset="x")
    with pytest.raises(ValueError, match="cannot tell the format"):
        client.datasets.upload(tmp_path / "d.csv", dataset="x", filename="d.xlsx")
    with pytest.raises(FileNotFoundError):
        client.datasets.upload(tmp_path / "missing.csv", dataset="x")


def test_upload_respects_max_bytes(
    client: Client, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    from mapi_spec.errors import PayloadTooLarge

    f = tmp_path / "d.zarr.zip"
    f.write_bytes(b"x" * 100)
    ds = example(Dataset)
    sess = _session(example, 100, "application/zip")
    sess["upload"]["max_bytes"] = 10
    api.post(f"{V1}/datasets/{ds['id']}/uploads").respond(201, json=sess)
    put = api.put(f"{STORAGE}/upload/quarantine").respond(200)
    with pytest.raises(PayloadTooLarge):
        client.datasets.upload(f, dataset_id=ds["id"])
    assert put.call_count == 0


# -------------------------------------------------------------------------- specs


def test_specs_validate_create_revise_upsert(
    client: Client, api: respx.MockRouter, example: Ex
) -> None:
    yaml = "apiVersion: mapi/v1\nkind: ModelSpec\nmetadata: {name: eu-geo-mmm, project: x}\n"
    val = api.post(f"{V1}/specs:validate").respond(200, json=example(ValidateResponse))
    res = client.specs.validate(yaml, dataset="dataset://default/eu@v3")
    assert res.valid is False and res.issues[0].code is ErrorCode.LEAKAGE_RISK
    assert body_of(val.calls.last.request) == {"yaml": yaml, "dataset": "dataset://default/eu@v3"}

    doc = example(SpecDoc)
    api.get(f"{V1}/specs/{doc['id']}").respond(200, json=doc)
    rev = api.post(f"{V1}/specs/{doc['id']}/revisions").respond(201, json=example(SpecRevision))
    client.specs.revise(doc["id"], yaml, message="tune")
    assert body_of(rev.calls.last.request) == {
        "yaml": yaml,
        "parent_revision": 2,
        "message": "tune",
    }

    api.get(f"{V1}/specs").respond(200, json=page([doc]))
    out = client.specs.upsert(yaml)
    assert out.revision == 2 and rev.call_count == 2

    api.get(f"{V1}/specs").respond(200, json=page([]))
    created = api.post(f"{V1}/specs").respond(201, json=doc | {"latest": example(SpecRevision)})
    assert client.specs.upsert(yaml).spec_id == doc["id"]
    assert created.call_count == 1
    with pytest.raises(ValueError, match=r"metadata\.name"):
        client.specs.upsert("apiVersion: mapi/v1\n")


def test_validate_request_needs_exactly_one_source(client: Client) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        client.specs.validate()


# ------------------------------------------------------------------- models/score


def _tree_bytes() -> bytes:
    tree = xr.DataTree.from_dict(
        {
            "summary": xr.Dataset(
                {"y_hat": (("stat", "t"), np.ones((2, 3)))},
                coords={"stat": ["mean", "sd"], "t": [0, 1, 2]},
            )
        }
    )
    out = write_datatree_zip(tree)
    assert out is not None
    return out


def _link(name: str, data: bytes) -> dict[str, Any]:
    return {
        "name": name,
        "url": f"{STORAGE}/out/{name}?sig=1",
        "expires_at": "2026-10-15T09:45:00Z",
        "size": len(data),
        "crc32c": crc32c_to_base64(crc32c(data)),
        "generation": 4,
    }


def test_score_long_polls_with_the_same_key_and_returns_a_datatree(
    client: Client, api: respx.MockRouter, example: Ex
) -> None:
    data = _tree_bytes()
    running = example(ScoreCallResult) | {"done": False, "outputs": []}
    running["run"] = running["run"] | {"state": "running"}
    done = example(ScoreCallResult) | {"outputs": [_link("score.zarr.zip", data)]}
    route = api.post(f"{V1}/models/mdl_1/versions/1:score").mock(
        side_effect=[httpx.Response(202, json=running), httpx.Response(200, json=done)]
    )
    api.get(f"{STORAGE}/out/score.zarr.zip").respond(200, content=data)
    request = {
        "model": "model://default/eu@v1",
        "mode": "forecast",
        "horizon": {"dim": "time", "periods": 8},
    }
    result = client.models.score("mdl_1", 1, request, wait=30)
    assert result.done and result.tree is not None
    assert result.tree["summary"]["y_hat"].shape == (2, 3)
    first, second = (c.request for c in route.calls)
    assert first.headers["Prefer"] == "wait=30"
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    sent = body_of(first)
    assert sent["request"]["apiVersion"] == "mapi/v1" and sent["request"]["kind"] == "ScoreRequest"


def test_score_failure_raises(client: Client, api: respx.MockRouter, example: Ex) -> None:
    failed = example(ScoreCallResult) | {"outputs": []}
    problem = Problem.from_code(ErrorCode.HORIZON_EXCEEDS_FIT, "26 > max_extension 12")
    failed["run"] = failed["run"] | {"state": "failed", "problem": problem.model_dump()}
    api.post(f"{V1}/models/mdl_1/versions/1:score").respond(200, json=failed)
    req = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: model://default/eu@v1\n"
    with pytest.raises(MapiError) as err:
        client.models.score("mdl_1", 1, req)
    assert err.value.code is ErrorCode.HORIZON_EXCEEDS_FIT
    res = client.models.score("mdl_1", 1, req, raise_on_failure=False)
    assert res.run.state.value == "failed" and res.tree is None


def test_score_timeout(client: Client, api: respx.MockRouter, example: Ex) -> None:
    running = example(ScoreCallResult) | {"done": False, "outputs": []}
    running["run"] = running["run"] | {"state": "running"}
    api.post(f"{V1}/models/mdl_1/versions/1:score").respond(202, json=running)
    req = {"model": "model://default/eu@v1"}
    with pytest.raises(TimeoutError):
        client.models.score("mdl_1", 1, req, timeout=0.01, poll_interval=0.0)


def test_score_read_timeout_covers_the_prefer_wait(api: respx.MockRouter, example: Ex) -> None:
    """The server may hold a ``Prefer: wait=60`` response for 60 s: the read timeout of
    that request must exceed it, whatever the client's (shorter) default timeout is."""
    done = example(ScoreCallResult) | {"outputs": []}
    seen: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.extensions["timeout"]))
        return httpx.Response(200, json=done)

    api.post(f"{V1}/models/mdl_1/versions/1:score").mock(side_effect=respond)
    get = api.get(f"{V1}/runs/run_01").respond(200, json=run_json(example))
    with Client(config=ClientConfig(base_url=BASE, timeout=1.5)) as c:
        c.models.score("mdl_1", 1, {"model": "model://default/eu@v1"}, download=False)
        c.models.score("mdl_1", 1, {"model": "model://default/eu@v1"}, wait=5, download=False)
        c.runs.get("run_01")
    assert seen[0]["read"] >= 60 + 1.5 and seen[0]["connect"] == 1.5
    assert seen[1]["read"] >= 5 + 1.5
    assert get.calls[0].request.extensions["timeout"]["read"] == 1.5  # other calls: untouched


def test_score_keeps_waiting_through_read_timeouts(
    api: respx.MockRouter, example: Ex, sleeps: list[float]
) -> None:
    """A read timeout on the keyed score call means "still running", not a failure: the
    same Idempotency-Key is replayed until the run finishes or ``timeout`` passes."""
    done = example(ScoreCallResult) | {"outputs": []}
    route = api.post(f"{V1}/models/mdl_1/versions/1:score").mock(
        side_effect=[httpx.ReadTimeout("slow")] * 4 + [httpx.Response(200, json=done)]
    )
    with Client(config=ClientConfig(base_url=BASE, max_retries=2), sleep=sleeps) as c:
        res = c.models.score("mdl_1", 1, {"model": "model://default/eu@v1"}, timeout=600)
    assert res.done and route.call_count == 5
    keys = {call.request.headers["Idempotency-Key"] for call in route.calls}
    assert len(keys) == 1


def test_score_read_timeouts_still_honour_the_deadline(
    api: respx.MockRouter, sleeps: list[float]
) -> None:
    api.post(f"{V1}/models/mdl_1/versions/1:score").mock(side_effect=httpx.ReadTimeout("slow"))
    config = ClientConfig(base_url=BASE, max_retries=0)
    with Client(config=config, sleep=sleeps) as c, pytest.raises(TimeoutError):
        c.models.score("mdl_1", 1, {"model": "model://default/eu@v1"}, timeout=0.01)


def test_score_connection_errors_are_not_treated_as_waiting(
    api: respx.MockRouter, sleeps: list[float]
) -> None:
    api.post(f"{V1}/models/mdl_1/versions/1:score").mock(side_effect=httpx.ConnectError("no"))
    config = ClientConfig(base_url=BASE, max_retries=1)
    with Client(config=config, sleep=sleeps) as c, pytest.raises(TransportError):
        c.models.score("mdl_1", 1, {"model": "model://default/eu@v1"}, timeout=600)


def test_downloads_are_verified(
    client: Client, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    data = b"bundle bytes"
    link = _link("bundle.zarr.zip", data)
    art = api.get(f"{V1}/models/mdl_1/versions/1/artifacts/ui/summaries.json").respond(
        200, json=link
    )
    api.get(f"{STORAGE}/out/bundle.zarr.zip").respond(200, content=data)
    dest = tmp_path / "out" / "b.zip"
    assert client.models.download("mdl_1", 1, "ui/summaries.json", dest) == dest
    assert dest.read_bytes() == data and art.call_count == 1

    bad = link | {"crc32c": "AAAAAA=="}
    with pytest.raises(IntegrityFailure, match="CRC32C"):
        client.download(ArtifactLink.model_validate(bad), tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()
    assert not list(tmp_path.glob(".bad.zip*"))  # the temp file was cleaned up
    short = link | {"size": 5}
    with pytest.raises(IntegrityFailure, match="larger"):
        client.download(ArtifactLink.model_validate(short))


def test_dataset_download_and_versions(client: Client, api: respx.MockRouter, example: Ex) -> None:
    data = b"zip"
    ds_id = example(Dataset)["id"]
    api.get(f"{V1}/datasets/{ds_id}/versions/3/artifacts/data.zarr.zip").respond(
        200, json=_link("data.zarr.zip", data)
    )
    api.get(f"{STORAGE}/out/data.zarr.zip").respond(200, content=data)
    assert client.datasets.download(ds_id, 3) == data
    api.get(f"{V1}/datasets/{ds_id}/versions").respond(200, json=page([example(DatasetVersion)]))
    assert [v.version for v in client.datasets.versions(ds_id)] == [3]


def test_catalog_and_plots(client: Client, api: respx.MockRouter, example: Ex) -> None:
    from mapi_spec.plots import PlotSpec

    api.get(f"{V1}/catalog").respond(200, json=example(CatalogResponse))
    api.get(f"{V1}/schemas/modelspec.json").respond(200, json={"title": "ModelSpec"})
    val = api.post(f"{V1}/plots:validate").respond(200, json=example(PlotValidateResponse))
    assert client.catalog.get().steps[0].id == "core.log1p"
    assert client.catalog.schema("modelspec.json") == {"title": "ModelSpec"}
    plot = example(PlotSpec)
    assert client.plots.validate(plot).valid is False
    assert body_of(val.calls.last.request)["kind"] == plot["kind"]


def test_page_model_parses(example: Ex) -> None:
    assert Page[Run].model_validate(page([run_json(example)])).items[0].state.value == "running"
