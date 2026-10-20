"""The v0.12 derived quantities and the FitWindow totals they build on (prior-draw fits)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from mapi_plugin_mmm.base import DERIVED
from mapi_plugin_mmm.bayesian import MMMBayesian
from mapi_plugin_mmm.calibration import LiftPlan, lift_test_group
from mapi_plugin_mmm.derived import (
    FitWindow,
    adstock_dataset,
    effectiveness_dataset,
    response_curves_dataset,
)
from mapi_plugin_mmm.frequentist import MMMFrequentist
from mapi_plugin_mmm.planning import _baseline
from mapi_plugin_mmm.settings import GridSettings


def _window(pf: Any) -> FitWindow:
    rp, fit = pf.plugin.replay(pf.fitted)
    return FitWindow(rp, fit)


def _old_baseline(win: FitWindow) -> np.ndarray:
    """``(S, n_cell)`` the v0.11 formula: every non-channel term summed over the window."""
    comps = win.evaluate(1.0)
    total = comps.baseline + comps.trend[:, None, :] + comps.seasonality[:, None, :]
    if comps.controls.shape[1]:
        total = total + comps.controls.sum(axis=1)
    return np.asarray(total.sum(axis=2))


def test_both_models_declare_the_new_derived_quantities() -> None:
    names = [d["name"] for d in DERIVED]
    assert names[-2:] == ["adstock", "effectiveness"]
    for plugin in (MMMBayesian, MMMFrequentist):
        declared = {d.name for d in plugin.manifest().derived}
        assert {"adstock", "effectiveness"} <= declared


@pytest.mark.parametrize("which", ["geo_prior_fit", "national_prior_fit"])
def test_baseline_cells_equal_the_old_formula(which: str, request: pytest.FixtureRequest) -> None:
    win = _window(request.getfixturevalue(which))
    np.testing.assert_allclose(win.baseline_cells(), _old_baseline(win), rtol=1e-9)
    np.testing.assert_allclose(win.expected_cells(), win.evaluate(1.0).mu.sum(axis=2), rtol=1e-12)
    np.testing.assert_allclose(_baseline(win), _old_baseline(win).sum(axis=1), rtol=1e-9)


def test_the_response_curve_baseline_is_unchanged(geo_prior_fit: Any) -> None:
    win = _window(geo_prior_fit)
    ds = response_curves_dataset(win, GridSettings(points=5), None)
    np.testing.assert_allclose(ds["baseline"].values, _old_baseline(win).sum(axis=1), rtol=1e-9)
    np.testing.assert_allclose(ds["baseline_by_geo"].values, _old_baseline(win), rtol=1e-9)


@pytest.mark.parametrize("which", ["geo_prior_fit", "national_prior_fit"])
def test_adstock_weights_sum_to_one(which: str, request: pytest.FixtureRequest) -> None:
    win = _window(request.getfixturevalue(which))
    ds = adstock_dataset(win)
    assert ds["weight"].dims == ("sample", "channel", "lag")
    np.testing.assert_allclose(ds["weight"].sum("lag").values, 1.0)
    lag = ds["lag"].values
    np.testing.assert_allclose(
        ds["mean_lag"].values, (ds["weight"].values * lag).sum(axis=2), rtol=1e-12
    )
    assert ds.attrs["frequency"] == "W"
    assert (ds["weight"].values >= 0).all()


def test_effectiveness_is_constant_without_time_varying_channels(geo_prior_fit: Any) -> None:
    win = _window(geo_prior_fit)
    ds = effectiveness_dataset(win, None)
    eff = ds["effectiveness"].values  # (S, C, T)
    assert not ds["time_varying"].values.any()
    np.testing.assert_allclose(eff, np.repeat(eff[:, :, :1], eff.shape[2], axis=2))
    spend, _ = win.arrays()
    level = spend.mean(axis=0)
    want = win.replay.steady_state(level, win.cells).sum(axis=1) / level.sum(axis=0)
    np.testing.assert_allclose(eff[:, :, 0], want, rtol=1e-12)


def test_effectiveness_converts_units(geo_prior_fit: Any) -> None:
    win = _window(geo_prior_fit)

    def to_original(da: Any, src: str) -> Any:
        out = da * (2.0 if src == "y" else 4.0)
        out.attrs = {"units": "EUR", "output_space": "original"}
        return out

    plain = effectiveness_dataset(win, None)["effectiveness"]
    conv = effectiveness_dataset(win, to_original)["effectiveness"]
    np.testing.assert_allclose(conv.values, plain.values / 2.0)
    assert conv.attrs["output_space"] == "original" and "units" not in conv.attrs


def test_the_derive_hook_serves_the_new_quantities(geo_prior_fit: Any) -> None:
    from mapi_sdk.models import ScoreContext

    pf = geo_prior_fit
    ctx = ScoreContext(draw_index=np.array([[0, k] for k in range(4)]))
    ad = pf.plugin.derive(pf.fitted, "adstock", pf.stage, ctx)
    eff = pf.plugin.derive(pf.fitted, "effectiveness", pf.stage, ctx)
    assert ad.sizes["sample"] == 4 and eff.sizes == {"sample": 4, "channel": 2, "time": 52}


def test_no_lift_tests_no_group(geo_prior_fit: Any) -> None:
    assert lift_test_group(LiftPlan([], [], {}), geo_prior_fit.fitted.posterior) is None
