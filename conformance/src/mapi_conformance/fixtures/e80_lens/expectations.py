"""E80 checks: the Einstein radius of the mock, cached critical curves, simulated noise."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_plugin_science.lens import CRITICAL_CURVES, StrongLensModel
from mapi_plugin_science.lens_data import LENS_TRUTH
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}


def registry(base: Registry) -> Registry:
    """``sci.strong_lens`` until the science plugin pack registers it."""
    reg = base.copy()
    reg.register_model(StrongLensModel, replace=True)
    return reg


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    te = bundle.model.posterior["theta_E"].values
    assert abs(te.mean() - LENS_TRUTH["theta_E"]) < 0.03, f"theta_E {te.mean():.3f}"
    cc = bundle.model.derived[CRITICAL_CURVES]
    assert {"sample", "curve_point"} <= set(cc.dims)
    caps = bundle.model.manifest.capabilities
    assert not caps.loo_valid and caps.loo_invalid_reason


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    summary = tree["summary"].to_dataset()
    if name == "predict_image_and_source":
        mean = summary["model_image"].sel(stat="mean")
        resid = (data["image"] - mean) * np.sqrt(data["weight"])
        assert float(resid.std()) < 1.5
    if name == "critical_curves":
        cc = tree["derived/critical_curves"].to_dataset()
        assert "critical_x" in cc and "caustic_x" in cc


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    noise = float(np.sqrt((1.0 / data["weight"]).mean()))
    assert values["rmse"] < 1.3 * noise, f"in-sample RMSE {values['rmse']:.4f}, noise {noise:.4f}"
    assert 0.75 <= values["coverage_90"] <= 0.99, f"90% coverage {values['coverage_90']:.2f}"
