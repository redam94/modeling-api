"""E19 checks: the true edges get a high probability, the network is kept for the plots, the
log-likelihood is one value per cell, and held-out cells are covered."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_plugin_science.ggm import PAIR_DIM, REFERENCE, VAR_DIM, GraphicalModel
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}
TINY_SETTINGS: dict[str, object] = {}


def registry(base: Registry) -> Registry:
    """Register the plugin until ``mapi_plugin_science.PLUGINS`` lists it."""
    base.register_model(GraphicalModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    groups = bundle.model.groups
    truth = data["consensus"].values
    np.testing.assert_array_equal(groups["constant_data"][REFERENCE].values, truth)
    ll = groups["log_likelihood"]["value"]
    assert ll.dims == ("chain", "draw", "cell"), f"log-likelihood dims {ll.dims}"
    assert bundle.evaluation is not None, "evaluate_then_refit keeps the evaluation bundle"
    mean = groups["posterior"]["edge_prob"].mean(("chain", "draw"))
    prob = mean.transpose(VAR_DIM, PAIR_DIM).values
    iu = np.triu_indices(truth.shape[0], 1)
    true_edges = truth[iu] == 1
    assert prob[iu][true_edges].min() > 0.8, "a true edge has a low probability"
    assert np.median(prob[iu][~true_edges]) < 0.3, "the absent edges are not rejected"


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    if name == "predict_graph":
        summary = tree["summary"].to_dataset()
        assert summary["level"].attrs["units"] == "log_intensity"
        mean = summary["pcor"].sel(stat="mean").values
        assert np.allclose(np.diag(mean), 1.0) and np.allclose(mean, mean.T, atol=1e-8)
        level = summary["level"].sel(stat="mean")
        np.testing.assert_allclose(
            level.isel(cell=0).values, data["level"].mean("cell").values, atol=0.3
        )
    if name == "simulate_cells":
        sim = tree["draws"].to_dataset()["level"]
        spread = sim.std([d for d in sim.dims if d != "protein"])
        assert float(spread.min()) > 0.1, "simulated cells must vary"


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    assert values["coverage_90"] >= 0.8, f"holdout coverage {values['coverage_90']:.2f}"
