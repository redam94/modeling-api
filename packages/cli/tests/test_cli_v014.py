"""v0.14 on the command line: run media (list, download, log-image), run comparison, saved
views, experiments, ``ls --experiment/--view``, ``fit --experiment``, ``workflows run
--experiment`` and media lines of the renderer. The API is mocked with respx."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import respx
from mapi_cli.output import Output, strip_control
from mapi_cli.progress import METRICS_EVERY_S, EventRenderer
from mapi_sdk.media import encode_png
from mapi_spec.api import (
    Experiment,
    ExperimentRunsResult,
    Run,
    RunCompare,
    RunMedia,
    RunView,
    SpecDoc,
    SpecRevision,
    TrackingFrame,
)
from mapi_spec.executor import RunEventType
from mapi_spec.storage import crc32c, crc32c_to_base64
from mapi_spec.workflow import Workflow, WorkflowRun
from rich.console import Console
from typer.testing import Result

Invoke = Callable[..., Result]
AsJson = Callable[[Result], Any]
Ex = Callable[..., dict[str, Any]]

URL = "http://api.test"
V1 = f"{URL}/v1"
STORE = "http://storage.test"
EXP = "exp_01jaf3f1g2h3j4k5m6n7p8q9r0"
PNG = encode_png(np.arange(12, dtype=np.uint8).reshape(3, 4))[0]


def page(items: list[Any]) -> dict[str, Any]:
    return {"items": items, "next_cursor": None}


def body_of(route: respx.Route, i: int = -1) -> Any:
    return json.loads(route.calls[i].request.content)


def _media(example: Ex, data: bytes = PNG, **changes: Any) -> dict[str, Any]:
    fields = {
        "size": len(data),
        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        "crc32c": crc32c_to_base64(crc32c(data)),
        "url": f"{STORE}/m/{changes.get('id', 'rm_1')}",
    }
    return example(RunMedia, 0, **{**fields, **changes})


def _experiments(api: respx.MockRouter, example: Ex) -> dict[str, Any]:
    exp = example(Experiment)
    api.get(f"{V1}/experiments").respond(200, json=page([exp]))
    api.get(f"{V1}/experiments/{EXP}").respond(200, json=exp)
    return exp


# ===================================================================================== media


def test_runs_media_table_and_download(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    items = [
        _media(example),
        _media(example, id="rm_2", step=4, caption="last epoch"),
        _media(example, id="rm_3", step=4, attempt=2),  # same key/labels/step, attempt 2
    ]
    route = api.get(f"{V1}/runs/run_1/media").respond(200, json=page(items))
    res = mapi("--url", URL, "runs", "media", "run_1", "--key", "samples")
    assert res.exit_code == 0, res.output
    assert "samples" in res.stdout and "split=val" in res.stdout and "last epoch" in res.stdout
    assert "image 64x64" in res.stdout
    assert route.calls.last.request.url.params["links"] == "false"
    for i in (1, 2, 3):
        api.get(f"{STORE}/m/rm_{i}").respond(200, content=PNG)
    out = tmp_path / "media"
    res = mapi("--url", URL, "runs", "media", "run_1", "--download", str(out))
    assert res.exit_code == 0, res.output
    assert sorted(p.name for p in out.iterdir()) == [
        "samples-val-3.png",
        "samples-val-4-a2.png",
        "samples-val-4.png",
    ]
    assert (out / "samples-val-3.png").read_bytes() == PNG


def test_runs_media_download_refuses_bad_bytes(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    api.get(f"{V1}/runs/run_1/media").respond(200, json=page([_media(example)]))
    api.get(f"{STORE}/m/rm_1").respond(200, content=PNG[:-1] + b"\x01")
    res = mapi("--url", URL, "runs", "media", "run_1", "--download", str(tmp_path / "d"))
    assert res.exit_code == 1
    assert not list((tmp_path / "d").iterdir())


def test_runs_media_empty(mapi: Invoke, api: respx.MockRouter) -> None:
    api.get(f"{V1}/runs/run_1/media").respond(200, json=page([]))
    res = mapi("--url", URL, "runs", "media", "run_1")
    assert res.exit_code == 0 and "did not log images or tables" in res.stderr


def test_log_image(mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    route = api.post(f"{V1}/runs/run_1/media").respond(201, json=_media(example))
    res = mapi(
        "--url", URL, "runs", "log-image", "run_1", "samples", str(path),
        "--step", "2", "--label", "split=val", "--caption", "epoch 2",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    sent = body_of(route)
    assert (sent["key"], sent["step"], sent["labels"], sent["caption"]) == (
        "samples",
        2,
        {"split": "val"},
        "epoch 2",
    )
    bad = tmp_path / "b.gif"
    bad.write_bytes(b"GIF89a")
    res = mapi("--url", URL, "runs", "log-image", "run_1", "samples", str(bad))
    assert res.exit_code == 1 and "not a PNG or a JPEG" in res.stderr
    assert route.call_count == 1


# ================================================================================ comparison


def test_runs_compare_tables(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    doc = example(RunCompare)
    route = api.get(f"{V1}/run-compare").respond(200, json=doc)
    run_a, run_b = doc["runs"][0]["id"], doc["runs"][1]["id"]
    res = mapi("--url", URL, "runs", "compare", run_a, run_b, "--key", "loss")
    assert res.exit_code == 0, res.output
    params = route.calls.last.request.url.params
    assert params.get_list("run") == [run_a, run_b] and params.get_list("key") == ["loss"]
    assert "params that differ" in res.stdout and "settings.adstock.l_max" in res.stdout
    js = mapi("--url", URL, "--json", "runs", "compare", run_a, run_b)
    assert json.loads(js.stdout)["params"][0]["differs"] is True
    res = mapi("--url", URL, "runs", "compare", run_a)
    assert res.exit_code == 1 and "compare 2 to 10 runs" in res.stderr
    res = mapi("--url", URL, "runs", "compare", run_a, run_b, "--agg", "median")
    assert res.exit_code == 1


# ============================================================================== saved views


def test_views_save_ls_rm_and_ls_view(
    mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson
) -> None:
    _experiments(api, example)
    view = example(RunView)
    api.get(f"{V1}/run-views").respond(200, json=page([view]))
    create = api.post(f"{V1}/run-views").respond(201, json={**view, "name": "fresh"})
    patch = api.patch(f"{V1}/run-views/{view['id']}").respond(200, json=view)
    delete = api.delete(f"{V1}/run-views/{view['id']}").respond(204)
    res = mapi(
        "--url", URL, "runs", "views", "save", "fresh",
        "-f", "metric.rmse < 15", "-c", "metric.rmse", "--order-by", "metric.rmse",
        "--experiment", "adstock length study",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    sent = body_of(create)
    assert sent["name"] == "fresh" and sent["experiment_id"] == EXP
    assert sent["query"]["filters"] == ["metric.rmse < 15"]
    # the same name again replaces the view's query
    res = mapi("--url", URL, "runs", "views", "save", "best adstock fits", "-f", "metric.r2 > 0")
    assert res.exit_code == 0, res.output
    assert body_of(patch)["query"]["filters"] == ["metric.r2 > 0"]
    res = mapi("--url", URL, "--json", "runs", "views", "ls")
    assert as_json(res)[0]["id"] == view["id"]
    runs = api.get(f"{V1}/runs").respond(200, json=page([example(Run)]))
    res = mapi("--url", URL, "runs", "ls", "--view", "best adstock fits", "-f", "metric.r2 > 0")
    assert res.exit_code == 0, res.output
    params = runs.calls.last.request.url.params
    assert params.get_list("filter") == [*view["query"]["filters"], "metric.r2 > 0"]
    assert params["order_by"] == "metric.rmse" and params["experiment_id"] == EXP
    assert params.get_list("column") == view["query"]["columns"]
    res = mapi("--url", URL, "runs", "views", "rm", "best adstock fits", "--yes")
    assert res.exit_code == 0 and delete.called
    res = mapi("--url", URL, "runs", "ls", "--view", "nope")
    assert res.exit_code == 1 and "no saved view" in res.stderr


def test_ls_experiment(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    _experiments(api, example)
    runs = api.get(f"{V1}/runs").respond(200, json=page([]))
    res = mapi("--url", URL, "runs", "ls", "--experiment", "ADSTOCK LENGTH STUDY")
    assert res.exit_code == 0, res.output
    assert runs.calls.last.request.url.params["experiment_id"] == EXP
    res = mapi("--url", URL, "runs", "ls", "--experiment", "unknown")
    assert res.exit_code == 1 and "no experiment has the id or the name" in res.stderr


# =============================================================================== experiments


def test_experiments_commands(
    mapi: Invoke, api: respx.MockRouter, example: Ex, as_json: AsJson
) -> None:
    exp = _experiments(api, example)
    create = api.post(f"{V1}/experiments").respond(201, json=exp)
    patch = api.patch(f"{V1}/experiments/{EXP}").respond(200, json={**exp, "archived": True})
    moves = api.post(f"{V1}/experiments/{EXP}/runs").respond(
        200, json=example(ExperimentRunsResult, 0, missing=["run_x"])
    )
    delete = api.delete(f"{V1}/experiments/{EXP}").respond(204)
    res = mapi("--url", URL, "experiments", "create", "adstock length study", "--description", "d")
    assert res.exit_code == 0, res.output
    assert body_of(create) == {"name": "adstock length study", "description": "d"}
    res = mapi("--url", URL, "experiments", "ls")
    assert res.exit_code == 0 and "adstock length study" in res.stdout
    assert as_json(mapi("--url", URL, "--json", "experiments", "show", EXP))["id"] == EXP
    for args, body in (
        (("rename", EXP, "new name"), {"name": "new name"}),
        (("archive", "adstock length study"), {"archived": True}),
        (("unarchive", EXP), {"archived": False}),
    ):
        res = mapi("--url", URL, "experiments", *args)
        assert res.exit_code == 0, res.output
        assert body_of(patch) == body
    res = mapi("--url", URL, "experiments", "add", EXP, "run_1", "run_x")
    assert res.exit_code == 0, res.output
    assert body_of(moves) == {"add": ["run_1", "run_x"], "remove": []}
    assert "run_x" in res.stderr
    res = mapi("--url", URL, "experiments", "remove", EXP, "run_1")
    assert body_of(moves) == {"add": [], "remove": ["run_1"]}
    res = mapi("--url", URL, "experiments", "rm", EXP, input="n\n")
    assert res.exit_code == 1 and not delete.called
    res = mapi("--url", URL, "experiments", "rm", EXP, "--yes")
    assert res.exit_code == 0 and delete.called


def test_fit_experiment(mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    assert mapi("new", "glm", "-o", str(spec)).exit_code == 0
    res = mapi("fit", str(spec), "--data", str(spec), "--experiment", "x")
    assert res.exit_code == 1 and "--experiment needs --remote" in res.stderr
    _experiments(api, example)
    api.get(f"{V1}/specs").respond(200, json=page([]))
    api.post(f"{V1}/specs").respond(201, json=example(SpecDoc) | {"latest": example(SpecRevision)})
    runs = api.post(f"{V1}/runs").respond(201, json=example(Run, 0, state="queued"))
    res = mapi(
        "--url", URL, "fit", str(spec), "--remote", "@", "--no-wait",
        "--experiment", "adstock length study",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert body_of(runs)["experiment_id"] == EXP


def test_workflow_run_experiment(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    """``workflows run --experiment`` names the experiment at start (node runs join it)."""
    _experiments(api, example)
    wf = example(Workflow)
    api.get(f"{V1}/workflows/{wf['id']}").respond(200, json=wf)
    start = api.post(f"{V1}/workflows/{wf['id']}/runs").respond(
        202, json=example(WorkflowRun, status="queued")
    )
    res = mapi(
        "--url", URL, "workflows", "run", wf["id"], "-i", "data=dataset://growth/sales@v4",
        "--experiment", "adstock length study",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert body_of(start)["experiment_id"] == EXP
    res = mapi("--url", URL, "workflows", "run", wf["id"], "--experiment", "unknown")
    assert res.exit_code == 1 and "no experiment has the id or the name" in res.stderr


# ================================================================================== renderer


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _media_event(step: int, size: int = 12_288) -> Any:
    payload = {"key": "samples", "kind": "image", "labels": {"split": "val"}, "step": step,
               "size": size}  # fmt: skip
    return SimpleNamespace(type=RunEventType.MEDIA, payload=payload)


def test_media_lines_are_throttled() -> None:
    clock = _Clock()
    buf = io.StringIO()
    r = EventRenderer(Output(err=Console(file=buf, width=300)), "fit", clock=clock)
    with r:
        r.handle(_media_event(0))
        for step in range(1, 10):
            clock.t += 0.1
            r.handle(_media_event(step))
        clock.t += METRICS_EVERY_S
        r.handle(_media_event(10, size=500))
        r.handle(_media_event(11, size=3 * 1024 * 1024))  # printed at the end
        r.tracking(TrackingFrame(run_id="run_1", media=True))
    out = [x for x in strip_control(buf.getvalue()).splitlines() if x.startswith("media")]
    assert out[0] == "media image samples{split=val} step 0 (12 KB)"
    assert out[1] == "media image samples{split=val} step 10 (500 B)"
    assert out[2] == "media new images or tables"
    assert len(out) == 3
