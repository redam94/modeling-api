"""The numpy Kalman filter, smoother and simulator of ``ts.bsts`` (fast, no MCMC).

The parity tests build the pymc-extras model, take prior draws, and check that the numpy
log-likelihood of each draw equals the ``obs`` log-density PyMC samples from.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from mapi_plugin_ts.bsts import (
    StructureSettings,
    build_model,
    system_from_posterior,
)
from mapi_plugin_ts.ss_math import (
    StructuralSystem,
    crn_normal,
    kalman_filter,
    one_step_draws,
    rts_smoother,
    simulate_ahead,
)

DRAWS = 5


def prior_draws(model: pm.Model, draws: int = DRAWS, seed: int = 1) -> xr.Dataset:
    """Prior draws of the free variables, shaped like a posterior (chain, draw)."""
    free = [rv.name for rv in model.free_RVs]
    with model:
        tree = pm.sample_prior_predictive(draws=draws, var_names=free, random_seed=seed)
    prior = tree["prior"]
    return prior.to_dataset(inherit=False) if isinstance(prior, xr.DataTree) else prior


def obs_logp(model: pm.Model, draws: xr.Dataset) -> np.ndarray:
    """``logp(obs)`` of the model at every (chain, draw) of ``draws``."""
    fn = model.compile_logp(vars=[model["obs"]])
    flat = {
        rv.name: np.asarray(draws[rv.name].transpose("chain", "draw", ...).values).reshape(
            -1, *draws[rv.name].shape[2:]
        )
        for rv in model.free_RVs
    }
    n = next(iter(flat.values())).shape[0]
    out = []
    for i in range(n):
        point: dict[str, Any] = {}
        for rv in model.free_RVs:
            value = flat[rv.name][i]
            tr = model.rvs_to_transforms[rv]
            if tr is not None:
                value = tr.forward(value, *rv.owner.inputs).eval()
            point[model.rvs_to_values[rv].name] = value
        out.append(float(fn(point)))
    return np.asarray(out)


def series(n: int = 36, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    y = np.cumsum(rng.normal(0.0, 0.3, n)) + np.sin(np.arange(n) * 2 * np.pi / 4)
    y = (y - y.mean()) / y.std()
    y[[4, 17, 18]] = np.nan
    return y


@pytest.mark.parametrize(
    "settings",
    [
        {"trend": "level"},
        {"trend": "local_linear", "seasonality": [{"period": 4, "stochastic": True}]},
        {"trend": "smooth", "seasonality": [{"period": 12, "harmonics": 2}]},
    ],
    ids=["level", "local_linear+dummy4", "smooth+harmonics"],
)
def test_filter_loglik_equals_the_pymc_extras_likelihood(settings: dict[str, Any]) -> None:
    s = StructureSettings.model_validate(settings)
    y = series()
    model, ss = build_model(s, y, None, [])
    draws = prior_draws(model)
    system, beta = system_from_posterior(model, ss, draws, [])
    assert beta.shape == (DRAWS, 0)
    fr = kalman_filter(system, y)
    np.testing.assert_allclose(fr.loglik, obs_logp(model, draws), rtol=1e-8)


def _rw_system(s_len: int = 1, q: float = 0.0, h: float = 1.0) -> StructuralSystem:
    one = np.ones((s_len, 1, 1))
    return StructuralSystem(
        T=one.copy(),
        Z=np.ones((s_len, 1)),
        RQR=one * q,
        H=np.full(s_len, h),
        x0=np.zeros((s_len, 1)),
        P0=one.copy(),
    )


def _local_linear(s_len: int = 3) -> StructuralSystem:
    rng = np.random.default_rng(3)
    T = np.broadcast_to(np.array([[1.0, 1.0], [0.0, 1.0]]), (s_len, 2, 2)).copy()
    q = rng.uniform(0.01, 0.1, (s_len, 2))
    return StructuralSystem(
        T=T,
        Z=np.broadcast_to(np.array([1.0, 0.0]), (s_len, 2)).copy(),
        RQR=np.stack([np.diag(v) for v in q]),
        H=rng.uniform(0.1, 0.5, s_len),
        x0=rng.normal(size=(s_len, 2)),
        P0=np.broadcast_to(np.eye(2), (s_len, 2, 2)).copy(),
    )


def test_smoother_of_a_model_without_state_noise_is_flat() -> None:
    y = np.random.default_rng(0).normal(2.0, 1.0, 30)
    y[7] = np.nan
    sys = _rw_system(2, q=0.0)
    fr = kalman_filter(sys, y, keep=True)
    sm = rts_smoother(sys, fr)
    assert sm.shape == (2, 30, 1)
    assert np.ptp(sm[:, :, 0], axis=1).max() < 1e-6
    # with one level for every period, the smoothed level is the shrunken mean of y
    obs = y[np.isfinite(y)]
    np.testing.assert_allclose(sm[0, 0, 0], obs.sum() / (obs.size + 1.0), rtol=1e-6)


def test_smoother_needs_the_kept_moments() -> None:
    sys = _rw_system()
    with pytest.raises(ValueError, match="keep=True"):
        rts_smoother(sys, kalman_filter(sys, np.zeros(3)))


def test_missing_values_skip_the_update() -> None:
    sys = _local_linear()
    y = np.array([0.1, np.nan, 0.3, 0.2])
    fr = kalman_filter(sys, y, keep=True)
    assert fr.filt_mean is not None and fr.pred_mean is not None
    np.testing.assert_allclose(fr.filt_mean[:, 1], fr.pred_mean[:, 1])
    dropped = kalman_filter(sys, y[[0]], keep=False)
    assert np.isfinite(fr.loglik).all() and (fr.loglik < dropped.loglik + 10).all()


def test_filtering_in_two_parts_continues_exactly() -> None:
    sys = _local_linear()
    y = np.random.default_rng(1).normal(size=20)
    d = np.random.default_rng(2).normal(size=(sys.S, 20))
    whole = kalman_filter(sys, y, d)
    first = kalman_filter(sys, y[:12], d[:, :12])
    second = kalman_filter(sys, y[12:], d[:, 12:], start=(first.last_mean, first.last_cov))
    np.testing.assert_allclose(second.last_mean, whole.last_mean, atol=1e-10)
    np.testing.assert_allclose(second.mean, whole.mean[:, 12:], atol=1e-10)
    np.testing.assert_allclose(first.loglik + second.loglik, whole.loglik, rtol=1e-12)


def test_simulate_ahead_expected_is_the_mean_propagation() -> None:
    sys = _local_linear()
    mean = np.random.default_rng(4).normal(size=(sys.S, 2))
    cov = np.broadcast_to(0.1 * np.eye(2), (sys.S, 2, 2)).copy()
    d = np.random.default_rng(5).normal(size=(sys.S, 6))
    expected, predictive = simulate_ahead(sys, mean, cov, 6, d, seed=1, name="f")
    want = np.empty((sys.S, 6))
    for s in range(sys.S):
        x = mean[s]
        for j in range(6):
            x = sys.T[s] @ x
            want[s, j] = sys.Z[s] @ x + d[s, j]
    np.testing.assert_allclose(expected, want, atol=1e-12)
    assert predictive.shape == (sys.S, 6) and not np.allclose(predictive, expected)


def test_common_random_numbers() -> None:
    a = crn_normal(7, "baseline", (3, 4))
    np.testing.assert_array_equal(a, crn_normal(7, "baseline", (3, 4)))
    assert not np.allclose(a, crn_normal(7, "scenario", (3, 4)))
    assert not np.allclose(a, crn_normal(8, "baseline", (3, 4)))
    sys = _local_linear()
    mean, cov = np.zeros((sys.S, 2)), np.broadcast_to(np.eye(2), (sys.S, 2, 2)).copy()
    _, p1 = simulate_ahead(sys, mean, cov, 5, None, seed=3, name="cf")
    _, p2 = simulate_ahead(sys, mean, cov, 5, np.ones((sys.S, 5)), seed=3, name="cf")
    np.testing.assert_allclose(p2 - p1, 1.0, atol=1e-12)  # same noise: only d moves
    _, longer = simulate_ahead(sys, mean, cov, 9, None, seed=3, name="cf")
    np.testing.assert_allclose(longer[:, :5], p1, atol=1e-12)  # horizon-stable


def test_predictive_spread_grows_with_the_horizon() -> None:
    sys = _rw_system(4000, q=0.5, h=0.2)
    mean = np.zeros((sys.S, 1))
    cov = np.full((sys.S, 1, 1), 0.1)
    _, pred = simulate_ahead(sys, mean, cov, 12, None, seed=0, name="rw")
    var = pred.var(axis=0)
    assert (np.diff(var) > 0).all()
    # Var(y_h) = P + h q + H
    np.testing.assert_allclose(var, 0.1 + 0.5 * np.arange(1, 13) + 0.2, rtol=0.1)


def test_one_step_draws_add_d_and_noise() -> None:
    sys = _local_linear()
    y = np.random.default_rng(1).normal(size=8)
    d = np.full((sys.S, 8), 2.0)
    fr = kalman_filter(sys, y, d)
    a = one_step_draws(fr, d, seed=1, name="y_hat")
    np.testing.assert_array_equal(a, one_step_draws(fr, d, seed=1, name="y_hat"))
    z = crn_normal(1, "y_hat", fr.mean.shape)
    np.testing.assert_allclose(a, fr.mean + 2.0 + np.sqrt(fr.var) * z)


def test_take_selects_draws() -> None:
    sys = _local_linear(5)
    sub = sys.take(np.array([4, 1]))
    assert sub.S == 2 and sub.k == 2
    np.testing.assert_array_equal(sub.H, sys.H[[4, 1]])
