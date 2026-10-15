"""E24 checks: terminal state, relabelled draws and the updated state after filtering."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    term = bundle.model.terminal_state
    assert term is not None, "the HMM must write a terminal_state group"
    assert set(term["alpha_T"].dims) == {"chain", "draw", "animal", "state"}
    np.testing.assert_allclose(term["alpha_T"].sum("state"), 1.0)
    assert int(term.attrs["step"]) == int(data["step"].values.max())
    mu = bundle.model.posterior["mu"].transpose("chain", "draw", "state").values
    assert (np.diff(mu, axis=-1) > 0).all(), "draws are not relabelled by ordered means"
    caps = bundle.model.manifest.capabilities
    assert not caps.loo_valid and caps.loo_invalid_reason


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    summary = tree["summary"].to_dataset()
    probs = summary["state_prob"].sel(stat="mean")
    np.testing.assert_allclose(probs.sum("state"), 1.0, atol=1e-9)
    if name == "filter_live_telemetry":
        term = tree["terminal_state"].to_dataset()
        assert int(term.attrs["step"]) == int(data["step"].values.max()) + 6
        assert int(term.attrs["previous_step"]) == int(data["step"].values.max())
        np.testing.assert_allclose(term["alpha_T"].sum("state"), 1.0)
