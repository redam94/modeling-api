"""The NumPy replay of the fitted model against mmm-framework's own references.

Every check uses draws from mmm-framework's prior of the model (no MCMC) as the
"posterior": the replay must reproduce the graph's registered deterministics (the
component sum identity) and mmm-framework's ``PosteriorForecaster`` to floating point.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_mmm.forward import (
    LEVEL_OFFSET,
    TREND_BY_PERIOD,
    CellRef,
    Posterior,
    Replay,
    adstock_kernels,
    compact_posterior,
    history_arrays,
)
from mapi_plugin_mmm.frequentist import MMMFrequentist

_COMPONENTS = (
    "intercept_component",
    "trend_component",
    "seasonality_component",
    "geo_component",
    "product_component",
    "media_total",
    "controls_total",
)


@pytest.mark.parametrize("kind", ["geometric", "delayed", "weibull", "none"])
@pytest.mark.parametrize("normalize", [True, False])
def test_kernels_match_mmm_frameworks_adstock_weights(kind: str, normalize: bool) -> None:
    from mmm_framework.transforms.adstock import adstock_weights

    rng = np.random.default_rng(0)
    alpha, theta = rng.uniform(0.05, 0.95, 7), rng.uniform(0, 3, 7)
    shape, scale = rng.uniform(0.5, 3, 7), rng.uniform(0.5, 5, 7)
    got = adstock_kernels(kind, 8, normalize=normalize, alpha=alpha, theta=theta, shape=shape,
                          scale=scale, n=7)  # fmt: skip
    for s in range(7):
        want = adstock_weights(
            kind, 8, alpha=alpha[s], theta=theta[s], shape=shape[s], scale=scale[s],
            normalize=normalize,
        )  # fmt: skip
        np.testing.assert_allclose(got[s], want, rtol=1e-12, atol=1e-15)


def _replay(pf: Any) -> tuple[Replay, Any]:
    rp, fit = pf.plugin.replay(pf.fitted)
    return rp, fit


def _graph_mu(pf: Any) -> np.ndarray:
    """``(S, n_cell, T)`` the graph's mean from its registered component deterministics
    (KPI units)."""
    prior = pf.prior
    m = pf.model
    total = 0.0
    for name in _COMPONENTS:
        if name in prior:
            arr = np.asarray(prior[name].values, dtype=float)
            total = total + arr.reshape(arr.shape[0] * arr.shape[1], -1)
    mu = np.asarray(total) * m.y_std + m.y_mean  # (S, n_obs) period-major
    return mu.reshape(mu.shape[0], m.n_periods, m.n_cells).transpose(0, 2, 1)


@pytest.mark.parametrize("which", ["geo_prior_fit", "national_prior_fit"])
def test_the_replay_reproduces_the_graph(which: str, request: pytest.FixtureRequest) -> None:
    pf = request.getfixturevalue(which)
    rp, fit = _replay(pf)
    spend, ctrl = history_arrays(fit)
    comps = rp.evaluate(spend, ctrl, np.arange(fit.n_periods), rp.fit_cells())
    np.testing.assert_allclose(comps.mu, _graph_mu(pf), rtol=1e-9, atol=1e-6)
    ref = np.asarray(pf.prior["channel_contributions"].values, dtype=float)
    ref = ref.reshape(rp.n, fit.n_periods, fit.n_cells, -1).transpose(0, 3, 2, 1) * pf.model.y_std
    np.testing.assert_allclose(comps.channel, ref, rtol=1e-9, atol=1e-6)


@pytest.mark.parametrize("which", ["geo_prior_fit", "national_prior_fit"])
def test_the_replay_matches_mmm_frameworks_forecaster(
    which: str, request: pytest.FixtureRequest
) -> None:
    from mmm_framework.validation.backtest import PosteriorForecaster

    pf = request.getfixturevalue(which)
    rp, fit = _replay(pf)
    m = pf.model
    m._trace = xr.DataTree.from_dict({"posterior": pf.prior})
    fc = PosteriorForecaster(m, strict=True)
    extra = 6
    n_cells = fit.n_cells
    x_full = np.concatenate([m.X_media_raw, m.X_media_raw[-extra * n_cells :] * 1.3])
    c_full = (
        np.concatenate([m.X_controls_raw, m.X_controls_raw[-extra * n_cells :]])
        if m.X_controls_raw is not None
        else None
    )
    want = fc.forecast(x_full, c_full, np.arange(x_full.shape[0]), include_noise=False)
    t_full = fit.n_periods + extra
    want = want.reshape(rp.n, t_full, n_cells).transpose(0, 2, 1)
    spend = x_full.reshape(t_full, n_cells, -1)
    ctrl = (
        c_full.reshape(t_full, n_cells, -1)
        if c_full is not None
        else np.zeros((t_full, n_cells, 0))
    )
    comps = rp.evaluate(spend, ctrl, np.arange(t_full), rp.fit_cells())
    np.testing.assert_allclose(comps.mu, want, rtol=1e-9, atol=1e-6)


def test_the_bundle_posterior_is_compact(geo_prior_fit: Any) -> None:
    post = geo_prior_fit.fitted.posterior
    n_obs = geo_prior_fit.model.n_obs
    for v in post.data_vars:
        assert "obs" not in post[v].dims, v
        assert all(post.sizes[d] != n_obs for d in post[v].dims if d not in ("chain", "draw")), v
    assert post[TREND_BY_PERIOD].sizes["trend_period"] == 52
    assert post[LEVEL_OFFSET].sizes["cell"] == 3
    assert {"intercept", "sigma", "beta_tv", "roi_tv", "adstock_alpha_tv"} <= set(post.data_vars)


def test_windows_inside_the_history_use_the_carry_over(geo_prior_fit: Any) -> None:
    """Scoring the last periods alone gives the same answer as scoring the whole history
    (the replay reads the stored spend in front of the window)."""
    rp, fit = _replay(geo_prior_fit)
    spend, ctrl = history_arrays(fit)
    full = rp.evaluate(spend, ctrl, np.arange(fit.n_periods), rp.fit_cells())
    tail = rp.evaluate(spend, ctrl, np.arange(40, fit.n_periods), rp.fit_cells())
    np.testing.assert_allclose(tail.mu, full.mu[:, :, 40:])
    zeroed = spend.copy()
    zeroed[:40] = 0.0
    cold = rp.evaluate(zeroed, ctrl, np.arange(40, fit.n_periods), rp.fit_cells())
    assert not np.allclose(cold.channel[:, :, :, 0], tail.channel[:, :, :, 0])


def test_new_levels(geo_prior_fit: Any) -> None:
    rp, fit = _replay(geo_prior_fit)
    spend, ctrl = history_arrays(fit)
    pos = np.arange(fit.n_periods)
    cells = [CellRef(0, 0, "north/_"), CellRef(None, 0, "east/_")]
    sp = spend[:, [0, 0]]
    ct = ctrl[:, [0, 0]]
    pooled = rp.evaluate(sp, ct, pos, cells, strategy={"geo": "pooled"})
    # a pooled new geo has the population level: no offset
    np.testing.assert_allclose(
        pooled.baseline[:, 1], (rp.intercept[:, None] * rp.y_std + rp.y_mean) * np.ones(pos.size)
    )
    hyper = rp.evaluate(sp, ct, pos, cells, strategy={"geo": "hyperprior"}, seed=3)
    again = rp.evaluate(sp, ct, pos, cells, strategy={"geo": "hyperprior"}, seed=3)
    np.testing.assert_allclose(hyper.baseline, again.baseline)
    assert not np.allclose(hyper.baseline[:, 1], pooled.baseline[:, 1])
    zero = rp.evaluate(sp, ct, pos, cells, strategy={"geo": "zero"})
    assert (zero.channel[:, :, 1] == 0).all() and (zero.channel[:, :, 0] != 0).any()


def test_linear_trends_are_extrapolated_and_flexible_ones_held(
    geo_prior_fit: Any, national_prior_fit: Any
) -> None:
    rp, _ = _replay(geo_prior_fit)
    t = rp.trend_at(np.array([50, 51, 52, 60]))
    slope = rp.trend_slope[:, None] / 51.0  # type: ignore[index]
    np.testing.assert_allclose(np.diff(t, axis=1)[:, :2], np.broadcast_to(slope, (rp.n, 2)))
    rp2, fit2 = _replay(national_prior_fit)
    held = rp2.trend_at(np.array([fit2.n_periods - 1, fit2.n_periods + 5]))
    np.testing.assert_allclose(held[:, 0], held[:, 1])
    spend, ctrl = history_arrays(fit2)
    comps = rp2.evaluate(
        np.concatenate([spend, spend[-3:]]), np.concatenate([ctrl, ctrl[-3:]]),
        np.arange(fit2.n_periods, fit2.n_periods + 3), rp2.fit_cells(),
    )  # fmt: skip
    assert any("held at its last fitted level" in n for n in comps.notes)


def test_noise_is_common_random_numbers(geo_prior_fit: Any) -> None:
    rp, fit = _replay(geo_prior_fit)
    spend, ctrl = history_arrays(fit)
    pos = np.arange(fit.n_periods)
    a = rp.evaluate(spend, ctrl, pos, rp.fit_cells())
    b = rp.evaluate(spend * 1.5, ctrl, pos, rp.fit_cells())
    np.testing.assert_allclose(a.noise(7), b.noise(7))
    assert not np.allclose(a.noise(7), a.noise(8))


def test_steady_state_is_the_limit_of_a_constant_spend(geo_prior_fit: Any) -> None:
    rp, fit = _replay(geo_prior_fit)
    level = np.array([[300.0, 150.0]] * fit.n_cells)
    want = rp.steady_state(level, rp.fit_cells())  # (S, cell, C)
    long = np.broadcast_to(level, (30, fit.n_cells, 2)).copy()
    got = rp.channel_at(long, np.array([29]), rp.fit_cells())[:, :, :, 0] * rp.y_std
    np.testing.assert_allclose(got.transpose(0, 2, 1), want, rtol=1e-9)


def test_posterior_from_thinned_samples(geo_prior_fit: Any) -> None:
    post = geo_prior_fit.fitted.posterior
    stacked = post.stack(sample=("chain", "draw"), create_index=False).drop_vars(
        ["chain", "draw"], errors="ignore"
    )
    full = Posterior.from_dataset(post)
    thin = Posterior.from_dataset(stacked.isel(sample=[0, 5, 9]))
    np.testing.assert_array_equal(thin.need("intercept"), full.need("intercept")[[0, 5, 9]])


def test_frequentist_traces_replay_too(
    prior_fit_factory: Callable[..., Any], geo_stage: xr.Dataset
) -> None:
    pf = prior_fit_factory(
        geo_stage, {"hierarchy": {"pool_over": ["geo"]}, "adstock": {"l_max": 3}},
        plugin=MMMFrequentist(),
    )  # fmt: skip
    rp, fit = _replay(pf)
    spend, ctrl = history_arrays(fit)
    comps = rp.evaluate(spend, ctrl, np.arange(fit.n_periods), rp.fit_cells())
    np.testing.assert_allclose(comps.mu, _graph_mu(pf), rtol=1e-9, atol=1e-6)


def test_compact_posterior_drops_observation_sized_arrays() -> None:
    post = xr.Dataset(
        {
            "a": (("chain", "draw"), np.zeros((1, 2))),
            "media_total": (("chain", "draw", "media_total_dim_0"), np.zeros((1, 2, 12))),
            "x": (("chain", "draw", "x_dim_0"), np.zeros((1, 2, 12))),
            "trend_component": (
                ("chain", "draw", "trend_component_dim_0"),
                np.arange(24.0).reshape(1, 2, 12),
            ),
        }
    )
    out = compact_posterior(post, n_obs=12, n_cells=3)
    assert set(out.data_vars) == {"a", TREND_BY_PERIOD}
    np.testing.assert_array_equal(out[TREND_BY_PERIOD].values[0, 0], [0, 3, 6, 9])
