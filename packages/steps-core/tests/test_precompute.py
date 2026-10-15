"""Adstock and saturation formulas against brute-force references."""

from __future__ import annotations

import math
from typing import Any

import mapi_steps as ms
import numpy as np
import pytest
import xarray as xr
from mapi_sdk.steps import StepContext
from mapi_spec.errors import ErrorCode, MapiError
from mapi_steps.precompute import AdstockSettings, SaturationSettings, adstock_weights, saturate
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def run(step: Any, ds: xr.Dataset, settings: dict[str, Any]) -> xr.Dataset:
    inst = step()
    s = inst.parse_settings(settings)
    ctx = StepContext(step_id="t")
    return inst.transform(ds, s, inst.fit(ds, s, ctx), ctx)


def test_weights_match_the_formulas() -> None:
    np.testing.assert_allclose(adstock_weights("geometric", 4, alpha=0.5), [1, 0.5, 0.25, 0.125])
    np.testing.assert_allclose(
        adstock_weights("delayed", 4, alpha=0.5, theta=1.0), [0.5, 1.0, 0.5, 0.0625]
    )
    lam, k, l_max = 2.0, 1.5, 5
    pdf = [(k / lam) * (t / lam) ** (k - 1) * math.exp(-((t / lam) ** k)) for t in range(1, 6)]
    lo, hi = min(pdf), max(pdf)
    np.testing.assert_allclose(
        adstock_weights("weibull_pdf", l_max, lam=lam, k=k), [(p - lo) / (hi - lo) for p in pdf]
    )
    surv = [math.exp(-((t / lam) ** k)) for t in range(1, l_max)]
    cdf = [1.0, *np.cumprod(surv)]
    np.testing.assert_allclose(adstock_weights("weibull_cdf", l_max, lam=lam, k=k), cdf)
    w = adstock_weights("geometric", 3, alpha=np.array([0.1, 0.9]), normalize=True)
    assert w.shape == (2, 3)
    np.testing.assert_allclose(w.sum(axis=-1), 1.0)


def test_adstock_convolution_matches_brute_force(weekly: xr.Dataset) -> None:
    alpha = {"tv": 0.7, "search": 0.2, "social": 0.5}
    out = run(
        ms.AdstockStep,
        weekly,
        {"vars": ["spend"], "alpha": alpha, "by": "channel", "l_max": 4, "within": ["geo"]},
    )
    x = weekly["spend"].transpose("geo", "channel", "time").values
    want = np.zeros_like(x)
    for ci, ch in enumerate(weekly["channel"].values):
        w = [alpha[str(ch)] ** lag for lag in range(4)]
        for t in range(x.shape[-1]):
            want[:, ci, t] = sum(w[lag] * x[:, ci, t - lag] for lag in range(4) if t - lag >= 0)
    np.testing.assert_allclose(out["spend"].transpose("geo", "channel", "time").values, want)


def test_adstock_missing_values_and_masks(weekly: xr.Dataset) -> None:
    spend = weekly["spend"].copy()
    spend[:, :, :2] = np.nan  # leading rows: no history yet
    out = run(
        ms.AdstockStep, weekly.assign(spend=spend), {"vars": ["spend"], "alpha": 0.5, "l_max": 3}
    )
    assert bool(out["spend"].isel(time=slice(0, 2)).isnull().all())
    np.testing.assert_allclose(out["spend"].isel(time=2), spend.isel(time=2))
    masked = weekly.assign(ok=weekly["time"] != weekly["time"][5])
    out = run(ms.AdstockStep, masked, {"vars": ["spend"], "alpha": 0.5, "l_max": 2, "mask": "ok"})
    assert bool(out["spend"].isel(time=5).isnull().all())


def test_adstock_settings_validation() -> None:
    with pytest.raises(ValidationError, match="needs"):
        AdstockSettings.model_validate({"vars": ["x"], "kind": "delayed", "alpha": 0.5})
    with pytest.raises(ValidationError, match="must lie"):
        AdstockSettings.model_validate({"vars": ["x"], "alpha": 1.5})
    with pytest.raises(ValidationError, match="need 'by'"):
        AdstockSettings.model_validate({"vars": ["x"], "alpha": {"a": 0.5}})
    with pytest.raises(ValidationError, match="must lie"):
        AdstockSettings.model_validate({"vars": ["x"], "kind": "weibull_pdf", "lam": 0, "k": 1})


def test_saturation_curves(weekly: xr.Dataset) -> None:
    x = weekly["spend"]
    out = run(ms.SaturationStep, weekly, {"vars": ["spend"], "lam": 0.02})
    xr.testing.assert_allclose(out["spend"], (1 - np.exp(-0.02 * x)) / (1 + np.exp(-0.02 * x)))
    assert out["spend"].attrs["units"] == "1"
    out = run(
        ms.SaturationStep, weekly, {"vars": ["spend"], "kind": "hill", "slope": 2, "half": 50}
    )
    xr.testing.assert_allclose(out["spend"], x**2 / (50**2 + x**2))
    out = run(
        ms.SaturationStep,
        weekly,
        {"vars": ["spend"], "kind": "michaelis_menten", "alpha": 3, "lam": 40},
    )
    xr.testing.assert_allclose(out["spend"], 3 * x / (40 + x))
    out = run(ms.SaturationStep, weekly, {"vars": ["spend"], "kind": "tanh", "alpha": 2, "c": 30})
    xr.testing.assert_allclose(out["spend"], 2 * np.tanh(x / 60))
    np.testing.assert_allclose(saturate(np.array([0.0, 1e9]), "logistic", lam=1.0), [0.0, 1.0])


def test_saturation_errors(weekly: xr.Dataset) -> None:
    with pytest.raises(MapiError, match="non-negative"):
        run(ms.SaturationStep, weekly.assign(spend=-weekly["spend"]), {"vars": ["spend"], "lam": 1})
    with pytest.raises(MapiError) as err:
        run(ms.SaturationStep, weekly, {"vars": ["spend"], "lam": {"tv": 1.0}, "by": "channel"})
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    with pytest.raises(MapiError, match="labelled dim"):
        run(ms.SaturationStep, weekly, {"vars": ["spend"], "lam": {"tv": 1.0}, "by": "nope"})
    with pytest.raises(ValidationError, match="needs"):
        SaturationSettings.model_validate({"vars": ["x"], "kind": "hill", "slope": 1})
    with pytest.raises(ValidationError, match="need 'by'"):
        SaturationSettings.model_validate({"vars": ["x"], "lam": {"a": 1.0}})
