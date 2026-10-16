"""``workflow_node`` runs through the real supervisor and worker processes (M3.5): inputs
named ``in.<port>.<k>``, inline JSON payloads, ``node_result.json`` and derived datasets."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mapi_sdk.wire import read_zarr_zip, write_zarr_zip
from mapi_spec.executor import RunKind
from mapi_spec.run import RunnerExit
from mapi_spec.stattests import StatTestResult
from mapi_spec.workflow import NodeResultSidecar, WorkflowNodeJob
from mapi_workflows.payloads import MetricsPayload
from mapi_workflows.testing import weekly_series
from runner_helpers import Result, make_run, run_supervisor
from shimgcs import ShimGcs

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

REGISTRY = "mapi_workflows.testing:test_registry"


def _ok(result: Result) -> None:
    errors = [e.payload for e in result.of("error")]
    assert result.code == RunnerExit.OK, (errors, result.stderr[-4000:])


def _put_data(gcs: ShimGcs, name: str = "sales", n: int = 120) -> tuple[str, str]:
    data = weekly_series(n, random_walk=True)
    d = gcs.buckets.data
    key = f"t/default/datasets/{name}/data.zarr.zip"
    gcs.put(d, key, write_zarr_zip(data) or b"")
    return d, key


def _job(node_id: str, use: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return WorkflowNodeJob(
        workflow_run_id="run_01jaf3q2x8m9v7k5h4g3f2e1d0",
        node_id=node_id,
        use=use,
        params=params or {},
        **kw,
    ).model_dump(mode="json")


def _sidecar(result: Result) -> NodeResultSidecar:
    return NodeResultSidecar.model_validate_json(result.output("node_result.json"))


def test_a_test_node_reads_its_dataset_and_writes_its_result(gcs: ShimGcs) -> None:
    data = _put_data(gcs)
    job = _job(
        "adf",
        "test.adf@1.0.0",
        {"alpha": 0.05, "regression": "c", "autolag": "AIC", "per_series": True},
        input_types={"in.data.0": "dataset"},
    )
    result = run_supervisor(make_run(gcs, RunKind.WORKFLOW_NODE, job, inputs={"in.data.0": data}))
    _ok(result)
    side = _sidecar(result)
    assert side.node_id == "adf" and side.use == "test.adf@1.0.0"
    assert side.result["decision"] == "unit_root"
    res = StatTestResult.model_validate(side.outputs["result"].inline)
    assert res.test == "test.adf" and res.var == "sales"
    assert "node_result.json" in result.manifest().objects


def test_a_transform_writes_a_derived_dataset(gcs: ShimGcs) -> None:
    data = _put_data(gcs)
    job = _job(
        "diff",
        "transform.difference@1.0.0",
        {"order": 1, "dim": "time"},
        input_types={"in.data.0": "dataset"},
    )
    result = run_supervisor(make_run(gcs, RunKind.WORKFLOW_NODE, job, inputs={"in.data.0": data}))
    _ok(result)
    side = _sidecar(result)
    out = side.outputs["data"]
    assert out.type.value == "dataset" and out.object == "out/data/data.zarr.zip"
    ds = read_zarr_zip(result.output("out/data/data.zarr.zip"))
    assert ds.sizes["time"] == 119
    schema = json.loads(result.output("out/data/schema.json"))
    assert schema["vars"]["sales"]["role"] == "target"
    meta = json.loads(result.output("out/data/meta.json"))
    assert meta["replayable"] is False and "difference" in meta["notes"][0]
    assert "out/data/summary.json" in result.manifest().objects


def test_a_backtest_fits_in_process_with_inline_split_plan(gcs: ShimGcs) -> None:
    from mapi_workflows.runtime.splits import resolve_plan
    from mapi_workflows.runtime.values import DatasetValue, NodeContext

    data = _put_data(gcs)
    ctx = NodeContext(
        node_id="folds",
        use="split.expanding_window@1.0.0",
        params={},
        inputs={"data": [DatasetValue(data=weekly_series(120, random_walk=True))]},
    )
    plan = resolve_plan({"kind": "expanding_window", "horizons": [1, 4], "n_folds": 3}, ctx)
    job = _job(
        "bt",
        "model.backtest@1.0.0",
        {
            "model": "ts.sarimax@^1",
            "settings": {"order": [1, 1, 0], "exog": ["price"]},
            "metrics": ["mae", "rmse"],
            "refit": True,
        },
        input_types={"in.data.0": "dataset", "in.split.0": "split_plan"},
        inline_inputs={"in.split.0": plan.model_dump(mode="json")},
    )
    result = run_supervisor(
        make_run(gcs, RunKind.WORKFLOW_NODE, job, inputs={"in.data.0": data}), registry=REGISTRY
    )
    _ok(result)
    side = _sidecar(result)
    assert side.result["n_folds"] == 3
    metrics = MetricsPayload.model_validate(side.outputs["metrics"].inline)
    assert metrics.candidates[0].model == "ts.sarimax@^1"
    assert set(metrics.candidates[0].by_horizon) == {"1", "4"}


def test_node_failures_are_reported_as_errors(gcs: ShimGcs) -> None:
    data = _put_data(gcs, n=8)
    job = _job("adf", "test.adf@1.0.0", input_types={"in.data.0": "dataset"})
    result = run_supervisor(make_run(gcs, RunKind.WORKFLOW_NODE, job, inputs={"in.data.0": data}))
    assert result.code != RunnerExit.OK
    (err,) = result.of("error")
    assert err.payload["code"] == "USER_CODE_ERROR"
    assert "SCHEMA_INVALID" in str(err.payload["message"])  # the worker is untrusted
