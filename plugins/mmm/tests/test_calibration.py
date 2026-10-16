"""Business priors → model-space priors and lift-test calibration (``mapi_plugin_mmm.calibration``).

The round-trip property tests are the contract of the translation: sampling the translated
priors afresh puts the stated credibility inside the stated range (within Monte Carlo error)
whenever the solve reports no infeasibility.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mapi_plugin_mmm.calibration import (
    LIFT_DIM,
    LIFT_VAR,
    LiftDesign,
    ModelScales,
    lift_design,
    model_scales,
    prior_preview,
    translate_business_priors,
)
from mapi_plugin_mmm.hierarchical_geo import HierarchicalGeoMMM
from mapi_plugin_mmm.settings import HierarchicalGeoSettings
from mapi_plugin_mmm.transforms import adstock_weights, saturate
from mapi_pymc.context import BuildContext
from mapi_spec.calibration import BusinessPriors, LiftTest
from mapi_spec.errors import ErrorCode, MapiError, Severity

pytestmark = pytest.mark.unit

GEOS = ["north", "south"]
CHANNELS = ["tv", "search"]
N_TIME, N_LAG = 26, 3


def model_stage(seed: int = 0, *, zero_channel: int | None = None) -> xr.Dataset:
    """A model-stage dataset: ``y(geo, time)`` and the spend lag tensor (model units)."""
    rng = np.random.default_rng(seed)
    times = np.asarray(
        [np.datetime64("2024-01-07") + np.timedelta64(7 * i, "D") for i in range(N_TIME)]
    )
    spend = rng.gamma(4.0, 0.25, size=(len(GEOS), len(CHANNELS), N_TIME))
    if zero_channel is not None:
        spend[:, zero_channel, :] = 0.0
    lagged = np.stack(
        [np.concatenate([np.repeat(spend[..., :1], k, axis=-1), spend[..., : N_TIME - k]], -1)
         for k in range(N_LAG)],
        axis=-1,
    )  # fmt: skip
    y = 1.0 + 0.1 * rng.standard_normal((len(GEOS), N_TIME))
    return xr.Dataset(
        {
            "y": (("geo", "time"), y),
            "spend": (("geo", "channel", "time", "lag"), lagged),
        },
        coords={"geo": GEOS, "channel": CHANNELS, "time": times, "lag": np.arange(N_LAG)},
    )


def scaled(factor_y: float = 1000.0, factor_x: float = 50.0) -> Any:
    """A linear ``to_original``: KPI x ``factor_y``, spend x ``factor_x`` (per geo)."""

    def convert(da: xr.DataArray, src: str) -> xr.DataArray:
        geo_scale = xr.DataArray([1.0, 2.0], dims=["geo"], coords={"geo": GEOS})
        f = factor_y if src == "y" else factor_x
        out = da * f * geo_scale
        out.attrs = {"units": "EUR", "output_space": "weekly"}
        return out

    return convert


SETTINGS = HierarchicalGeoSettings.model_validate({"adstock": {"l_max": N_LAG}})


def _translate(
    beliefs: dict[str, Any], s: HierarchicalGeoSettings = SETTINGS, seed: int = 1
) -> tuple[Any, ModelScales, xr.Dataset]:
    ds = model_stage()
    scales = model_scales(ds, s, scaled())
    priors = BusinessPriors.model_validate(beliefs)
    outcome = translate_business_priors(priors, scales, s, ds, draws=1500, seed=seed)
    return outcome, scales, ds


# ================================================================================ scales


def test_scales_read_units_through_the_pipeline_inverse() -> None:
    ds = model_stage()
    sc = model_scales(ds, SETTINGS, scaled())
    assert sc.target_scale.shape == (2, N_TIME)
    np.testing.assert_allclose(sc.target_scale[:, 0], [1000.0, 2000.0])
    np.testing.assert_allclose(sc.spend_scale[1, 0, 0], 100.0)
    np.testing.assert_allclose(sc.spend, ds["spend"].values[..., 0] * sc.spend_scale)
    assert sc.kpi_units == "EUR" and sc.spend_units == "EUR"
    assert sc.kpi_total == pytest.approx(float((ds["y"].values * sc.target_scale).sum()))
    ident = model_scales(ds, SETTINGS, None)
    assert np.all(ident.target_scale == 1.0) and ident.kpi_units is None


def test_non_linear_preprocessing_is_refused() -> None:
    def log_inverse(da: xr.DataArray, src: str) -> xr.DataArray:
        return np.expm1(da)

    with pytest.raises(MapiError) as err:
        model_scales(model_stage(), SETTINGS, log_inverse)
    assert err.value.code is ErrorCode.CALIBRATION_UNSUPPORTED


# =========================================================================== translation


def _coverage_ok(result: Any, outcome: Any, label: str, cred: float) -> None:
    narrow = [i for i in outcome.issues if "narrower" in i.message and label in i.message]
    if narrow:
        return  # infeasible by construction: reported, not met
    assert abs(result.coverage[label] - cred) < 0.07, (label, result.coverage, cred)


@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    derandomize=True,
)
@given(
    lo=st.floats(0.3, 5.0),
    ratio=st.floats(2.0, 12.0),
    cred=st.floats(0.5, 0.95),
    pooling=st.sampled_from(["partial", "complete", "none"]),
)
def test_roi_ranges_round_trip(lo: float, ratio: float, cred: float, pooling: str) -> None:
    s = HierarchicalGeoSettings.model_validate({"adstock": {"l_max": N_LAG}, "pooling": pooling})
    hi = lo * ratio
    outcome, scales, ds = _translate(
        {"channels": {"tv": {"roi": [lo, hi], "credibility": cred}}}, s
    )
    result, preview = prior_preview(outcome, scales, s, ds, draws=3000, seed=11)
    _coverage_ok(result, outcome, "tv", cred)
    q = (1 - cred) / 2
    roi = preview["roi"].sel(channel="tv").values
    if not any("narrower" in i.message for i in outcome.issues):
        lo_q, hi_q = np.quantile(roi, [q, 1 - q])
        width = np.log(ratio)
        assert abs(np.log(lo_q / lo)) < 0.2 * width + 0.05
        assert abs(np.log(hi_q / hi)) < 0.2 * width + 0.05
    # the untouched channel keeps a log-normal like its default prior
    spec = outcome.priors["beta_channel"]
    assert spec["distribution"] == "LogNormal" and len(spec["mu"]) == 2


@settings(max_examples=8, deadline=None, derandomize=True)
@given(lo=st.floats(0.01, 0.3), ratio=st.floats(1.8, 4.0), cred=st.floats(0.6, 0.95))
def test_share_ranges_round_trip(lo: float, ratio: float, cred: float) -> None:
    hi = min(lo * ratio, 0.95)
    outcome, scales, ds = _translate(
        {"channels": {"search": {"contribution_share": [lo, hi], "credibility": cred}}}
    )
    result, _ = prior_preview(outcome, scales, SETTINGS, ds, draws=3000, seed=5)
    _coverage_ok(result, outcome, "search", cred)


def test_a_baseline_belief_alone_translates() -> None:
    # Guided's "Results without any media" with no channel ticked (M3 review)
    outcome, scales, ds = _translate({"baseline_share": {"share": [0.5, 0.8], "credibility": 0.8}})
    assert not [i for i in outcome.issues if i.severity is Severity.ERROR]
    assert "intercept" in outcome.priors
    result, _ = prior_preview(outcome, scales, SETTINGS, ds, draws=3000, seed=4)
    assert result.coverage["baseline"] == pytest.approx(0.8, abs=0.08)
    assert set(result.preview.roi) == set(CHANNELS)  # every channel at its default


def test_mixed_beliefs_baseline_and_the_preview() -> None:
    beliefs = {
        "channels": {
            "tv": {"roi": [1.5, 4.0], "credibility": 0.8},
            "search": {"contribution_share": [0.05, 0.15], "credibility": 0.9},
        },
        "baseline_share": {"share": [0.5, 0.8], "credibility": 0.8},
    }
    outcome, scales, ds = _translate(beliefs)
    assert set(outcome.priors) >= {"beta_channel", "intercept"}
    # the result validates as settings (the spec's priors block)
    merged = HierarchicalGeoSettings.model_validate(
        {"adstock": {"l_max": N_LAG}, "priors": outcome.priors}
    )
    assert merged.priors.beta_channel is not None
    result, preview = prior_preview(outcome, scales, SETTINGS, ds, draws=3000, seed=2)
    assert result.coverage["tv"] == pytest.approx(0.8, abs=0.07)
    assert result.coverage["search"] == pytest.approx(0.9, abs=0.07)
    assert result.coverage["baseline"] == pytest.approx(0.8, abs=0.08)
    assert set(result.preview.roi) == {"tv", "search"}
    assert result.preview.roi["tv"].units == "EUR per EUR"
    fan = result.preview.kpi_fan
    assert fan is not None and len(fan.coords) == N_TIME and fan.observed is not None
    assert fan.coords[0] == "2024-01-07"
    assert list(preview["stated_quantity"].values) == ["roi", "contribution_share"]
    assert result.channels == CHANNELS and result.draws == 3000


def test_unknown_channels_zero_spend_and_ranges_from_zero() -> None:
    ds = model_stage(zero_channel=1)
    scales = model_scales(ds, SETTINGS, None)
    priors = BusinessPriors.model_validate(
        {
            "channels": {
                "radio": {"roi": [1, 2]},
                "search": {"roi": [1, 2]},
                "tv": {"roi": [0.0, 3.0]},
            }
        }
    )
    out = translate_business_priors(priors, scales, SETTINGS, ds, draws=400, seed=0)
    errors = {i.pointer: i for i in out.issues if i.severity is Severity.ERROR}
    assert "/business_priors/channels/radio" in errors
    assert "/business_priors/channels/search/roi" in errors
    info = [i for i in out.issues if i.severity is Severity.INFO]
    assert any("starts at 0" in i.message for i in info)
    assert set(out.ranges) == {"tv"}


def test_a_range_tighter_than_the_other_priors_is_reported() -> None:
    outcome, _, _ = _translate({"channels": {"tv": {"roi": [2.0, 2.02], "credibility": 0.95}}})
    assert any("narrower" in i.message for i in outcome.issues)


def test_translation_is_deterministic_given_the_seed() -> None:
    a, _, _ = _translate({"channels": {"tv": {"roi": [1.0, 3.0]}}}, seed=4)
    b, _, _ = _translate({"channels": {"tv": {"roi": [1.0, 3.0]}}}, seed=4)
    assert a.priors == b.priors


# ============================================================================ lift tests


def _test(**kw: Any) -> LiftTest:
    base: dict[str, Any] = {
        "channel": "tv",
        "start": date(2024, 2, 5),
        "end": date(2024, 3, 3),
        "spend_delta": -100.0,
        "kpi_delta": -150.0,
        "se": 40.0,
    }
    return LiftTest.model_validate({**base, **kw})


def test_lift_design_spreads_the_extra_spend_in_proportion() -> None:
    ds = model_stage()
    sc = model_scales(ds, SETTINGS, scaled())
    design = lift_design([_test(geo="south")], sc)
    assert design is not None and design.n_tests == 1
    weeks = np.flatnonzero(
        (sc.times >= np.datetime64("2024-02-05")) & (sc.times <= np.datetime64("2024-03-03"))
    )
    assert len(design.test) == weeks.size == 4
    assert set(design.geo) == {1}
    # lag 0 of each cell carries that week's share of the removed spend, in model units
    alloc = design.dx[:, 0] * sc.spend_scale[1, 0, weeks]
    base = sc.spend[1, 0, weeks]
    np.testing.assert_allclose(alloc, -100.0 * base / base.sum())
    assert alloc.sum() == pytest.approx(-100.0)
    # carry-over inside the window: lag 1 of week k is week k-1's change
    np.testing.assert_allclose(design.dx[1:, 1], design.dx[:-1, 0])
    assert design.dx[0, 1] == 0.0
    np.testing.assert_allclose(design.target_scale, sc.target_scale[1, weeks])


def _contribution(
    x: np.ndarray, alpha: np.ndarray, lam: np.ndarray, beta: np.ndarray, ts: np.ndarray
) -> np.ndarray:
    w = adstock_weights("geometric", np.arange(N_LAG), alpha=alpha[:, None])
    ad = np.einsum("gctl,cl->gct", x, w)
    return ts[:, None, :] * beta[:, :, None] * saturate(ad, "logistic", lam=lam[None, :, None])


def test_lift_equals_the_difference_of_two_full_contributions() -> None:
    """The cell design reproduces sum_window(contrib(x + dx) - contrib(x)) exactly."""
    ds = model_stage()
    sc = model_scales(ds, SETTINGS, scaled())
    tests = [_test(), _test(channel="search", spend_delta=60.0, kpi_delta=80.0, id="s-up")]
    design = lift_design(tests, sc)
    assert design is not None
    alpha, lam = np.array([0.4, 0.2]), np.array([1.5, 0.8])
    beta = np.array([[0.3, 0.2], [0.25, 0.1]])
    got = design.lift(
        adstock_weights("geometric", np.arange(N_LAG), alpha=alpha[:, None]),
        {"lam": lam},
        beta,
        "logistic",
    )
    window = (sc.times >= np.datetime64("2024-02-05")) & (sc.times <= np.datetime64("2024-03-03"))
    for k, test in enumerate(tests):
        c = CHANNELS.index(test.channel)
        base = sc.spend[:, c, window]
        dx_orig = np.zeros_like(sc.spend[:, c, :])
        dx_orig[:, window] = test.spend_delta * base / base.sum()
        dx = dx_orig / sc.spend_scale[:, c, :]
        x1 = sc.x.copy()
        for lag in range(N_LAG):
            shifted = np.zeros_like(dx)
            shifted[:, lag:] = dx[:, : N_TIME - lag]
            x1[:, c, :, lag] += shifted
        diff = _contribution(x1, alpha, lam, beta, sc.target_scale) - _contribution(
            sc.x, alpha, lam, beta, sc.target_scale
        )
        assert got[k] == pytest.approx(float(diff[:, c, window].sum()), rel=1e-10)


@pytest.mark.parametrize(
    ("kw", "pointer"),
    [
        ({"channel": "radio"}, "/calibration/lift_tests/0/channel"),
        ({"geo": "west"}, "/calibration/lift_tests/0/geo"),
        ({"spend_delta": -1e9}, "/calibration/lift_tests/0/spend_delta"),
        ({"start": date(2024, 2, 5), "end": date(2024, 2, 6)}, "/calibration/lift_tests/0"),
    ],
)
def test_lift_design_refuses_tests_the_data_cannot_carry(kw: dict[str, Any], pointer: str) -> None:
    sc = model_scales(model_stage(), SETTINGS, None)
    with pytest.raises(MapiError) as err:
        lift_design([_test(**kw)], sc)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.pointer == pointer


def test_tests_after_the_fitted_periods_are_skipped() -> None:
    sc = model_scales(model_stage(), SETTINGS, scaled())
    logged: list[str] = []
    later = _test(start=date(2025, 1, 6), end=date(2025, 2, 2), id="later")
    assert lift_design([later], sc, log=logged.append) is None
    design = lift_design([_test(id="now"), later], sc)
    assert isinstance(design, LiftDesign) and design.ids == ["now"] and design.skipped == ["later"]


def test_the_fit_graph_carries_the_lift_likelihood_and_score_does_not() -> None:
    ds = model_stage()
    sc = model_scales(ds, SETTINGS, scaled())
    plugin = HierarchicalGeoMMM()
    plugin._lift = lift_design([_test()], sc)
    schema = HierarchicalGeoMMM.manifest().schema_template.resolve({})
    fit_ctx = BuildContext(phase="fit", ds=ds, schema=schema, seed=1)
    model = plugin.build_model(ds, SETTINGS, fit_ctx)
    assert LIFT_VAR in model.named_vars and model.named_vars_to_dims[LIFT_VAR] == (LIFT_DIM,)
    assert np.isfinite(model.compile_logp()(model.initial_point()))
    lift = pm.draw(model[f"{LIFT_VAR}__model"], draws=2, random_seed=0)
    assert lift.shape == (2, 1) and np.all(lift < 0)  # removing spend loses KPI
    score_ctx = BuildContext(phase="score", ds=ds, schema=schema, seed=1, fit_coords={})
    assert LIFT_VAR not in plugin.build_model(ds, SETTINGS, score_ctx).named_vars
