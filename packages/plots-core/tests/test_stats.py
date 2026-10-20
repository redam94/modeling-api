"""The numpy diagnostics behind the recipes, checked against ArviZ and brute force."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra.numpy import arrays

pytest.importorskip("xarray")

from mapi_plots.recipes import _stats

pytestmark = pytest.mark.unit


def _chains(seed: int, shift: float = 0.0, ar: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((4, 300))
    for t in range(1, x.shape[1]):
        x[:, t] += ar * x[:, t - 1]
    x[1] += shift
    return x


@pytest.mark.parametrize(("shift", "ar"), [(0.0, 0.0), (0.0, 0.8), (0.5, 0.3), (2.0, 0.0)])
def test_matches_arviz(shift: float, ar: float) -> None:
    az = pytest.importorskip("arviz")
    x = _chains(7, shift, ar)
    assert _stats.rhat(x) == pytest.approx(float(np.asarray(az.rhat(x, method="rank"))), rel=1e-9)
    assert _stats.ess_bulk(x) == pytest.approx(
        float(np.asarray(az.ess(x, method="bulk"))), rel=1e-9
    )
    assert _stats.ess_tail(x) == pytest.approx(
        float(np.asarray(az.ess(x, method="tail", prob=(0.05, 0.95)))), rel=1e-9
    )


def test_diagnostics_behave() -> None:
    good, bad = _chains(1), _chains(1, shift=3.0)
    assert _stats.rhat(good) < 1.01 < _stats.rhat(bad)
    assert _stats.ess_bulk(_chains(2, ar=0.9)) < _stats.ess_bulk(good)
    for fn in (_stats.rhat, _stats.ess_bulk, _stats.ess_tail):
        assert np.isnan(fn(np.ones((2, 50))))
        assert np.isnan(fn(np.zeros((2, 2))))
        assert np.isnan(fn(np.array([[1.0, np.nan, 2.0, 3.0, 4.0]] * 2)))


def test_rankdata_averages_ties() -> None:
    np.testing.assert_array_equal(
        _stats._rankdata(np.array([3.0, 1.0, 3.0, 2.0])), [3.5, 1.0, 3.5, 2.0]
    )


@settings(max_examples=60, deadline=None)
@given(
    arrays(np.float64, st.integers(5, 60), elements=st.floats(-1e3, 1e3)),
    st.floats(0.1, 0.95),
)
def test_hdi_is_the_narrowest_covering_window(x: np.ndarray, prob: float) -> None:
    lo, hi = _stats.hdi(x, prob)
    s = np.sort(x)
    k = max(1, min(len(s) - 1, int(np.floor(prob * len(s)))))
    widths = [s[i + k] - s[i] for i in range(len(s) - k)]
    assert hi - lo == pytest.approx(min(widths), abs=1e-9)
    assert np.mean((x >= lo) & (x <= hi)) >= k / len(x) - 1e-12


def test_hdi_eti_kde_pit_coverage_waic() -> None:
    rng = np.random.default_rng(0)
    x = rng.standard_normal(4000)
    lo, hi = _stats.hdi(x, 0.9)
    assert lo == pytest.approx(-1.645, abs=0.1) and hi == pytest.approx(1.645, abs=0.1)
    elo, ehi = _stats.eti(x, 0.9)
    assert ehi == pytest.approx(1.645, abs=0.1)
    assert elo == pytest.approx(-1.645, abs=0.1)
    with pytest.raises(ValueError):
        _stats.hdi(x, 1.5)
    grid, dens = _stats.kde(x)
    assert np.trapezoid(dens, grid) == pytest.approx(1.0, abs=0.02)
    g, d = _stats.kde(rng.standard_normal(100), points=50)
    assert g.size == 50 and np.trapezoid(d, g) == pytest.approx(1.0, abs=0.05)
    assert _stats.kde(np.array([np.nan]))[1].sum() == 0
    assert np.isfinite(_stats.kde(np.array([2.0, 2.0]))[1]).all()
    draws = rng.standard_normal((500, 300))
    y = rng.standard_normal(300)
    pit = _stats.pit_values(draws, y)
    assert abs(pit.mean() - 0.5) < 0.05 and pit.min() >= 0 and pit.max() <= 1
    cov = dict(_stats.coverage_curve(draws, y, [0.5, 0.9]))
    assert cov[0.5] == pytest.approx(0.5, abs=0.1) and cov[0.9] == pytest.approx(0.9, abs=0.06)
    ll = -0.5 * (y[None, :] - 0.05 * rng.standard_normal((400, 1))) ** 2
    elpd, se, p, pw = _stats.waic(ll)
    assert elpd == pytest.approx(pw.sum()) and se > 0 and p >= 0
    brute = np.log(np.mean(np.exp(ll), axis=0)) - np.var(ll, axis=0, ddof=1)
    np.testing.assert_allclose(pw, brute, rtol=1e-10)
    assert np.isnan(_stats.waic(ll[:, :1])[1])


def _loo_example(seed: int, draws: int) -> np.ndarray:
    """``(S, N)`` log-likelihood of a normal model with one outlier."""
    rng = np.random.default_rng(seed)
    y = rng.normal(size=30)
    y[5] = 6.0
    mu = rng.normal(y.mean(), 0.2, draws)
    sigma = np.abs(rng.normal(1.3, 0.15, draws))
    return -0.5 * ((y[None] - mu[:, None]) / sigma[:, None]) ** 2 - np.log(sigma[:, None])


@pytest.mark.parametrize("draws", [120, 1000])
def test_psis_matches_arviz(draws: int) -> None:
    pytest.importorskip("arviz")
    array_stats = pytest.importorskip("arviz_stats.base").array_stats
    ll = _loo_example(3, draws)
    log_w, k = _stats.psis(-ll)
    _, ak = array_stats.psislw(ll.T, r_eff=1, axis=-1)
    assert np.max(np.abs(k - np.asarray(ak))) < 0.05
    assert np.allclose(np.exp(log_w).sum(axis=0), 1.0)
    assert k[5] == k.max() and k[5] > 0.7


def test_psis_short_tail_and_flat_weights() -> None:
    log_w, k = _stats.psis(np.zeros((10, 3)))  # 10 draws: tail too short to fit
    assert np.isinf(k).all() and np.allclose(np.exp(log_w), 0.1)
    rng = np.random.default_rng(0)
    log_w, k = _stats.psis(rng.normal(scale=0.01, size=(500, 4)))
    assert (k < 0.5).all() and np.allclose(np.exp(log_w).sum(axis=0), 1.0)
