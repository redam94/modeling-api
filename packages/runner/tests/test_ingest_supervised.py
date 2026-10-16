"""Guided ingest through the real supervisor and worker: an MFF upload with a geo-MMM draft's
mapping lands as a dataset plus ``columns.json``, and a refusal reaches the run's error event
with its plain message and suggested action."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from mapi_sdk.wire import read_zarr_zip
from mapi_spec.executor import RunKind
from mapi_spec.guided import GUIDED_TEMPLATES, ColumnProfile, GuidedAnswers, build_draft
from mapi_spec.ingest import MffOptions
from mapi_spec.jobs import DATASET_OUTPUTS, IngestJob
from mapi_spec.run import RunnerExit
from mapi_spec.schema import DataSchema
from runner_helpers import assert_signed_stream, make_run, run_supervisor
from shimgcs import ShimGcs

pytestmark = pytest.mark.integration

MFF = Path(__file__).parent / "fixtures" / "mff_long.csv"
UPLOAD = "t/default/uploads/upl_bi"


def _job() -> IngestJob:
    cols = [
        ColumnProfile(name="Period", dtype="date", n_rows=10, detected_freq="W-MON"),
        ColumnProfile(name="Geography", dtype="string", n_rows=10, n_distinct=4),
    ] + [
        ColumnProfile(name=v, dtype="number", n_rows=10, min=0.0, origin="mff_variable")
        for v in ("Sales", "media_var_0", "media_var_1")
    ]
    req = GuidedAnswers(
        template_id="geo_mmm", name="mff", dataset="mff", format="mff",
        answers={"kpi": "Sales", "date": "Period", "geo": "Geography",
                 "spend": ["media_var_0", "media_var_1"]},
        columns=cols,
    )  # fmt: skip
    mapping = build_draft(GUIDED_TEMPLATES["geo_mmm"], req, project="default").mapping
    combined = mapping.model_copy(
        update={"mff": MffOptions(combine={"Geography": ["Geography", "Product"]})}
    )
    return IngestJob(mapping=combined)


def test_mff_ingest_writes_the_dataset_and_column_profiles(gcs: ShimGcs) -> None:
    gcs.put(gcs.buckets.quarantine, UPLOAD, MFF.read_bytes(), "text/csv")
    result = run_supervisor(
        make_run(
            gcs,
            RunKind.INGEST,
            _job().model_dump(mode="json"),
            inputs={"upload": (gcs.buckets.quarantine, UPLOAD)},
        )
    )
    assert result.code == RunnerExit.OK, [e.payload for e in result.of("error")]
    assert_signed_stream(result)
    objects = set(result.manifest().objects)
    # M3.5: the cheap Data Explorer sections ride along under ui/explore/
    assert {o for o in objects if not o.startswith("ui/explore/")} == set(DATASET_OUTPUTS)
    assert "ui/explore/index.json" in objects and "ui/explore/overview.json" in objects
    ds = read_zarr_zip(result.output("data.zarr.zip"))
    assert dict(ds.sizes) == {"period": 40, "geography": 4, "channel": 2}
    schema = DataSchema.model_validate_json(result.output("schema.json"))
    assert schema.vars["spend"].role.value == "spend"
    profiles = [ColumnProfile.model_validate(c) for c in json.loads(result.output("columns.json"))]
    assert {p.name for p in profiles if p.origin == "mff_variable"} >= {"Sales", "media_var_0"}


def test_identical_data_writes_identical_outputs(gcs: ShimGcs) -> None:
    """Ingest outputs are stored content-addressed with the data, so the same file uploaded
    again - as v2, or into another dataset or project - must write the same objects (the
    Data Explorer index used to name the dataset version)."""
    gcs.put(gcs.buckets.quarantine, UPLOAD, MFF.read_bytes(), "text/csv")
    outputs = []
    for ref in ("dataset://default/mff@v1", "dataset://other/sales@v7"):
        job = _job().model_copy(update={"dataset": ref})
        result = run_supervisor(
            make_run(
                gcs,
                RunKind.INGEST,
                job.model_dump(mode="json"),
                inputs={"upload": (gcs.buckets.quarantine, UPLOAD)},
            )
        )
        assert result.code == RunnerExit.OK, [e.payload for e in result.of("error")]
        objects = sorted(result.manifest().objects)
        outputs.append({name: result.output(name) for name in objects})
    first, second = outputs
    assert set(first) == set(second) and "ui/explore/index.json" in first
    # only the plot specs differ (their provenance carries a timestamp; the finalizer keeps
    # the stored ones): the data, its sidecars, the views and the index are identical
    diff = [n for n in first if first[n] != second[n]]
    assert all(n.startswith("ui/explore/plots/") for n in diff), diff
    assert json.loads(first["ui/explore/index.json"])["ref"] == "dataset://upload/content@latest"


def test_a_refused_upload_explains_itself(gcs: ShimGcs) -> None:
    bomb = gzip.compress(b"Period,VariableName\n" + b"2020-01-06,x\n" * 2_000_000)
    gcs.put(gcs.buckets.quarantine, UPLOAD, bomb, "application/gzip")
    job = _job().model_copy(
        update={"max_decompression_ratio": 2.0, "max_uncompressed_bytes": 10_000}
    )
    result = run_supervisor(
        make_run(
            gcs,
            RunKind.INGEST,
            job.model_dump(mode="json"),
            inputs={"upload": (gcs.buckets.quarantine, UPLOAD)},
        )
    )
    assert result.code != RunnerExit.OK
    [error] = result.of("error")
    # the worker is untrusted: its code is recorded, its text travels as untrusted text
    assert "PAYLOAD_TOO_LARGE" in error.payload["message"]
    assert "expands to more than" in (error.payload.get("untrusted_text") or "")
    # each issue arrives as a plain-language diagnostic with the suggested action
    [diag] = [
        d.payload for d in result.of("diagnostic") if d.payload["code"] == "PAYLOAD_TOO_LARGE"
    ]
    assert "expands to more than" in diag["message_plain"] and diag["suggested_action"]
