"""Shared fixtures for the SDK tests: toy registry, panel data and an E33-like spec."""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_sdk.binding import BindingMap
from mapi_sdk.registry import Registry
from mapi_sdk.schema import infer_schema
from mapi_sdk.testing import toy
from mapi_spec import DataSchema, ModelSpec, load_model_spec

E33_TOY = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: toy-geo, project: growth}
data:
  dataset: dataset://growth/eu_media_daily@latest
  side_tables:
    fx: {ref: dataset://growth/fx_daily@latest, score_policy: pinned}
  bind:
    dims: {geo: market, channel: channel, time: date}
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 4, after_step: weekly}
pipeline:
  - {id: to_eur, use: toy.fx@^1, with: {vars: [y, spend], table: fx, table_along: date}}
  - {id: weekly, use: toy.resample@^1, with: {dim: time, freq: W-SUN}}
  - {id: scale_spend, use: toy.scale@^1, with: {vars: [spend], over: [time], unseen_levels: pooled}}
  - {id: scale_y, use: toy.scale@^1, with: {vars: [y], over: [time], unseen_levels: pooled}}
  - {id: lags, use: toy.lag@^1, with: {vars: [spend], along: time, within: [geo], lags: [1, 2], fill: 0.0}}
  - {id: season, use: toy.fourier@^1, with: {along: time, period: 52.18, order: 2}}
model:
  use: __MODEL__
  fit: {mode: __MODE__, seed: 7, draws: 60, chains: 2}
evaluate: {metrics: [__METRICS__]}
"""

BIND = {
    "dims": {"geo": "market", "time": "date"},
    "vars": {"y": "revenue_local", "spend": "spend_local"},
}


def _e33_spec(
    model: str = "toy.bayes_linear@^1",
    mode: str = "evaluate_then_refit",
    metrics: str = "rmse, mape, coverage_90, elpd_loo",
) -> ModelSpec:
    text = E33_TOY.replace("__MODEL__", model).replace("__MODE__", mode)
    return load_model_spec(text.replace("__METRICS__", metrics))


@pytest.fixture
def registry() -> Registry:
    return toy.toy_registry()


@pytest.fixture
def panel() -> tuple[xr.Dataset, xr.Dataset]:
    return toy.panel_dataset(days=7 * 24)


@pytest.fixture
def canonical_panel(panel: tuple[xr.Dataset, xr.Dataset]) -> xr.Dataset:
    return BindingMap(BIND).apply(panel[0])


def _weekly_panel(
    weeks: int = 20, groups: tuple[str, ...] = ("a", "b", "c"), seed: int = 0
) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    time = pd.date_range("2024-01-07", periods=weeks, freq="W-SUN").as_unit("ns")
    return xr.Dataset(
        {
            "y": (("geo", "time"), rng.uniform(1, 2, (len(groups), weeks))),
            "x": (("geo", "time"), rng.uniform(0, 1, (len(groups), weeks))),
        },
        coords={"geo": list(groups), "time": time},
    )


@pytest.fixture
def e33_spec() -> Callable[..., ModelSpec]:
    """Factory: ``e33_spec(model=..., mode=..., metrics=...)`` -> toy E33-like ModelSpec."""
    return _e33_spec


@pytest.fixture
def weekly_panel() -> Callable[..., xr.Dataset]:
    """Factory: a weekly (geo, time) panel with ``y`` and ``x``."""
    return _weekly_panel


@pytest.fixture
def raw_schema(panel: tuple[xr.Dataset, xr.Dataset]) -> DataSchema:
    """Raw schema of the toy panel: markets allow new levels (pooled), days extend."""
    schema = infer_schema(panel[0], targets=["revenue_local"], policies={"market": "new_levels"})
    market = schema.dims["market"].model_copy(update={"new_level_strategy": "pooled"})
    return schema.model_copy(update={"dims": {**schema.dims, "market": market}})
