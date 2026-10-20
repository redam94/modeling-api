"""causal_impact checks: the ``impact`` group and terminal state exist, the simulated lift
gives a positive total effect, and the effect over the data is positive after the start."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle

from mapi_conformance.fixtures.causal_impact.data import START_WEEK

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    groups = bundle.model.groups
    assert "impact" in groups, "causal.impact must write an impact group"
    impact = groups["impact"]
    for var in ("actual", "counterfactual", "effect", "cumulative_effect", "post"):
        assert var in impact, var
    assert impact["counterfactual"].dims == ("sample", "time")
    post = impact["post"].values
    assert post.sum() == data.sizes["week"] - START_WEEK
    assert impact.attrs["intervention_start"] == "2024-01-01"
    total = impact["effect"].where(impact["post"]).sum("time")
    assert float(total.mean()) > 0, "the simulated lift must give a positive total effect"
    term = bundle.model.terminal_state
    assert term is not None and {"one_step", "components"} <= set(groups)
    assert term.attrs["last_label"] == "2023-12-25"
    caps = bundle.model.manifest.capabilities
    assert not caps.loo_valid and caps.loo_invalid_reason


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    summary = tree["summary"].to_dataset()
    if name == "predict_over_data":
        effect = summary["effect"].sel(stat="mean").values
        assert np.nanmean(effect[START_WEEK:]) > 0
        assert abs(np.nanmean(effect[:START_WEEK])) < np.nanmean(effect[START_WEEK:])
    if name == "forecast_4":
        lo, hi = summary["y_hat"].sel(stat="q5"), summary["y_hat"].sel(stat="q95")
        assert bool((lo <= hi).all())
        assert summary["week"].values.min() > data["week"].values.max()
