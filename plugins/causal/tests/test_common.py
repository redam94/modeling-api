"""Shared causal pieces: windows, the ``impact`` group, summaries and tracking (I-5)."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_causal._common import (
    IMPACT_DRAWS,
    CausalBase,
    CausalSettings,
    Window,
    effect_summary,
    impact_group,
    label_value,
    log_effect_summary,
    parse_window,
    split_labels,
    thin_samples,
    unique_labels,
)
from mapi_sdk.models import FitContext
from mapi_sdk.tracking import MetricRecorder
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import ValidationError

WEEKS = np.array(
    ["2024-01-01", "2024-01-08", "2024-01-15", "2024-01-22", "2024-01-29", "2024-02-05"],
    dtype="datetime64[ns]",
)


# ------------------------------------------------------------------------------ settings


def test_settings_accept_dates_and_numbers() -> None:
    s = CausalSettings(intervention_start="2024-01-20", intervention_end="")
    assert s.intervention_end is None
    s = CausalSettings.model_validate({"intervention_start": 4, "intervention_end": 6})
    assert (s.intervention_start, s.intervention_end) == ("4", "6")
    s = CausalSettings(intervention_start="-3")
    assert s.intervention_end is None


@pytest.mark.parametrize(
    "start,end",
    [
        ("Jan 2024", None),
        ("2024-02-01", "2024-01-01"),
        ("5", "3"),
        ("2024-01-01", "5"),
        ("2024-01-01", "soon"),
    ],
)
def test_settings_reject_bad_windows(start: str, end: str | None) -> None:
    with pytest.raises(ValidationError):
        CausalSettings(intervention_start=start, intervention_end=end)


@pytest.mark.parametrize(
    "start,end,field",
    [
        ("2024-02-30", None, "intervention_start"),
        ("2023-13-01", "", "intervention_start"),
        ("2024-01-01", "2024-02-30", "intervention_end"),
    ],
)
def test_settings_reject_impossible_dates(start: str, end: str | None, field: str) -> None:
    """Regression: '2024-02-30' matched the date pattern and failed later in the fit with a
    bare ValueError (no SETTINGS_INVALID, no pointer)."""
    with pytest.raises(ValidationError) as err:
        CausalSettings(intervention_start=start, intervention_end=end)
    assert err.value.errors()[0]["loc"] == (field,)
    assert "not a valid date" in str(err.value)


def test_window_with_an_impossible_date_points_at_the_setting() -> None:
    with pytest.raises(MapiError) as err:
        parse_window(WEEKS, "2024-02-30", None)
    assert err.value.code == ErrorCode.SETTINGS_INVALID
    assert err.value.pointer == "/model/settings/intervention_start"
    with pytest.raises(MapiError) as err:
        parse_window(WEEKS, "2024-01-22", "2024-02-31")
    assert err.value.pointer == "/model/settings/intervention_end"


def test_split_labels() -> None:
    assert split_labels("geo_01, geo_02 ,,") == ["geo_01", "geo_02"]
    assert split_labels([" a", "b "]) == ["a", "b"]
    assert split_labels("") == []


# ------------------------------------------------------------------------------ windows


def test_window_dates_between_periods() -> None:
    w = parse_window(WEEKS, "2024-01-20", "2024-01-31")
    assert (w.start, w.end, w.start_label, w.end_label, w.open_end) == (
        3,
        4,
        "2024-01-22",
        "2024-01-29",
        False,
    )
    assert w.post_mask(6).tolist() == [False, False, False, True, True, False]
    assert w.pre_mask(6).tolist() == [True, True, True, False, False, False]


def test_window_open_end() -> None:
    for end in (None, ""):
        w = parse_window(WEEKS, "2024-01-22", end)
        assert (w.start, w.end, w.end_label, w.open_end) == (3, 5, "", True)


def test_window_integer_periods_are_exact() -> None:
    t = np.arange(10, 20)
    w = parse_window(t, "15", "17")
    assert (w.start, w.end, w.start_label, w.end_label) == (5, 7, "15", "17")
    with pytest.raises(MapiError) as err:
        parse_window(t, "25", None)
    assert err.value.code == ErrorCode.SETTINGS_INVALID
    with pytest.raises(MapiError):
        parse_window(t, "2024-01-01", None)


def test_window_errors_point_at_the_setting() -> None:
    with pytest.raises(MapiError) as err:
        parse_window(WEEKS, "2024-01-09", None)  # two periods before it
    assert err.value.code == ErrorCode.SETTINGS_INVALID
    assert err.value.pointer == "/model/settings/intervention_start"
    assert parse_window(WEEKS, "2024-01-09", None, min_pre=2).start == 2
    with pytest.raises(MapiError) as err:
        parse_window(WEEKS, "2024-03-01", None)  # after the data
    assert err.value.pointer == "/model/settings/intervention_start"
    with pytest.raises(MapiError) as err:
        parse_window(WEEKS, "2024-01-23", "2024-01-24")  # no period in the window
    assert err.value.pointer == "/model/settings/intervention_end"
    with pytest.raises(MapiError):
        parse_window(WEEKS, "10", None)


def test_label_value() -> None:
    assert label_value("North East / 2") == "North_East___2"
    assert label_value("") == "_"
    assert len(label_value("x" * 100)) == 63


def test_unique_labels_keep_units_apart() -> None:
    """Regression: 'store 1' and 'store_1' (or two names with the same first 63 characters)
    gave one label, so their per-unit metrics overwrote each other."""
    long = "m" * 70
    names = unique_labels(["store 1", "store_1", "store_1_2", long + "a", long + "b", "b"])
    assert names["store 1"] == "store_1"
    assert names["store_1"] == "store_1_2"
    assert names["store_1_2"] == "store_1_2_2"
    assert names[long + "a"] == "m" * 63
    assert names[long + "b"] == "m" * 61 + "_2"
    assert len(set(names.values())) == len(names) == 6
    assert unique_labels(["a", "a"]) == {"a": "a"}


def test_check_time() -> None:
    CausalBase.check_time(WEEKS)
    with pytest.raises(MapiError) as err:
        CausalBase.check_time(WEEKS[::-1])
    assert err.value.code == ErrorCode.SCHEMA_INVALID


def test_thin_samples() -> None:
    post = xr.Dataset(
        {"a": (("chain", "draw"), np.arange(2 * 400.0).reshape(2, 400))},
        coords={"chain": [0, 1], "draw": np.arange(400)},
    )
    out = thin_samples(post)
    assert out.sizes == {"sample": IMPACT_DRAWS}
    assert len(np.unique(out["a"].values)) == IMPACT_DRAWS
    assert thin_samples(post, 10, seed=1).sizes == {"sample": 10}


# ------------------------------------------------------------------------------ impact group


def _window(start: int, end: int, n: int) -> Window:
    return Window(start, end, str(start), "" if end == n - 1 else str(end), end == n - 1)


def test_impact_group_single_series() -> None:
    n = 6
    actual = np.array([1.0, 2.0, 3.0, 5.0, np.nan, 7.0])
    cf = np.tile(np.array([1.0, 2.0, 3.0, 4.0, 4.0, 4.0]), (3, 1))
    w = _window(3, 5, n)
    g = impact_group(
        WEEKS, actual, cf, w.post_mask(n), units=None, window=w, model_ref="m@1", units_label="kg"
    )
    assert g["counterfactual"].dims == ("sample", "time")
    assert g["actual"].dims == ("time",) and g["post"].dims == ("time",)
    assert "unit" not in g.dims
    np.testing.assert_allclose(g["effect"].values[0], [0, 0, 0, 1, np.nan, 3])
    cum = g["cumulative_effect"].values[0]
    assert np.isnan(cum[:3]).all()
    np.testing.assert_allclose(cum[3:], [1, 1, 4])
    assert g.attrs == {
        "intervention_start": "3",
        "intervention_end": "",
        "model": "m@1",
        "units": "kg",
        "effect_kind": "predictive",
    }


def test_impact_group_with_units_and_per_unit_post() -> None:
    actual = np.ones((2, 6))
    cf = np.zeros((4, 2, 6))
    post = np.zeros((2, 6), dtype=bool)
    post[0, 3:] = True
    post[1, 4:] = True
    w = _window(3, 5, 6)
    g = impact_group(
        WEEKS, actual, cf, post, units=["a", "b"], window=w, model_ref="m@1", units_label=None
    )
    assert g["counterfactual"].dims == ("sample", "unit", "time")
    assert g["post"].dims == ("unit", "time")
    assert g["unit"].values.tolist() == ["a", "b"]
    assert g.attrs["units"] == ""
    np.testing.assert_allclose(g["cumulative_effect"].sel(unit="a").values[0, 3:], [1, 2, 3])
    np.testing.assert_allclose(g["cumulative_effect"].sel(unit="b").values[0, 4:], [1, 2])
    assert np.isnan(g["cumulative_effect"].sel(unit="b").values[:, :4]).all()


# ------------------------------------------------------------------------------ summaries


def _hand_built() -> xr.Dataset:
    """Pre-period cf off by +-1, post effect = 2 per cell for draw 0, 4 for draw 1."""
    actual = np.array([10.0, 10.0, 10.0, 12.0, 12.0, np.nan])
    cf = np.array(
        [
            [11.0, 9.0, 10.0, 10.0, 10.0, 10.0],
            [11.0, 9.0, 10.0, 8.0, 8.0, 8.0],
        ]
    )
    w = _window(3, 5, 6)
    return impact_group(
        WEEKS, actual, cf, w.post_mask(6), units=None, window=w, model_ref="m@1", units_label=""
    )


def test_effect_summary_numbers() -> None:
    s = effect_summary(_hand_built())
    totals = np.array([4.0, 8.0])  # two observed post cells
    assert s["total"]["mean"] == pytest.approx(6.0)
    assert s["total"]["sd"] == pytest.approx(float(totals.std(ddof=1)))
    assert s["total"]["p05"] == pytest.approx(float(np.quantile(totals, 0.05)))
    assert s["total"]["p95"] == pytest.approx(float(np.quantile(totals, 0.95)))
    assert s["total"]["p_positive"] == 1.0
    assert s["average"]["mean"] == pytest.approx(3.0)
    assert s["relative"]["mean"] == pytest.approx(np.mean([4 / 20, 8 / 16]))
    assert s["pre_rmse"] == pytest.approx(np.sqrt(2 / 3))  # cf mean = [11, 9, 10]
    assert s["pre_mape"] == pytest.approx(np.mean([0.1, 0.1, 0.0]))
    assert (s["n_pre"], s["n_post"], s["n_treated"]) == (3, 2, 1)
    assert s["by_unit"] == {}


def test_effect_summary_by_unit() -> None:
    one = _hand_built()
    two = xr.concat([one, one], dim="unit").assign_coords(unit=["a", "b"])
    s = effect_summary(two)
    assert s["total"]["mean"] == pytest.approx(12.0)
    assert s["n_treated"] == 2
    assert set(s["by_unit"]) == {"a", "b"}
    assert s["by_unit"]["a"]["total"]["mean"] == pytest.approx(6.0)


def test_log_effect_summary_keys() -> None:
    rec = MetricRecorder()
    ctx = FitContext(tracker=rec)
    log_effect_summary(ctx, _hand_built(), params={"bsts.trend": "level"})
    assert rec.params == {
        "causal.intervention_start": "3",
        "causal.intervention_end": "",
        "causal.n_pre": 3,
        "causal.n_post": 2,
        "causal.n_treated": 1,
        "bsts.trend": "level",
    }
    keys = {k for k, _ in rec.series()}
    assert keys == {
        "effect_total",
        "effect_total_sd",
        "effect_total_p05",
        "effect_total_p95",
        "effect_average",
        "effect_relative",
        "p_effect_positive",
        "pre_rmse",
        "pre_mape",
    }
    assert rec.series()[("effect_total", "")] == [(None, pytest.approx(6.0))]


def test_log_effect_summary_per_unit_and_no_tracker() -> None:
    one = _hand_built()
    two = xr.concat([one, one], dim="unit").assign_coords(unit=["north east", "b"])
    rec = MetricRecorder()
    log_effect_summary(FitContext(tracker=rec), two, params={})
    series = rec.series()
    for key in ("effect_total", "effect_relative", "p_effect_positive"):
        assert (key, "unit=north_east") in series and (key, "unit=b") in series
    log_effect_summary(FitContext(), two, params={})  # no tracker: no-op, no error


def test_log_effect_summary_units_that_sanitise_alike() -> None:
    one = _hand_built()
    two = xr.concat([one, one * 2], dim="unit").assign_coords(unit=["store 1", "store_1"])
    rec = MetricRecorder()
    log_effect_summary(FitContext(tracker=rec), two, params={})
    series = rec.series()
    assert series[("effect_total", "unit=store_1")] == [(None, pytest.approx(6.0))]
    assert series[("effect_total", "unit=store_1_2")] == [(None, pytest.approx(12.0))]
