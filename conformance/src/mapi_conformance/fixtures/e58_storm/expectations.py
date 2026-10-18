"""E58 checks: PSF width and positions against the simulated blinks, in-sample camera fit."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import xarray as xr
from mapi_plugin_science.storm import StormLocalisationModel
from mapi_plugin_science.storm_data import STORM_TRUTH
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

from mapi_conformance.fixtures.e58_storm.data import truth

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}
TINY_SETTINGS = {"max_spots": 16, "score_draws": 20, "ppc_draws": 10}


def registry(base: Registry) -> Registry:
    """``sci.storm_localisation`` until the science plugin pack registers it."""
    reg = base.copy()
    reg.register_model(StormLocalisationModel, replace=True)
    return reg


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    post = bundle.model.posterior
    px = STORM_TRUTH["pixel_nm"]
    psf = float(np.median(post["psf_sd"].mean(("chain", "draw")).values)) / px
    assert abs(psf / STORM_TRUTH["psf_sd_px"] - 1) < 0.1, f"median PSF sd {psf:.3f} px"
    assert "log_likelihood" in bundle.model.groups
    elpd = bundle.metrics.values.get("elpd_loo")
    assert elpd is not None and np.isfinite(elpd)


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset, ctx: Any) -> None:
    summary = tree["summary"].to_dataset()
    assert summary["x"].attrs["units"] == "nm"
    if name != "predict_spots":
        return
    blinks = truth(ctx.seed, ctx.size)
    px = STORM_TRUTH["pixel_nm"]
    frames = bundle.model.groups["constant_data"]["spot_frame"].values
    errors = []
    for k, f in enumerate(frames.tolist()):
        cand = np.flatnonzero(blinks["frame"].values == f)
        mx = float(summary["x"].sel(stat="mean", spot=k)) / px
        my = float(summary["y"].sel(stat="mean", spot=k)) / px
        d = np.hypot(blinks["x"].values[cand] - mx, blinks["y"].values[cand] - my)
        if cand.size:
            errors.append(float(d.min()))
    errors_nm = np.asarray(errors) * px
    share = float(np.mean(errors_nm < 50))
    assert share >= 0.85, f"spots within 50 nm of a true blink: {share:.2f}"
    assert np.median(errors_nm) < 25, f"median position error {np.median(errors_nm):.1f} nm"


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    # background pixels dominate: about scale x sqrt(55 counts) = 100 ADU of noise
    assert 60 < values["mae"] < 200, f"in-sample MAE {values['mae']:.1f} ADU"
