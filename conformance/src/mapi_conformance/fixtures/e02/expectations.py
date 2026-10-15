"""E02 checks: LOO, recovered effects, shuffle invariance and hyperprior spread."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_tabular.datasets import RADON_TRUTH
from mapi_sdk.bundle import Bundle

TINY_FIT = {"tune": 200, "draws": 100, "chains": 2}

_IN_SAMPLE: dict[str, xr.Dataset] = {}


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    assert bundle.metrics.values.get("elpd_loo") is not None, "LOO is valid for this model"
    post = bundle.model.posterior
    floor = float(post["beta"].sel(covariate="floor").mean())
    assert abs(floor - RADON_TRUTH["floor"]) < 0.35, f"floor effect {floor:.2f}"
    assert post["a"].sizes["county"] == data.sizes["county"]


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    summary = tree["summary"].to_dataset()
    if name == "predict_in_sample":
        _IN_SAMPLE[bundle.bundle_id] = summary
    elif name == "shuffled_counties":
        base = _IN_SAMPLE.get(bundle.bundle_id)
        assert base is not None, "predict_in_sample must run first"
        xr.testing.assert_allclose(summary, base)
    elif name == "new_county_from_hyperprior":
        draws = tree["draws"].to_dataset()["y_hat"]
        sd = draws.std("sample").values
        # rows 2-5 belong to the new county: their spread includes the county sd
        assert sd[2:].mean() > sd[:2].mean(), "a new county should be more uncertain"
        assert np.isfinite(summary["y_hat"]).all()
    elif name == "counterfactual_first_floor":
        delta = tree["delta/summary"].to_dataset()["y_hat"].sel(stat="mean").values
        floor = np.array([0.0, 1.0, 0.0, 1.0, 0.0, 0.0])
        assert (delta[floor == 0] < 0).all() and np.allclose(delta[floor == 1], 0.0)
