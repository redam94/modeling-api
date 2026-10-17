"""Forecasts conditioned on actuals through the score path (``mapi_sdk.actuals``).

``toy.ar1`` is the reference plugin: an AR(1) with a driver per region whose
``forecast_with_actuals`` runs the fitted recursion over the history with the actuals in
place. The tests pin the platform side: detection, refusal, the appended horizon, the
future-driver fill, per-series checks and the conditioning record.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_sdk.actuals import ForecastActuals, future_mask, observed_mask
from mapi_sdk.bundle import Bundle
from mapi_sdk.future import fill_future_rows
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.models import Fitted, ScoreContext
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model, toy
from mapi_spec import DataSchema, ScoreRequest
from mapi_spec.actuals import CONDITIONING_ATTR, ForecastConditioning
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FutureCovariates

pytestmark = pytest.mark.unit

MONTHS = 36
FIT_END = pd.Timestamp("2022-12-01")

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: sales-ar, project: test}
data:
  dataset: dataset://test/sales@v1
  bind:
    dims: {geo: region, time: month}
    vars: {y: sales, x: price}
model:
  use: __MODEL__@^1
  settings: {exog: [x]}
  fit: {mode: train_only}
"""

RAW = DataSchema.model_validate(
    {
        "dims": {
            "region": {"kind": "categorical"},
            "month": {"kind": "time", "policy": "extend", "freq": "MS"},
        },
        "vars": {
            "sales": {"dims": ["region", "month"], "dtype": "float64", "role": "target"},
            "price": {"dims": ["region", "month"], "dtype": "float64", "future_required": True},
        },
    }
)


def _data() -> xr.Dataset:
    rng = np.random.default_rng(3)
    t = pd.date_range("2020-01-01", periods=MONTHS, freq="MS").as_unit("ns")
    price = 10 + rng.normal(0, 1, size=(2, MONTHS))
    y = np.zeros((2, MONTHS))
    y[:, 0] = 50
    for j in range(1, MONTHS):
        y[:, j] = 5 + 0.8 * y[:, j - 1] + 1.5 * price[:, j] + rng.normal(0, 0.5, size=2)
    return xr.Dataset(
        {"sales": (("region", "month"), y), "price": (("region", "month"), price)},
        coords={"region": ["north", "south"], "month": t},
    )


@pytest.fixture(scope="module")
def data() -> xr.Dataset:
    return _data()


def _fit(model: str, data: xr.Dataset) -> Bundle:
    return fit_spec(
        SPEC.replace("__MODEL__", model), data, registry=toy.toy_registry(), raw_schema=RAW
    )


@pytest.fixture(scope="module")
def bundle(data: xr.Dataset) -> Bundle:
    return _fit("toy.ar1", data)


def _req(periods: int = 4, policy: str | None = "carry_last") -> ScoreRequest:
    doc: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "endpoint://test/sales",
        "mode": "forecast",
        "horizon": {"dim": "time", "periods": periods},
        "output": {"summary": ["point"], "names": "dataset"},
    }
    if policy is not None:
        doc["future_covariates"] = policy
    return ScoreRequest.model_validate(doc)


def _months(start: str, n: int) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="MS").as_unit("ns")


def _payload(
    start: str, sales: list[list[float]], price: list[list[float]] | None = None
) -> xr.Dataset:
    n = len(sales[0])
    out = xr.Dataset(
        {"sales": (("region", "month"), np.asarray(sales, dtype=float))},
        coords={"region": ["north", "south"], "month": _months(start, n)},
    )
    if price is not None:
        out["price"] = (("region", "month"), np.asarray(price, dtype=float))
    return out


def _score(bundle: Bundle, req: ScoreRequest, ds: xr.Dataset | None, data: xr.Dataset) -> Any:
    return score_bundle(bundle, req, ds, registry=toy.toy_registry(), history=data)


def _point(tree: xr.DataTree) -> xr.DataArray:
    node = tree["summary"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()["y_hat"].sel(stat="point")


def _record(tree: xr.DataTree) -> ForecastConditioning:
    return ForecastConditioning.model_validate_json(str(tree.attrs[CONDITIONING_ATTR]))


def _issues(tree: xr.DataTree) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = json.loads(str(tree.attrs["issues"]))
    return out


def _recursion(
    bundle: Bundle, region: str, last: float, x: list[float], steps: int | None = None
) -> list[float]:
    """The fitted AR(1) run forward from ``last`` with drivers ``x``."""
    p = bundle.model.group("params").sel(geo=region)
    c, phi, beta = float(p["c"]), float(p["phi"]), float(p["beta"].values[0])
    out, state = [], last
    for xv in x[: steps or len(x)]:
        state = c + phi * state + beta * xv
        out.append(state)
    return out


# ------------------------------------------------------------------------- plain forecasts


def test_every_forecast_reports_its_conditioning(bundle: Bundle, data: xr.Dataset) -> None:
    tree = _score(bundle, _req(4), None, data)
    rec = _record(tree)
    assert not rec.conditioned and rec.horizon_source == "horizon" and rec.periods == 4
    assert rec.fit_end == "2022-12-01" and rec.n_series == 2 and rec.parameters == "fitted"
    assert rec.series[0].forecast_from == "2023-01-01" and rec.series[0].forecast_to == "2023-04-01"
    last = data["price"].sel(region="north").values[-1]
    expected = _recursion(bundle, "north", float(data["sales"].values[0, -1]), [last] * 4)
    np.testing.assert_allclose(_point(tree).sel(region="north").values, expected)


# ---------------------------------------------------------------------- conditioned forecasts


def test_the_fitted_values_as_actuals_reproduce_the_plain_forecast(
    bundle: Bundle, data: xr.Dataset
) -> None:
    plain = _point(_score(bundle, _req(4), None, data))
    tail = data.isel(month=slice(-3, None))
    tree = _score(bundle, _req(4), tail, data)
    cond = _point(tree)
    assert list(cond["month"].values) == [*tail["month"].values, *plain["month"].values]
    np.testing.assert_allclose(cond.sel(month=plain["month"]).values, plain.values, rtol=1e-12)
    rec = _record(tree)
    assert rec.conditioned and rec.n_actuals == 6 and rec.revised == 6
    assert rec.horizon_source == "horizon" and rec.actuals_through == "2022-12-01"
    assert "6 of them replaced fitted values" in rec.message


def test_new_actuals_move_the_forecast_origin(bundle: Bundle, data: xr.Dataset) -> None:
    new = _payload("2023-01-01", [[120.0, 130.0], [60.0, 61.0]], [[11.0, 12.0], [9.0, 9.5]])
    tree = _score(bundle, _req(3), new, data)
    point = _point(tree)
    assert list(point["month"].values) == list(_months("2023-01-01", 5))
    # the forecast runs from the last actual (130), with the last driver carried forward
    expected = _recursion(bundle, "north", 130.0, [12.0] * 3)
    np.testing.assert_allclose(point.sel(region="north").values[2:], expected)
    # the actual months themselves get one-step-ahead predictions
    one_step = _recursion(bundle, "north", 120.0, [12.0], steps=1)
    np.testing.assert_allclose(point.sel(region="north").values[1], one_step[0])
    rec = _record(tree)
    assert rec.n_actuals == 4 and rec.revised == 0 and rec.actuals_through == "2023-02-01"
    north = next(s for s in rec.series if s.series == {"geo": "north"})
    assert (north.origin, north.forecast_from, north.n_forecast) == ("2023-02-01", "2023-03-01", 3)
    assert rec.message.startswith("Forecast updated with 4 actuals (2023-01-01 to 2023-02-01)")


def test_actual_values_are_used_never_ignored(bundle: Bundle, data: xr.Dataset) -> None:
    def forecast(value: float) -> np.ndarray:
        ds = _payload("2023-01-01", [[value], [60.0]], [[11.0], [9.0]])
        return np.asarray(_point(_score(bundle, _req(2), ds, data)).sel(region="north").values)

    low, high = forecast(1.0), forecast(9999.0)
    assert not np.allclose(low[1:], high[1:])


def test_listed_future_periods_are_forecast_exactly(bundle: Bundle, data: xr.Dataset) -> None:
    ds = _payload(
        "2023-01-01",
        [[120.0, np.nan, np.nan], [60.0, np.nan, np.nan]],
        [[11.0, 13.0, 14.0], [9.0, 9.0, 9.0]],
    )
    tree = _score(bundle, _req(8, policy=None), ds, data)
    point = _point(tree)
    assert list(point["month"].values) == list(_months("2023-01-01", 3))
    expected = _recursion(bundle, "north", 120.0, [13.0, 14.0])
    np.testing.assert_allclose(point.sel(region="north").values[1:], expected)
    rec = _record(tree)
    assert rec.horizon_source == "rows" and rec.periods == 2
    warned = [i for i in _issues(tree) if i["pointer"] == "/horizon/periods"]
    assert warned and "horizon.periods (8) is not used" in warned[0]["message"]


def test_empty_future_drivers_follow_the_policy_from_the_actuals(
    bundle: Bundle, data: xr.Dataset
) -> None:
    ds = _payload(
        "2023-01-01",
        [[120.0, np.nan], [60.0, np.nan]],
        [[42.0, np.nan], [9.0, np.nan]],
    )
    tree = _score(bundle, _req(1), ds, data)
    expected = _recursion(bundle, "north", 120.0, [42.0])  # carried from the actual's month
    np.testing.assert_allclose(_point(tree).sel(region="north").values[1], expected[0])
    assert any("filled with 'carry_last'" in i["message"] for i in _issues(tree))
    with pytest.raises(MapiError) as err:
        _score(bundle, _req(1, policy=None), ds, data)
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


def test_revisions_inside_the_fit_forecast_after_the_fit(bundle: Bundle, data: xr.Dataset) -> None:
    """A payload that only revises mid-history periods still forecasts after the fit."""
    mid = data.isel(month=slice(10, 13)).copy(deep=True)
    tree = _score(bundle, _req(4), mid, data)
    point = _point(tree)
    after = list(_months("2023-01-01", 4))
    # the fitted periods between the revisions and the end of the fit keep their values
    assert list(point["month"].values) == [*data["month"].values[10:], *after]
    # the fitted values themselves as revisions: the plain forecast, unchanged
    plain = _point(_score(bundle, _req(4), None, data))
    np.testing.assert_allclose(point.sel(month=after).values, plain.values, rtol=1e-12)
    rec = _record(tree)
    assert rec.conditioned and rec.revised == rec.n_actuals == 6 and rec.periods == 4
    north = rec.series[0]
    assert (north.origin, north.forecast_from, north.forecast_to) == (
        "2022-12-01",
        "2023-01-01",
        "2023-04-01",
    )
    assert "inside the fitted history" in rec.message and "after the fitted" not in rec.message


def test_ragged_panels_forecast_each_series_after_its_own_last_actual(
    bundle: Bundle, data: xr.Dataset
) -> None:
    ds = _payload(
        "2023-01-01",
        [[120.0, 125.0, np.nan], [60.0, np.nan, np.nan]],
        [[11.0, 11.0, 11.0], [9.0, 9.0, 9.0]],
    )
    tree = _score(bundle, _req(2, policy=None), ds, data)
    rec = _record(tree)
    north, south = rec.series
    assert (north.actuals_through, north.n_forecast) == ("2023-02-01", 1)
    assert (south.actuals_through, south.n_forecast) == ("2023-01-01", 2)
    expected = _recursion(bundle, "south", 60.0, [9.0, 9.0])
    np.testing.assert_allclose(_point(tree).sel(region="south").values[1:], expected)


# --------------------------------------------------------------------------------- refusals


def test_a_model_without_the_capability_refuses_actuals(data: xr.Dataset) -> None:
    plain = _fit("toy.ar1_plain", data)
    ds = _payload("2023-01-01", [[120.0], [60.0]], [[11.0], [9.0]])
    with pytest.raises(MapiError) as err:
        _score(plain, _req(3), ds, data)
    exc = err.value
    assert exc.code is ErrorCode.ACTUALS_NOT_SUPPORTED and exc.status == 422
    issue = exc.to_problem().errors[0]
    assert "2 periods" not in issue.message and "1 period (2023-01-01)" in issue.message
    assert issue.suggested_action is not None and "Refit" in issue.suggested_action
    # an empty target is no actual: the same model forecasts the listed periods
    blank = _payload("2023-01-01", [[np.nan], [np.nan]], [[11.0], [9.0]])
    rec = _record(_score(plain, _req(1), blank, data))
    assert not rec.conditioned
    # predict never reads targets as actuals
    predict = _req(1).model_copy(update={"mode": "predict", "horizon": None})
    score_bundle(plain, predict, data.isel(month=slice(-3, None)), registry=toy.toy_registry())


def test_the_default_hook_refuses(data: xr.Dataset) -> None:
    class Claims(toy.AR1Model):  # claims the capability, implements nothing
        id = "test.claims"

        forecast_with_actuals = toy.CustomModel.forecast_with_actuals  # type: ignore[assignment]

    reg = toy.toy_registry()
    reg.register_model(Claims)
    b = fit_spec(
        SPEC.replace("__MODEL__", "test.claims"),
        data,
        registry=reg,
        raw_schema=RAW,
    )
    ds = _payload("2023-01-01", [[120.0], [60.0]], [[11.0], [9.0]])
    with pytest.raises(MapiError) as err:
        score_bundle(b, _req(2), ds, registry=reg, history=data)
    assert err.value.code is ErrorCode.ACTUALS_NOT_SUPPORTED


def test_gaps_need_a_model_that_filters_missing_targets(data: xr.Dataset, bundle: Bundle) -> None:
    gap = _payload(
        "2023-01-01",
        [[120.0, np.nan, 130.0], [60.0, 61.0, 62.0]],
        [[11.0, 11.0, 11.0], [9.0, 9.0, 9.0]],
    )
    with pytest.raises(MapiError) as err:
        _score(bundle, _req(2), gap, data)
    assert err.value.code is ErrorCode.ACTUALS_INVALID
    lenient = _fit("toy.ar1_missing", data)
    tree = _score(lenient, _req(2), gap, data)
    rec = _record(tree)
    north = next(s for s in rec.series if s.series == {"geo": "north"})
    assert north.missing == 1 and "filtered as missing" in rec.message
    filled = _recursion(lenient, "north", 120.0, [11.0])[0]  # the gap is predicted
    expected = _recursion(lenient, "north", 130.0, [11.0, 11.0])
    np.testing.assert_allclose(_point(tree).sel(region="north").values[3:], expected)
    assert np.isfinite(filled)


def test_actuals_after_the_fit_need_their_drivers(bundle: Bundle, data: xr.Dataset) -> None:
    ds = _payload("2023-01-01", [[120.0], [60.0]])  # no price at all
    with pytest.raises(MapiError) as err:
        _score(bundle, _req(2), ds, data)
    assert err.value.code is ErrorCode.ACTUALS_INVALID
    assert "drivers" in err.value.detail


def test_actuals_that_start_late_are_refused_statically(bundle: Bundle, data: xr.Dataset) -> None:
    late = _payload(
        "2023-01-01",
        [[np.nan, 120.0], [np.nan, 60.0]],
        [[11.0, 11.0], [9.0, 9.0]],
    )
    with pytest.raises(MapiError) as err:
        _score(bundle, _req(2), late, data)
    assert err.value.code is ErrorCode.ACTUALS_INVALID


# ----------------------------------------------------------------------- building blocks


def test_masks_and_the_future_rows_fill() -> None:
    ds = _payload(
        "2023-01-01",
        [[1.0, np.nan, 2.0, np.nan], [1.0, np.nan, np.nan, np.nan]],
        [[5.0, 6.0, np.nan, np.nan], [7.0, np.nan, np.nan, 8.0]],
    )
    obs = observed_mask(ds, ["sales"], "month")
    assert obs is not None
    fut = future_mask(obs, "month", np.datetime64("2022-12-01"))
    assert fut.sel(region="north").values.tolist() == [False, False, False, True]
    assert fut.sel(region="south").values.tolist() == [False, True, True, True]
    out, issues = fill_future_rows(
        ds, FutureCovariates.CARRY_LAST, dim="month", future=fut, names=["price"]
    )
    # north: only the last month is future (the empty month before is a gap: untouched)
    assert np.isnan(out["price"].sel(region="north").values[2])
    assert out["price"].sel(region="north").values[3] == 5.0 + 1.0  # carried from 6.0
    assert out["price"].sel(region="south").values.tolist() == [7.0, 7.0, 7.0, 8.0]
    assert issues and "carry_last" in issues[0].message
    same, none = fill_future_rows(
        ds, FutureCovariates.SUPPLIED, dim="month", future=fut, names=["price"]
    )
    assert same is ds and none == []


def test_forecast_actuals_is_what_the_hook_receives(bundle: Bundle, data: xr.Dataset) -> None:
    seen: list[ForecastActuals] = []

    class Spy(toy.AR1Model):
        id = "test.spy"

        def forecast_with_actuals(
            self,
            fitted: Fitted,
            ds: xr.Dataset,
            actuals: ForecastActuals,
            req: ScoreRequest,
            ctx: ScoreContext,
        ) -> xr.DataTree:
            seen.append(actuals)
            return super().forecast_with_actuals(fitted, ds, actuals, req, ctx)

    reg: Registry = toy.toy_registry()
    reg.register_model(Spy)
    b = fit_spec(SPEC.replace("__MODEL__", "test.spy"), data, registry=reg, raw_schema=RAW)
    ds = xr.concat(
        [
            data.isel(month=[-1]),
            _payload("2023-01-01", [[120.0], [60.0]], [[11.0], [9.0]]),
        ],
        dim="month",
    )
    score_bundle(b, _req(2), ds, registry=reg, history=data)
    (a,) = seen
    assert a.dim == "time" and a.targets == ("y",)
    assert a.observed.dims == ("geo", "time") and int(a.observed.sum()) == 4
    assert int(a.revised.sum()) == 2 and int(a.future.sum()) == 4
    assert not bool(a.gap.any())
    assert a.last_actual().sel(geo="north").values == np.datetime64("2023-01-01")
    assert a.conditioning.n_actuals == 4


# --------------------------------------------------------------------------- contract kit


def test_contract_kit_checks_forecast_actuals(data: xr.Dataset) -> None:
    for model, verdict in (("toy.ar1", "reproduce the plain"), ("toy.ar1_plain", "refused")):
        plugin = toy.toy_registry().model(f"{model}@1.0.0")
        report = check_model(
            plugin,
            ModelFixture(
                spec=SPEC.replace("__MODEL__", model),
                data=data,
                raw_schema=RAW,
                registry=toy.toy_registry(),
            ),
            checks=["manifest", "fit", "forecast_actuals"],
        )
        result = report.get("forecast_actuals")
        assert result.passed and not result.skipped and verdict in result.detail


def test_a_template_with_every_value_empty_forecasts_from_the_fit(
    bundle: Bundle, data: xr.Dataset
) -> None:
    """The endpoint's ``actuals_example`` as sent: the target and the drivers empty on the
    periods after the fit, ``carry_last`` - a plain forecast of exactly those periods."""
    empty = [[np.nan] * 3, [np.nan] * 3]
    tree = _score(bundle, _req(3), _payload("2023-01-01", empty, empty), data)
    rec = _record(tree)
    assert not rec.conditioned and rec.horizon_source == "rows" and rec.periods == 3
    last_x = float(data["price"].sel(region="north").values[-1])
    expected = _recursion(bundle, "north", float(data["sales"].values[0, -1]), [last_x] * 3)
    np.testing.assert_allclose(_point(tree).sel(region="north").values, expected)
