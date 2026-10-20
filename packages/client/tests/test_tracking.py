"""Run tracking from the client (v0.11): writes split into batches, the buffered RunTracker,
run search, history reads and the SSE ``tracking`` frame - sync and async."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import respx
from mapi_client import (
    UNSET,
    AsyncClient,
    Client,
    ClientConfig,
    RunEnd,
    RunSnapshot,
    RunTrackingFrame,
)
from mapi_spec.api import (
    MetricHistory,
    MetricPointIn,
    MetricSeries,
    Run,
    RunParam,
    TrackingFrame,
    encode_sse,
)

BASE = "http://api.test"
V1 = f"{BASE}/v1"
Ex = Callable[..., dict[str, Any]]


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content)


def _metrics_ok(request: httpx.Request) -> httpx.Response:
    n = len(body_of(request)["metrics"])
    return httpx.Response(200, json={"accepted": n, "duplicates": 0})


def _params_ok(request: httpx.Request) -> httpx.Response:
    n = len(body_of(request)["params"])
    return httpx.Response(200, json={"accepted": n, "unchanged": 0})


def _run_ok(example: Ex) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        tags = {k: v for k, v in (body_of(request).get("tags") or {}).items() if v is not None}
        return httpx.Response(200, json=example(Run, 0, tags=tags))

    return respond


# ------------------------------------------------------------------------------------ sync


def test_log_metrics_splits_2500_points_into_three_posts(
    client: Client, api: respx.MockRouter
) -> None:
    route = api.post(f"{V1}/runs/run_1/metrics").mock(side_effect=_metrics_ok)
    values = {f"m{i}": float(i) for i in range(2500)}
    res = client.runs.log_metrics("run_1", values, step=4, labels={"split": "val"})
    assert res.accepted == 2500 and res.duplicates == 0
    sizes = [len(body_of(c.request)["metrics"]) for c in route.calls]
    assert sizes == [1000, 1000, 500]
    first = body_of(route.calls[0].request)["metrics"][0]
    assert first == {"key": "m0", "value": 0.0, "step": 4, "labels": {"split": "val"}}


def test_log_metric_scalar_and_point_lists(client: Client, api: respx.MockRouter) -> None:
    route = api.post(f"{V1}/runs/run_1/metrics").mock(side_effect=_metrics_ok)
    client.runs.log_metric("run_1", "revenue_lift", 0.042, labels={"region": "north"})
    client.runs.log_metrics("run_1", [MetricPointIn(key="loss", value=1.5, step=2)])
    assert body_of(route.calls[0].request) == {
        "metrics": [{"key": "revenue_lift", "value": 0.042, "labels": {"region": "north"}}]
    }
    assert body_of(route.calls[1].request)["metrics"][0]["step"] == 2
    # nothing finite: no request
    assert client.runs.log_metric("run_1", "loss", float("nan")).accepted == 0
    assert route.call_count == 2
    with pytest.raises(ValueError):
        client.runs.log_metric("run_1", "train/loss", 1.0)
    assert route.call_count == 2


def test_log_params_splits_by_500(client: Client, api: respx.MockRouter) -> None:
    route = api.post(f"{V1}/runs/run_1/params").mock(side_effect=_params_ok)
    res = client.runs.log_params("run_1", {f"p{i}": i for i in range(1200)})
    assert res.accepted == 1200
    assert [len(body_of(c.request)["params"]) for c in route.calls] == [500, 500, 200]


def test_update_and_set_tags_send_merge_patches(
    client: Client, api: respx.MockRouter, example: Ex
) -> None:
    route = api.patch(f"{V1}/runs/run_1").mock(side_effect=_run_ok(example))
    client.runs.update("run_1", name="weekly", note=None)
    run = client.runs.set_tags("run_1", {"stage": "candidate", "old": None})
    assert body_of(route.calls[0].request) == {"name": "weekly", "note": None}
    assert body_of(route.calls[1].request) == {"tags": {"stage": "candidate", "old": None}}
    assert run.tags == {"stage": "candidate"}
    client.runs.update("run_1", name=UNSET, note="text")
    assert body_of(route.calls[2].request) == {"note": "text"}
    with pytest.raises(ValueError):
        client.runs.update("run_1")


def test_run_tracker_buffers_and_flushes_on_exit(
    client: Client, api: respx.MockRouter, example: Ex
) -> None:
    metrics = api.post(f"{V1}/runs/run_1/metrics").mock(side_effect=_metrics_ok)
    params = api.post(f"{V1}/runs/run_1/params").mock(side_effect=_params_ok)
    patch = api.patch(f"{V1}/runs/run_1").mock(side_effect=_run_ok(example))
    with client.runs.track("run_1", flush_every=3) as t:
        t.log_metric("loss", 1.0, step=1)
        t.log_metric("loss", 0.5, step=2)
        assert metrics.call_count == 0
        t.log_metrics({"loss": 0.25, "acc": 0.9}, step=3)  # 4 points wait: flush
        assert metrics.call_count == 1
        t.log_param("analyst", "kim")
        t.log_param("analyst", "kim")  # the same value again is fine
        with pytest.raises(ValueError, match="write-once"):
            t.log_param("analyst", "lee")
        t.set_tag("stage", "candidate")
        t.log_metric("kpi", 3.0)
        with pytest.raises(ValueError):
            t.log_metric("bad/key", 1.0)
        assert params.call_count == 0 and patch.call_count == 0
    assert metrics.call_count == 2 and params.call_count == 1 and patch.call_count == 1
    sent = body_of(metrics.calls[0].request)["metrics"]
    assert [p["step"] for p in sent] == [1, 2, 3, 3]
    assert all("ts" in p for p in sent)  # buffered points keep the time they were logged
    assert body_of(params.calls[0].request) == {"params": {"analyst": "kim"}}
    assert body_of(patch.calls[0].request) == {"tags": {"stage": "candidate"}}


def test_search_grammar_errors_raise_before_any_request(
    client: Client, api: respx.MockRouter
) -> None:
    route = api.get(f"{V1}/runs").mock(return_value=httpx.Response(200, json={"items": []}))
    with pytest.raises(ValueError, match="metric key"):
        client.runs.list(filters=["metric.train/loss < 1"])
    with pytest.raises(ValueError):
        client.runs.list(order_by="-metric.rmse.median")
    assert route.call_count == 0
    list(client.runs.list(filters=["metric.rmse < 0.5"], order_by="-metric.rmse", tags=["x"]))
    req = route.calls[0].request
    assert req.url.params.get_list("filter") == ["metric.rmse < 0.5"]
    assert req.url.params["order_by"] == "-metric.rmse" and req.url.params["tag"] == "x"


def test_reads(client: Client, api: respx.MockRouter, example: Ex) -> None:
    series = example(MetricSeries)
    api.get(f"{V1}/runs/run_1/metrics").mock(
        return_value=httpx.Response(200, json={"items": [series], "next_cursor": None})
    )
    hist = api.get(f"{V1}/runs/run_1/metrics/step_size").mock(
        return_value=httpx.Response(200, json=example(MetricHistory))
    )
    api.get(f"{V1}/runs/run_1/params").mock(
        return_value=httpx.Response(200, json={"items": [example(RunParam)]})
    )
    assert [s.key for s in client.runs.metrics("run_1")] == ["step_size"]
    h = client.runs.metric_history("run_1", "step_size", labels={"chain": "0"})
    assert h.series[0].labels == {"chain": "0"}
    assert hist.calls[0].request.url.params.get_list("label") == ["chain:0"]
    assert hist.calls[0].request.url.params["max_points"] == "1000"
    assert [p.key for p in client.runs.params("run_1", prefix="fit.")] == ["fit.draws"]


def test_stream_decodes_tracking_frames(client: Client, api: respx.MockRouter, example: Ex) -> None:
    run = Run.model_validate(example(Run, 0, state="running"))
    done = Run.model_validate(
        example(Run, 0, state="succeeded", finished_at="2026-10-15T09:40:00Z")
    )
    frame = TrackingFrame.model_validate(example(TrackingFrame))
    body = encode_sse("snapshot", run) + encode_sse("tracking", frame) + encode_sse("end", done)
    api.get(f"{V1}/runs/run_1/events").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()
        )
    )
    items = list(client.runs.stream_events("run_1"))
    assert isinstance(items[0], RunSnapshot) and isinstance(items[-1], RunEnd)
    assert isinstance(items[1], RunTrackingFrame)
    assert items[1].frame.series[0].key == "step_size" and items[1].frame.params


# ----------------------------------------------------------------------------------- async


@pytest.fixture
async def aclient(api: respx.MockRouter) -> AsyncIterator[AsyncClient]:
    async def nosleep(_s: float) -> None:
        return None

    async with AsyncClient(config=ClientConfig(base_url=BASE), sleep=nosleep) as c:
        yield c


async def test_async_parity(aclient: AsyncClient, api: respx.MockRouter, example: Ex) -> None:
    metrics = api.post(f"{V1}/runs/run_1/metrics").mock(side_effect=_metrics_ok)
    params = api.post(f"{V1}/runs/run_1/params").mock(side_effect=_params_ok)
    patch = api.patch(f"{V1}/runs/run_1").mock(side_effect=_run_ok(example))
    res = await aclient.runs.log_metrics("run_1", {f"m{i}": 1.0 for i in range(2500)})
    assert res.accepted == 2500 and metrics.call_count == 3
    assert (await aclient.runs.log_params("run_1", {"a": 1})).accepted == 1
    await aclient.runs.update("run_1", name="weekly")
    async with aclient.runs.track("run_1", flush_every=2) as t:
        await t.log_metric("loss", 1.0, step=1)
        await t.log_metric("loss", 0.5, step=2)  # flush
        assert metrics.call_count == 4
        t.log_param("b", 2)
        t.set_tag("stage", "x")
    assert params.call_count == 2 and patch.call_count == 2
    with pytest.raises(ValueError):
        aclient.runs.list(filters=["metric.rmse"])
