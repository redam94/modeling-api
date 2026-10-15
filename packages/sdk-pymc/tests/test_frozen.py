"""FrozenPredictor (ported to PyMC 6 / PyTensor 3) and the fast ≈ exact contract check."""

from __future__ import annotations

from typing import Any

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import pytest
import xarray as xr
from mapi_pymc import FrozenPredictorSpec, Reducer, create_frozen_predictor
from mapi_pymc.examples import RadonModel, radon_dataset
from mapi_pymc.frozen import _find_needed_nodes, _find_protected_dims, _remove_specify_shape
from mapi_pymc.testing import FastScoreReport, check_fast_score
from mapi_sdk.bundle import Bundle
from mapi_sdk.coords import canonicalise_coords
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import ValidationError


@pytest.fixture(scope="module")
def fitted_linear() -> tuple[pm.Model, xr.DataTree]:
    rng = np.random.default_rng(42)
    x = rng.standard_normal(20)
    y = 1.0 + 2.0 * x + 0.5 * rng.standard_normal(20)
    with pm.Model(coords={"obs": np.arange(20)}) as model:
        xd = pm.Data("x", x, dims="obs")
        a = pm.Normal("alpha", 0.0, 2.0)
        b = pm.Normal("beta", 0.0, 2.0)
        s = pm.HalfNormal("sigma", 1.0)
        mu = pm.Deterministic("mu", a + b * xd, dims="obs")
        pm.Normal("y", mu, s, observed=y, dims="obs")
    return model, _sample(model)


def _sample(model: pm.Model, **kw: Any) -> xr.DataTree:
    opts: dict[str, Any] = {
        "draws": 100,
        "tune": 100,
        "chains": 2,
        "random_seed": 42,
        "progressbar": False,
        "nuts_sampler": "pymc",
        "cores": 1,
        "compute_convergence_checks": False,
    }
    opts.update(kw)
    with model:
        out = pm.sample(**opts)
    assert isinstance(out, xr.DataTree)
    return out


def test_shapes_scenarios_and_dynamic_sizes(fitted_linear: tuple) -> None:
    model, idata = fitted_linear
    pred = create_frozen_predictor(model, idata, {"mu": "mu"}, ["x"], num_samples=50, random_seed=1)
    assert pred.num_samples == 50
    one = pred(x=np.linspace(-1, 1, 7))["mu"]
    assert one.shape == (50, 7)
    many = pred(x=np.outer(np.linspace(0.5, 2, 4), np.linspace(-1, 1, 7)))["mu"]
    assert many.shape == (50, 4, 7)
    np.testing.assert_allclose(many[:, 0], pred(x=0.5 * np.linspace(-1, 1, 7))["mu"])
    full = create_frozen_predictor(model, idata, {"mu": "mu"}, ["x"])
    assert full.num_samples == 200
    post = idata["posterior"].to_dataset().stack(sample=("chain", "draw"))
    x = np.array([0.0, 1.0])
    want = post["alpha"].values[:, None] + post["beta"].values[:, None] * x
    np.testing.assert_allclose(full(x=x)["mu"], want)  # chain-major sample order


def test_seeded_subsampling_and_reducers(fitted_linear: tuple) -> None:
    model, idata = fitted_linear
    a = create_frozen_predictor(model, idata, {"mu": "mu"}, ["x"], num_samples=30, random_seed=3)
    b = create_frozen_predictor(model, idata, {"mu": "mu"}, ["x"], num_samples=30, random_seed=3)
    c = create_frozen_predictor(model, idata, {"mu": "mu"}, ["x"], num_samples=30, random_seed=4)
    x = np.linspace(-2, 2, 5)
    np.testing.assert_array_equal(a(x=x)["mu"], b(x=x)["mu"])
    assert not np.array_equal(a(x=x)["mu"], c(x=x)["mu"])
    mean = create_frozen_predictor(
        model, idata, {"mu": "mu"}, ["x"], num_samples=30, random_seed=3, reducer="mean"
    )
    np.testing.assert_allclose(mean(x=x)["mu"], a(x=x)["mu"].mean(axis=0))
    med = create_frozen_predictor(
        model, idata, {"mu": "mu"}, ["x"], num_samples=30, random_seed=3, reducer=Reducer.MEDIAN
    )
    np.testing.assert_allclose(med(x=x)["mu"], np.median(a(x=x)["mu"], axis=0))


def test_post_processing_numpy_and_pytensor(fitted_linear: tuple) -> None:
    model, idata = fitted_linear
    n = 100
    lo, hi = round((n - 1) * 0.05), round((n - 1) * 0.95)
    pred = create_frozen_predictor(
        model,
        idata,
        {
            "mean": ("mu", lambda v: v.mean(axis=0)),
            "low": ("mu", lambda v: pt.sort(v, axis=0)[lo]),
            "high": ("mu", lambda v: pt.sort(v, axis=0)[hi]),
            "np_q": ("mu", lambda v: np.quantile(np.asarray(v), 0.5, axis=0)),
        },
        ["x"],
        num_samples=n,
        random_seed=42,
    )
    out = pred(x=np.linspace(-2, 2, 20))
    assert {k: v.shape for k, v in out.items()} == {k: (20,) for k in out}
    assert np.all(out["low"] <= out["mean"]) and np.all(out["mean"] <= out["high"])
    with pytest.raises(MapiError, match="in-graph"):
        pred.predict({"x": xr.DataArray(np.zeros(3), dims=["obs"])})


def test_xarray_predict_wraps_samples_and_scenarios(fitted_linear: tuple) -> None:
    model, idata = fitted_linear
    pred = create_frozen_predictor(model, idata, {"mu": "mu"}, ["x"], num_samples=20, random_seed=0)
    single = pred.predict({"x": xr.DataArray(np.linspace(0, 1, 4), dims=["obs"])})
    assert single["mu"].dims == ("sample", "obs") and single["chain"].size == 20
    grid = xr.DataArray(
        np.ones((3, 4)) * np.array([0.5, 1.0, 2.0])[:, None],
        dims=["scenario", "obs"],
        coords={"scenario": ["low", "mid", "high"]},
    )
    out = pred.predict({"x": grid})
    assert out["mu"].dims == ("sample", "scenario", "obs")
    assert list(out["scenario"].values) == ["low", "mid", "high"]
    with pytest.raises(MapiError) as err:
        pred()
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


def test_input_dependent_observed_variables_use_frozen_icdf() -> None:
    rng = np.random.default_rng(42)
    n = 30
    spend = rng.uniform(0.2, 0.8, n)
    visits = 100 + 50 * spend + rng.normal(0, 5, n)
    purchases = 10 + 0.1 * visits + rng.normal(0, 3, n)
    with pm.Model(coords={"time": np.arange(n)}) as model:
        s = pm.Data("spend", spend, dims="time")
        mv = pm.Deterministic(
            "mu_visits", pm.Normal("av", 100, 30) + pm.Normal("bs", 50, 20) * s, dims="time"
        )
        v = pm.Normal("visits", mv, pm.HalfNormal("sv", 20), observed=visits, dims="time")
        mp = pm.Deterministic(
            "mu_purchases", pm.Normal("ap", 10, 10) + pm.Normal("bv", 0.1, 0.05) * v, dims="time"
        )
        pm.Normal("purchases", mp, pm.HalfNormal("sp", 10), observed=purchases, dims="time")
    idata = _sample(model, draws=60, tune=60)
    stage2 = create_frozen_predictor(
        model, idata, {"p": "mu_purchases"}, ["spend"], num_samples=40, random_seed=1
    )
    lo = stage2(spend=np.full(5, 0.2))["p"].mean()
    hi = stage2(spend=np.full(5, 0.8))["p"].mean()
    assert hi > lo  # spend raises visits, which raise purchases
    again = create_frozen_predictor(
        model, idata, {"p": "mu_purchases"}, ["spend"], num_samples=40, random_seed=1
    )
    np.testing.assert_array_equal(
        stage2(spend=np.full(5, 0.5))["p"], again(spend=np.full(5, 0.5))["p"]
    )
    stage1 = create_frozen_predictor(model, idata, {"v": "mu_visits"}, ["spend"], num_samples=10)
    assert stage1(spend=np.linspace(0, 1, 12))["v"].shape == (10, 12)


def test_protected_dims_keep_their_length() -> None:
    rng = np.random.default_rng(42)
    n = 20
    with pm.Model(coords={"date": np.arange(n)}) as model:
        spend = pm.Data("spend", rng.uniform(10, 100, n), dims="date")
        base = pm.Data("baseline", rng.uniform(50, 150, n), dims="date")
        beta = pm.Normal("beta", 0.5, 0.2)
        sat = pm.Deterministic("saturated", spend * beta, dims="date")
        total = pm.Deterministic("total", sat + base, dims="date")
        pm.Normal(
            "y", total, pm.HalfNormal("sigma", 20), observed=rng.normal(100, 10, n), dims="date"
        )
    idata = _sample(model, draws=30, tune=30)
    sat_pred = create_frozen_predictor(model, idata, {"s": "saturated"}, ["spend"], num_samples=10)
    assert sat_pred(spend=np.linspace(0, 200, 100))["s"].shape == (10, 100)
    tot_pred = create_frozen_predictor(model, idata, {"t": "total"}, ["spend"], num_samples=10)
    assert tot_pred(spend=np.linspace(0, 200, n))["t"].shape == (10, n)
    with pytest.raises(Exception):  # noqa: B017 - the baseline fixes the date length
        tot_pred(spend=np.linspace(0, 200, 100))
    _, _, anc, _ = _find_needed_nodes(model, [model["saturated"]], ["spend"])
    assert _find_protected_dims(model, [model["saturated"]], {"spend"}, anc)[0] == set()
    _, _, anc2, _ = _find_needed_nodes(model, [model["total"]], ["spend"])
    assert "date" in _find_protected_dims(model, [model["total"]], {"spend"}, anc2)[0]


def test_remove_specify_shape() -> None:
    x = pt.vector("x")
    y = pt.specify_shape(x, (5,)) * 2
    (out,) = _remove_specify_shape([y], set(), {})
    assert out.eval({x: np.ones(3)}).shape == (3,)
    assert _remove_specify_shape([x * 2], set(), {})[0] is not None
    named = pt.specify_shape(x, (5,))
    named.name = "keep"
    z = named + 1
    x.name = "keep"
    kept = _remove_specify_shape([z], {"d"}, {"keep": ("d",)})
    with pytest.raises(Exception):  # noqa: B017 - protected: still length 5
        kept[0].eval({x: np.ones(3)})


def test_errors_and_spec(fitted_linear: tuple) -> None:
    model, idata = fitted_linear
    with pytest.raises(MapiError, match="not found"):
        create_frozen_predictor(model, idata, {"mu": "mu"}, ["nope"])
    without = idata["posterior"].to_dataset().drop_vars("beta")
    with pytest.raises(MapiError) as err:
        create_frozen_predictor(model, without, {"mu": "mu"}, ["x"])
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    with pytest.raises(MapiError, match="chain and draw"):
        create_frozen_predictor(model, xr.Dataset({"beta": ("k", [1.0])}), {"mu": "mu"}, ["x"])
    spec = FrozenPredictorSpec(response_exprs={"y": "mu"}, input_vars=["x"], reducer="mean")
    assert spec.reducer is Reducer.MEAN
    with pytest.raises(ValidationError):
        FrozenPredictorSpec(response_exprs={}, input_vars=["x"])
    with pytest.raises(ValidationError):
        FrozenPredictorSpec(response_exprs={"y": "mu"}, input_vars=["x"], extra=1)  # type: ignore[call-arg]


def _canonical_payload(bundle: Bundle) -> xr.Dataset:
    payload = radon_dataset(counties=3, per_county=4, seed=9, obs_start=48).drop_vars("y")
    payload = payload.assign_coords(county=["county_0", "county_2", "county_5"])
    rs = bundle.resolved
    canon, _ = canonicalise_coords(
        payload, bundle.fit_coords[rs.model_stage], rs.model.input_schema, pad_extend=False
    )
    return canon


def test_fast_score_matches_exact_within_mcse(radon_bundle: Bundle) -> None:
    plugin = RadonModel()
    ds = _canonical_payload(radon_bundle)
    full = check_fast_score(plugin, radon_bundle.model, ds)
    assert full.ok and full.samples == 300 and full.max_z["y"] < 1e-6  # identical draws
    sub = check_fast_score(plugin, radon_bundle.model, ds, num_samples=120, seed=4)
    assert sub.ok and sub.samples == 120 and 0 < sub.max_z["y"] <= 4.0
    xs = np.linspace(0, 1, ds.sizes["obs"])
    other = check_fast_score(
        plugin, radon_bundle.model, ds.assign(floor=("obs", xs)), inputs={"floor": xs}
    )
    assert other.ok
    report = FastScoreReport(max_z={"y": 9.0})
    assert not report.ok
    with pytest.raises(AssertionError, match="MCSE"):
        report.raise_for_failure()
    fast = plugin.frozen_predictor(radon_bundle.model, ds, num_samples=10, seed=1)
    assert fast.predict({"floor": ds["floor"]})["y"].dims == ("sample", "obs")


def test_frozen_predictor_requires_a_spec(radon_bundle: Bundle) -> None:
    class NoFast(RadonModel):
        id = "test.nofast"
        fast_score_spec = None

    with pytest.raises(MapiError) as err:
        NoFast().frozen_predictor(radon_bundle.model, _canonical_payload(radon_bundle))
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    with pytest.raises(AssertionError, match="fast_score_spec"):
        check_fast_score(NoFast(), radon_bundle.model, _canonical_payload(radon_bundle))
