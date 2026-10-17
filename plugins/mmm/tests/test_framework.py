"""Our model-stage data and settings mapped onto mmm-framework (and back)."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_mmm.base import positions_of
from mapi_plugin_mmm.framework import (
    Controls,
    PanelData,
    build_configs,
    detect_controls,
    engine_plan,
    infer_frequency,
    make_model,
)
from mapi_plugin_mmm.settings import BayesianSettings, FrequentistSettings
from mapi_spec.errors import ErrorCode, MapiError


def _panel(stage: xr.Dataset, pool: list[str], **kw: object) -> PanelData:
    controls = detect_controls(stage, None)
    return PanelData.from_dataset(stage, pool, controls, with_target=True, **kw)  # type: ignore[arg-type]


def test_controls_are_detected_and_flattened(geo_stage: xr.Dataset) -> None:
    assert detect_controls(geo_stage, None) == ["price", "season"]
    data = _panel(geo_stage, ["geo"])
    assert data.controls.variables == ("price", "season")
    assert data.controls.features == ("price", "season[mode=cos]", "season[mode=sin]")
    assert data.controls.block("season") == [1, 2]
    assert data.spend.shape == (52, 3, 1, 2)
    assert data.ctrl.shape == (52, 3, 1, 3)
    # the national season feature is broadcast to every geo
    np.testing.assert_array_equal(data.ctrl[:, 0, 0, 1], data.ctrl[:, 2, 0, 1])
    with pytest.raises(MapiError) as err:
        detect_controls(geo_stage, ["nope"])
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


def test_fit_data_round_trips(geo_stage: xr.Dataset) -> None:
    data = _panel(geo_stage, ["geo"])
    group = data.to_dataset()
    back = PanelData.from_fit_data(group, Controls.from_json(data.controls.to_json()))
    np.testing.assert_array_equal(back.spend, data.spend)
    np.testing.assert_array_equal(back.ctrl, data.ctrl)
    np.testing.assert_array_equal(back.y, data.y)  # type: ignore[arg-type]
    assert back.geos == data.geos and back.products is None and back.panel == ("geo",)
    np.testing.assert_array_equal(back.times, data.times)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda ds: ds.drop_vars("spend"), ErrorCode.SCHEMA_INVALID),
        (lambda ds: ds.assign(y=ds.y.where(ds.time != ds.time[3])), ErrorCode.SCHEMA_INVALID),
        (lambda ds: ds.assign(spend=-ds.spend), ErrorCode.SCHEMA_INVALID),
        (lambda ds: ds.isel(time=slice(None, None, -1)), ErrorCode.SCHEMA_INVALID),
        (lambda ds: ds.assign_coords(channel=["baseline", "search"]), ErrorCode.SCHEMA_INVALID),
    ],
)
def test_bad_data_is_refused(
    geo_stage: xr.Dataset, change: Callable[[xr.Dataset], xr.Dataset], code: ErrorCode
) -> None:
    with pytest.raises(MapiError) as err:
        _panel(change(geo_stage), ["geo"])
    assert err.value.code is code


def test_the_panel_dims_must_match_the_spend_dims(geo_stage: xr.Dataset) -> None:
    with pytest.raises(MapiError, match=r"hierarchy\.pool_over"):
        _panel(geo_stage, [])


def test_missing_values_in_padded_cells_are_filled_but_not_in_requested_ones(
    geo_stage: xr.Dataset,
) -> None:
    ds = geo_stage.drop_vars("y")
    ds["spend"] = ds.spend.where(ds.geo != "west")
    ds = ds.assign_coords(geo__requested=("geo", [True, True, False]))
    data = PanelData.from_dataset(
        ds, ["geo"], ["price", "season"], with_target=False, fill_missing=True
    )
    assert np.isfinite(data.spend).all() and (data.spend[:, 2] == 0).all()
    ds = ds.assign_coords(geo__requested=("geo", [True, True, True]))
    with pytest.raises(MapiError) as err:
        PanelData.from_dataset(
            ds, ["geo"], ["price", "season"], with_target=False, fill_missing=True
        )
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


@pytest.mark.parametrize(
    ("dates", "freq"),
    [
        (pd.date_range("2024-01-07", periods=10, freq="W-SUN"), "W"),
        (pd.date_range("2024-01-01", periods=10, freq="D"), "D"),
        (pd.date_range("2024-01-01", periods=10, freq="MS"), "M"),
    ],
)
def test_frequency_is_read_from_the_dates(dates: pd.DatetimeIndex, freq: str) -> None:
    assert infer_frequency(dates.values) == freq


def test_integer_time_labels_need_a_frequency() -> None:
    with pytest.raises(MapiError) as err:
        infer_frequency(np.arange(10))
    assert err.value.pointer == "/model/settings/frequency"
    assert infer_frequency(np.arange(10), "W") == "W"


def test_positions_inside_and_after_the_fit_window() -> None:
    fit = pd.date_range("2024-01-07", periods=10, freq="W-SUN").values
    score = pd.date_range("2024-02-04", periods=10, freq="W-SUN").values
    np.testing.assert_array_equal(positions_of(fit, score, "W"), np.arange(4, 14))
    months = pd.date_range("2024-01-01", periods=6, freq="MS").values
    later = pd.date_range("2024-05-01", periods=4, freq="MS").values
    np.testing.assert_array_equal(positions_of(months, later, "M"), [4, 5, 6, 7])
    np.testing.assert_array_equal(
        positions_of(np.arange(0, 20, 2), np.array([18, 20]), "W"), [9, 10]
    )


def test_positions_refuse_gaps_and_labels_before_the_fit() -> None:
    fit = pd.date_range("2024-01-07", periods=10, freq="W-SUN").values
    gap = pd.date_range("2024-04-07", periods=2, freq="W-SUN").values
    with pytest.raises(MapiError) as err:
        positions_of(fit, gap, "W")
    assert err.value.code is ErrorCode.NON_CONTIGUOUS_EXTENSION
    before = pd.date_range("2023-12-03", periods=2, freq="W-SUN").values
    with pytest.raises(MapiError) as err:
        positions_of(fit, before, "W")
    assert err.value.code is ErrorCode.COORD_NOT_ALLOWED
    off = (pd.date_range("2024-01-08", periods=2, freq="W-MON")).values
    with pytest.raises(MapiError) as err:
        positions_of(fit, off, "W")
    assert err.value.code is ErrorCode.COORD_NOT_ALLOWED


def test_settings_map_one_to_one_onto_mmm_framework(geo_stage: xr.Dataset) -> None:
    from mmm_framework.config import AdstockType, InferenceMethod, SaturationType
    from mmm_framework.model.trend_config import TrendType

    data = _panel(geo_stage, ["geo"])
    s = BayesianSettings.model_validate(
        {
            "hierarchy": {"pool_over": ["geo"], "media_by_geo": True, "media_geo_sigma": 0.2},
            "adstock": {"kind": "delayed", "l_max": 6, "normalize": False},
            "saturation": {"kind": "hill"},
            "channels": {"search": {"saturation": {"kind": "root"}}},
            "trend": {"kind": "spline", "n_knots": 7},
            "seasonality": {"yearly_order": 3, "prior_sigma": 0.5},
            "priors": {
                "roi": {"tv": {"mu": 0.4, "sigma": 0.3}},
                "intercept": {"mu": 0.1, "sigma": 0.2},
            },
            "likelihood": "student_t",
        }
    )
    plan = engine_plan("numpyro", draws=10, tune=5, chains=3, target_accept=0.95, seed=9,
                       default_engine="nutpie")  # fmt: skip
    mff, model, trend = build_configs(data, s, frequency="W", plan=plan)
    tv, search = mff.media_channels
    assert tv.adstock.type is AdstockType.DELAYED and tv.adstock.l_max == 6
    assert not tv.adstock.normalize
    assert tv.saturation.type is SaturationType.HILL
    assert search.saturation.type is SaturationType.ROOT
    assert (tv.roi_prior_mu, tv.roi_prior_sigma) == (0.4, 0.3)
    assert search.roi_prior_mu is None
    assert [c.name for c in mff.controls] == list(data.controls.features)
    assert model.inference_method is InferenceMethod.BAYESIAN_NUMPYRO
    assert (model.n_chains, model.n_draws, model.n_tune) == (3, 10, 5)
    assert model.target_accept == 0.95 and model.optim_seed == 9
    assert model.hierarchical.pool_across_geo and model.hierarchical.vary_media_by_geo
    assert model.hierarchical.media_geo_sigma == 0.2
    assert model.seasonality.yearly == 3 and model.seasonality.prior_sigma == 0.5
    assert (model.intercept_prior_mu, model.intercept_prior_sigma) == (0.1, 0.2)
    assert model.media_prior_mode == "roi"
    assert model.likelihood.family.value == "student_t"
    assert trend.type is TrendType.SPLINE and trend.n_knots == 7
    assert mff.frequency == "W"


def test_frequentist_settings_select_the_ridge_estimator(geo_stage: xr.Dataset) -> None:
    from mmm_framework.config import InferenceMethod

    data = _panel(geo_stage, ["geo"])
    s = FrequentistSettings.model_validate(
        {"hierarchy": {"pool_over": ["geo"]}, "bootstrap": 50, "search_budget": 30}
    )
    _, model, _ = build_configs(data, s, frequency="W")
    assert model.inference_method is InferenceMethod.FREQUENTIST_RIDGE
    assert model.bootstrap_samples == 50 and model.optim_maxiter == 30


def test_unknown_channels_in_settings_are_refused(geo_stage: xr.Dataset) -> None:
    data = _panel(geo_stage, ["geo"])
    with pytest.raises(MapiError) as err:
        build_configs(
            data,
            BayesianSettings.model_validate({"priors": {"roi": {"radio": {"mu": 0, "sigma": 1}}}}),
            frequency="W",
        )
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    with pytest.raises(MapiError):
        build_configs(
            data, BayesianSettings.model_validate({"channels": {"radio": {}}}), frequency="W"
        )


def test_unknown_engines_are_refused() -> None:
    with pytest.raises(MapiError) as err:
        engine_plan("blackjax", draws=1, tune=1, chains=1, target_accept=None, seed=0,
                    default_engine="nutpie")  # fmt: skip
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    plan = engine_plan(None, draws=None, tune=None, chains=None, target_accept=None, seed=1,
                       default_engine="nutpie")  # fmt: skip
    assert (plan.method, plan.nuts_sampler, plan.draws, plan.chains) == ("nuts", "nutpie", 1000, 4)
    assert engine_plan("advi", draws=5, tune=None, chains=None, target_accept=None, seed=1,
                       default_engine="nutpie").method == "advi"  # fmt: skip


def _mff_frame(rng: np.random.Generator) -> tuple[pd.DataFrame, dict[str, np.ndarray]]:
    periods = pd.date_range("2024-01-01", periods=20, freq="W-MON")
    geos = ["East", "West"]
    rows = []
    values: dict[str, np.ndarray] = {}
    for var in ("Sales", "TV", "Search", "Price"):
        arr = rng.gamma(4.0, 50.0, (len(periods), len(geos)))
        values[var] = arr
        for i, p in enumerate(periods):
            for j, g in enumerate(geos):
                rows.append(
                    {
                        "Period": p.strftime("%Y-%m-%d"),
                        "Geography": g,
                        "Product": "All",
                        "Outlet": "All",
                        "Campaign": "All",
                        "Creative": "All",
                        "VariableName": var,
                        "VariableValue": float(arr[i, j]),
                    }
                )
    return pd.DataFrame(rows), values


def test_our_mff_ingest_agrees_with_mmm_frameworks_mff_loader() -> None:
    """An MFF file ingested with our ``mff`` preset (then bound like the Guided geo
    template binds it) gives the same panel as mmm-framework's own ``load_mff``."""
    ingest = pytest.importorskip("mapi_runner.worker.ingest")
    from mapi_spec.ingest import MffOptions
    from mmm_framework.config import (
        ControlVariableConfig,
        DimensionType,
        KPIConfig,
        MediaChannelConfig,
        MFFConfig,
    )
    from mmm_framework.data_loader import load_mff

    df, _ = _mff_frame(np.random.default_rng(4))
    ours, _ = ingest.dataset_from_mff(df, MffOptions())
    stage = xr.Dataset(
        {
            "y": ours["Sales"].transpose("geo", "date"),
            "spend": xr.concat([ours["TV"], ours["Search"]], dim="channel")
            .assign_coords(channel=["TV", "Search"])
            .transpose("geo", "channel", "date"),
            "Price": ours["Price"].transpose("geo", "date"),
        }
    ).rename({"date": "time"})
    data = PanelData.from_dataset(stage, ["geo"], ["Price"], with_target=True)
    dims = [DimensionType.PERIOD, DimensionType.GEOGRAPHY]
    cfg = MFFConfig(
        kpi=KPIConfig(name="Sales", dimensions=dims),
        media_channels=[MediaChannelConfig(name=c, dimensions=dims) for c in ("TV", "Search")],
        controls=[ControlVariableConfig(name="Price", dimensions=dims)],
    )
    theirs = load_mff(df, cfg)
    mine = data.to_panel(cfg, "W")
    np.testing.assert_allclose(mine.y.values, theirs.y.values)
    np.testing.assert_allclose(
        mine.X_media[["TV", "Search"]].values, theirs.X_media[["TV", "Search"]].values
    )
    np.testing.assert_allclose(mine.X_controls.values, theirs.X_controls.values)  # type: ignore[union-attr]
    assert list(mine.index) == list(theirs.index)
    assert mine.coords.geographies == theirs.coords.geographies


def test_national_media_and_campaign_rows_ingest_like_mmm_frameworks_mff_loader() -> None:
    """Regression: national rows (blank Geography) became a phantom '' geo (TV only there,
    sales NaN there) and rows that differ only by Campaign became a campaign dim, while
    mmm-framework's ``MFFLoader`` spreads a national variable across the geos (equal
    weights by default) and sums Campaign/Outlet/Creative up to the modelled dims."""
    ingest = pytest.importorskip("mapi_runner.worker.ingest")
    from mapi_spec.ingest import MffOptions
    from mmm_framework.config import (
        DimensionType,
        KPIConfig,
        MediaChannelConfig,
        MFFConfig,
    )
    from mmm_framework.data_loader import load_mff

    rng = np.random.default_rng(7)
    periods = pd.date_range("2024-01-01", periods=12, freq="W-MON")
    geos = ["East", "West"]
    rows: list[dict[str, object]] = []

    def row(p: pd.Timestamp, geo: str, var: str, value: float, campaign: str = "") -> None:
        rows.append({"Period": p.strftime("%Y-%m-%d"), "Geography": geo, "Product": "",
                     "Outlet": "", "Campaign": campaign, "Creative": "",
                     "VariableName": var, "VariableValue": value})  # fmt: skip

    for p in periods:
        for g in geos:
            row(p, g, "Sales", float(rng.gamma(4.0, 50.0)))
            for camp in ("brand", "promo"):
                row(p, g, "Search", float(rng.gamma(2.0, 10.0)), campaign=camp)
        row(p, "", "TV", float(rng.gamma(3.0, 40.0)))  # national
    df = pd.DataFrame(rows)

    ours, _ = ingest.dataset_from_mff(df, MffOptions(roles={"Sales": "target"}))
    assert [str(g) for g in ours["geo"].values] == geos  # no phantom '' geo
    assert "campaign" not in ours.dims
    assert not np.isnan(ours["TV"].values).any() and not np.isnan(ours["Sales"].values).any()

    by_geo = [DimensionType.PERIOD, DimensionType.GEOGRAPHY]
    cfg = MFFConfig(
        kpi=KPIConfig(name="Sales", dimensions=by_geo),
        media_channels=[
            MediaChannelConfig(name="TV", dimensions=[DimensionType.PERIOD]),
            MediaChannelConfig(name="Search", dimensions=by_geo),
        ],
    )
    theirs = load_mff(df, cfg)
    stage = xr.Dataset(
        {
            "y": ours["Sales"].transpose("geo", "date"),
            "spend": xr.concat([ours["TV"], ours["Search"]], dim="channel")
            .assign_coords(channel=["TV", "Search"])
            .transpose("geo", "channel", "date"),
        }
    ).rename({"date": "time"})
    mine = PanelData.from_dataset(stage, ["geo"], [], with_target=True).to_panel(cfg, "W")
    np.testing.assert_allclose(mine.y.values, theirs.y.values)
    np.testing.assert_allclose(
        mine.X_media[["TV", "Search"]].values, theirs.X_media[["TV", "Search"]].values
    )


def test_a_built_model_sees_our_layout(geo_stage: xr.Dataset) -> None:
    data = _panel(geo_stage, ["geo"])
    s = BayesianSettings.model_validate({"hierarchy": {"pool_over": ["geo"]}})
    model = make_model(data, s, frequency="W")
    assert model.n_obs == 52 * 3 and model.n_cells == 3 and model.n_periods == 52
    # observations are period-major, then geo (mmm-framework's MFFLoader layout)
    np.testing.assert_allclose(model.y_raw[:3], data.y[0, :, 0])  # type: ignore[index]
    np.testing.assert_allclose(model.X_media_raw[3:6, 1], data.spend[1, :, 0, 1])
    assert model.geo_idx[:6].tolist() == [0, 1, 2, 0, 1, 2]
    assert model.time_idx[:6].tolist() == [0, 0, 0, 1, 1, 1]
