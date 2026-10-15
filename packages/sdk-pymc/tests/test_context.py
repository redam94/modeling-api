"""BuildContext helpers: coords, data/observed, hier_effect strategies, positions."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pymc as pm
import pytest
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema

SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "geo": {"kind": "categorical", "policy": "new_levels", "new_level_strategy": "pooled"},
            "time": {"kind": "time", "policy": "extend", "freq": "W-SUN", "max_extension": 3},
        },
        "vars": {"y": {"dims": ["geo", "time"], "dtype": "float64", "role": "target"}},
    }
)
WEEKS = pd.date_range("2024-01-07", periods=5, freq="W-SUN").as_unit("ns")


def _fit_ds() -> xr.Dataset:
    return xr.Dataset(
        {
            "y": (("geo", "time"), np.arange(10.0).reshape(2, 5)),
            "x": (("geo", "time"), np.ones((2, 5))),
            "flag": (("geo",), np.array([True, False])),
        },
        coords={"geo": ["a", "b"], "time": WEEKS},
    )


def _score_ds(new: bool = True) -> xr.Dataset:
    geos = ["a", "b", "c"] if new else ["a", "b"]
    times = pd.date_range("2024-01-07", periods=7, freq="W-SUN").as_unit("ns")
    ds = xr.Dataset(
        {"x": (("geo", "time"), np.ones((len(geos), 7)))},
        coords={"geo": geos, "time": times},
    )
    return ds.assign_coords(geo__is_new=("geo", np.array([g == "c" for g in geos])))


def _score_ctx(ds: xr.Dataset, schema: DataSchema = SCHEMA, seed: int = 0) -> BuildContext:
    return BuildContext(
        "score",
        ds,
        schema=schema,
        fit_coords={"geo": np.array(["a", "b"]), "time": WEEKS},
        seed=seed,
    )


def test_fit_context_coords_data_and_observed() -> None:
    ds = _fit_ds()
    ctx = BuildContext("fit", ds, schema=SCHEMA)
    assert ctx.is_fit and not ctx.is_score
    assert list(ctx.fit_coords["geo"]) == ["a", "b"]
    coords = ctx.coords()
    assert coords["geo"] == ["a", "b"] and len(coords["time"]) == 5
    assert "geo__fit" not in coords
    with pm.Model(coords=coords):
        x = ctx.data("x")
        f = ctx.data("flag")
        obs = ctx.observed("y")
    assert x.eval().shape == (2, 5) and f.eval().dtype.kind == "i"
    np.testing.assert_array_equal(obs, ds["y"].values)
    assert ctx.data_vars == {"x": ("geo", "time"), "flag": ("geo",)}
    assert ctx.observed_vars == {"y": ("geo", "time")}
    assert ctx.new_levels("geo").size == 0
    np.testing.assert_array_equal(ctx.rng.normal(size=3), ctx.rng.normal(size=3))


def test_data_errors_and_explicit_values() -> None:
    ds = _fit_ds().assign(z=(("geo",), np.array([np.nan, 1.0])))
    ctx = BuildContext("fit", ds)
    with pm.Model(coords=ctx.coords()):
        with pytest.raises(MapiError) as err:
            ctx.data("z")
        assert err.value.code is ErrorCode.SCHEMA_INVALID
        with pytest.raises(MapiError, match="dims="):
            ctx.data("w", np.zeros(3))
        assert ctx.data("w", np.zeros(5), dims=["time"]).eval().shape == (5,)
        assert ctx.data("scalar", 2.0).eval() == 2.0
        with pytest.raises(MapiError) as err2:
            ctx.data("missing")
        assert err2.value.code is ErrorCode.SCHEMA_INVALID
    score = _score_ctx(_score_ds())
    with pm.Model(coords=score.coords()), pytest.raises(MapiError) as err3:
        score.data("absent")
    assert err3.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


def test_score_context_adds_fit_and_new_coords_and_placeholders() -> None:
    ctx = _score_ctx(_score_ds())
    coords = ctx.coords()
    assert coords["geo"] == ["a", "b", "c"]
    assert coords["geo__fit"] == ["a", "b"] and coords["geo__new"] == ["c"]
    assert list(ctx.new_levels("geo")) == ["c"] and list(ctx.fit_levels("geo")) == ["a", "b"]
    obs = ctx.observed("y")  # sized from the schema when the target is absent
    assert obs.shape == (3, 7) and not obs.any()
    with pytest.raises(MapiError, match="dims="):
        BuildContext("score", _score_ds(), fit_coords={"geo": np.array(["a"])}).observed("q")
    shuffled = _score_ds().isel(geo=[2, 0, 1])
    with pytest.raises(MapiError, match="must follow the fit levels"):
        _score_ctx(shuffled).coords()


def _effect(ctx: BuildContext, **kw: object) -> np.ndarray:
    with pm.Model(coords=ctx.coords()) as model:
        eff = ctx.hier_effect("a", "geo", **kw)  # type: ignore[arg-type]
    point = {"a__mu": 1.0, "a__sigma": 2.0}
    values = {model[k]: v for k, v in point.items() if k in model.named_vars}
    for rv in model.free_RVs:
        if rv not in values:
            values[rv] = np.full(rv.shape.eval(), 0.5)
    return np.asarray(eff.eval(values, on_unused_input="ignore"))


def test_hier_effect_new_level_strategies() -> None:
    fit = BuildContext("fit", _fit_ds(), schema=SCHEMA)
    assert _effect(fit).shape == (2,)
    assert fit.hier_effects["a"].n_new == 0 and fit.frozen == {}
    pooled = _score_ctx(_score_ds())
    out = _effect(pooled)
    np.testing.assert_allclose(out, [2.0, 2.0, 1.0])  # mu + sigma*0.5 for fit levels, mu for new

    def with_strategy(strategy: str) -> DataSchema:
        geo = SCHEMA.dims["geo"].model_copy(update={"new_level_strategy": strategy})
        return SCHEMA.model_copy(update={"dims": {**SCHEMA.dims, "geo": geo}})

    np.testing.assert_allclose(_effect(_score_ctx(_score_ds(), with_strategy("zero"))), [2, 2, 0])
    hyper = _score_ctx(_score_ds(), with_strategy("hyperprior"))
    np.testing.assert_allclose(_effect(hyper), [2.0, 2.0, 2.0])  # z = 0.5 for the frozen latent
    assert set(hyper.frozen) == {"a__new_z"} and hyper.frozen["a__new_z"].dims == ("geo__new",)
    with pytest.raises(MapiError) as err:
        _effect(_score_ctx(_score_ds(), with_strategy("error")))
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    centred = _score_ctx(_score_ds(new=False))
    assert _effect(centred, centered=True).shape == (2,)
    assert centred.hier_effects["a"].centered


def test_hier_effect_with_vector_mean_and_bad_shapes() -> None:
    ctx = _score_ctx(_score_ds())
    with pm.Model(coords=ctx.coords()):
        eff = ctx.hier_effect("a", "geo", mu=np.array([1.0, 2.0, 3.0]), sigma=0.0)
        np.testing.assert_allclose(eff.eval({"a__offset": np.zeros(2)}), [1.0, 2.0, 3.0])
        with pytest.raises(MapiError, match="scalar"):
            ctx.hier_effect("b", "geo", sigma=np.ones(3))


def test_positions_and_anticipated_coords() -> None:
    fit = BuildContext("fit", _fit_ds(), schema=SCHEMA)
    np.testing.assert_allclose(fit.position("time"), np.arange(5))
    score = _score_ctx(_score_ds())
    np.testing.assert_allclose(score.position("time"), np.arange(7))
    np.testing.assert_allclose(score.position("time", scale=True), np.arange(7) / 4)
    np.testing.assert_allclose(score.position("geo"), [0, 1, 2])
    antic = score.anticipated_coords
    assert antic["time"].size == 5 + 3 and antic["time"][-1] == np.datetime64("2024-02-25")
    ints = BuildContext("fit", xr.Dataset(coords={"t": np.array([10, 12, 14])}))
    np.testing.assert_allclose(ints.position("t"), [0, 1, 2])
    no_freq = BuildContext("fit", _fit_ds())  # freq inferred from the labels
    np.testing.assert_allclose(no_freq.position("time"), np.arange(5))
    assert no_freq.anticipated_coords == {}
    odd = BuildContext(
        "fit", xr.Dataset(coords={"time": np.array(["2024-01-01", "2024-01-09"], "M8[ns]")})
    )
    with pytest.raises(MapiError, match="freq"):
        odd.position("time")
    with pytest.raises(MapiError):
        BuildContext("fit", xr.Dataset()).labels("nope")
    assert BuildContext("fit", xr.Dataset({"v": (("k",), [1, 2])})).labels("k").tolist() == [0, 1]
    assert fit.policy("time").value == "extend" and fit.strategy("geo") == "pooled"
    assert BuildContext("fit", _fit_ds()).strategy("geo") == "hyperprior"
