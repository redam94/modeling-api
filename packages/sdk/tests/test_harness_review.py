"""Regression tests for the M0 review findings on the fit/score harness (spec-sdk area)."""

# ruff: noqa: E501

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_spec import load_model_spec, load_score_request

REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/toy\n"


def req(extra: str = "mode: predict\n") -> Any:
    return load_score_request(REQ + extra)


LOG_SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: toy-log, project: growth}
data:
  dataset: dataset://growth/sales@latest
  bind: {dims: {geo: market, time: date}, vars: {y: revenue_local}}
  split: {kind: time_holdout, dim: time, last: 3}
pipeline:
  - {id: weekly, use: toy.resample@^1, with: {dim: time, freq: W-SUN}}
  - {id: logy, use: toy.log1p@^1, with: {vars: [y]}}
  - {id: season, use: toy.fourier@^1, with: {along: time, period: 52.18, order: 2}}
model:
  use: toy.linear@^1
  settings: {features: [fourier], retransform: __RT__}
  fit: {mode: evaluate_then_refit, seed: 1}
evaluate: {metrics: [rmse, mae]}
"""


# ------------------------------------------------------------------- statistics-1 (smearing)


def test_smearing_residuals_come_from_the_target_shaped_output(
    registry: Registry, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    """``contributions`` (extra ``component`` dim) must not overwrite the y_hat residuals."""
    data = panel[0][["revenue_local"]]
    smear = fit_spec(load_model_spec(LOG_SPEC.replace("__RT__", "smearing")), data, None, registry)
    r = smear.residuals["y"].values
    # in-sample least-squares residuals of y_hat have (numerically) zero mean
    assert abs(float(r.mean())) < 1e-6
    median = fit_spec(load_model_spec(LOG_SPEC.replace("__RT__", "median")), data, None, registry)
    horizon = req("mode: forecast\nhorizon: {dim: time, periods: 3}\noutput: {names: canonical}\n")
    ys = score_bundle(smear, horizon, None, registry=registry)["summary"].to_dataset()["y_hat"]
    ym = score_bundle(median, horizon, None, registry=registry)["summary"].to_dataset()["y_hat"]
    # smearing corrects the retransformation bias by a small factor, never by an order of magnitude
    ratio = (ys / ym).values
    assert bool((ratio >= 0.999).all()) and bool((ratio < 1.5).all())
    assert json.loads(
        score_bundle(smear, horizon, None, registry=registry).attrs["retransform"]
    )["y_hat"] == "smearing"


def _unused() -> None:  # keep imports stable as later sections are added
    _ = (np, pytest)
