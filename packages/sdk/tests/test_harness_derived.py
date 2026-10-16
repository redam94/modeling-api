"""Fit-cached derived quantities (``outputs.derived`` + ``derive`` hook) and models that own
their inverse units."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest
import xarray as xr
from mapi_sdk.binding import BindingMap
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.models import Fitted, ScoreContext
from mapi_sdk.registry import Registry
from mapi_sdk.testing import toy
from mapi_spec import ModelSpec, load_score_request
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.errors import ErrorCode, MapiError

BIND = {
    "dims": {"geo": "market", "time": "date"},
    "vars": {"y": "revenue_local", "spend": "spend_local"},
}
SEEN: dict[str, Any] = {}


class DerivedLinear(toy.LinearModel):
    """The toy linear model plus fit-cached derived quantities."""

    id = "toy.derived_linear"
    derived: ClassVar[list[dict[str, Any]]] = [
        {"name": "levels", "dims": ["geo", "time"], "cache": "fit"},
        {"name": "lazy", "dims": ["geo"], "cache": "on_demand"},
        {"name": "broken", "dims": ["geo"], "cache": "fit"},
        {"name": "missing", "dims": ["geo"], "cache": "fit"},
    ]

    def derive(self, fitted: Fitted, name: str, ds: xr.Dataset, ctx: ScoreContext) -> xr.Dataset:
        SEEN.setdefault("names", []).append(name)
        SEEN["draw_index"] = ctx.draw_index
        if name == "broken":
            raise ZeroDivisionError("boom")
        if name == "missing":
            return super().derive(fitted, name, ds, ctx)
        assert ctx.to_original is not None
        lag1 = ds["spend_lag"].sel(lag=1, drop=True)
        return xr.Dataset(
            {
                "y": ctx.to_original(ds["y"], "y"),
                "spend": ctx.to_original(ds["spend"], "spend"),
                "spend_lag": ctx.to_original(ds["spend_lag"], "spend"),
                "lag1": ctx.to_original(lag1, "spend"),
            }
        )


class OwnedLinear(toy.LinearModel):
    """Claims its outputs are already in original units (a model that owns the inverse)."""

    id = "toy.owned_linear"
    capabilities = Capabilities(
        inference="point",
        representation=Representation.POINT,
        score_modes=frozenset({"predict", "forecast", "evaluate"}),
        owns=frozenset({"inverse_units"}),
    )


@pytest.fixture
def reg(registry: Registry) -> Registry:
    registry.register_model(DerivedLinear)
    registry.register_model(OwnedLinear)
    return registry


def _spec(e33_spec: Callable[..., ModelSpec], model: str, derived: list[str]) -> ModelSpec:
    spec = e33_spec(model=model, metrics="rmse, mape")
    return spec.model_copy(
        update={"outputs": spec.outputs.model_copy(update={"derived": derived, "ui_draws": 7})}
    )


def _weekly(bundle: Bundle, reg: Registry, data: xr.Dataset, fx: xr.Dataset) -> xr.Dataset:
    raw = BindingMap(BIND).apply(data)
    trace = bundle.pipeline(reg).transform(
        raw, bundle.states, end="weekly", side_tables={"fx": fx}, replay=False
    )
    return trace.data


def test_fit_cached_derived_are_in_the_output_space(
    reg: Registry,
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    tmp_path: Path,
) -> None:
    data, fx = panel
    SEEN.clear()
    bundle = fit_spec(
        _spec(e33_spec, "toy.derived_linear@^1", ["levels", "lazy"]), data, {"fx": fx}, reg
    )
    # computed once, on the production fit only; on-demand quantities are not cached
    assert SEEN["names"] == ["levels"]
    assert bundle.evaluation is not None and not bundle.evaluation.model.derived
    assert SEEN["draw_index"] is None  # a point model has no draws
    levels = bundle.model.derived["levels"]
    weekly = _weekly(bundle, reg, data, fx)
    xr.testing.assert_allclose(
        levels["y"].transpose(*weekly["y"].dims), weekly["y"], check_dim_order=False
    )
    xr.testing.assert_allclose(
        levels["spend"].transpose(*weekly["spend"].dims), weekly["spend"], check_dim_order=False
    )
    assert levels["y"].attrs["output_space"] == "weekly"
    assert levels["spend"].attrs["output_space"] == "weekly"
    # an array derived from the lagged copy inverts with the spend chain too
    assert levels["spend_lag"].attrs["output_space"] == "weekly"
    lag1 = levels["lag1"]
    want = weekly["spend"].shift(time=1).fillna(0.0)
    xr.testing.assert_allclose(lag1.transpose(*want.dims), want, check_dim_order=False)
    # cached quantities survive the bundle round trip
    bundle.write(tmp_path)
    back = Bundle.read(tmp_path, reg)
    xr.testing.assert_allclose(back.model.derived["levels"]["y"], levels["y"])


def test_derive_failures_are_typed(
    reg: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    with pytest.raises(MapiError) as err:
        fit_spec(_spec(e33_spec, "toy.derived_linear@^1", ["broken"]), data, {"fx": fx}, reg)
    assert err.value.code is ErrorCode.USER_CODE_ERROR
    assert "ZeroDivisionError" in (err.value.untrusted_text or "")
    with pytest.raises(MapiError) as err:
        fit_spec(_spec(e33_spec, "toy.derived_linear@^1", ["missing"]), data, {"fx": fx}, reg)
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert err.value.pointer == "/outputs/derived/0"


def test_models_owning_inverse_units_are_not_inverted_for_metrics(
    reg: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    owned = fit_spec(_spec(e33_spec, "toy.owned_linear@^1", []), data, {"fx": fx}, reg)
    plain = fit_spec(_spec(e33_spec, "toy.linear@^1", []), data, {"fx": fx}, reg)
    assert owned.metrics.output_space == "model-owned"
    assert plain.metrics.output_space == "weekly"
    # the owned model's predictions are compared with model-stage observations (scaled
    # units), so the scale-free error matches the plain model's EUR-space error
    assert owned.metrics.values["mape"] == pytest.approx(plain.metrics.values["mape"], rel=1e-6)
    assert owned.metrics.values["rmse"] < plain.metrics.values["rmse"] / 10
    req = load_score_request(
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://g/t\nmode: predict\n"
    )
    out = score_bundle(owned, req, data.isel(date=slice(-28, None)), registry=reg)
    assert set(json.loads(out.attrs["output_space"]).values()) == {"model-owned"}


def test_model_owned_outputs_carry_per_variable_units_and_space(
    reg: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    """A model owning ``inverse_units`` reports its outputs in the units of its own input
    (the model stage); every output variable says so, not only the root attrs."""
    data, fx = panel
    owned = fit_spec(_spec(e33_spec, "toy.owned_linear@^1", []), data, {"fx": fx}, reg)
    req = load_score_request(
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://g/t\nmode: predict\n"
    )
    out = score_bundle(owned, req, data.isel(date=slice(-28, None)), registry=reg, return_full=True)
    want = owned.resolved.stages[owned.resolved.model_stage].vars["y"].units  # type: ignore[union-attr]
    for name in ("y_hat", "contributions"):
        attrs = out["predictions"].to_dataset()[name].attrs
        assert attrs["output_space"] == "model-owned"
        assert attrs.get("units") == want


def test_derived_quantities_requested_in_output_vars(
    reg: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    """``output.vars`` naming a fit-cached derived quantity returns it; one that is not
    cached in the bundle is refused instead of silently returning nothing."""
    data, fx = panel
    bundle = fit_spec(_spec(e33_spec, "toy.derived_linear@^1", ["levels"]), data, {"fx": fx}, reg)
    head = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://g/t\nmode: predict\n"
    window = data.isel(date=slice(-28, None))
    both = load_score_request(head + "output: {vars: [y_hat, levels]}\n")
    out = score_bundle(bundle, both, window, registry=reg)
    paths = {n.path for n in out.subtree}
    assert "/derived/levels" in paths
    got = out["derived/levels"].to_dataset()
    assert "revenue_local" in got  # reverse-bound like every other group
    assert "y_hat" in out["summary"].to_dataset()
    lazy = load_score_request(head + "output: {vars: [lazy]}\n")
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, lazy, window, registry=reg)
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert err.value.pointer == "/output/vars/0"
