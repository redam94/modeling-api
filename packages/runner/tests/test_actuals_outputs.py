"""What a forecast was conditioned on reaches the score run's outputs: ``summary.json`` (the
``ScoreSummary`` the API inlines) and ``result.json`` (:mod:`mapi_spec.actuals`)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_runner.worker.score import _result_json, summary_document
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.testing import toy
from mapi_spec import DataSchema, ScoreRequest
from mapi_spec.api import ScoreSummary

pytestmark = pytest.mark.unit

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: ar, project: test}
data:
  dataset: dataset://test/sales@v1
  bind: {dims: {geo: region, time: month}, vars: {y: sales}}
model:
  use: toy.ar1@^1
  fit: {mode: train_only}
"""

RAW = DataSchema.model_validate(
    {
        "dims": {
            "region": {"kind": "categorical"},
            "month": {"kind": "time", "policy": "extend", "freq": "MS"},
        },
        "vars": {"sales": {"dims": ["region", "month"], "dtype": "float64", "role": "target"}},
    }
)


def _tree(payload: xr.Dataset | None) -> xr.DataTree:
    t = pd.date_range("2021-01-01", periods=24, freq="MS").as_unit("ns")
    y = 100 + np.cumsum(np.random.default_rng(0).normal(0, 1, size=(1, 24)), axis=1)
    data = xr.Dataset({"sales": (("region", "month"), y)}, coords={"region": ["n"], "month": t})
    reg = toy.toy_registry()
    bundle = fit_spec(SPEC, data, registry=reg, raw_schema=RAW)
    req = ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": "endpoint://test/ar",
            "mode": "forecast",
            "horizon": {"dim": "time", "periods": 3},
        }
    )
    return score_bundle(bundle, req, payload, registry=reg)


def test_summary_and_result_carry_the_conditioning() -> None:
    payload = xr.Dataset(
        {"sales": (("region", "month"), [[130.0, 131.0]])},
        coords={"region": ["n"], "month": pd.date_range("2023-01-01", periods=2, freq="MS")},
    )
    tree = _tree(payload)
    doc = summary_document(tree)
    assert doc is not None
    summary = ScoreSummary.model_validate(doc)
    rec = summary.conditioning
    assert rec is not None and rec.conditioned and rec.n_actuals == 2
    assert rec.actuals_through == "2023-02-01" and rec.periods == 3
    assert summary.tables and len(summary.tables[0].rows) == 5  # 2 actuals + 3 forecasts
    result = _result_json(tree)
    assert result["conditioning"]["n_actuals"] == 2  # parsed JSON, not a string


def test_plain_forecasts_say_they_used_no_actuals() -> None:
    doc = summary_document(_tree(None))
    assert doc is not None
    rec = ScoreSummary.model_validate(doc).conditioning
    assert rec is not None and not rec.conditioned and rec.fit_end == "2022-12-01"
