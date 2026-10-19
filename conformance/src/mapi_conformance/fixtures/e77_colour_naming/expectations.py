"""E77 checks: language shares on the palette, a new speaker is less certain than a fitted
one, sampled names are one-hot, and held-out speakers are named better than a guess."""

from __future__ import annotations

import json

import numpy as np
import xarray as xr
from mapi_plugin_science.colour_naming import ColourNamingModel
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

TINY_FIT = {"tune": 150, "draws": 100, "chains": 2}
TINY_SETTINGS = {"new_speakers": 5, "score_draws": 50}


def registry(base: Registry) -> Registry:
    """``sci.colour_naming`` until the science plugin pack registers it."""
    base.register_model(ColourNamingModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    post = bundle.model.posterior
    total = post["p_term"].sum("term", skipna=False).values
    filled = np.isfinite(total)
    assert filled.sum() == data.sizes["chip"] * post.sizes["chain"] * post.sizes["draw"]
    assert np.allclose(total[filled], 1.0, atol=1e-4), "shares over terms must sum to 1"
    k_spk, k_lang = (float(post[k].mean()) for k in ("k_eff_speaker", "k_eff_language"))
    assert 1.0 < k_lang <= k_spk + 0.05, f"effective categories {k_spk:.2f} / {k_lang:.2f}"
    ll = bundle.model.groups["log_likelihood"]["naming"]
    assert ll.dims == ("chain", "draw", "chip"), "one pointwise value per chip"
    assert bundle.model.learned["terms"] == [str(t) for t in post["term"].values]


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    if name == "predict_new_speaker":
        p = tree["summary"].to_dataset()["p_name"]
        assert p.attrs["units"] == "probability"
        assert np.allclose(p.sel(stat="mean").sum("term").values, 1.0, atol=1e-6)
        # the fitted speaker's own effects are known; the visitor's are drawn fresh
        width = (p.sel(stat="q95") - p.sel(stat="q5")).mean(("chip", "term"))
        first = str(data["speaker"].values[0])
        assert float(width.sel(speaker="visitor")) > float(width.sel(speaker=first))
    if name == "predictive_names":
        mean = tree["summary"].to_dataset()["p_name"].sel(stat="mean")
        assert np.allclose(mean.sum("term").values, 1.0, atol=1e-6)


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    assert values["log_score"] > -1.0, f"held-out log score {values['log_score']:.3f}"
    assert values["accuracy_lift"] > 0.1, f"accuracy lift {values['accuracy_lift']:.2f}"
