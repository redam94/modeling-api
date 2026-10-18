"""E21 checks: dyads in the likelihood, aligned positions, recovered homophily effects,
symmetric simulated graphs and a holdout that beats the base rate."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_plugin_science.network import (
    DYAD_DIM,
    GRAPH_STATS,
    POSITION_REF,
    NetworkLatentSpaceModel,
    align_to,
)
from mapi_plugin_science.network_data import NETWORK_TRUTH
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}
MODEL_CHECKS = (
    "manifest",
    "about",
    "fit",
    "bundle_roundtrip_no_pickle",
    "shuffled_scoring",
    "new_level_policies",
    "unsupported_mode",
    "output_space_and_units",
    "outputs_match_manifest",
    "representation_aggregation",
    "seed_determinism",
    "plots_sanitize",
)
"""Every check but ``subset_scoring``, which keeps one level of the ``covariate`` dim: the
model refuses a pair with fewer covariates than the fit (``MISSING_FUTURE_COVARIATE``)."""


def registry(base: Registry) -> Registry:
    """Register the plugin (``mapi_plugin_science.PLUGINS`` lists it too)."""
    base.register_model(NetworkLatentSpaceModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    groups = bundle.model.groups
    n = data.sizes["lawyer"]
    obs = groups["observed_data"]["tie"]
    assert obs.dims == (DYAD_DIM,) and obs.size == n * (n - 1) // 2
    ll = groups["log_likelihood"]["tie"]
    assert ll.sizes[DYAD_DIM] == obs.size, "one log-likelihood value per dyad"
    post = groups["posterior"]
    z = (post["z_raw"] * post["sigma_z"]).transpose("chain", "draw", "node", "z_axis").values
    s = z.shape[0] * z.shape[1]
    again = align_to(z.reshape(s, n, -1), groups["constant_data"][POSITION_REF].values)
    np.testing.assert_allclose(again, post["position"].values.reshape(s, n, -1), atol=1e-8)
    for name, true in NETWORK_TRUTH["beta"].items():
        draws = post["beta"].sel(cov=name).values.ravel()
        lo, hi = np.quantile(draws, [0.005, 0.995])
        assert lo < true < hi, f"beta[{name}] interval ({lo:.2f}, {hi:.2f}) misses {true}"
    assert bundle.metrics.values.get("elpd_loo") is not None


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    summary = tree["summary"].to_dataset()
    if name == "predict_ties":
        p = summary["tie_prob"].sel(stat="mean").values
        np.testing.assert_allclose(p, p.T, atol=1e-12)
        assert np.diag(p).max() == 0.0 and p.min() >= 0.0 and p.max() <= 1.0
    if name == "simulate_graphs":
        a = data["cowork"].values
        observed = float(a[np.triu_indices(a.shape[0], 1)].mean())
        density = float(summary["graph_stat"].sel(stat="mean", statistic="density"))
        assert list(summary["statistic"].values) == list(GRAPH_STATS)
        assert abs(density - observed) < 0.05, f"simulated density {density:.3f}"


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    a = data["cowork"].values
    base = float(a[np.triu_indices(a.shape[0], 1)].mean())
    # a model that knows nothing predicts the density: RMSE sqrt(p (1 - p))
    assert values["rmse"] < np.sqrt(base * (1 - base)), f"holdout RMSE {values['rmse']:.3f}"
