"""Inline XrJSON score payloads (endpoint calls, plan §3.2): the API stores the call's
``data``/``scenario`` as run inputs ``inline.data``/``inline.scenario``; the worker decodes
them in the sandbox with the wire-format caps and the dims' types, and scores exactly what
the same rows uploaded as a dataset would score."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_conformance.fixtures.xgb_store_demand import data as xgb
from mapi_sdk.wire import read_datatree_zip, to_xrjson, write_zarr_zip
from mapi_spec.endpoints import INPUT_INLINE_DATA
from mapi_spec.executor import RunKind
from mapi_spec.loader import canonical_json, load_model_spec
from mapi_spec.run import ErrorPayload, RunnerExit
from mapi_spec.xrjson import XrJsonDataset
from runner_helpers import Result, make_run, run_supervisor
from shimgcs import ShimGcs

pytestmark = pytest.mark.integration


def _inline(ds: xr.Dataset) -> bytes:
    """What the API stores for an inline payload: the XrJsonDataset DTO's JSON (no dtypes,
    ISO dates, plain numbers)."""
    doc = to_xrjson(ds)
    for section in ("coords", "data_vars"):
        for var in doc[section].values():
            var.pop("dtype", None)
    return json.dumps(XrJsonDataset.model_validate(doc).model_dump(mode="json")).encode()


def _ok(result: Result) -> None:
    errors = [e.payload for e in result.of("error")]
    assert result.code == RunnerExit.OK, (errors, result.stderr[-4000:])


@pytest.fixture
def fitted(gcs: ShimGcs) -> dict[str, Any]:
    data, _ = xgb.generate(0, "tiny")
    data.attrs["mapi_schema"] = xgb.schema().model_dump_json()
    d = gcs.buckets.data
    gcs.put(d, "t/default/datasets/sd/data.zarr.zip", write_zarr_zip(data) or b"")
    text = (Path(xgb.__file__).parent / "spec.yaml").read_text()
    spec = json.loads(canonical_json(load_model_spec(text)))
    spec["model"]["settings"]["estimator"]["params"] = {"n_estimators": 20, "max_depth": 3}
    spec["model"]["fit"]["mode"] = "train_only"
    spec.pop("evaluate", None)
    fit = run_supervisor(
        make_run(
            gcs, RunKind.FIT, spec, inputs={"dataset": (d, "t/default/datasets/sd/data.zarr.zip")}
        )
    )
    _ok(fit)
    runs = gcs.buckets.runs
    return {
        "data": data,
        "bundle": {
            "bundle": (runs, fit.run.prefix + "bundle.zarr.zip"),
            "bundle.manifest": (runs, fit.run.prefix + "bundle.manifest.json"),
        },
    }


REQUEST = {
    "apiVersion": "mapi/v1",
    "kind": "ScoreRequest",
    "model": "endpoint://default/store-demand",
    "mode": "predict",
}


def _prediction(result: Result) -> xr.Dataset:
    tree = read_datatree_zip(result.output("score.zarr.zip"))
    for node in tree.subtree:
        ds = node.to_dataset()
        if ds.data_vars:
            return ds
    raise AssertionError("the score output holds no data")


def test_inline_payload_scores_like_the_same_rows_uploaded(
    gcs: ShimGcs, fitted: dict[str, Any]
) -> None:
    rows = fitted["data"]
    d = gcs.buckets.data
    gcs.put(d, "t/default/payloads/rows.zarr.zip", write_zarr_zip(rows) or b"")
    gcs.put(gcs.buckets.quarantine, "t/default/inline/rows.json", _inline(rows))
    uploaded = run_supervisor(
        make_run(
            gcs, RunKind.SCORE, REQUEST,
            inputs={**fitted["bundle"], "dataset": (d, "t/default/payloads/rows.zarr.zip")},
        )
    )  # fmt: skip
    inline = run_supervisor(
        make_run(
            gcs, RunKind.SCORE, REQUEST,
            inputs={**fitted["bundle"],
                    INPUT_INLINE_DATA: (gcs.buckets.quarantine, "t/default/inline/rows.json")},
        )
    )  # fmt: skip
    _ok(uploaded)
    _ok(inline)
    a, b = _prediction(uploaded), _prediction(inline)
    assert set(a.data_vars) == set(b.data_vars) and a.data_vars
    for name in a.data_vars:
        np.testing.assert_allclose(a[name].values, b[name].values, rtol=1e-9)
    assert list(b["week"].values) == list(rows["week"].values)


def test_inline_payload_named_like_the_endpoint_contract_scores_the_same(
    gcs: ShimGcs, fitted: dict[str, Any]
) -> None:
    """An endpoint's input contract names dims and variables canonically (``time``, not the
    dataset's ``week``); such a payload is typed and scored like the uploaded rows."""
    rows = fitted["data"]
    canonical = rows.rename(
        {"week": "time", "store_id": "store", "units_sold": "y", "promo_flag": "promo"}
    )
    q = gcs.buckets.quarantine
    gcs.put(q, "t/default/inline/dataset-names.json", _inline(rows))
    gcs.put(q, "t/default/inline/canonical.json", _inline(canonical))
    results = []
    for key in ("dataset-names", "canonical"):
        inputs = {**fitted["bundle"], INPUT_INLINE_DATA: (q, f"t/default/inline/{key}.json")}
        results.append(run_supervisor(make_run(gcs, RunKind.SCORE, REQUEST, inputs=inputs)))
    for result in results:
        _ok(result)
    a, b = (_prediction(r) for r in results)
    assert set(a.data_vars) == set(b.data_vars) and a.data_vars
    for name in a.data_vars:
        np.testing.assert_allclose(a[name].values, b[name].values, rtol=1e-9)
    assert b["week"].dtype.kind == "M"  # typed as the time dim, named as in the dataset


def test_unreadable_inline_payloads_are_schema_errors(gcs: ShimGcs, fitted: dict[str, Any]) -> None:
    q = gcs.buckets.quarantine
    rows = fitted["data"].isel(week=slice(-2, None), store_id=slice(0, 1))
    doc = json.loads(_inline(rows))
    doc["coords"]["week"]["data"] = ["not a date"] * len(doc["coords"]["week"]["data"])
    gcs.put(q, "t/default/inline/bad-week.json", json.dumps(doc).encode())
    gcs.put(q, "t/default/inline/nan.json", b'{"data_vars": {"price": {"dims": [], "data": NaN}}}')
    for key, why in (("bad-week", "coordinate 'week'"), ("nan", "not a readable dataset")):
        result = run_supervisor(
            make_run(
                gcs, RunKind.SCORE, REQUEST,
                inputs={**fitted["bundle"], INPUT_INLINE_DATA: (q, f"t/default/inline/{key}.json")},
            )
        )  # fmt: skip
        assert result.code != RunnerExit.OK
        [err] = [ErrorPayload.model_validate(e.payload) for e in result.of("error")]
        text = f"{err.message} {err.untrusted_text or ''}"
        assert "inline data payload" in text and why in text, err
