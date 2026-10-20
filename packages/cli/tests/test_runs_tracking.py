"""The run log on the command line (v0.11): run search flags, metrics/params tables, writes
(log-metric, log-param, tag, rename, note) and the throttled tracking lines of the renderer."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import respx
from mapi_cli.output import Output, strip_control
from mapi_cli.progress import METRICS_EVERY_S, EventRenderer
from mapi_spec.api import (
    LogLine,
    MetricHistory,
    MetricSeries,
    Run,
    RunParam,
    TrackingFrame,
    encode_sse,
)
from mapi_spec.executor import RunEventType
from rich.console import Console
from typer.testing import Result

Invoke = Callable[..., Result]
AsJson = Callable[[Result], Any]
Ex = Callable[..., dict[str, Any]]

URL = "http://api.test"
V1 = f"{URL}/v1"


def page(items: list[Any]) -> dict[str, Any]:
    return {"items": items, "next_cursor": None}


def body_of(route: respx.Route, i: int = -1) -> Any:
    return json.loads(route.calls[i].request.content)


# --------------------------------------------------------------------------------- commands


def test_ls_search_flags_reach_the_api(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    run = example(Run, 0, name="weekly sales", columns={"metric.rmse": 0.4321})
    route = api.get(f"{V1}/runs").respond(200, json=page([run]))
    res = mapi(
        "--url", URL, "runs", "ls",
        "-f", "metric.rmse < 0.5", "--filter", "param.fit.draws >= 1000",
        "--order-by", "-metric.rmse", "-c", "metric.rmse",
        "--tag", "stage=candidate", "--tag", "reviewed",
        "--name", "weekly", "--parent", "run_0",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    params = route.calls.last.request.url.params
    assert params.get_list("filter") == ["metric.rmse < 0.5", "param.fit.draws >= 1000"]
    assert params["order_by"] == "-metric.rmse" and params.get_list("column") == ["metric.rmse"]
    assert params.get_list("tag") == ["stage:candidate", "reviewed"]
    assert params["name"] == "weekly" and params["parent_run_id"] == "run_0"
    assert "weekly sales" in res.stdout and "0.4321" in res.stdout


def test_ls_bad_filter_fails_before_the_request(mapi: Invoke, api: respx.MockRouter) -> None:
    route = api.get(f"{V1}/runs").respond(200, json=page([]))
    res = mapi("--url", URL, "runs", "ls", "-f", "metric.train/loss < 1")
    assert res.exit_code == 1 and "metric key 'train/loss'" in res.stderr
    assert route.call_count == 0


def test_metrics_and_params_tables(
    mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson
) -> None:
    api.get(f"{V1}/runs/run_1/metrics").respond(
        200, json=page([example(MetricSeries), example(MetricSeries, 1)])
    )
    res = mapi("--url", URL, "runs", "metrics", "run_1")
    assert res.exit_code == 0, res.output
    assert "step_size" in res.stdout and "chain=0" in res.stdout and "12.7" in res.stdout
    hist = api.get(f"{V1}/runs/run_1/metrics/step_size").respond(200, json=example(MetricHistory))
    res = mapi("--url", URL, "runs", "metrics", "run_1", "--key", "step_size", "--points", "50")
    assert res.exit_code == 0, res.output
    assert hist.calls.last.request.url.params["max_points"] == "50"
    assert "0.45" in res.stdout
    api.get(f"{V1}/runs/run_1/params").respond(200, json=page([example(RunParam)]))
    js = mapi("--url", URL, "--json", "runs", "params", "run_1", "--prefix", "fit.")
    assert as_json(js)[0]["key"] == "fit.draws"


def test_empty_metrics(mapi: Invoke, api: respx.MockRouter) -> None:
    api.get(f"{V1}/runs/run_1/metrics").respond(200, json=page([]))
    res = mapi("--url", URL, "runs", "metrics", "run_1")
    assert res.exit_code == 0 and "did not log metrics" in res.stderr


def test_writes(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    metrics = api.post(f"{V1}/runs/run_1/metrics").respond(
        200, json={"accepted": 1, "duplicates": 0}
    )
    res = mapi(
        "--url", URL, "runs", "log-metric", "run_1", "revenue_lift", "0.042",
        "--label", "region=north", "--step", "3",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert body_of(metrics) == {
        "metrics": [
            {"key": "revenue_lift", "value": 0.042, "step": 3, "labels": {"region": "north"}}
        ]
    }
    params = api.post(f"{V1}/runs/run_1/params").respond(200, json={"accepted": 1, "unchanged": 0})
    assert mapi("--url", URL, "runs", "log-param", "run_1", "weeks", "8").exit_code == 0
    assert body_of(params) == {"params": {"weeks": 8}}
    mapi("--url", URL, "runs", "log-param", "run_1", "code", "08", "--text")
    assert body_of(params) == {"params": {"code": "08"}}
    mapi("--url", URL, "runs", "log-param", "run_1", "analyst", "kim")
    assert body_of(params) == {"params": {"analyst": "kim"}}

    patch = api.patch(f"{V1}/runs/run_1").respond(
        200, json=example(Run, 0, name="weekly", tags={"stage": "a"})
    )
    res = mapi("--url", URL, "runs", "tag", "run_1", "stage=a", "--delete", "old")
    assert res.exit_code == 0, res.output
    assert body_of(patch) == {"tags": {"stage": "a", "old": None}}
    assert "stage=a" in res.stdout
    assert mapi("--url", URL, "runs", "rename", "run_1", "weekly").exit_code == 0
    assert body_of(patch) == {"name": "weekly"}
    assert mapi("--url", URL, "runs", "note", "run_1", "-", input="line one\n").exit_code == 0
    assert body_of(patch) == {"note": "line one\n"}
    assert mapi("--url", URL, "runs", "note", "run_1", "").exit_code == 0
    assert body_of(patch) == {"note": None}
    res = mapi("--url", URL, "runs", "tag", "run_1")
    assert res.exit_code == 1 and patch.call_count == 4
    res = mapi("--url", URL, "runs", "log-metric", "run_1", "train/loss", "1")
    assert res.exit_code == 1 and metrics.call_count == 1


def test_logs_level_filter_and_user_lines(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    line = example(
        LogLine, stream="user", level="warning", line="grid point 3 failed", fields={"point": 3}
    )
    route = api.get(f"{V1}/runs/run_1/logs").respond(200, json=page([line]))
    res = mapi("--url", URL, "runs", "logs", "run_1", "--level", "warning", "--stream", "user")
    assert res.exit_code == 0, res.output
    assert route.calls.last.request.url.params["level"] == "warning"
    assert "user warning" in res.stdout and "point=3" in res.stdout


def test_watch_json_includes_tracking_frames(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    run = Run.model_validate(example(Run, 0, state="running"))
    done = example(Run, 0, state="succeeded", finished_at="2026-10-15T09:40:00Z")
    frame = TrackingFrame.model_validate(example(TrackingFrame))
    body = encode_sse("snapshot", run) + encode_sse("tracking", frame) + encode_sse("end", done)
    api.get(f"{V1}/runs/{run.id}/events").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()
        )
    )
    api.get(f"{V1}/runs/{run.id}").respond(200, json=done)
    res = mapi("--url", URL, "--json", "runs", "watch", run.id)
    assert res.exit_code == 0, res.output
    docs = [json.loads(x) for x in res.stdout.splitlines()]
    assert [d["event"] for d in docs] == ["snapshot", "tracking", "end"]


def test_remote_watch_prints_the_params_and_tags_that_changed(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    """Params and tags never travel on the stream: after a ``tracking`` frame that flags
    them, the watch reads them and prints the ones that changed."""
    run = Run.model_validate(example(Run, 0, state="running", tags={"team": "a"}))
    done = example(
        Run, 0, state="succeeded", finished_at="2026-10-15T09:40:00Z",
        tags={"team": "a", "stage": "prod"},
    )  # fmt: skip
    flags = {"run_id": run.id, "params": True, "tags": True}
    body = (
        encode_sse("snapshot", run)
        + encode_sse("tracking", TrackingFrame.model_validate(flags))
        + encode_sse("end", done)
    )
    api.get(f"{V1}/runs/{run.id}/events").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()
        )
    )
    gets = api.get(f"{V1}/runs/{run.id}").mock(
        side_effect=[
            httpx.Response(200, json=run.model_dump(mode="json")),
            httpx.Response(200, json=done),
        ]
    )
    param = example(RunParam, 0)
    api.get(f"{V1}/runs/{run.id}/params").respond(200, json=page([param]))
    res = mapi("--url", URL, "runs", "watch", run.id)
    assert res.exit_code == 0, res.output
    text = strip_control(res.stderr)
    assert f"params: {param['key']}={param['value']}" in text
    assert "tags: stage=prod" in text and "team=a" not in text.split("tags: ")[1].split("\n")[0]
    assert gets.call_count == 2


# --------------------------------------------------------------------------------- renderer


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _renderer(clock: _Clock) -> tuple[io.StringIO, EventRenderer]:
    buf = io.StringIO()
    err = Console(file=buf, force_terminal=False, width=300)
    return buf, EventRenderer(Output(err=err), "fit", clock=clock)


def _metrics(key: str, step: int, value: float, labels: dict[str, str] | None = None) -> Any:
    payload = {
        "t0": "2026-10-15T00:00:00Z",
        "series": [
            {"key": key, "labels": labels or {}, "steps": [step], "values": [value], "ts_ms": [0]}
        ],
    }
    return SimpleNamespace(type=RunEventType.METRICS, payload=payload)


def test_metrics_lines_are_throttled() -> None:
    clock = _Clock()
    buf, r = _renderer(clock)
    with r:
        r.handle(_metrics("loss", 1, 2.0))  # the first update prints at once
        for i in range(2, 50):
            clock.t += 0.05
            r.handle(_metrics("loss", i, 1.0 / i))
        r.handle(_metrics("step_size", 49, 0.12, {"chain": "0"}))
        clock.t += METRICS_EVERY_S
        r.handle(_metrics("loss", 50, 0.02))
        r.handle(_metrics("loss", 51, 0.01))  # printed at the end (pending)
    out = [x for x in strip_control(buf.getvalue()).splitlines() if x.startswith("metrics")]
    assert out[0] == "metrics loss=2 (step 1)"
    assert out[1] == "metrics loss=0.02 (step 50), step_size{chain=0}=0.12 (step 49)"
    assert out[2] == "metrics loss=0.01 (step 51), step_size{chain=0}=0.12 (step 49)"
    assert len(out) == 3


def test_params_tags_user_logs_and_tracking_frames() -> None:
    clock = _Clock()
    buf, r = _renderer(clock)
    long = [{"key": f"settings.k{i}", "value": i} for i in range(100)]
    with r:
        r.handle(SimpleNamespace(type=RunEventType.PARAMS, payload={"params": long}))
        r.handle(
            SimpleNamespace(type=RunEventType.TAGS, payload={"tags": [{"key": "a", "value": "b"}]})
        )
        r.handle(
            SimpleNamespace(
                type=RunEventType.LOG,
                payload={
                    "stream": "user",
                    "line": "half way",
                    "level": "error",
                    "fields": {"n": 2},
                },
            )
        )
        frame = TrackingFrame.model_validate(
            {
                "run_id": "run_01jaf3q2x8m9v7k5h4g3f2e1d0",
                "series": [
                    {
                        "key": "rmse", "attempt": 1, "count": 1, "last": 0.5, "last_step": None,
                        "min": 0.5, "max": 0.5, "first_ts": "2026-10-15T00:00:00Z",
                        "last_ts": "2026-10-15T00:00:00Z",
                    }
                ],
            }
        )  # fmt: skip
        r.tracking(frame)
    text = strip_control(buf.getvalue())
    params = next(x for x in text.splitlines() if x.startswith("params: "))
    assert params.startswith("params: settings.k0=0, ") and params.endswith("...")
    assert len(params) <= len("params: ") + 200
    assert "tags: a=b" in text
    assert "[error] half way n=2" in text
    assert "metrics rmse=0.5" in text


def test_json_mode_renders_nothing() -> None:
    buf = io.StringIO()
    r = EventRenderer(Output(json_mode=True, err=Console(file=buf)), "fit")
    with r:
        r.handle(_metrics("loss", 1, 1.0))
    assert buf.getvalue() == ""
