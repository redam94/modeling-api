"""Business priors to mmm-framework ROI priors, the prior preview, and lift tests."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_mmm.calibration import (
    UnitScales,
    lift_measurements,
    lift_window_cut,
    prior_preview,
    translate_business_priors,
    unit_scales,
    with_priors,
)
from mapi_plugin_mmm.framework import PanelData, detect_controls, make_model
from mapi_plugin_mmm.settings import BayesianSettings
from mapi_spec.calibration import BusinessPriors, LiftTest, PriorTranslationResult
from mapi_spec.errors import ErrorCode, MapiError, Severity
from scipy import stats


@pytest.fixture(scope="module")
def setup(geo_stage: xr.Dataset) -> tuple[PanelData, BayesianSettings, Any]:
    s = BayesianSettings.model_validate(
        {"hierarchy": {"pool_over": ["geo"]}, "adstock": {"l_max": 4}, "trend": {"kind": "none"}}
    )
    data = PanelData.from_dataset(
        geo_stage, ["geo"], detect_controls(geo_stage, None), with_target=True
    )

    def factory(st: BayesianSettings) -> Any:
        return make_model(data, st, frequency="W")

    return data, s, factory


def _beliefs(raw: dict[str, Any]) -> BusinessPriors:
    return BusinessPriors.model_validate(raw)


def test_an_roi_range_is_the_lognormal_with_those_quantiles(setup: Any) -> None:
    data, s, factory = setup
    out = translate_business_priors(
        _beliefs({"channels": {"tv": {"roi": [1.0, 3.0], "credibility": 0.8}}}),
        data, s, UnitScales(), model_factory=factory, draws=200,
    )  # fmt: skip
    prior = out.priors["roi"]["tv"]
    dist = stats.lognorm(s=prior["sigma"], scale=math.exp(prior["mu"]))
    np.testing.assert_allclose(dist.ppf([0.1, 0.9]), [1.0, 3.0], rtol=1e-9)
    assert out.ranges["tv"] == ("roi", 1.0, 3.0, 0.8)
    assert "intercept" not in out.priors


def test_a_share_range_is_an_roi_range_scaled_by_kpi_over_spend(setup: Any) -> None:
    data, s, factory = setup
    out = translate_business_priors(
        _beliefs({"channels": {"search": {"contribution_share": [0.05, 0.15]}}}),
        data, s, UnitScales(), model_factory=factory, draws=200,
    )  # fmt: skip
    k = float(data.y.sum()) / float(data.spend[..., 1].sum())  # type: ignore[union-attr]
    prior = out.priors["roi"]["search"]
    dist = stats.lognorm(s=prior["sigma"], scale=math.exp(prior["mu"]))
    np.testing.assert_allclose(dist.ppf([0.1, 0.9]), [0.05 * k, 0.15 * k], rtol=1e-9)


def test_units_convert_business_roi_to_model_roi(setup: Any) -> None:
    data, s, factory = setup
    scales = UnitScales(kpi_factor=4.0, spend_factor=2.0)
    out = translate_business_priors(
        _beliefs({"channels": {"tv": {"roi": [2.0, 6.0]}}}), data, s, scales,
        model_factory=factory, draws=200,
    )  # fmt: skip
    prior = out.priors["roi"]["tv"]
    assert math.exp(prior["mu"]) == pytest.approx(math.sqrt(2.0 * 6.0) / 2.0)


def test_zero_lows_unknown_channels_and_dark_channels(setup: Any, geo_stage: xr.Dataset) -> None:
    data, s, factory = setup
    out = translate_business_priors(
        _beliefs({"channels": {"tv": {"roi": [0.0, 2.0]}, "radio": {"roi": [1.0, 2.0]}}}),
        data, s, UnitScales(), model_factory=factory, draws=200,
    )  # fmt: skip
    codes = {(i.severity, i.pointer) for i in out.issues}
    assert (Severity.INFO, "/business_priors/channels/tv/roi") in codes
    assert (Severity.ERROR, "/business_priors/channels/radio") in codes
    assert set(out.priors["roi"]) == {"tv"}
    dark = geo_stage.copy()
    dark["spend"] = dark.spend.where(dark.channel != "search", 0.0)
    d2 = PanelData.from_dataset(dark, ["geo"], ["price", "season"], with_target=True)
    out = translate_business_priors(
        _beliefs({"channels": {"search": {"contribution_share": [0.1, 0.2]}}}), d2, s, UnitScales(),
        model_factory=lambda st: make_model(d2, st, frequency="W"), draws=200,
    )  # fmt: skip
    assert [i.severity for i in out.issues] == [Severity.ERROR]


def test_the_preview_covers_the_stated_ranges(setup: Any) -> None:
    data, s, factory = setup
    beliefs = _beliefs(
        {
            "channels": {"tv": {"roi": [0.5, 2.0], "credibility": 0.8},
                         "search": {"contribution_share": [0.05, 0.2], "credibility": 0.8}},
            "baseline_share": {"share": [0.5, 0.9], "credibility": 0.8},
        }
    )  # fmt: skip
    out = translate_business_priors(beliefs, data, s, UnitScales(), model_factory=factory,
                                    draws=600, seed=1)  # fmt: skip
    assert set(out.priors) == {"roi", "intercept"}
    result, preview = prior_preview(
        out, data, s, UnitScales(kpi_units="EUR"),
        model_factory=factory, draws=600, seed=2,
        lift_tests=[LiftTest(channel="tv", start=date(2024, 3, 3), end=date(2024, 3, 31),
                             spend_delta=500.0, kpi_delta=400.0, se=100.0)],
    )  # fmt: skip
    PriorTranslationResult.model_validate(result.model_dump(mode="json"))
    # ROI is exactly log-normal under mmm-framework's ROI parameterisation
    assert result.coverage["tv"] == pytest.approx(0.8, abs=0.06)
    assert result.coverage["search"] == pytest.approx(0.8, abs=0.06)
    assert result.coverage["baseline"] == pytest.approx(0.8, abs=0.12)
    assert set(preview.data_vars) >= {"roi", "contribution_share", "baseline_share", "kpi",
                                      "kpi_observed", "stated_low", "stated_high"}  # fmt: skip
    assert preview["kpi"].sizes["time"] == 52
    assert preview["baseline_share"].attrs["range_low"] == 0.5
    assert result.preview.kpi_fan is not None and len(result.preview.kpi_fan.coords) == 52
    assert result.preview.roi["tv"].quantiles


def test_with_priors_merges_channel_priors(setup: Any) -> None:
    _, s, _ = setup
    a = with_priors(s, {"roi": {"tv": {"mu": 0.0, "sigma": 1.0}}})
    b = with_priors(a, {"roi": {"search": {"mu": 1.0, "sigma": 0.5}}})
    assert set(b.priors.roi) == {"tv", "search"}


def test_unit_scales_read_the_pipeline_inverse(geo_stage: xr.Dataset) -> None:
    assert unit_scales(geo_stage, None).kpi_units == "EUR"

    def double(da: xr.DataArray, src: str) -> xr.DataArray:
        out = da * (2.0 if src == "y" else 1.0)
        out.attrs["units"] = "GBP"
        return out

    sc = unit_scales(geo_stage, double)
    assert (sc.kpi_factor, sc.spend_factor, sc.kpi_units) == (2.0, 1.0, "GBP")

    def logged(da: xr.DataArray, src: str) -> xr.DataArray:
        return np.log1p(da)

    with pytest.raises(MapiError) as err:
        unit_scales(geo_stage, logged)
    assert err.value.code is ErrorCode.CALIBRATION_UNSUPPORTED


# ======================================================================== lift tests


def _test(**kw: Any) -> LiftTest:
    base: dict[str, Any] = {"channel": "tv", "start": date(2024, 3, 3), "end": date(2024, 3, 24),
                            "kpi_delta": 300.0, "se": 60.0}  # fmt: skip
    return LiftTest.model_validate({**base, **kw})


def test_extra_spend_becomes_a_marginal_roas_measurement(setup: Any) -> None:
    from mmm_framework.calibration import ExperimentEstimand

    data = setup[0]
    plan = lift_measurements([_test(spend_delta=400.0, geo="north")], data, UnitScales())
    (m,) = plan.measurements
    window = data.spend[
        (data.times >= np.datetime64("2024-03-03")) & (data.times <= np.datetime64("2024-03-24"))
    ]
    spend = float(window[:, 0, 0, 0].sum())
    assert m.estimand is ExperimentEstimand.MROAS
    assert m.spend_lift_pct == pytest.approx(100 * 400.0 / spend)
    assert m.value == pytest.approx(300.0 / 400.0) and m.se == pytest.approx(60.0 / 400.0)
    assert m.holdout_regions == ["north"] and m.channel == "tv"
    assert plan.used == ["lift_0"]


def test_a_full_holdout_is_a_contribution_measurement(setup: Any) -> None:
    from mmm_framework.calibration import ExperimentEstimand

    data = setup[0]
    mask = (data.times >= np.datetime64("2024-03-03")) & (data.times <= np.datetime64("2024-03-24"))
    spend = float(data.spend[mask][..., 0].sum())
    plan = lift_measurements(
        [_test(spend_delta=-spend, kpi_delta=-900.0, se=None, ci=(-1200.0, -600.0))],
        data, UnitScales(),
    )  # fmt: skip
    (m,) = plan.measurements
    assert m.estimand is ExperimentEstimand.CONTRIBUTION
    assert m.value == pytest.approx(900.0)
    assert m.se == pytest.approx(600.0 / (2 * stats.norm.ppf(0.95)))


def test_partial_cuts_and_skips(setup: Any) -> None:
    data = setup[0]
    mask = (data.times >= np.datetime64("2024-03-03")) & (data.times <= np.datetime64("2024-03-24"))
    spend = float(data.spend[mask][..., 0].sum())
    plan = lift_measurements(
        [
            _test(id="small", spend_delta=-0.1 * spend, kpi_delta=-50.0),
            _test(id="half", spend_delta=-0.5 * spend, kpi_delta=-200.0),
            _test(id="later", start=date(2026, 1, 4), end=date(2026, 1, 25), spend_delta=10.0),
            _test(id="cut", start=date(2024, 12, 15), end=date(2025, 2, 2), spend_delta=10.0),
        ],
        data, UnitScales(),
    )  # fmt: skip
    # Regression: a partial cut measures the slope *below* today's spend (a backward
    # difference), but mmm-framework's marginal-ROAS estimand is the slope *above* it
    # (spend_lift_pct > 0 only); under a concave curve the backward slope is larger
    # (e^{0.2 lam} times for 1 - exp(-lam x) and a 20% cut), so mapping one onto the other
    # biased the saturation curve. Partial cuts are skipped with the reason instead.
    assert plan.used == []
    assert set(plan.skipped) == {"small", "half", "later", "cut"}
    assert "partial cut" in plan.skipped["half"]
    assert "below" in plan.skipped["small"] and "above" in plan.skipped["small"]
    assert plan.record()["used"] == []
    lam, c = 2.0, 0.2
    curve = lambda x: 1 - np.exp(-lam * x)  # noqa: E731
    backward = (curve(1.0) - curve(1 - c)) / c
    forward = (curve(1 + c) - curve(1.0)) / c
    assert backward / forward == pytest.approx(np.exp(c * lam))


@pytest.mark.parametrize(
    ("kw", "pointer"),
    [({"channel": "radio"}, "/channel"), ({"geo": "mars"}, "/geo")],
)
def test_lift_tests_on_unknown_channels_or_geos(
    setup: Any, kw: dict[str, Any], pointer: str
) -> None:
    with pytest.raises(MapiError) as err:
        lift_measurements([_test(spend_delta=10.0, **kw)], setup[0], UnitScales())
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    assert err.value.pointer == "/calibration/lift_tests/0" + pointer


def test_window_cut() -> None:
    days = np.arange(
        np.datetime64("2024-01-07"), np.datetime64("2024-03-31"), np.timedelta64(7, "D")
    )
    assert not lift_window_cut(days, date(2024, 2, 1), date(2024, 2, 20))
    assert lift_window_cut(days, date(2024, 3, 20), date(2024, 4, 30))
    assert not lift_window_cut(days, date(2024, 3, 20), date(2024, 3, 30))


def test_the_translate_priors_hook(geo_stage: xr.Dataset) -> None:
    from mapi_plugin_mmm.bayesian import MMMBayesian
    from mapi_sdk.models import FitContext
    from mapi_spec.calibration import PriorTranslationJob

    plugin = MMMBayesian()
    s = plugin.parse_settings({"hierarchy": {"pool_over": ["geo"]}, "adstock": {"l_max": 3}})
    job = PriorTranslationJob(
        spec={},
        business_priors=_beliefs({"channels": {"tv": {"roi": [0.5, 1.5]}}}),
        draws=200,
        seed=4,
    )
    out = plugin.translate_priors(geo_stage, s, job, FitContext(seed=1))
    assert set(out.result.priors["roi"]) == {"tv"}
    assert out.result.draws == 200 and "tv" in out.result.coverage
    # the translated priors are valid settings
    merged = with_priors(s, out.result.priors)
    assert merged.priors.roi["tv"].sigma > 0
