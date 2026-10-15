"""``HierarchicalGeoMMM.build`` variants and typed errors (graph only, no sampling)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from mapi_plugin_mmm.examples import E33Truth
from mapi_plugin_mmm.hierarchical_geo import HierarchicalGeoMMM
from mapi_plugin_mmm.settings import HierarchicalGeoSettings
from mapi_pymc.context import BuildContext
from mapi_sdk.binding import BindingMap
from mapi_sdk.pipeline import Pipeline
from mapi_sdk.registry import Registry
from mapi_spec import DataSchema, ModelSpec
from mapi_spec.errors import ErrorCode, MapiError

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def model_ds(
    spec: ModelSpec,
    registry: Registry,
    small: tuple[xr.Dataset, E33Truth],
    tables: dict[str, xr.Dataset],
) -> xr.Dataset:
    pipe = Pipeline.from_spec(spec, registry)
    raw = BindingMap(spec.data.bind).apply(small[0])
    return pipe.fit(raw, side_tables=tables).data


def _schema(strategy: str = "hyperprior", pooling: str = "partial") -> DataSchema:
    schema = HierarchicalGeoMMM.manifest().schema_template.resolve({"pooling": pooling})
    if pooling == "none":
        return schema
    geo = schema.dims["geo"].model_copy(update={"new_level_strategy": strategy})
    return schema.model_copy(update={"dims": {**schema.dims, "geo": geo}})


def _build(ds: xr.Dataset, settings: dict[str, Any], **ctx: Any) -> tuple[pm.Model, BuildContext]:
    s = HierarchicalGeoSettings.model_validate(settings)
    bctx = BuildContext(phase=ctx.pop("phase", "fit"), ds=ds, schema=_schema(**ctx), seed=3)
    return HierarchicalGeoMMM().build_model(ds, s, bctx), bctx


def _with_new_geo(ds: xr.Dataset) -> xr.Dataset:
    new = ds.sel(geo=["NL"]).assign_coords(geo=["FR"])
    out = xr.concat([ds, new], dim="geo", data_vars="all")
    flags = np.array([False] * ds.sizes["geo"] + [True])
    return out.assign_coords(geo__is_new=("geo", flags))


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"centred": True},
        {"pooling": "complete"},
        {"pooling": "none"},
        {"adstock": {"kind": "delayed", "normalize": False}, "saturation": {"kind": "hill"}},
        {"saturation": {"kind": "none"}, "controls": ["holiday"]},
        {
            "priors": {
                "alpha": {"distribution": "Beta", "alpha": 2, "beta": 2},
                "beta_channel": {"distribution": "LogNormal", "mu": -1, "sigma": 0.5},
                "lam": {"distribution": "Gamma", "alpha": [3, 2], "beta": 1, "dims": ["channel"]},
            }
        },
    ],
)
def test_variants_build_with_finite_logp(model_ds: xr.Dataset, settings: dict[str, Any]) -> None:
    pooling = settings.get("pooling", "partial")
    model, bctx = _build(model_ds, settings, pooling=pooling)
    logp = model.compile_logp()(model.initial_point())
    assert np.isfinite(logp)
    mu, contrib = pm.draw([model["mu"], model["contributions"]], draws=3, random_seed=1)
    assert mu.shape == (3, model_ds.sizes["geo"], model_ds.sizes["time"])
    comps = model.coords["component"]
    assert list(comps[:2]) == ["tv", "search"] and comps[2] == "baseline"
    assert contrib.shape[1] == len(comps)
    np.testing.assert_allclose(contrib.sum(axis=1), mu, rtol=1e-10)
    assert set(bctx.data_vars) >= {"spend"}


@pytest.mark.parametrize(
    ("strategy", "kind"), [("hyperprior", "noise"), ("pooled", "mean"), ("zero", "zero")]
)
def test_new_geos_follow_the_strategy(model_ds: xr.Dataset, strategy: str, kind: str) -> None:
    ds = _with_new_geo(model_ds)
    model, bctx = _build(ds, {}, phase="score", strategy=strategy)
    assert "beta__new" in model.named_vars
    assert model.coords["geo__new"] == ("FR",)
    if kind == "noise":
        assert "beta__new_z" in bctx.frozen
    beta_new, bc = pm.draw([model["beta__new"], model["beta_channel"]], draws=1, random_seed=5)
    if kind == "mean":
        np.testing.assert_allclose(beta_new[0], bc)
    if kind == "zero":
        np.testing.assert_allclose(beta_new, 0.0)


def test_new_geos_are_rejected_when_they_cannot_be_scored(model_ds: xr.Dataset) -> None:
    ds = _with_new_geo(model_ds)
    with pytest.raises(MapiError) as err:
        _build(ds, {}, phase="score", strategy="error")
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    with pytest.raises(MapiError) as err:
        _build(ds, {"pooling": "none"}, phase="score", pooling="none")
    assert err.value.code is ErrorCode.UNSEEN_LEVELS


def test_score_time_missing_spend_is_filled_fit_time_is_an_error(model_ds: xr.Dataset) -> None:
    holey = model_ds.copy()
    spend = holey["spend"].copy()
    spend[0, 0, 0, :] = np.nan
    holey["spend"] = spend
    with pytest.raises(MapiError, match="missing"):
        _build(holey, {})
    model, bctx = _build(holey, {}, phase="score")
    assert np.isfinite(bctx.data_values["spend"]).all()
    del model


@pytest.mark.parametrize(
    ("mutate", "settings", "code", "match"),
    [
        (lambda d: d, {"adstock": {"l_max": 7}}, ErrorCode.SCHEMA_INVALID, "l_max"),
        (lambda d: d.drop_vars("spend"), {}, ErrorCode.SCHEMA_INVALID, "'spend'"),
        (lambda d: d.assign(spend=-d["spend"]), {}, ErrorCode.SCHEMA_INVALID, "non-negative"),
        (lambda d: d.assign(y=d["y"].where(d["y"] > 0.9)), {}, ErrorCode.SCHEMA_INVALID, "target"),
        (lambda d: d, {"controls": ["nope"]}, ErrorCode.MISSING_FUTURE_COVARIATE, "nope"),
        (
            lambda d: d.assign(baseline=d["holiday"]),
            {"controls": ["baseline"]},
            ErrorCode.SCHEMA_INVALID,
            "distinct",
        ),
        (
            lambda d: d.assign(tv_ctl=d["spend"].isel(lag=0)),
            {"controls": ["tv_ctl"]},
            ErrorCode.SCHEMA_INVALID,
            "channel",
        ),
        (
            lambda d: d.isel(lag=0),
            {},
            ErrorCode.SCHEMA_INVALID,
            "lag_tensor",
        ),
    ],
)
def test_typed_errors(
    model_ds: xr.Dataset, mutate: Any, settings: dict[str, Any], code: ErrorCode, match: str
) -> None:
    with pytest.raises(MapiError, match=match) as err:
        _build(mutate(model_ds), settings)
    assert err.value.code is code
