"""Guided MMM expectations on mmm-framework: the beliefs are true to the simulation, the
fit translated them (no translate_priors run) into ROI priors and conditioned on the lift
test (an mmm-framework experiment likelihood), ROI and baseline share land near the truth
(what the Guided scorecard's plausibility checks read), and the report's forecast and
channel-off preset behave."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle

from mapi_conformance.fixtures.guided_mmm.data import HORIZON_WEEKS, SIZES, _simulate, truth
from mapi_conformance.runner import FixtureContext

__all__ = ["MODEL_CHECKS", "TINY_FIT", "check_fit", "check_score"]

TINY_FIT = {"engine": "pymc", "tune": 80, "draws": 50, "chains": 2}
MODEL_CHECKS = (
    "manifest",
    "fit",
    "bundle_roundtrip_no_pickle",
    "output_space_and_units",
    "additive_contributions",
)
"""The contract kit's checks to run (each refits; the calibrated fit is the slow part)."""


def _group(tree: xr.DataTree, name: str) -> xr.Dataset:
    node = tree[name]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


def check_fit(bundle: Bundle, data: xr.Dataset, *, ctx: FixtureContext) -> None:
    t = truth(ctx.seed, ctx.size)
    cal = bundle.spec.calibration
    assert cal is not None and cal.business_priors is not None
    beliefs = cal.business_priors
    roi = beliefs.channels["tv"].roi
    share = beliefs.channels["search"].contribution_share
    assert roi is not None and roi[0] <= t.roi["tv"] <= roi[1], t.roi
    assert share is not None and share[0] <= t.share["search"] <= share[1], t.share
    assert beliefs.baseline_share is not None
    lo, hi = beliefs.baseline_share.share
    assert lo <= t.baseline_share <= hi, t.baseline_share
    [test] = cal.lift_tests
    assert abs(test.kpi_delta - t.lift) <= 2 * test.sigma, t.lift
    # translated at fit time: no translate_priors run is recorded
    assert cal.prior_translation_run is None
    learned = bundle.model.learned["calibration"]
    assert isinstance(learned, dict)
    translated = learned["translated_priors"]
    assert isinstance(translated, dict)
    # mmm-framework's ROI parameterisation: a log-normal ROI prior per stated channel, and
    # the intercept for the baseline share
    assert set(translated["roi"]) == {"tv", "search"}  # type: ignore[arg-type]
    assert {"mu", "sigma"} <= set(translated["intercept"])  # type: ignore[arg-type]
    # the lift test entered as an mmm-framework experiment likelihood
    assert learned["lift_tests"] == {"used": ["tv-north"], "skipped": {}}
    roi_tv = float(np.median(bundle.model.posterior["roi_tv"].values))
    assert 0.5 < roi_tv < 8.0, roi_tv
    # ROI and baseline share from the response curves at the actual spend - what the
    # scorecard's plausibility checks measure
    rc = bundle.model.derived["response_curves"]
    at_one = rc["contribution"].sel(multiplier=1.0, method="nearest").sum("geo")
    roi_draws = at_one / rc["current_spend"]
    assert 1.0 < float(roi_draws.sel(channel="tv").mean("sample")) < 6.0
    assert float((roi_draws < 0).mean()) == 0.0  # positive effects: no negative ROI
    base = rc["baseline"]
    base_share = (base / (base + at_one.sum("channel"))).mean("sample")
    assert 0.5 < float(base_share) < 0.95, float(base_share)


def check_score(
    name: str, tree: xr.DataTree, bundle: Bundle, data: xr.Dataset, *, ctx: FixtureContext
) -> None:
    if name == "forecast_4w":
        y = _group(tree, "summary")["y_hat"].sel(stat="mean")
        assert y.sizes["week"] == HORIZON_WEEKS and bool(np.isfinite(y).all())
        weeks = SIZES[ctx.size]["weeks"]
        full, _ = _simulate(ctx.seed, ctx.size, weeks + HORIZON_WEEKS)
        want = full["revenue"].isel(week=slice(weeks, None))
        rel = float(abs(y.transpose(*want.dims) - want.values).mean() / want.mean())
        assert rel < 0.15, rel
    if name == "tv_off":
        delta = _group(tree, "delta/summary")
        assert float(delta["y_hat"].sel(stat="mean").sum()) < 0
        comp = delta["contributions"].sel(stat="mean")
        assert float(comp.sel(component="tv").sum()) < 0
        others = comp.sel(component=[c for c in comp["component"].values if c != "tv"])
        assert float(abs(others).max()) < 1e-6
