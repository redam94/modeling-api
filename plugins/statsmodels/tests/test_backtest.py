"""A rolling-origin backtest through the SDK harness: every fold of an expanding-window
split plan (``mapi_spec.splitplan``) is an ``as_of`` split, fitted and evaluated through
the score path. The ARIMAX (with its drivers) must beat a random-walk benchmark on
average over the folds."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import xarray as xr
from mapi_plugin_statsmodels.datasets import SALES_SCHEMA, sales_dataset
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import Registry
from mapi_spec.schema import DataSchema
from mapi_spec.splitplan import ExpandingWindowPlan, plan_folds


def spec(model: str, settings: str, split: dict[str, object]) -> str:
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: backtest, project: tests}}
data:
  dataset: dataset://tests/bt@latest
  bind: {{dims: {{t: date}}, vars: {{y: sales}}}}
  split: {json.dumps(split)}
model: {{use: "{model}@^1", settings: {settings}, fit: {{mode: train_only, seed: 1}}}}
evaluate: {{metrics: [rmse, mae, coverage_90]}}
"""


def backtest(
    registry: Registry,
    model: str,
    settings: str,
    data: xr.Dataset,
    schema: DataSchema,
    plan: ExpandingWindowPlan,
) -> list[dict[str, float]]:
    labels = [pd.Timestamp(v).isoformat() for v in data["date"].values]
    out = []
    for fold in plan_folds(plan, data.sizes["date"]):
        assert fold.test_stop is not None
        split = fold.as_of_split("t", labels).model_dump(mode="json")  # type: ignore[arg-type]
        window = data.isel(date=slice(0, fold.test_stop))
        bundle = fit_spec(
            spec(model, settings, split), window, registry=registry, raw_schema=schema
        )
        out.append({k: float(v) for k, v in bundle.metrics.values.items() if v is not None})
    return out


def test_arimax_beats_a_random_walk_across_folds(registry: Registry) -> None:
    data = sales_dataset(periods=100, seed=21)
    plan = ExpandingWindowPlan(kind="expanding_window", dim="t", horizons=[1, 4], n_folds=4, gap=1)
    arimax = backtest(
        registry, "ts.sarimax", "{order: [1, 0, 0], exog: [price, promo]}", data, SALES_SCHEMA, plan
    )
    only_y = data.drop_vars(["price", "promo"])
    raw = SALES_SCHEMA.model_dump()
    raw["vars"] = {"sales": raw["vars"]["sales"]}
    naive = backtest(
        registry, "ts.arima", "{order: [0, 1, 0]}", only_y, DataSchema.model_validate(raw), plan
    )
    assert len(arimax) == len(naive) == 4
    for a in arimax:
        assert np.isfinite(a["rmse"]) and 0.0 <= a["coverage_90"] <= 1.0
    assert np.mean([a["rmse"] for a in arimax]) < np.mean([n["rmse"] for n in naive])
    mean_cov = float(np.mean([a["coverage_90"] for a in arimax]))
    assert mean_cov >= 0.6  # 90% intervals: most held-out weeks inside
