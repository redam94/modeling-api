"""``mmm.pymc_marketing``: pymc-marketing 1.2's MMM (on PyMC 6) behind the platform
contract - it owns scaling, carry-over and the inverse to original units."""

# ruff: noqa: E501

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_mmm.examples import E33Truth, e33_dataset, e33_raw_schema, future_daily
from mapi_plugin_mmm.pymc_marketing import PyMCMarketingMMM, pymc_marketing_available
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import ModelSpec, load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not pymc_marketing_available(), reason="pymc-marketing not installed"),
]

MARKETS = ("NL", "DE", "UK")
CHANNELS = ("tv", "search")
WEEKS = 30
SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: pmm-geo, project: growth}
data:
  dataset: dataset://growth/eu_media_daily@latest
  side_tables:
    fx:       {ref: dataset://growth/fx_daily@latest, score_policy: pinned}
    holidays: {ref: dataset://shared/holidays_eu@v3, score_policy: pinned}
  bind:
    dims: {geo: market, channel: channel, time: date}
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 4, after_step: weekly}
pipeline:
  - {id: to_eur, use: core.fx_convert@^1, with: {vars: [y, spend], table: fx, table_along: date, currency_dim: geo, currencies: {UK: GBP}, default_currency: EUR}}
  - {id: weekly, use: core.resample@^1,   with: {dim: time, freq: W-SUN, how: sum, full_periods_only: true}}
  - {id: hols,   use: core.holidays@^1,   with: {table: holidays, table_along: date, along: time}}
model:
  use: mmm.pymc_marketing@^1
  settings: {dims: [geo], adstock: {l_max: 4}, controls: [holiday], yearly_seasonality: 1}
  fit: {mode: evaluate_then_refit, seed: 3, tune: 100, draws: 60, chains: 2, engine: pymc}
evaluate: {metrics: [mape, coverage_90]}
"""
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/pmm-geo\n"


@pytest.fixture(scope="module")
def data() -> tuple[xr.Dataset, E33Truth]:
    return e33_dataset(markets=MARKETS, channels=CHANNELS, weeks=WEEKS, seed=2)


@pytest.fixture(scope="module")
def spec() -> ModelSpec:
    return load_model_spec(SPEC)


@pytest.fixture(scope="module")
def pmm(
    spec: ModelSpec, data: tuple[xr.Dataset, E33Truth], tables: dict, registry: Registry
) -> Bundle:
    return fit_spec(spec, data[0], tables, registry, raw_schema=e33_raw_schema(data[0]))


def _pred(tree: xr.DataTree, group: str = "predictions") -> xr.Dataset:
    node = tree[group]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


def test_static_validation_and_owned_preprocessing(
    spec: ModelSpec, data: tuple[xr.Dataset, E33Truth], registry: Registry
) -> None:
    res = validate_spec(spec, registry.catalog(), e33_raw_schema(data[0]))
    assert res.ok, [(i.pointer, i.message) for i in res.errors]
    rs = res.raise_for_errors()
    assert rs.model.resolved == "mmm.pymc_marketing@1.1.0"
    # markets are fixed for the adapter even though the data allow new levels
    assert rs.model.effective_policies["geo"].value == "fixed"
    bad = load_model_spec(SPEC.replace("engine: pymc", "engine: advi"))
    issues = validate_spec(bad, registry.catalog(), e33_raw_schema(data[0])).errors
    assert any(i.pointer == "/model/fit/engine" for i in issues)


def test_fit_stores_a_pickle_free_rebuildable_model(pmm: Bundle) -> None:
    groups = pmm.model.groups
    assert {"posterior", "sample_stats", "fit_data"} <= set(groups)
    assert "channel_contribution" not in groups["posterior"]  # recomputed at score time
    info = pmm.model.learned["pymc_marketing"]
    assert isinstance(info, dict) and info["version"].startswith("1.2")
    assert json.loads(info["attrs"]["channel_columns"]) == list(CHANNELS)
    assert pmm.metrics.output_space == "model-owned"
    assert pmm.metrics.values["mape"] < 15.0
    assert {d.code for d in pmm.diagnostics} >= {"RHAT"}


def test_forecast_in_original_units_with_additive_contributions(
    pmm: Bundle, registry: Registry, tables: dict, data: tuple[xr.Dataset, E33Truth], tmp_path: Path
) -> None:
    fut = future_daily(weeks=WEEKS, future_weeks=4, markets=MARKETS, channels=CHANNELS, seed=2)
    req = load_score_request(REQ + "mode: forecast\nhorizon: {dim: time, periods: 4}\n")
    out = score_bundle(pmm, req, fut, side_tables=tables, registry=registry, return_full=True)
    pred = _pred(out)
    y = pred["y_hat"]
    assert set(y.dims) == {"chain", "draw", "market", "date"} and y.sizes["date"] == 4
    assert json.loads(out.attrs["output_space"]) == {
        "contributions": "model-owned",
        "y_hat": "model-owned",
    }
    # original units: the weekly EUR revenue level, not pymc-marketing's scaled target
    weekly_eur = data[1].weekly["revenue_eur"].mean().item()
    assert 0.5 * weekly_eur < float(y.mean()) < 2.0 * weekly_eur
    total = pred["contributions"].sum("component")
    np.testing.assert_allclose(total.transpose(*y.dims), y, rtol=1e-8)
    assert list(pred["contributions"]["component"].values) == [
        "tv",
        "search",
        "baseline",
        "holiday",
        "seasonality",
    ]
    pmm.write(tmp_path)
    back = Bundle.read(tmp_path, registry)
    again = score_bundle(back, req, fut, side_tables=tables, registry=registry, return_full=True)
    xr.testing.assert_allclose(_pred(again)["y_hat"], y)


def test_the_adapter_owns_carryover(
    pmm: Bundle, registry: Registry, tables: dict, data: tuple[xr.Dataset, E33Truth]
) -> None:
    """A window scored alone matches the same weeks of a full-series score: the adapter
    prepends the fit history before the window (no platform lag context exists)."""
    req = load_score_request(REQ + "mode: predict\n")
    full = score_bundle(pmm, req, data[0], side_tables=tables, registry=registry, return_full=True)
    window = data[0].isel(date=slice(-7 * 6, None))
    part = score_bundle(pmm, req, window, side_tables=tables, registry=registry, return_full=True)
    a = _pred(part)["y_hat"]
    b = _pred(full)["y_hat"].sel(date=a["date"])
    xr.testing.assert_allclose(a, b, rtol=1e-8)


def test_counterfactual_moves_the_treated_channel_only(
    pmm: Bundle, registry: Registry, tables: dict, data: tuple[xr.Dataset, E33Truth]
) -> None:
    req = load_score_request(
        REQ
        + "mode: counterfactual\n"
        + "interventions: [{var: spend_local, sel: {channel: search}, multiply: 1.5}]\n"
        + "output: {kind: predictive, draws: 20}\n"
    )
    window = data[0].isel(date=slice(-28, None))
    out = score_bundle(pmm, req, window, side_tables=tables, registry=registry, return_full=True)
    delta = _pred(out, "delta/predictions")
    assert float(delta["contributions"].sel(component="search").mean()) > 0
    rest = delta["contributions"].sel(component=["tv", "baseline", "holiday", "seasonality"])
    assert float(abs(rest).max()) < 1e-6
    # predictive noise comes from the same seeded stream in scenario and baseline
    np.testing.assert_allclose(
        delta["y_hat"],
        delta["contributions"].sum("component").transpose(*delta["y_hat"].dims),
        atol=1e-6,
    )


def test_unsupported_engines_are_typed(
    spec: ModelSpec, data: tuple[xr.Dataset, E33Truth], tables: dict, registry: Registry
) -> None:
    from mapi_sdk.models import FitContext
    from mapi_spec.spec import FitConfig

    plugin = PyMCMarketingMMM()
    s = plugin.parse_settings({"dims": ["geo"], "adstock": {"l_max": 4}})
    ds = xr.Dataset(
        {
            "y": (("geo", "time"), np.ones((1, 3))),
            "spend": (("geo", "channel", "time"), np.ones((1, 1, 3))),
        },
        coords={"geo": ["a"], "channel": ["tv"], "time": np.arange(3)},
    )
    with pytest.raises(MapiError) as err:
        plugin.fit(ds, s, FitConfig(engine="advi"), FitContext())
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    clash = ds.assign_coords(channel=["y"])
    with pytest.raises(MapiError, match="clash"):
        plugin.fit(clash, s, FitConfig(engine="pymc"), FitContext())


@pytest.mark.slow
@pytest.mark.mapi_contract
def test_pymc_marketing_adapter_passes_the_contract_kit(
    data: tuple[xr.Dataset, E33Truth], tables: dict, registry: Registry
) -> None:
    fixture = ModelFixture(
        spec=SPEC,
        data=data[0],
        side_tables=tables,
        raw_schema=e33_raw_schema(data[0]),
        registry=registry,
        window=28,
    )
    report = check_model(PyMCMarketingMMM, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "shuffled_scoring",
        "subset_scoring",
        "new_level_policies",
        "extend_typed_errors",
        "context_window_equivalence",
        "evaluate_path_equals_score_path",
        "representation_aggregation",
        "additive_contributions",
    } <= ran
    # a market unseen at fit is rejected: pymc-marketing cannot score new panel levels
    assert "UNSEEN_LEVELS" in report.get("new_level_policies").detail


# ------------------------------------------------------------------------- lift tests (M3)

LIFT = """
calibration:
  lift_tests:
    - {id: tv-de, channel: tv, geo: DE, start: 2023-03-06, end: 2023-04-02,
       spend_delta: -4000, kpi_delta: -6000, se: 1500}
"""


def _lift_inputs() -> tuple[object, object, list[str]]:
    import pandas as pd
    from mapi_plugin_mmm.settings import PyMCMarketingSettings

    dates = pd.date_range("2023-03-05", periods=6, freq="W-SUN")
    frame = pd.DataFrame(
        {
            "date": list(dates) * 2,
            "geo": ["DE"] * 6 + ["NL"] * 6,
            "tv": [100.0] * 12,
            "search": [50.0] * 12,
            "y": [1000.0] * 12,
        }
    )
    s = PyMCMarketingSettings.model_validate({"dims": ["geo"], "adstock": {"l_max": 2}})
    return frame, s, ["tv", "search"]


def test_lift_tests_become_per_period_rows() -> None:
    from datetime import date

    from mapi_plugin_mmm.pymc_marketing import lift_frame
    from mapi_spec.calibration import LiftTest

    frame, s, channels = _lift_inputs()
    base = {"channel": "tv", "geo": "DE", "start": date(2023, 3, 6), "end": date(2023, 4, 2),
            "spend_delta": -200.0, "kpi_delta": -300.0, "se": 40.0}  # fmt: skip
    df = lift_frame([LiftTest.model_validate(base)], frame, s, channels)  # type: ignore[arg-type]
    assert df.to_dict("records") == [
        {
            "channel": "tv",
            "x": 100.0,
            "delta_x": -50.0,
            "delta_y": -75.0,
            "sigma": 10.0,
            "geo": "DE",
        }
    ]
    later = LiftTest.model_validate({**base, "start": date(2024, 1, 1), "end": date(2024, 2, 1)})
    assert lift_frame([later], frame, s, channels).empty  # type: ignore[arg-type]
    for bad, pointer in (
        ({"geo": None}, "/calibration/lift_tests/0/geo"),
        ({"geo": "FR"}, "/calibration/lift_tests/0/geo"),
        ({"channel": "radio"}, "/calibration/lift_tests/0/channel"),
        ({"spend_delta": -5000.0}, "/calibration/lift_tests/0/spend_delta"),
    ):
        with pytest.raises(MapiError) as err:
            lift_frame([LiftTest.model_validate({**base, **bad})], frame, s, channels)  # type: ignore[arg-type]
        assert err.value.pointer == pointer


@pytest.mark.slow
def test_a_lift_test_calibrates_the_adapter(
    data: tuple[xr.Dataset, E33Truth], tables: dict, registry: Registry
) -> None:
    spec = load_model_spec(SPEC.replace("mode: evaluate_then_refit", "mode: train_only") + LIFT)
    assert "lift_tests" in PyMCMarketingMMM.manifest().capabilities.calibration
    b = fit_spec(spec, data[0], tables, registry, raw_schema=e33_raw_schema(data[0]))
    assert b.model.learned["calibration"] == {"lift_rows": 1}
    # pymc-marketing's lift likelihood conditioned the fit
    assert "lift_measurements" in b.model.groups["observed_data"]


# ========================================================================= business priors

BELIEFS = {
    "channels": {"tv": {"roi": [1.5, 4.0], "credibility": 0.8},
                 "search": {"contribution_share": [0.02, 0.10], "credibility": 0.9}},
    "baseline_share": {"share": [0.5, 0.8], "credibility": 0.8},
}  # fmt: skip


def test_business_priors_translate_to_pymc_marketing_priors(
    data: tuple[xr.Dataset, E33Truth], tables: dict, registry: Registry
) -> None:
    from mapi_plugin_mmm.settings import PyMCMarketingSettings
    from mapi_sdk.harness import prepare_model_stage
    from mapi_spec.calibration import BusinessPriors, PriorTranslationJob

    assert "business_priors" in PyMCMarketingMMM.manifest().capabilities.calibration
    stage = prepare_model_stage(SPEC, data[0], tables, registry, raw_schema=e33_raw_schema(data[0]))
    s = PyMCMarketingSettings.model_validate(stage.settings)
    job = PriorTranslationJob(spec={}, business_priors=BusinessPriors.model_validate(BELIEFS),
                              draws=400, seed=3)  # fmt: skip
    out = PyMCMarketingMMM().translate_priors(stage.data, s, job, stage.fit_context)
    priors = out.result.priors
    beta = priors["saturation_beta"]
    assert beta["distribution"] == "LogNormal" and beta["dims"] == ["channel"]
    assert len(beta["mu"]) == 2 and out.result.channels == ["tv", "search"]  # type: ignore[arg-type]
    assert priors["intercept"]["distribution"] == "Normal"
    # the stated ranges hold the stated prior mass (Monte Carlo error aside)
    cov = out.result.coverage
    assert abs(cov["tv"] - 0.8) < 0.08 and abs(cov["search"] - 0.9) < 0.06
    assert abs(cov["baseline"] - 0.8) < 0.1
    # pymc-marketing's wide seasonality prior had to be narrowed to reach the baseline range
    assert "gamma_fourier" in priors and "gamma_control" in priors
    assert any("narrowed" in i.message for i in out.result.issues)
    preview = out.preview
    assert set(preview.data_vars) >= {"roi", "contribution_share", "baseline_share", "kpi"}
    assert out.result.preview.kpi_fan is not None
    tv = out.result.preview.roi["tv"]
    assert 1.0 < tv.quantiles["0.5"] < 4.0 and tv.units == "EUR per EUR"
    # the priors apply: settings validate and become pymc-marketing model_config priors
    applied = PyMCMarketingSettings.model_validate(
        {**s.model_dump(mode="json", exclude_none=True), "priors": priors}
    )
    from mapi_plugin_mmm.pymc_marketing import _model_config

    config = _model_config(applied, 2)
    assert config["saturation_beta"].dims == ("geo", "channel")
    assert config["intercept"].dims == ("geo",)
    assert config["gamma_fourier"].dims == ("geo", "fourier_mode")


def test_unknown_channels_in_business_priors_are_errors(
    data: tuple[xr.Dataset, E33Truth], tables: dict, registry: Registry
) -> None:
    from mapi_plugin_mmm.settings import PyMCMarketingSettings
    from mapi_sdk.harness import prepare_model_stage
    from mapi_spec.calibration import BusinessPriors, PriorTranslationJob
    from mapi_spec.errors import Severity

    stage = prepare_model_stage(SPEC, data[0], tables, registry, raw_schema=e33_raw_schema(data[0]))
    s = PyMCMarketingSettings.model_validate(stage.settings)
    beliefs = BusinessPriors.model_validate({"channels": {"radio": {"roi": [1, 2]}}})
    job = PriorTranslationJob(spec={}, business_priors=beliefs, draws=100, seed=1)
    out = PyMCMarketingMMM().translate_priors(stage.data, s, job, stage.fit_context)
    [issue] = [i for i in out.result.issues if i.severity is Severity.ERROR]
    assert issue.pointer == "/business_priors/channels/radio" and "radio" in issue.message
    from mapi_plugin_mmm.pymc_marketing import _model_config

    three = PyMCMarketingSettings.model_validate(
        {"priors": {"saturation_beta": {"distribution": "LogNormal", "mu": [0.0, 1.0, 2.0],
                                        "sigma": [1.0, 1.0, 1.0], "dims": ["channel"]}}}
    )  # fmt: skip
    with pytest.raises(MapiError) as exc:
        _model_config(three, 2)  # one value per channel
    assert exc.value.code is ErrorCode.SETTINGS_INVALID


@pytest.mark.slow
def test_business_priors_are_translated_at_fit_time(
    data: tuple[xr.Dataset, E33Truth], tables: dict, registry: Registry
) -> None:
    import json as _json

    cal = "calibration:\n  business_priors: " + _json.dumps(BELIEFS) + "\n"
    spec = load_model_spec(SPEC.replace("mode: evaluate_then_refit", "mode: train_only") + cal)
    b = fit_spec(spec, data[0], tables, registry, raw_schema=e33_raw_schema(data[0]))
    translated = b.model.learned["calibration"]["translated_priors"]  # type: ignore[index]
    assert translated["saturation_beta"]["distribution"] == "LogNormal"  # type: ignore[index]
    # the fitted model was built with them (pymc-marketing's own model description)
    attrs = b.model.learned["pymc_marketing"]["attrs"]  # type: ignore[index]
    config = _json.loads(attrs["model_config"])  # type: ignore[index]
    assert config["saturation_beta"]["dist"] == "LogNormal"
    # and it scores after a round trip through the bundle
    req = load_score_request(REQ + "mode: predict\noutput: {kind: expected, draws: 10}\n")
    tree = score_bundle(b, req, data[0], side_tables=tables, registry=registry)
    assert np.isfinite(tree["summary"].to_dataset()["y_hat"]).all()
