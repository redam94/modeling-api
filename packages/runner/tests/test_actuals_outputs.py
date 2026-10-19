"""What a forecast was conditioned on reaches the score run's outputs: ``summary.json`` (the
``ScoreSummary`` the API inlines) and ``result.json`` (:mod:`mapi_spec.actuals`)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_runner.worker.score import _result_json, actuals_tables, summary_document
from mapi_sdk.bundle import Bundle
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


def _scored(
    payload: xr.Dataset | None, **output: object
) -> tuple[Bundle, ScoreRequest, xr.DataTree]:
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
            **({"output": output} if output else {}),
        }
    )
    return bundle, req, score_bundle(bundle, req, payload, registry=reg)


def test_the_summary_carries_the_actuals_next_to_their_predictions() -> None:
    payload = xr.Dataset(
        {"sales": (("region", "month"), [[130.0, 131.0, np.nan]])},
        coords={"region": ["n"], "month": pd.date_range("2023-01-01", periods=3, freq="MS")},
    )
    bundle, req, tree = _scored(payload)
    actuals = actuals_tables(bundle, req, payload, tree)
    summary = ScoreSummary.model_validate(summary_document(tree, actuals))
    [table] = summary.actuals
    pred = next(t for t in summary.tables if t.variable == table.variable)
    assert table.columns == ["region", "month", "actual"]
    assert table.rows == [["n", "2023-01-01", 130.0], ["n", "2023-02-01", 131.0]]
    # every actual has a (one-step-ahead) prediction to compare with, on the same coordinates
    keys = {(r[pred.columns.index("region")], r[pred.columns.index("month")]) for r in pred.rows}
    assert {("n", "2023-01-01"), ("n", "2023-02-01")} <= keys
    # canonical names follow the output; model-space output has nothing comparable
    _, creq, ctree = _scored(payload, names="canonical")
    [canon] = actuals_tables(bundle, creq, payload, ctree)
    assert canon["columns"] == ["geo", "time", "actual"]
    _, mreq, mtree = _scored(payload, units="model")
    assert actuals_tables(bundle, mreq, payload, mtree) == []
    # a plain forecast has none
    _, preq, ptree = _scored(None)
    assert actuals_tables(bundle, preq, None, ptree) == []


def test_the_summary_tables_mark_circular_outputs_with_their_period() -> None:
    """``summary.json`` carries the ``period`` of a circular output summarised on the circle,
    so a reader knows that an interval with lower > upper crosses the seam."""
    from mapi_sdk.summarise import summarise
    from mapi_spec import Representation

    rng = np.random.default_rng(0)
    north = (rng.normal(0.0, 5.0, (2, 200, 2)) % 360.0).astype(float)
    pred = xr.Dataset(
        {
            "heading": (("chain", "draw", "obs"), north, {"period": 360.0}),
            "speed": (("chain", "draw", "obs"), north + 1.0),
        },
        coords={"obs": ["a", "b"]},
    )
    summary = summarise(pred, Representation.DRAWS, ["mean", "hdi_90"], periods={"heading": 360.0})
    doc = summary_document(xr.DataTree.from_dict({"summary": summary}))
    assert doc is not None
    parsed = ScoreSummary.model_validate(doc)
    tables = {t.variable: t for t in parsed.tables}
    assert tables["heading"].period == 360.0 and tables["speed"].period is None
    assert "period" not in parsed.model_dump(mode="json")["tables"][1]  # speed: omitted
    cols = tables["heading"].columns
    for row in tables["heading"].rows:
        cell = dict(zip(cols, row, strict=True))
        mean, lo, hi = cell["mean"], cell["hdi_90_lower"], cell["hdi_90_upper"]
        assert isinstance(mean, float) and isinstance(lo, float) and isinstance(hi, float)
        assert min(mean, 360.0 - mean) < 2.0  # north, not south
        assert lo > 300.0 and hi < 60.0  # the interval crosses the seam
