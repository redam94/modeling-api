"""DiD checks: the event study (reference period 0, flat leads), the ATT and the counterfactual."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle

TINY_FIT = {"tune": 100, "draws": 100, "chains": 2}
TRUE_EFFECT = 5.0
"""The effect of :func:`mapi_plugin_causal.datasets_panel.did_dataset` (its default)."""


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    study = bundle.model.groups["event_study"]
    assert study["event_time"].values.tolist() == list(range(-4, 7))
    assert bool((study["coef"].sel(event_time=-1) == 0).all()), "the reference period is 0"
    assert study.attrs["reference"] == -1 and study.attrs["design"] == "event_study"
    lo, hi = np.quantile(study["att"].values, [0.02, 0.98])
    assert lo <= TRUE_EFFECT <= hi, f"ATT interval [{lo:.2f}, {hi:.2f}] misses the true effect"
    imp = bundle.model.groups["impact"]
    treated = int((data["treatment"].max("month") == 1).sum())
    assert imp.sizes["unit"] == treated
    np.testing.assert_array_equal(
        imp["post"].values, data["treatment"].sel(store=imp["unit"].values).values == 1
    )
    assert not any(d.code == "STAGGERED_ADOPTION" for d in bundle.diagnostics)


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    if name != "no_treatment_counterfactual":
        return
    delta = tree["delta/summary"].to_dataset()["y_hat"].sel(stat="mean")
    treated = data["treatment"] == 1
    # delta = scenario (no treatment) - baseline: minus the effect on treated cells; the lead
    # dummies move the pre-adoption cells of treated stores, never those of untreated ones
    assert abs(float(delta.where(treated).mean()) + TRUE_EFFECT) < 1.5
    never = data["treatment"].max("month") == 0
    np.testing.assert_allclose(delta.sel(store=never).values, 0.0, atol=1e-6)
