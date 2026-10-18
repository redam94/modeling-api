"""Tree datasets end to end through the real supervisor and worker (M4): a NetCDF file with
groups is ingested as a tree version, a tree-input model is fitted from it (``data.nodes``
selects the flat pipeline input; the model also gets the tree), scored and validated; a raw
HDF5 file is inspected; an unsafe HDF5 upload is refused with ``INGEST_UNSAFE_FILE``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pytest
from mapi_sdk.testing import toy
from mapi_sdk.tree import read_tree_zip
from mapi_sdk.wire import read_datatree_zip
from mapi_spec.api import ValidateResponse
from mapi_spec.errors import ErrorCode
from mapi_spec.executor import RunKind
from mapi_spec.ingest import UploadInspection
from mapi_spec.jobs import INSPECT_OUTPUT, TREE_DATASET_OUTPUTS, IngestJob, InspectJob
from mapi_spec.loader import canonical_json, load_model_spec
from mapi_spec.run import ErrorPayload, RunnerExit
from mapi_spec.tree import TreeSchema
from mapi_spec.views import ExplorerIndex
from runner_helpers import Result, assert_signed_stream, make_run, run_supervisor
from shimgcs import ShimGcs
from tree_fixtures import write_raw_hdf5

pytestmark = pytest.mark.integration

TOY = "mapi_sdk.testing.toy:toy_registry"
UPLOAD = "t/default/uploads/upl_tree"


def _ok(result: Result) -> None:
    errors = [e.payload for e in result.of("error")]
    assert result.code == RunnerExit.OK, (errors, result.stderr[-4000:])


def _spec(**over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(
        canonical_json(load_model_spec(toy.tree_spec(split=False, mode="train_only")))
    )
    spec["data"].update(over)
    return spec


@pytest.fixture
def tree_version(gcs: ShimGcs, tmp_path: Path) -> tuple[str, str]:
    """A tree version ingested from a NetCDF-4 file with groups: ``(bucket, data key)``."""
    path = tmp_path / "toy.nc"
    toy.tree_dataset(weeks=20).to_netcdf(path, engine="h5netcdf")
    gcs.put(gcs.buckets.quarantine, UPLOAD, path.read_bytes(), "application/x-netcdf")
    job = IngestJob.model_validate(
        {"mapping": {"format": "netcdf"}, "dataset": "dataset://growth/toy_tree@v1"}
    )
    result = run_supervisor(
        make_run(
            gcs,
            RunKind.INGEST,
            job.model_dump(mode="json"),
            inputs={"upload": (gcs.buckets.quarantine, UPLOAD)},
        )
    )
    _ok(result)
    assert_signed_stream(result)
    objects = set(result.manifest().objects)
    assert {o for o in objects if not o.startswith("ui/")} == set(TREE_DATASET_OUTPUTS) - {
        "columns.json"
    }
    schema = TreeSchema.model_validate_json(result.output("tree_schema.json"))
    assert set(schema.nodes) == {"/", "/sales", "/media", "/adjust"}
    assert schema.default_node() == "/sales"
    tree = read_tree_zip(result.output("data.tree.zip"))
    assert tree["sales"].to_dataset()["revenue"].sizes["date"] == 20
    nodes = json.loads(result.output("ui/explore/nodes/index.json"))
    assert set(nodes["nodes"]) == {"/sales", "/media", "/adjust"}
    return gcs.buckets.runs, result.run.prefix + "data.tree.zip"


def test_fit_and_score_a_tree_input_model_from_a_tree_version(
    gcs: ShimGcs, tree_version: tuple[str, str]
) -> None:
    fit = run_supervisor(
        make_run(gcs, RunKind.FIT, _spec(), inputs={"dataset": tree_version}), registry=TOY
    )
    _ok(fit)
    bundle = read_datatree_zip(fit.output("bundle.zarr.zip"))
    learned = json.loads(str(bundle["model"].attrs["learned"]))
    assert learned["tree_nodes"] == ["/", "/adjust", "/media", "/sales"]
    runs = gcs.buckets.runs
    request = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "model://growth/toy-tree@latest",
        "mode": "predict",
        "data": "dataset://growth/toy_tree@v1",
    }
    score = run_supervisor(
        make_run(
            gcs,
            RunKind.SCORE,
            {"request": request},
            inputs={
                "bundle": (runs, fit.run.prefix + "bundle.zarr.zip"),
                "bundle.manifest": (runs, fit.run.prefix + "bundle.manifest.json"),
                "dataset": tree_version,
            },
        ),
        registry=TOY,
    )
    _ok(score)
    out = read_datatree_zip(score.output("score.zarr.zip"))
    assert "summary" in out.children


def test_validate_reports_a_bad_node_selection_as_an_issue(
    gcs: ShimGcs, tree_version: tuple[str, str]
) -> None:
    spec = _spec(nodes={"sales": {"path": "/sales"}, "media": {"path": "/nowhere"}})
    result = run_supervisor(
        make_run(gcs, RunKind.VALIDATE, spec, inputs={"dataset": tree_version}), registry=TOY
    )
    _ok(result)
    report = ValidateResponse.model_validate_json(result.output("validation.json"))
    assert not report.valid
    assert any(i.pointer == "/data/nodes/media" for i in report.issues)
    good = run_supervisor(
        make_run(gcs, RunKind.VALIDATE, _spec(), inputs={"dataset": tree_version}), registry=TOY
    )
    _ok(good)
    assert ValidateResponse.model_validate_json(good.output("validation.json")).valid


def test_explore_a_node_of_a_tree_version(gcs: ShimGcs, tree_version: tuple[str, str]) -> None:
    job = {
        "target": "dataset",
        "ref": "dataset://growth/toy_tree@v1",
        "data": {"sections": ["overview"], "node": "/media"},
    }
    result = run_supervisor(make_run(gcs, RunKind.EXPLORE, job, inputs={"dataset": tree_version}))
    _ok(result)
    index = ExplorerIndex.model_validate_json(result.output("ui/explore/index.json"))
    assert index.node == "/media"
    assert json.loads(result.output("ui/explore/overview.json"))["node"] == "/media"


def test_inspect_lists_a_raw_hdf5_upload(gcs: ShimGcs, tmp_path: Path) -> None:
    path = write_raw_hdf5(tmp_path / "raw.h5")
    gcs.put(gcs.buckets.quarantine, UPLOAD, path.read_bytes(), "application/x-hdf5")
    result = run_supervisor(
        make_run(
            gcs,
            RunKind.INSPECT,
            InspectJob().model_dump(mode="json"),
            inputs={"upload": (gcs.buckets.quarantine, UPLOAD)},
        )
    )
    _ok(result)
    assert set(result.manifest().objects) == {INSPECT_OUTPUT}
    doc = UploadInspection.model_validate_json(result.output(INSPECT_OUTPUT))
    assert doc.format == "hdf5" and "/observations/image" in {a.path for a in doc.arrays}


def test_an_unsafe_upload_fails_with_ingest_unsafe_file(gcs: ShimGcs, tmp_path: Path) -> None:
    path = tmp_path / "evil.nc"
    with h5py.File(path, "w") as f:
        f["x"] = np.arange(3.0)
        f["passwd"] = h5py.ExternalLink("/etc/passwd", "/")
    gcs.put(gcs.buckets.quarantine, UPLOAD, path.read_bytes(), "application/x-netcdf")
    result = run_supervisor(
        make_run(
            gcs,
            RunKind.INGEST,
            {"mapping": {"format": "netcdf"}},
            inputs={"upload": (gcs.buckets.quarantine, UPLOAD)},
        )
    )
    assert result.code != RunnerExit.OK
    [error] = result.of("error")
    payload = ErrorPayload.model_validate(error.payload)
    # the worker is untrusted: its code is recorded, its text travels as untrusted text
    assert ErrorCode.INGEST_UNSAFE_FILE.value in payload.message
    assert "external link" in (payload.untrusted_text or "")
    # ... and its typed issue is the run's error, for clients to branch on
    assert [e.code for e in payload.errors] == [ErrorCode.INGEST_UNSAFE_FILE]
    assert payload.errors[0].pointer == "/upload"
    assert "external link" in (payload.errors[0].untrusted_text or "")
    assert "external link" not in payload.errors[0].message
    [diag] = [
        d.payload
        for d in result.of("diagnostic")
        if d.payload["code"] == ErrorCode.INGEST_UNSAFE_FILE.value
    ]
    assert "external link" in diag["message_plain"] and diag["suggested_action"]
