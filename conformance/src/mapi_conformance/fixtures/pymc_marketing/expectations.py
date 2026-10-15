"""pymc-marketing adapter expectations: owned units and carry-over, additive
contributions, counterfactual locality and plots."""

from __future__ import annotations

import json
from collections.abc import Mapping

import numpy as np
import xarray as xr
from mapi_plots.recipes import render_plots
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

from mapi_conformance.fixtures.pymc_marketing.data import HORIZON_WEEKS

__all__ = ["check_bundle", "check_score"]


def _group(tree: xr.DataTree, name: str) -> xr.Dataset:
    node = tree[name]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


def check_bundle(bundle: Bundle, registry: Registry) -> None:
    caps = bundle.model.manifest.capabilities
    assert set(caps.owns) == {"scaling", "carryover", "inverse_units"}
    assert bundle.metrics.output_space == "model-owned"
    assert "fit_data" in bundle.model.groups
    report = render_plots(bundle.to_tree(), ["core.convergence"], registry=registry)
    assert [p.id for p in report.plots] == ["core.convergence"], report.issues


def check_score(
    name: str,
    tree: xr.DataTree,
    *,
    bundle: Bundle,
    registry: Registry,
    results: Mapping[str, xr.DataTree],
) -> None:
    summary = _group(tree, "summary")
    space = json.loads(tree.attrs["output_space"])
    assert set(space.values()) == {"model-owned"}
    mean = summary["y_hat"].sel(stat="mean")
    contrib = summary["contributions"].sel(stat="mean")
    # additive: expected contributions sum to the expected target (means are linear)
    np.testing.assert_allclose(contrib.sum("component").transpose(*mean.dims), mean, rtol=1e-6)
    if name == "forecast_4w":
        assert mean.sizes["date"] == HORIZON_WEEKS
        assert bool(np.isfinite(mean).all()) and float(mean.min()) > 0
        fan = render_plots(tree, ["core.ppc_fan"], registry=registry)
        assert [p.id for p in fan.plots] == ["core.ppc_fan"], fan.issues
    if name == "full_series" and "recent_window" in results:
        recent = _group(results["recent_window"], "summary")["y_hat"].sel(stat="mean")
        # the adapter prepends the fit history, so a window scores like the full series
        np.testing.assert_allclose(
            recent, mean.sel(date=recent["date"]).transpose(*recent.dims), rtol=1e-8
        )
    if name == "counterfactual_search":
        delta = _group(tree, "delta/summary")["contributions"].sel(stat="mean")
        assert float(delta.sel(component="search").sum()) > 0
        rest = delta.sel(component=[c for c in delta["component"].values if c != "search"])
        assert float(abs(rest).max()) < 1e-6
        report = render_plots(tree, ["core.counterfactual"], registry=registry)
        assert [p.id for p in report.plots] == ["core.counterfactual"], report.issues
