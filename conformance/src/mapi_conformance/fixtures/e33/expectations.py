"""E33 expectations beyond "fits and scores": units, derived quantities, plots and the
semantics of each score request (horizon, new market, weekly EUR plan, counterfactual)."""

from __future__ import annotations

import json
from collections.abc import Mapping

import numpy as np
import xarray as xr
from mapi_plots.recipes import render_plots
from mapi_plugin_mmm.examples import E33Truth
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry

from mapi_conformance.fixtures.e33.data import HORIZON_WEEKS, NEW_MARKET

__all__ = ["check_bundle", "check_score"]

CHANNEL_CONTRIB_RTOL = 0.5  # tiny fits; the plugin's recovery test is much tighter


def _group(tree: xr.DataTree, name: str) -> xr.Dataset:
    node = tree[name]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


def check_bundle(bundle: Bundle, registry: Registry, truth: E33Truth) -> None:
    """Production bundle: evaluation lineage, derived quantities in EUR, plots."""
    assert bundle.evaluation is not None, "evaluate_then_refit keeps the evaluation bundle"
    assert bundle.metrics.output_space == "weekly"
    assert {"coverage_90", "mape", "elpd_loo"} <= set(bundle.metrics.values)
    derived = bundle.model.derived
    assert set(derived) == {"contributions", "response_curves", "marginal_cost"}
    contrib = derived["contributions"]["channel_contribution"]
    assert contrib.attrs["units"] == "EUR" and contrib.attrs["output_space"] == "weekly"
    # the fitted EUR contributions are in the right range of the simulated ones
    weeks = contrib.sizes["time"]
    want = truth.weekly["contribution_eur"].isel(week=slice(0, weeks)).sum(["market", "week"])
    got = contrib.sum(["geo", "time"]).mean("sample")
    channels = [str(c) for c in got["channel"].values]
    np.testing.assert_allclose(
        got.sel(channel=channels).values,
        want.sel(channel=channels).values,
        rtol=CHANNEL_CONTRIB_RTOL,
    )
    rc = derived["response_curves"]
    assert rc["contribution"].dims == ("sample", "channel", "multiplier", "geo")
    assert bool((rc["contribution"].diff("multiplier") >= -1e-9).all())
    tree = bundle.to_tree()
    plots = [str(p) for p in bundle.spec.outputs.plots if str(p) != "core.ppc_fan"]
    report = render_plots(tree, plots, registry=registry)
    assert sorted(p.id for p in report.plots) == sorted(plots), report.issues


def check_score(
    name: str,
    tree: xr.DataTree,
    *,
    bundle: Bundle,
    registry: Registry,
    results: Mapping[str, xr.DataTree],
) -> None:
    """Per-request expectations; ``results`` holds the requests scored so far."""
    summary = _group(tree, "summary")["y_hat"]
    attrs = tree.attrs
    issues = json.loads(attrs["issues"])
    if name in ("forecast_8w", "new_market", "weekly_eur_plan"):
        assert summary.sizes["date"] == HORIZON_WEEKS
        assert set(summary["stat"].values) == {"mean", "hdi_90_lower", "hdi_90_upper"}
        assert bool(np.isfinite(summary).all())
        assert summary.attrs["units"] == "EUR"
        assert json.loads(attrs["output_space"])["y_hat"] == "weekly"
        assert _group(tree, "draws")["y_hat"].sizes["sample"] == 50
        fan = render_plots(tree, ["core.ppc_fan"], registry=registry)
        assert [p.id for p in fan.plots] == ["core.ppc_fan"], fan.issues
    if name == "new_market":
        assert NEW_MARKET in summary["market"].values
        assert any(i["code"] == "UNSEEN_LEVELS" for i in issues)
        draws = _group(tree, "draws")["y_hat"]
        rel = draws.std("sample") / draws.mean("sample")
        fitted = [m for m in summary["market"].values if m != NEW_MARKET]
        assert float(rel.sel(market=NEW_MARKET).mean()) > float(rel.sel(market=fitted).mean())
    if name == "weekly_eur_plan" and "forecast_8w" in results:
        # the planner's weekly EUR budget is the raw path's spend after fx + resampling
        raw = _group(results["forecast_8w"], "summary")["y_hat"].sel(stat="mean")
        plan = summary.sel(stat="mean")
        np.testing.assert_allclose(plan.transpose(*raw.dims), raw, rtol=1e-6)
    if name == "counterfactual_tv":
        delta = _group(tree, "delta/summary")
        assert float(delta["y_hat"].sel(stat="mean").sum()) > 0
        comp = delta["contributions"].sel(stat="mean")
        assert float(comp.sel(component="tv").sum()) > 0
        others = comp.sel(component=[c for c in comp["component"].values if c != "tv"])
        assert float(abs(others).max()) < 1e-6
        report = render_plots(tree, ["core.counterfactual"], registry=registry)
        assert [p.id for p in report.plots] == ["core.counterfactual"], report.issues
