"""Full runs through the real supervisor and worker: ingest -> fit -> score.

* store demand (sklearn/xgboost quantiles): CSV ingest with a column mapping, a fit with
  ``evaluate_then_refit``, then a 4-week forecast with a store the model never saw;
* E02 radon (PyMC): zarr ingest, a checkpointed fit stopped by SIGTERM (PARTIAL), a resumed
  second attempt that finishes from the checkpoint, then scoring a new county.
"""

from __future__ import annotations

import hashlib
import importlib.resources as resources
import itertools
import json
import signal
import subprocess
from typing import Any

import pytest
import xarray as xr
from mapi_conformance.fixtures.e02 import data as e02
from mapi_conformance.fixtures.xgb_store_demand import data as xgb
from mapi_plots.sanitize import sanitize_plot
from mapi_sdk.bundle import BundleManifest, MetricsReport
from mapi_sdk.wire import read_datatree_zip, read_zarr_zip, write_zarr_zip
from mapi_spec.api import DimMapping, IngestMapping, VarMapping
from mapi_spec.executor import RunEvent, RunEventType, RunKind
from mapi_spec.jobs import DATASET_OUTPUTS
from mapi_spec.loader import canonical_json, load_model_spec
from mapi_spec.plots import PlotSpec
from mapi_spec.run import (
    CheckpointPayload,
    CompletePayload,
    LogPayload,
    MetricPayload,
    OutputManifest,
    RunnerExit,
    verify_output_manifest,
    verify_output_objects,
)
from mapi_spec.schema import DataSchema
from runner_helpers import Result, assert_signed_stream, make_run, run_supervisor
from shimgcs import ShimGcs

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _ok(result: Result) -> OutputManifest:
    errors = [e.payload for e in result.of("error")]
    assert result.code == RunnerExit.OK, (errors, result.stderr[-4000:])
    assert_signed_stream(result)
    manifest = result.manifest()
    assert manifest.status == "complete"
    assert verify_output_manifest(manifest, result.run.key)
    listing = result.store.list(result.store.buckets.runs, result.run.prefix)
    check = verify_output_objects(manifest, listing, expected_prefix=result.run.prefix)
    assert check.ok, check.issues
    return manifest


def _spec(
    fixture: str, *, fit: dict[str, Any] | None = None, settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    text = (resources.files("mapi_conformance.fixtures") / fixture / "spec.yaml").read_text()
    spec = load_model_spec(text)
    doc = json.loads(canonical_json(spec))
    if fit:
        doc["model"]["fit"] = {**doc["model"].get("fit", {}), **fit}
    if settings:
        doc["model"]["settings"] = _merge(doc["model"].get("settings", {}), settings)
    return doc


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _check_plots(raw: bytes) -> list[str]:
    plots = json.loads(raw)
    for p in plots:
        sanitize_plot(PlotSpec.model_validate(p))
    return [p["id"] for p in plots]


# ======================================================================= store demand


def test_store_demand_csv_ingest_fit_and_forecast(gcs: ShimGcs) -> None:
    data, _ = xgb.generate(0, "tiny")
    schema: DataSchema = xgb.schema()
    csv = data.to_dataframe().reset_index().to_csv(index=False).encode()
    gcs.put(gcs.buckets.quarantine, "t/default/uploads/upl_1", csv, "text/csv")
    mapping = IngestMapping(
        format="csv",
        dims={d: DimMapping(column=d, spec=spec) for d, spec in schema.dims.items()},
        vars={
            v: VarMapping(column=v, role=s.role, units=s.units, missing=s.missing)
            for v, s in schema.vars.items()
        },
    )

    # ---- ingest -------------------------------------------------------------------
    ingest = run_supervisor(
        make_run(
            gcs,
            RunKind.INGEST,
            mapping.model_dump(mode="json"),
            inputs={"upload": (gcs.buckets.quarantine, "t/default/uploads/upl_1")},
        )
    )
    manifest = _ok(ingest)
    # M3.5: the cheap Data Explorer sections ride along under ui/explore/
    assert {o for o in manifest.objects if not o.startswith("ui/explore/")} == set(DATASET_OUTPUTS)
    assert "ui/explore/index.json" in manifest.objects
    raw = ingest.output("data.zarr.zip")
    profile = json.loads(ingest.output("profile.json"))
    assert profile["sha256"] == hashlib.sha256(raw).hexdigest()
    ingested = read_zarr_zip(raw)
    xr.testing.assert_allclose(
        ingested[list(schema.vars)].transpose(*data.dims), data[list(schema.vars)]
    )
    stored = DataSchema.model_validate_json(ingest.output("schema.json"))
    assert stored.dims["week"].freq == "W-SUN" and stored.vars["units_sold"].role.value == "target"
    preview = json.loads(ingest.output("preview.json"))
    assert preview["total_rows"] == data.sizes["store_id"] * data.sizes["sku"] * data.sizes["week"]
    data_key = ingest.run.prefix + "data.zarr.zip"

    # ---- fit ----------------------------------------------------------------------
    spec = _spec(
        "xgb_store_demand",
        settings={"estimator": {"params": {"n_estimators": 40, "max_depth": 3}}},
    )
    fit = run_supervisor(
        make_run(gcs, RunKind.FIT, spec, inputs={"dataset": (gcs.buckets.runs, data_key)})
    )
    manifest = _ok(fit)
    names = set(manifest.objects)
    assert {
        "bundle.zarr.zip",
        "bundle.manifest.json",
        "diagnostics.json",
        "plots.json",
        "metrics.json",
        "evaluation/bundle.zarr.zip",
        "ui/summaries.json",
        "ui/tree_index.json",
    } <= names
    sdk_manifest = BundleManifest.model_validate_json(fit.output("bundle.manifest.json"))
    assert sdk_manifest.role == "production" and "evaluation_bundle" in sdk_manifest.lineage
    metrics = MetricsReport.model_validate_json(fit.output("metrics.json"))
    assert metrics.values["coverage_80"] is not None
    metric_events = {MetricPayload.model_validate(e.payload).name for e in fit.of("metric")}
    assert {"rmse", "coverage_80"} <= metric_events
    assert sorted(_check_plots(fit.output("plots.json"))) == sorted(
        ["core.fit_vs_actual", "core.residuals_over_time", "ml.feature_importance"]
    )
    phases = [e.payload["phase"] for e in fit.of("state")]
    assert phases[:3] == ["bootstrap", "load_inputs", "fit"] and phases[-2:] == ["upload", "done"]
    progress = [e for e in fit.of("progress")]
    gaps = [(b.ts - a.ts).total_seconds() for a, b in itertools.pairwise(progress)]
    assert all(g >= 2.0 - 1e-3 for g in gaps), gaps  # coalesced to one per 2 s

    # ---- score: 4-week forecast with a new store ------------------------------------
    future = xgb.payload_future(data, 0, "tiny").drop_vars("units_sold")
    gcs.put(gcs.buckets.data, "t/default/payloads/future.zarr.zip", write_zarr_zip(future) or b"")
    request = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "endpoint://default/store-demand",
        "mode": "forecast",
        "horizon": {"dim": "time", "periods": 4},
        "output": {"summary": ["median", "q10", "q90"]},
    }
    score = run_supervisor(
        make_run(
            gcs,
            RunKind.SCORE,
            request,
            inputs={
                "bundle": (gcs.buckets.runs, fit.run.prefix + "bundle.zarr.zip"),
                "bundle.manifest": (gcs.buckets.runs, fit.run.prefix + "bundle.manifest.json"),
                "dataset": (gcs.buckets.data, "t/default/payloads/future.zarr.zip"),
            },
        )
    )
    manifest = _ok(score)
    assert {"score.zarr.zip", "result.json", "plots.json", "ui/summary.arrow"} <= set(
        manifest.objects
    )
    tree = read_datatree_zip(score.output("score.zarr.zip"))
    summary = tree["summary"].to_dataset()["y_hat"]
    assert summary.sizes["week"] == 4 and "s_new" in summary["store_id"].values
    result = json.loads(score.output("result.json"))
    assert result["representation"] == "quantiles"
    assert any(i["code"] == "UNSEEN_LEVELS" for i in result["issues"])


def test_score_with_a_tampered_bundle_is_an_integrity_failure(gcs: ShimGcs) -> None:
    gcs.put(gcs.buckets.runs, "t/default/x/bundle.zarr.zip", b"PK\x03\x04 not a bundle")
    fake = BundleManifest(bundle_id="b", created_at="2026-10-15T00:00:00Z", spec_hash="sha256:x")
    gcs.put(gcs.buckets.runs, "t/default/x/bundle.manifest.json", fake.model_dump_json().encode())
    run = make_run(
        gcs,
        RunKind.SCORE,
        {"apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": "endpoint://p/m"},
        inputs={
            "bundle": (gcs.buckets.runs, "t/default/x/bundle.zarr.zip"),
            "bundle.manifest": (gcs.buckets.runs, "t/default/x/bundle.manifest.json"),
        },
    )
    result = run_supervisor(run)
    assert result.code == RunnerExit.INTEGRITY_ERROR, result.stderr[-2000:]


# ============================================================================ E02 radon


def _sigterm_after_checkpoint() -> Any:
    sent: list[int] = []

    def on_event(ev: RunEvent, proc: subprocess.Popen[bytes]) -> None:
        if ev.type is RunEventType.CHECKPOINT and not sent:
            sent.append(1)
            proc.send_signal(signal.SIGTERM)

    return on_event


def test_e02_zarr_ingest_checkpointed_fit_sigterm_partial_resume_and_score(gcs: ShimGcs) -> None:
    data, _ = e02.generate(0, "tiny")
    data.attrs["mapi_schema"] = e02.schema().model_dump_json()
    gcs.put(gcs.buckets.quarantine, "t/default/uploads/radon", write_zarr_zip(data) or b"")

    ingest = run_supervisor(
        make_run(
            gcs,
            RunKind.INGEST,
            {"format": "zarr_zip"},
            inputs={"upload": (gcs.buckets.quarantine, "t/default/uploads/radon")},
        )
    )
    _ok(ingest)
    assert DataSchema.model_validate_json(ingest.output("schema.json")) == e02.schema()
    data_key = ingest.run.prefix + "data.zarr.zip"

    fit_cfg = {
        "tune": 50,
        "draws": 1500,
        "chains": 2,
        "checkpoint": {"every_draws": 25},
        "keep": {"log_likelihood": True},
    }
    spec = _spec("e02", fit=fit_cfg)
    inputs = {"dataset": (gcs.buckets.runs, data_key)}

    # ---- attempt 1: SIGTERM after the first checkpoint -> PARTIAL ---------------------
    first = run_supervisor(
        make_run(gcs, RunKind.FIT, spec, inputs=inputs, run_id="run_e02", checkpoints=True),
        on_event=_sigterm_after_checkpoint(),
    )
    assert first.code == RunnerExit.PARTIAL, first.stderr[-4000:]
    assert_signed_stream(first)
    partial = first.manifest()
    assert partial.status == "partial"
    assert verify_output_manifest(partial, first.run.key)
    listing = gcs.list(gcs.buckets.runs, first.run.prefix)
    assert verify_output_objects(partial, listing, expected_prefix=first.run.prefix).ok
    ckpt_names = sorted(n.removeprefix("checkpoints/") for n in partial.objects)
    assert len(ckpt_names) == 2 and all(n.startswith("ckpt-") for n in ckpt_names)
    complete = CompletePayload.model_validate(first.of("complete")[-1].payload)
    assert complete.status == "partial"
    saved = [CheckpointPayload.model_validate(e.payload) for e in first.of("checkpoint")]
    done_at = max(c.draws_done or 0 for c in saved)
    assert 0 < done_at < 1500
    assert not any(n.startswith("bundle") for n in partial.objects)

    # ---- attempt 2: resume from the uploaded checkpoints -------------------------------
    ckpt_prefix = first.run.rs.checkpoint_prefix
    assert ckpt_prefix is not None
    second = run_supervisor(
        make_run(
            gcs,
            RunKind.FIT,
            spec,
            inputs=inputs,
            run_id="run_e02",
            attempt=2,
            checkpoints=True,
            resume={n: (gcs.buckets.runs, ckpt_prefix + n) for n in ckpt_names},
        )
    )
    _ok(second)
    progress = second.of("progress")
    assert progress, "sampling progress is streamed"
    gaps = [(b.ts - a.ts).total_seconds() for a, b in itertools.pairwise(progress)]
    assert all(g >= 2.0 - 1e-3 for g in gaps), gaps  # coalesced: at most one per 2 s
    logs = [LogPayload.model_validate(e.payload).line for e in second.of("log")]
    assert any("resuming from a checkpoint" in line for line in logs), logs[-20:]
    tree = read_datatree_zip(second.output("bundle.zarr.zip"))
    post = tree["model/posterior"].to_dataset()
    assert post.sizes["draw"] == 1500 and post.sizes["chain"] == 2
    summaries = json.loads(second.output("ui/summaries.json"))
    assert summaries["rows"] and summaries["rows"][0]["r_hat"] is not None
    thin = read_datatree_zip(second.output("ui/thin.zarr.zip"))
    assert thin["posterior"].to_dataset().sizes["sample"] == 300
    assert sorted(_check_plots(second.output("plots.json"))) == [
        "core.convergence",
        "core.posterior_forest",
    ]
    diags = second.of("diagnostic")
    assert diags, "convergence diagnostics are streamed as events"

    # ---- score a new county -------------------------------------------------------------
    payload = e02.payload_new_county(data, 0, "tiny")
    gcs.put(gcs.buckets.data, "t/default/payloads/new_county", write_zarr_zip(payload) or b"")
    request = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "endpoint://default/radon",
        "mode": "predict",
        "output": {"kind": "expected", "draws": 40},
    }
    score = run_supervisor(
        make_run(
            gcs,
            RunKind.SCORE,
            request,
            inputs={
                "bundle": (gcs.buckets.runs, second.run.prefix + "bundle.zarr.zip"),
                "bundle.manifest": (gcs.buckets.runs, second.run.prefix + "bundle.manifest.json"),
                "dataset": (gcs.buckets.data, "t/default/payloads/new_county"),
            },
        )
    )
    _ok(score)
    out = read_datatree_zip(score.output("score.zarr.zip"))
    assert out["draws"].to_dataset()["y_hat"].sizes["sample"] == 40
    assert out["summary"].to_dataset()["y_hat"].sizes["home"] == 6
    issues = json.loads(score.output("result.json"))["issues"]
    assert any(i["code"] == "UNSEEN_LEVELS" for i in issues)


def test_mff_ingest_and_an_invalid_spec_dry_run(gcs: ShimGcs) -> None:
    rows = []
    for period in ("2026-01-04", "2026-01-11", "2026-01-18", "2026-01-25"):
        for geo in ("north", "south"):
            for var, value in (("sales", 10.0), ("tv_spend", 3.0)):
                rows.append(f"{period},{geo},all,,,,{var},{value}")
    header = "Period,Geography,Product,Campaign,Outlet,Creative,VariableName,VariableValue"
    gcs.put(gcs.buckets.quarantine, "t/default/uploads/mff", "\n".join([header, *rows]).encode())
    ingest = run_supervisor(
        make_run(
            gcs,
            RunKind.INGEST,
            {"preset": "mff", "mff": {"roles": {"sales": "target", "tv_spend": "spend"}}},
            inputs={"upload": (gcs.buckets.quarantine, "t/default/uploads/mff")},
        )
    )
    _ok(ingest)
    schema = DataSchema.model_validate_json(ingest.output("schema.json"))
    assert set(schema.dims) == {"date", "geo"} and schema.dims["date"].freq == "W-SUN"

    spec = _spec("xgb_store_demand")  # binds store_id/sku/week, which this data lacks
    val = run_supervisor(
        make_run(
            gcs,
            RunKind.VALIDATE,
            spec,
            inputs={"dataset": (gcs.buckets.runs, ingest.run.prefix + "data.zarr.zip")},
        )
    )
    _ok(val)  # the dry run succeeds; its report says the spec is invalid
    report = json.loads(val.output("validation.json"))
    assert report["valid"] is False and report["issues"]
    assert any(e.payload["code"] == "SPEC_INVALID" for e in val.of("warning"))
