"""E18 checks: the island is linked, the map goes into the bundle, the covariate effect is
recovered, held-out districts are predicted from their neighbours."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_plugin_science.areal import OBS_DIM, ArealBYM2Model
from mapi_plugin_science.areal_data import AREAL_TRUTH
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

TINY_FIT = {"tune": 300, "draws": 150, "chains": 2}


def registry(base: Registry) -> Registry:
    """Register the plugin directly (the science pack lists it in ``PLUGINS``)."""
    base.register_model(ArealBYM2Model, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    learned = bundle.model.learned
    assert learned["n_components_input"] == 2, "the island is a separate piece of the input"
    assert learned["n_components"] == 1 and len(learned["links"]) == 1  # type: ignore[arg-type]
    const = bundle.model.groups["constant_data"]
    for name in ("ring_x", "ring_y", "adjacency", "centroid", "smr"):
        assert name in const, f"constant_data/{name} feeds the map and graph plots"
    assert const.sizes["area"] == data.sizes["district"]
    assert "log_likelihood" in bundle.model.groups, "elpd_loo needs the pointwise values"
    b1 = bundle.model.posterior["b1"].values
    lo, hi = np.quantile(b1, [0.01, 0.99])
    assert lo < AREAL_TRUTH["b1"] < hi, f"b1 interval [{lo:.2f}, {hi:.2f}]"
    ev = bundle.evaluation
    assert ev is not None, "evaluate_then_refit keeps the evaluation bundle"
    obs = ev.model.groups["observed_data"]["cases"]
    assert obs.dims == (OBS_DIM,) and obs.sizes[OBS_DIM] < data.sizes["district"]


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    summary = tree["summary"].to_dataset()
    if name == "predict_map":
        p = summary["p_exceed"].sel(stat="mean")
        assert bool(((p >= 0) & (p <= 1)).all())
        rr = summary["rr"].sel(stat="mean")
        assert bool((rr > 0).all()) and summary["cases"].attrs["units"] == "cases"
    if name == "predict_two_areas":
        assert list(summary["district"].values) == list(data["district"].values[[3, 0]])


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    assert values["coverage_90"] >= 0.5, f"holdout coverage {values['coverage_90']:.2f}"
