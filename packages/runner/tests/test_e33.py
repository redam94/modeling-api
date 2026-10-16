"""E33-style geo MMM through the runner: validate, fit (side tables, evaluate_then_refit,
derived quantities, MMM plots, response grids, the scorecard and the what-if grid view),
derive, score an 8-week EUR forecast (with its guided view), a channel-off counterfactual,
evaluate, render (which needs vl-convert, absent here) and translate business priors."""

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
from mapi_spec.calibration import PriorTranslationResult
from mapi_spec.run import ErrorPayload, OutputManifest, RunnerExit, verify_output_objects
from mapi_spec.scorecard import SCORECARD_OUTPUT, Scorecard
from mapi_spec.views import (
    COUNTERFACTUAL_VIEW,
    FORECAST_VIEW,
    RESPONSE_GRID_VIEW,
    CounterfactualView,
    ForecastView,
    PreprocessView,
    ResponseGridView,
)
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
    pre = PreprocessView.model_validate_json(fit.output("ui/preprocess.json"))
    assert [s.id for s in pre.steps] == [
        "to_eur",
        "weekly",
        "scale_spend",
        "scale_y",
        "lags",
        "season",
        "hols",
    ]
    scale = next(s for s in pre.steps if s.id == "scale_spend")
    heat = next(a for a in scale.arrays if a.name == "scale__spend")
    assert scale.stateful and len(heat.dims) == 2 and set(heat.dims) == {"geo", "channel"}
    thin = read_datatree_zip(fit.output("ui/thin.zarr.zip"))
    assert thin["posterior"].to_dataset().sizes["sample"] == 100
    card = Scorecard.model_validate_json(fit.output(SCORECARD_OUTPUT))
    assert card.items and all(i.check and i.docs_anchor for i in card.items)
    assert {"convergence.rhat", "holdout.mape", "plausibility.roi_absurd"} <= {
        i.check for i in card.items
    }
    grid = ResponseGridView.model_validate_json(fit.output(RESPONSE_GRID_VIEW))
    assert grid.channels == ["tv", "search"] and grid.draws == 100 and grid.kpi_units == "EUR"
    assert grid.baseline is not None and grid.geos is not None
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
    assert "predictions" not in out  # full draws only fed the guided view
    fan_plots = json.loads(score.output("plots.json"))
    assert [p["id"] for p in fan_plots] == ["core.ppc_fan"]
    assert "training data ends" in fan_plots[0]["caption_plain"]  # history + split line
    forecast = ForecastView.model_validate_json(score.output(FORECAST_VIEW))
    assert forecast.history == 30 and len(forecast.fan.coords) == 38
    assert forecast.units == "EUR" and forecast.summed_over == ["market"]

    # ---- counterfactual: TV off over the fit window ------------------------------------------
    cf_request = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "endpoint://growth/eu-geo-mmm",
        "mode": "counterfactual",
        "side_tables": {"fx": "dataset://growth/fx_daily@latest"},
        "interventions": [{"var": "spend_local", "sel": {"channel": "tv"}, "set": 0.0}],
        "output": {"kind": "expected", "summary": ["mean", "hdi_90"]},
    }
    cf = run_supervisor(
        make_run(gcs, RunKind.SCORE, cf_request, inputs={**bundle_inputs, **inputs})
    )
    _ok(cf)
    view = CounterfactualView.model_validate_json(cf.output(COUNTERFACTUAL_VIEW))
    assert view.delta_total.mean < 0 and view.delta_fan is not None

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

    # ---- translate business priors (draft time: no fit) ---------------------------------------
    job = {
        "spec": spec,
        "business_priors": {
            "channels": {
                "tv": {"roi": [1.0, 3.0], "credibility": 0.8},
                "search": {"contribution_share": [0.05, 0.2]},
            },
            "baseline_share": {"share": [0.5, 0.8]},
        },
        "draws": 300,
        "seed": 3,
    }
    tr = run_supervisor(make_run(gcs, RunKind.TRANSLATE_PRIORS, job, inputs=inputs))
    manifest = _ok(tr)
    assert {"priors.json", "plots.json"} <= set(manifest.objects)
    result = PriorTranslationResult.model_validate_json(tr.output("priors.json"))
    assert result.priors["beta_channel"]["distribution"] == "LogNormal"
    assert set(result.coverage) == {"tv", "search", "baseline"}
    assert result.preview.kpi_fan is not None and result.preview.roi["tv"].units == "EUR per EUR"
    ids = sorted(p["id"] for p in json.loads(tr.output("plots.json")))
    assert ids == ["core.prior_predictive", "mmm.prior_beliefs"]
