"""M3.5 run kinds end to end through the real supervisor and worker against the GCS shim:
a fit writes the Model Explorer's fit sections, a ``governance`` run measures checks from
the pinned bundle, evaluation bundle and data, and ``explore`` runs compute views."""

from __future__ import annotations

import json
from typing import Any

import pytest
from gov_fixtures import SCHEMA, linear_data, linear_spec
from mapi_sdk.wire import write_zarr_zip
from mapi_spec.executor import RunKind
from mapi_spec.governance import GOVERNANCE_OUTPUT, GovernanceReport
from mapi_spec.views import EXPLORE_INDEX, ExplorerIndex
from runner_helpers import run_supervisor
from test_gcs_shim_compat import _prepare, _run, _verify, shim  # noqa: F401 - fixture

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_fit_governance_and_explore_runs_through_the_shim(shim: Any) -> None:  # noqa: F811
    b = shim.buckets
    ds = linear_data()
    ds.attrs["mapi_schema"] = SCHEMA.model_dump_json()
    raw = write_zarr_zip(ds)
    assert raw is not None

    async def upload() -> None:
        async with shim.store() as store:
            await store.put(b.runs, "t/default/data/lin.zarr.zip", raw)

    _run(upload())
    data_in = {"dataset": (b.runs, "t/default/data/lin.zarr.zip")}
    fit = run_supervisor(_run(_prepare(shim, RunKind.FIT, linear_spec(), data_in)))
    out = _run(_verify(shim, fit))
    index = ExplorerIndex.model_validate_json(out[EXPLORE_INDEX])
    status = {s.section: s.status for s in index.sections}
    assert status["params"] == "ok" and status["residuals"] == "ok"
    assert status["irf"] == "skipped"
    assert "ui/explore/plots/fit.json" in out
    p = fit.run.prefix
    model_in = {
        "bundle": (b.runs, p + "bundle.zarr.zip"),
        "bundle.manifest": (b.runs, p + "bundle.manifest.json"),
        "evaluation": (b.runs, p + "evaluation/bundle.zarr.zip"),
        "evaluation.manifest": (b.runs, p + "evaluation/manifest.json"),
        **data_in,
    }
    job = {
        "model_ref": "model://p/lin@v1",
        "checks": [
            {"check": "baseline.beats_naive"},
            {"check": "data.quality"},
            {"check": "null.noise_regressors", "params": {"n_noise": 2}},
        ],
        "budget": {"max_refits": 10},
    }
    gov = run_supervisor(_run(_prepare(shim, RunKind.GOVERNANCE, job, model_in)))
    report = GovernanceReport.model_validate_json(_run(_verify(shim, gov))[GOVERNANCE_OUTPUT])
    assert [r.status.value for r in report.results] == ["pass", "pass", "pass"]
    assert report.model_kind == "ml" and report.refits == 1
    explore = {
        "target": "model",
        "ref": "model://p/lin@v1",
        "model": {"sections": ["forecast", "params"], "horizon": 6},
    }
    ex = run_supervisor(_run(_prepare(shim, RunKind.EXPLORE, explore, model_in)))
    ex_out = _run(_verify(shim, ex))
    assert json.loads(ex_out["ui/explore/forecast.json"])["forecasts"][0]["kpi"] == "y"
    dexp = {"target": "dataset", "ref": "dataset://p/lin@v1", "data": {"sections": ["stl"]}}
    dx = run_supervisor(_run(_prepare(shim, RunKind.EXPLORE, dexp, data_in)))
    stl = json.loads(_run(_verify(shim, dx))["ui/explore/stl.json"])
    assert {i["var"] for i in stl["items"]} == {"y", "spend", "price"}
