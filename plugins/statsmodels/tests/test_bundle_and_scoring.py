"""Bundles without pickle, integrity checks on rebuild, panels and the score semantics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_statsmodels import PLUGIN_IDS
from mapi_plugin_statsmodels.datasets import SALES_SCHEMA, sales_dataset, sales_schema
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_spec import load_score_request
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://tests/sm\n"


def spec(model: str, settings: str, *, split: str = "") -> str:
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: sm, project: tests}}
data:
  dataset: dataset://tests/sm@latest
  bind: {{dims: {{t: date}}, vars: {{y: sales}}}}
  {split}
model: {{use: "{model}@^1", settings: {settings}, fit: {{seed: 2}}}}
"""


def score(
    registry: Registry, bundle: Bundle, body: str, payload: xr.Dataset | None
) -> xr.DataArray:
    tree = score_bundle(
        bundle, load_score_request(HEADER + body), payload, registry=registry, return_full=True
    )
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()["y_hat"]


# ================================================================================== bundles


@pytest.mark.parametrize("model_id", PLUGIN_IDS)
def test_bundles_hold_parameters_and_data_only(
    model_id: str, bundles: dict[str, Bundle], tmp_path: object
) -> None:
    b = bundles[model_id]
    assert b.model.native == {}  # nothing opaque: no pickle, no native blobs
    groups = b.model.groups
    assert {"observed_data", "params", "fit_stats", "residuals", "residual_tests"} <= set(groups)
    assert set(groups["params"].data_vars) == {
        "estimate", "std_err", "statistic", "p_value", "ci_lower", "ci_upper",
    }  # fmt: skip
    cells = b.model.learned["cells"]
    assert cells and all(isinstance(c["params"], list) for c in cells)
    assert b.model.learned["statsmodels_version"].startswith("0.15")


def test_round_trip_through_disk_reproduces_forecasts(
    bundles: dict[str, Bundle], registry: Registry, tmp_path: pytest.TempPathFactory
) -> None:
    b = bundles["ts.var"]
    path = str(tmp_path)
    b.write(path)
    back = Bundle.read(path, registry)
    body = "mode: forecast\nhorizon: {dim: t, periods: 4}\n"
    xr.testing.assert_identical(score(registry, b, body, None), score(registry, back, body, None))


def _tamper(bundle: Bundle, param: str, delta: float) -> Bundle:
    params = bundle.model.groups["params"].copy(deep=True)
    est = params["estimate"]
    idx = list(params["param"].values).index(param)
    vals = est.values.copy()
    vals.reshape(-1, vals.shape[-1])[..., idx] += delta
    params["estimate"] = est.copy(data=vals)
    bundle.model.groups["params"] = params
    return bundle


@pytest.mark.parametrize(("model_id", "param"), [("ts.var", "const.realgdp"), ("ts.theta", "b0")])
def test_tampered_least_squares_parameters_fail_integrity(
    model_id: str, param: str, registry: Registry
) -> None:
    from mapi_plugin_statsmodels.examples import examples

    case = examples()[model_id]
    b = fit_spec(case.spec, case.data, registry=registry, raw_schema=case.schema)
    _tamper(b, param, 1.0)
    targets = [v for v in case.schema.vars if case.schema.vars[v].role.value == "target"]
    with pytest.raises(MapiError) as info:
        score(registry, b, "mode: predict\n", case.data.drop_vars(targets))
    assert info.value.code is ErrorCode.INTEGRITY_FAILURE


def test_state_space_models_apply_the_stored_parameters(registry: Registry) -> None:
    """The Kalman filter rebuild uses the stored values: changing sigma2 changes the band."""
    data = sales_dataset(periods=60, seed=8).drop_vars(["price", "promo"])
    schema = DataSchema.model_validate(
        {
            "dims": SALES_SCHEMA.model_dump()["dims"],
            "vars": {"sales": SALES_SCHEMA.model_dump()["vars"]["sales"]},
        }
    )
    b = fit_spec(spec("ts.arima", "{order: [1, 0, 0]}"), data, registry=registry, raw_schema=schema)
    body = "mode: forecast\nhorizon: {dim: t, periods: 3}\n"
    before = score(registry, b, body, None)
    _tamper(b, "sigma2", 10.0)
    after = score(registry, b, body, None)
    width = lambda da: (da.sel(quantile=0.95) - da.sel(quantile=0.05)).values  # noqa: E731
    assert (width(after) > width(before)).all()


# ==================================================================================== panels


def test_panels_fit_one_model_per_series(registry: Registry) -> None:
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    data = sales_dataset(periods=70, geos=["east", "west", "north"], seed=6)
    b = fit_spec(
        spec("ts.sarimax", "{order: [1, 0, 0], exog: [price, promo], series_dims: [geo]}"),
        data,
        registry=registry,
        raw_schema=sales_schema(geo=True),
    )
    params = b.model.groups["params"]
    assert params["estimate"].dims == ("geo", "param")
    for g in ("east", "west", "north"):
        cell = data.sel(geo=g)
        y = pd.Series(np.asarray(cell["sales"].values), name="y")
        x = pd.DataFrame({n: np.asarray(cell[n].values) for n in ("price", "promo")})
        direct = SARIMAX(y, exog=x, order=(1, 0, 0), trend="c").fit(disp=False, maxiter=200)
        got = params["estimate"].sel(geo=g, param=list(direct.model.param_names)).values
        np.testing.assert_allclose(got, np.asarray(direct.params), rtol=1e-7)
    # scoring a subset of geos returns just those, identical to the full score
    future = sales_dataset(periods=74, geos=["east", "west", "north"], seed=6).isel(
        date=slice(70, None)
    )
    body = "mode: forecast\nhorizon: {dim: t, periods: 4}\n"
    full = score(registry, b, body, future.drop_vars("sales"))
    part = score(registry, b, body, future.drop_vars("sales").sel(geo=["west"]))
    xr.testing.assert_allclose(part, full.sel(geo=["west"]))


# ============================================================================ score semantics


@pytest.fixture(scope="module")
def arimax(registry: Registry) -> tuple[Bundle, xr.Dataset]:
    data = sales_dataset(periods=72, seed=12)
    b = fit_spec(
        spec("ts.sarimax", "{order: [1, 0, 0], exog: [price, promo]}"),
        data.isel(date=slice(0, 64)),
        registry=registry,
        raw_schema=SALES_SCHEMA,
    )
    return b, data


def test_forecast_without_future_drivers_is_a_typed_error(
    arimax: tuple[Bundle, xr.Dataset], registry: Registry
) -> None:
    b, data = arimax
    future = data.isel(date=slice(64, None)).drop_vars(["sales", "promo"])
    with pytest.raises(MapiError) as info:
        score(registry, b, "mode: forecast\nhorizon: {dim: t, periods: 8}\n", future)
    assert info.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


def test_missing_future_driver_values_are_a_typed_error(
    arimax: tuple[Bundle, xr.Dataset], registry: Registry
) -> None:
    b, data = arimax
    future = data.isel(date=slice(64, None)).drop_vars("sales").copy(deep=True)
    future["price"][3] = np.nan
    with pytest.raises(MapiError) as info:
        score(registry, b, "mode: predict\n", future)
    assert info.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


def test_carry_last_fills_future_drivers(
    arimax: tuple[Bundle, xr.Dataset], registry: Registry
) -> None:
    b, data = arimax
    body = "mode: forecast\nhorizon: {dim: t, periods: 3}\nfuture_covariates: carry_last\n"
    tree = score_bundle(
        b, load_score_request(HEADER + body), None, registry=registry,
        history=data.isel(date=slice(0, 64)), return_full=True,
    )  # fmt: skip
    assert tree["predictions"].to_dataset()["y_hat"].sizes["date"] == 3


def test_in_sample_predictions_ignore_payload_targets(
    arimax: tuple[Bundle, xr.Dataset], registry: Registry
) -> None:
    b, data = arimax
    train = data.isel(date=slice(0, 64))
    a = score(registry, b, "mode: predict\n", train)
    shifted = train.assign(sales=train["sales"] + 1000.0)
    c = score(registry, b, "mode: predict\n", shifted)
    xr.testing.assert_identical(a, c)


def test_scenarios_on_future_drivers_move_the_forecast(
    arimax: tuple[Bundle, xr.Dataset], registry: Registry
) -> None:
    b, data = arimax
    future = data.isel(date=slice(64, None)).drop_vars("sales")
    base = score(registry, b, "mode: predict\n", future)
    promo = score(registry, b, "mode: predict\n", future.assign(promo=future["promo"] * 0 + 1))
    effect = float(b.model.groups["params"]["estimate"].sel(param="promo"))
    diff = (promo - base).sel(quantile=0.5).values
    np.testing.assert_allclose(diff, effect * (1 - future["promo"].values), atol=1e-8)


def test_state_space_models_accept_missing_target_values(registry: Registry) -> None:
    data = sales_dataset(periods=60, seed=13)
    data["sales"][[10, 11, 30]] = np.nan
    raw = SALES_SCHEMA.model_dump()
    raw["vars"]["sales"]["missing"] = "allow"
    b = fit_spec(
        spec("ts.sarimax", "{order: [1, 0, 0], exog: [price, promo]}"),
        data,
        registry=registry,
        raw_schema=DataSchema.model_validate(raw),
    )  # fmt: skip
    assert bool(np.isnan(b.model.groups["residuals"]["resid"].values[[10, 11, 30]]).all())


def test_complete_series_models_reject_gaps_in_plain_words(registry: Registry) -> None:
    data = sales_dataset(periods=60, seed=13).drop_vars(["price", "promo"])
    data["sales"][5] = np.nan
    schema = DataSchema.model_validate(
        {
            "dims": SALES_SCHEMA.model_dump()["dims"],
            "vars": {"sales": {**SALES_SCHEMA.model_dump()["vars"]["sales"], "missing": "allow"}},
        }
    )
    with pytest.raises(MapiError) as info:
        fit_spec(spec("ts.theta", "{}"), data, registry=registry, raw_schema=schema)
    assert "core.impute" in (info.value.untrusted_text or info.value.detail)


def test_unevenly_spaced_periods_are_refused(registry: Registry) -> None:
    from mapi_plugin_statsmodels.base import _check_regular

    weeks = sales_dataset(periods=40, seed=3)["date"].values
    _check_regular(weeks)  # regular weekly dates pass
    _check_regular(np.arange(0, 40, 2))  # so do evenly stepped integers
    for bad in (np.delete(weeks, 10), np.array([0, 1, 2, 4, 5])):
        with pytest.raises(MapiError) as info:
            _check_regular(bad)
        assert info.value.code is ErrorCode.SCHEMA_INVALID
        assert "core.resample" in info.value.detail


def test_orders_too_large_for_the_history_are_settings_errors(registry: Registry) -> None:
    data = sales_dataset(periods=20, seed=1).drop_vars(["price", "promo"])
    schema = DataSchema.model_validate(
        {
            "dims": SALES_SCHEMA.model_dump()["dims"],
            "vars": {"sales": SALES_SCHEMA.model_dump()["vars"]["sales"]},
        }
    )
    with pytest.raises(MapiError) as info:
        fit_spec(spec("ts.sarimax", "{order: [2, 1, 1], seasonal_order: [1, 1, 0, 12]}"), data,
                 registry=registry, raw_schema=schema)  # fmt: skip
    assert info.value.code is ErrorCode.SETTINGS_INVALID


def test_counterfactual_mode_is_not_offered(
    arimax: tuple[Bundle, xr.Dataset], registry: Registry
) -> None:
    b, data = arimax
    body = "mode: counterfactual\ninterventions: [{var: price, multiply: 1.1}]\n"
    with pytest.raises(MapiError) as info:
        score(registry, b, body, data.isel(date=slice(60, 64)))
    assert info.value.code is ErrorCode.MODE_NOT_SUPPORTED
