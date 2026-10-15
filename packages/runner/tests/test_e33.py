"""E33-style geo MMM through the runner: validate, fit (side tables, evaluate_then_refit,
derived quantities, MMM plots, response grids), derive, score an 8-week EUR forecast,
evaluate, and render (which needs vl-convert, absent here)."""

from __future__ import annotations

import importlib.resources as resources
import json
from typing import Any

import pytest
from mapi_conformance.fixtures.e33 import data as e33
from mapi_sdk.wire import read_arrow_ipc, read_datatree_zip, write_zarr_zip
from mapi_spec.api import ValidateResponse
from mapi_spec.executor import RunKind
from mapi_spec.loader import canonical_json, load_model_spec
from mapi_spec.run import ErrorPayload, OutputManifest, RunnerExit, verify_output_objects
from runner_helpers import Result, assert_signed_stream, make_run, run_supervisor
from shimgcs import ShimGcs

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _ok(result: Result) -> OutputManifest:
    errors = [e.payload for e in result.of("error")]
    assert result.code == RunnerExit.OK, (errors, result.stderr[-4000:])
    assert_signed_stream(result)
    manifest = result.manifest()
    listing = result.store.list(result.store.buckets.runs, result.run.prefix)
    assert verify_output_objects(manifest, listing, expected_prefix=result.run.prefix).ok
    return manifest


@pytest.fixture
def e33_inputs(gcs: ShimGcs) -> dict[str, Any]:
    data, side = e33.generate(0, "tiny")
    data.attrs["mapi_schema"] = e33.schema().model_dump_json()
    d = gcs.buckets.data
    gcs.put(d, "t/default/datasets/e33/data.zarr.zip", write_zarr_zip(data) or b"")
    for name, table in side.items():
        gcs.put(d, f"t/default/datasets/{name}/data.zarr.zip", write_zarr_zip(table) or b"")
    future = e33.payload_future(data, 0, "tiny").drop_vars("revenue_local", errors="ignore")
    gcs.put(d, "t/default/payloads/future.zarr.zip", write_zarr_zip(future) or b"")
    text = (resources.files("mapi_conformance.fixtures") / "e33" / "spec.yaml").read_text()
    return {
        "spec": json.loads(canonical_json(load_model_spec(text))),
        "inputs": {
            "dataset": (d, "t/default/datasets/e33/data.zarr.zip"),
            **{f"side.{n}": (d, f"t/default/datasets/{n}/data.zarr.zip") for n in side},
        },
        "data": data,
    }


def test_e33_validate_fit_derive_score_evaluate_render(
    gcs: ShimGcs, e33_inputs: dict[str, Any]
) -> None:
    spec, inputs = e33_inputs["spec"], e33_inputs["inputs"]
    runs = gcs.buckets.runs

    # ---- validate (sandboxed dry run) ---------------------------------------------------
    val = run_supervisor(make_run(gcs, RunKind.VALIDATE, spec, inputs=inputs))
    _ok(val)
    report = ValidateResponse.model_validate_json(val.output("validation.json"))
    assert report.valid, report.issues
    stages = [s.name for s in report.stages]
    assert stages[0] == "raw" and stages[-1] == "hols"
    assert [s.name for s in report.stages if s.split] == ["weekly"]

    # ---- fit ----------------------------------------------------------------------------
    fit = run_supervisor(make_run(gcs, RunKind.FIT, spec, inputs=inputs))
    manifest = _ok(fit)
    names = set(manifest.objects)
    for grid in ("contributions", "response_curves", "marginal_cost"):
        assert f"ui/grids/{grid}.arrow" in names
    table = read_arrow_ipc(fit.output("ui/grids/response_curves.arrow"))
    assert {"sample", "channel", "multiplier"} <= set(table.column_names)
    plot_ids = sorted(p["id"] for p in json.loads(fit.output("plots.json")))
    assert plot_ids == sorted(
        ["core.ppc_fan", "core.convergence", "mmm.decomposition", "mmm.response_curves"]
    )
    for plot in json.loads(fit.output("plots.json")):  # external datasets are uploaded
        for ref in plot["datasets"].values():
            if ref["kind"] == "data_ref":
                assert ref["path"] in names
    thin = read_datatree_zip(fit.output("ui/thin.zarr.zip"))
    assert thin["posterior"].to_dataset().sizes["sample"] == 100
    bundle_inputs = {
        "bundle": (runs, fit.run.prefix + "bundle.zarr.zip"),
        "bundle.manifest": (runs, fit.run.prefix + "bundle.manifest.json"),
    }

    # ---- derive ---------------------------------------------------------------------------
    derive = run_supervisor(
        make_run(
            gcs,
            RunKind.DERIVE,
            {"names": ["contributions"], "draws": 20},
            inputs={**bundle_inputs, **inputs},
        )
    )
    _ok(derive)
    tree = read_datatree_zip(derive.output("derived.zarr.zip"))
    contrib = tree["derived/contributions"].to_dataset()["channel_contribution"]
    assert contrib.attrs["units"] == "EUR" and contrib.sizes["sample"] == 20

    # ---- score: 8 weeks ahead in EUR -------------------------------------------------------
    request = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "endpoint://growth/eu-geo-mmm",
        "mode": "forecast",
        "horizon": {"dim": "time", "periods": 8},
        "side_tables": {"fx": "dataset://growth/fx_daily@latest"},
        "output": {"kind": "expected", "summary": ["mean", "hdi_90"], "draws": 50},
    }
    score = run_supervisor(
        make_run(
            gcs,
            RunKind.SCORE,
            request,
            inputs={
                **bundle_inputs,
                "dataset": (gcs.buckets.data, "t/default/payloads/future.zarr.zip"),
                "side.fx": inputs["side.fx"],
            },
        )
    )
    _ok(score)
    out = read_datatree_zip(score.output("score.zarr.zip"))
    y = out["summary"].to_dataset()["y_hat"]
    assert y.sizes["date"] == 8 and y.attrs["units"] == "EUR"
    assert [p["id"] for p in json.loads(score.output("plots.json"))] == ["core.ppc_fan"]

    # ---- evaluate (in sample, through the score path) ---------------------------------------
    ev = run_supervisor(
        make_run(
            gcs,
            RunKind.EVALUATE,
            {"metrics": ["mape", "coverage_90"]},
            inputs={**bundle_inputs, **inputs},
        )
    )
    _ok(ev)
    metrics = json.loads(ev.output("metrics.json"))
    assert metrics["values"]["mape"] is not None and metrics["n_holdout"] > 0

    # ---- render: vl-convert is not installed here -------------------------------------------
    gcs.put(runs, "t/default/x/plots.json", fit.output("plots.json"))
    render = run_supervisor(
        make_run(
            gcs,
            RunKind.RENDER,
            {"plots": ["core.convergence"], "format": "png"},
            inputs={"plots": (runs, "t/default/x/plots.json")},
        )
    )
    try:
        import vl_convert  # noqa: F401
    except ImportError:
        assert render.code == RunnerExit.USER_ERROR
        err = ErrorPayload.model_validate(render.of("error")[0].payload)
        assert "CAPABILITY_UNSUPPORTED" in err.message
        assert "vl-convert" in (err.untrusted_text or "")
    else:  # pragma: no cover - only where the optional renderer is installed
        _ok(render)
        assert render.output("renders/core.convergence.png").startswith(b"\x89PNG")
