"""``ts.hsgp_trend`` end to end: sentinels, anticipated-coords domain, forecasts and the
``HORIZON_EXCEEDS_FIT`` refit boundary (C08)."""

from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_ts import HSGPTrendModel
from mapi_plugin_ts.datasets import CO2_SCHEMA, CO2_SENTINEL, CO2_SPEC_YAML, co2_dataset
from mapi_plugin_ts.trend import OBS_DIM, fourier_features
from mapi_pymc.context import BuildContext
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError

pytestmark = pytest.mark.slow

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://ts/test\n"


def _req(body: str) -> object:
    return load_score_request(HEADER + body)


def test_manifest_template_takes_max_extension_from_settings() -> None:
    man = HSGPTrendModel.manifest()
    assert man.capabilities.score_modes == frozenset({"predict", "forecast", "evaluate"})
    schema = man.schema_template.resolve({"max_extension": 7})
    assert schema.dims["t"].max_extension == 7 and schema.dims["t"].policy.value == "extend"
    assert man.schema_template.resolve({}).dims["t"].max_extension == 24
    assert man.outputs["y_hat"].units_of == "y"


def test_static_validation_resolves_the_spec(registry: Registry) -> None:
    res = validate_spec(load_model_spec(CO2_SPEC_YAML), registry.catalog(), CO2_SCHEMA)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]
    rs = res.raise_for_errors()
    assert rs.model.resolved == "ts.hsgp_trend@1.0.0"
    assert rs.split_stage == "clean"  # after the stateless sentinel step
    assert rs.model.input_schema is not None
    assert rs.model.input_schema.dims["t"].max_extension == 24


def test_fit_drops_sentinel_months_from_the_likelihood(co2_bundle: Bundle, co2: xr.Dataset) -> None:
    n_missing = int((co2["co2"] == CO2_SENTINEL).sum())
    assert n_missing > 0
    groups = co2_bundle.model.groups
    obs = groups["observed_data"]["y"]
    assert obs.dims == (OBS_DIM,) and obs.sizes[OBS_DIM] == co2.sizes["date"] - n_missing
    assert bool(np.isfinite(obs).all())
    assert OBS_DIM in obs.coords
    assert co2_bundle.model.learned["missing_periods"] == n_missing
    # mu covers every month, gaps included
    assert groups["posterior"]["mu"].sizes["t"] == co2.sizes["date"]
    assert {"rmse", "mape", "coverage_90"} <= set(co2_bundle.metrics.values)
    assert co2_bundle.metrics.values["mape"] < 1.0  # percent: back in ppm, within ~1 ppm
    assert co2_bundle.metrics.values["rmse"] < 2.0


def test_forecast_starts_after_the_refit_and_is_in_ppm(
    co2_bundle: Bundle, co2: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(
        co2_bundle,
        _req("mode: forecast\nhorizon: {dim: t, periods: 12}\noutput: {draws: 10}\n"),
        registry=registry,
        return_full=True,
    )
    pred = out["predictions"].to_dataset()
    last = co2["date"].values.max()
    assert pred.sizes["date"] == 12 and pred["date"].values.min() > last
    assert pred["y_hat"].attrs["units"] == "ppm"
    assert bool(np.isfinite(pred["y_hat"]).all())
    mean = pred["y_hat"].mean(("chain", "draw"))
    finite = co2["co2"].where(co2["co2"] > 0)
    assert float(finite.isel(date=slice(-12, None)).mean()) - 5 < float(mean.mean())
    # the trend has no seasonal cycle: it varies far less month to month than y_hat
    trend = pred["trend"].mean(("chain", "draw"))
    assert float(np.abs(np.diff(trend)).max()) < float(np.abs(np.diff(mean)).max())
    assert out["draws"].to_dataset().sizes["sample"] == 10


def test_horizon_past_max_extension_needs_a_refit(co2_bundle: Bundle, registry: Registry) -> None:
    with pytest.raises(MapiError) as err:
        score_bundle(
            co2_bundle, _req("mode: forecast\nhorizon: {dim: t, periods: 25}\n"), registry=registry
        )
    assert err.value.code is ErrorCode.HORIZON_EXCEEDS_FIT
    # the same limit holds for payloads that reach past it
    future = co2_dataset(years=9).isel(date=slice(72, 72 + 30)).drop_vars("co2")
    with pytest.raises(MapiError) as err2:
        score_bundle(co2_bundle, _req("mode: predict\n"), future, registry=registry)
    assert err2.value.code is ErrorCode.HORIZON_EXCEEDS_FIT
    ok = score_bundle(
        co2_bundle, _req("mode: forecast\nhorizon: {dim: t, periods: 24}\n"), registry=registry
    )
    assert ok["summary"].to_dataset().sizes["date"] == 24


def test_basis_at_forecast_positions_is_fixed_at_fit(co2_bundle: Bundle) -> None:
    """The HSGP domain comes from the anticipated coords, so it is identical at fit time
    and at score time (where only future months are present)."""
    model = HSGPTrendModel()
    s = model.parse_settings(co2_bundle.model.settings)
    schema = co2_bundle.model.schema
    fit_t = co2_bundle.model.coords["t"]
    fit_ctx = BuildContext(phase="fit", ds=xr.Dataset(coords={"t": fit_t}), schema=schema)
    future = co2_dataset(years=8)["date"].values[fit_t.size : fit_t.size + 5]
    score_ctx = BuildContext(
        phase="score", ds=xr.Dataset(coords={"t": future}), schema=schema, fit_coords={"t": fit_t}
    )
    assert model.domain(fit_ctx, s) == model.domain(score_ctx, s)
    np.testing.assert_allclose(score_ctx.position("t"), np.arange(fit_t.size, fit_t.size + 5))


def test_fourier_features_shape() -> None:
    f = fourier_features(np.arange(24.0), 12.0, 2)
    assert f.shape == (24, 4)
    np.testing.assert_allclose(f[:12], f[12:], atol=1e-12)


def test_seed_determinism_and_metrics_attrs(co2_bundle: Bundle, registry: Registry) -> None:
    req = _req("mode: forecast\nhorizon: {dim: t, periods: 3}\noutput: {kind: predictive}\n")
    a = score_bundle(co2_bundle, req, registry=registry)
    b = score_bundle(co2_bundle, req, registry=registry)
    xr.testing.assert_identical(a["summary"].to_dataset(), b["summary"].to_dataset())
    assert json.loads(a.attrs["output_space"])["y_hat"] == "clean"


@pytest.mark.mapi_contract
def test_hsgp_trend_passes_the_contract_kit(registry: Registry, co2: xr.Dataset) -> None:
    fixture = ModelFixture(spec=CO2_SPEC_YAML, data=co2, raw_schema=CO2_SCHEMA, registry=registry)
    report = check_model(HSGPTrendModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "extend_typed_errors",
        "context_window_equivalence",
        "evaluate_path_equals_score_path",
        "output_space_and_units",
        "seed_determinism",
    } <= ran
