"""Guided ingest through the real supervisor and worker: a BayesInsight MFF upload with the
imported mapping lands as a dataset plus ``columns.json``, and a refusal reaches the run's
error event with its plain message and suggested action."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from mapi_sdk.wire import read_zarr_zip
from mapi_spec.bayesinsight import import_bayesinsight
from mapi_spec.executor import RunKind
from mapi_spec.guided import BayesInsightImport, ColumnProfile
from mapi_spec.ingest import BayesInsightMetadata, BayesInsightModelDef
from mapi_spec.jobs import DATASET_OUTPUTS, IngestJob
from mapi_spec.run import RunnerExit
from mapi_spec.schema import DataSchema
from runner_helpers import assert_signed_stream, make_run, run_supervisor
from shimgcs import ShimGcs

pytestmark = pytest.mark.integration

MFF = Path(__file__).parent / "fixtures" / "bayesinsight_mff.csv"
UPLOAD = "t/default/uploads/upl_bi"


def _job() -> IngestJob:
    req = BayesInsightImport(
        name="bi",
        dataset="bi",
        model_def=BayesInsightModelDef.model_validate(
            {"variable_details": [
                {"variable_name": "Sales_Total_Total_Total", "variable_type": "exog"},
                {"variable_name": "media_var_0_Total_Total_Total", "variable_type": "media"},
                {"variable_name": "media_var_1_Total_Total_Total", "variable_type": "media"},
            ]}
        ),
        metadata=BayesInsightMetadata.model_validate(
            {"allowed_outlets": ["Total"], "allowed_campaigns": ["Total"],
             "allowed_creatives": ["Total"], "row_ids": ["Geography", "Product", "Period"]}
        ),
    )  # fmt: skip
    return IngestJob(mapping=import_bayesinsight(req, project="default").mapping)


def test_bayesinsight_ingest_writes_the_dataset_and_column_profiles(gcs: ShimGcs) -> None:
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
    assert set(result.manifest().objects) == set(DATASET_OUTPUTS)
    ds = read_zarr_zip(result.output("data.zarr.zip"))
    assert dict(ds.sizes) == {"date": 40, "geo": 4, "channel": 2}
    schema = DataSchema.model_validate_json(result.output("schema.json"))
    assert schema.vars["spend"].role.value == "spend"
    profiles = [ColumnProfile.model_validate(c) for c in json.loads(result.output("columns.json"))]
    assert {p.name for p in profiles if p.origin == "mff_variable"} >= {"Sales", "media_var_0"}


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
