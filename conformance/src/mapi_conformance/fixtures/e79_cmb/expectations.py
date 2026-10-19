"""E79 checks: the true spectrum inside the bandpower ranges, sky draws against the simulated
truth, the pixel noise left by the in-sample fit."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import xarray as xr
from mapi_plugin_science.cmb import NOISE, SKY_MOMENTS, TARGET, CMBFieldModel
from mapi_plugin_science.cmb_data import TINY_EDGES
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

from mapi_conformance.fixtures.e79_cmb.data import truth

TINY_FIT = {"tune": 10, "draws": 60, "chains": 2}
TINY_SETTINGS = {"border": 4, "field_draws": 8, "bin_edges": list(TINY_EDGES)}


def registry(base: Registry) -> Registry:
    """``sci.cmb_field`` until the science plugin pack registers it."""
    base.register_model(CMBFieldModel, replace=True)
    return base


def check_fit(bundle: Bundle, data: xr.Dataset) -> None:
    post = bundle.model.posterior
    assert bundle.model.learned["engine"] == "gibbs"
    true_q = truth(int(data.attrs["seed"]), "tiny" if data.sizes["row"] == 32 else "full")["q"]
    q = post["q"].values.reshape(-1, post.sizes["bin"])
    lo, hi = np.quantile(q, [0.005, 0.995], axis=0)
    inside = float(((true_q.values > lo) & (true_q.values < hi)).mean())
    assert inside >= 0.7, f"true q inside the 99% ranges for {inside:.0%} of the bins"
    caps = bundle.model.manifest.capabilities
    assert not caps.loo_valid and caps.loo_invalid_reason
    assert SKY_MOMENTS in bundle.model.derived


def check_score(name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset, ctx: Any) -> None:
    summary = tree["summary"].to_dataset()
    assert summary["sky"].attrs["units"] == "uK"
    if name != "predict_sky":
        return
    true = truth(ctx.seed, ctx.size)["sky"]
    obs = data[TARGET].notnull()
    mean = summary["sky"].sel(stat="mean")
    rmse = float(np.sqrt(((mean - true) ** 2).where(obs).mean()))
    assert rmse < 2 * float(data[NOISE].mean()), f"sky rmse on the data {rmse:.2f} uK"
    hole = ~obs
    sd = summary["sky"].sel(stat="sd")
    assert float(sd.where(hole).median()) > 3 * float(sd.where(obs).median())


def check_evaluate(tree: xr.DataTree, bundle: Bundle, data: xr.Dataset) -> None:
    values = json.loads(tree.attrs["metrics"])["values"]
    rmse = values["rmse"]
    assert rmse < 1.5 * float(data[NOISE].mean()), f"in-sample RMSE {rmse:.2f} uK"
