"""HMM numerics: the forward filter against brute force, the pytensor likelihood,
propagation, relabelling and the filter's continuation property."""

from __future__ import annotations

import itertools

import numpy as np
import pytensor.tensor as pt
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_plugin_ts.hmm_math import (
    forward_filter,
    gaussian_logpdf,
    hmm_loglik,
    propagate,
    relabel_permutation,
    stationary,
)


def _params(rng: np.random.Generator, s: int, k: int) -> tuple[np.ndarray, ...]:
    pi = rng.dirichlet(np.ones(k), size=s)
    a = rng.dirichlet(np.ones(k), size=(s, k))
    mu = rng.normal(0, 2, size=(s, k))
    sigma = rng.uniform(0.3, 1.5, size=(s, k))
    return pi, a, mu, sigma


def _brute_loglik(pi, a, mu, sigma, y, mask) -> float:  # type: ignore[no-untyped-def]
    """log p(y) summing over every hidden path (masked steps contribute no emission)."""
    k, t = pi.size, y.size
    total = 0.0
    for path in itertools.product(range(k), repeat=t):
        p = pi[path[0]]
        for i in range(1, t):
            p *= a[path[i - 1], path[i]]
        for i in range(t):
            if mask[i]:
                p *= np.exp(gaussian_logpdf(y[i], mu[path[i]], sigma[path[i]]))
        total += p
    return float(np.log(total))


@pytest.mark.parametrize("k", [2, 3])
def test_forward_loglik_equals_brute_force(k: int) -> None:
    rng = np.random.default_rng(k)
    pi, a, mu, sigma = _params(rng, 1, k)
    y = rng.normal(size=(2, 6))
    mask = np.ones_like(y, dtype=bool)
    mask[1, 2] = mask[1, 5] = False
    res = forward_filter(pi, a, mu, sigma, y, mask)
    for n in range(2):
        want = _brute_loglik(pi[0], a[0], mu[0], sigma[0], y[n], mask[n])
        assert res.loglik[0, n] == pytest.approx(want, rel=1e-10)
    np.testing.assert_allclose(res.filtered.sum(-1), 1.0)
    np.testing.assert_allclose(res.predicted[:, :, 0], np.broadcast_to(pi[:, None], (1, 2, k)))
    # a masked step only propagates
    np.testing.assert_allclose(res.filtered[0, 1, 2], res.predicted[0, 1, 2])


def test_nan_targets_count_as_missing() -> None:
    rng = np.random.default_rng(3)
    pi, a, mu, sigma = _params(rng, 2, 2)
    y = rng.normal(size=(1, 5))
    y_nan = y.copy()
    y_nan[0, 3] = np.nan
    mask = np.ones_like(y, dtype=bool)
    with_nan = forward_filter(pi, a, mu, sigma, y_nan, mask)
    explicit = mask.copy()
    explicit[0, 3] = False
    masked = forward_filter(pi, a, mu, sigma, y, explicit)
    np.testing.assert_allclose(with_nan.loglik, masked.loglik)
    np.testing.assert_allclose(with_nan.filtered, masked.filtered)


@pytest.mark.parametrize("t", [1, 7])
def test_pytensor_likelihood_matches_numpy(t: int) -> None:
    rng = np.random.default_rng(11)
    pi, a, mu, sigma = _params(rng, 1, 3)
    y = rng.normal(size=(3, t))
    mask = rng.uniform(size=y.shape) > 0.2
    y[~mask] = np.nan
    graph = hmm_loglik(
        pt.log(pt.as_tensor(pi[0])), pt.log(pt.as_tensor(a[0])), pt.as_tensor(mu[0]),
        pt.as_tensor(sigma[0]), y, mask,
    )  # fmt: skip
    want = forward_filter(pi, a, mu, sigma, y, mask).loglik.sum()
    assert float(graph.eval()) == pytest.approx(want, rel=1e-9)


def test_propagate_equals_matrix_powers_in_any_order() -> None:
    rng = np.random.default_rng(5)
    _, a, _, _ = _params(rng, 2, 3)
    alpha = rng.dirichlet(np.ones(3), size=(2, 4))  # (S, N, K)
    steps = np.array([3, 0, 1, 3, 7])
    out = propagate(alpha, a, steps)
    assert out.shape == (2, 4, 5, 3)
    for s in range(2):
        for j, h in enumerate(steps):
            want = alpha[s] @ np.linalg.matrix_power(a[s], int(h))
            np.testing.assert_allclose(out[s, :, j], want)
    assert propagate(alpha, a, np.array([], dtype=int)).shape == (2, 4, 0, 3)
    with pytest.raises(ValueError, match="non-negative"):
        propagate(alpha, a, np.array([-1]))


def test_stationary_distribution_is_a_fixed_point() -> None:
    rng = np.random.default_rng(8)
    _, a, _, _ = _params(rng, 4, 3)
    p = stationary(a)
    np.testing.assert_allclose(p.sum(-1), 1.0)
    np.testing.assert_allclose(np.einsum("sk,skj->sj", p, a), p, atol=1e-10)
    far = propagate(np.full((4, 3), 1 / 3), a, np.array([400]))[:, 0]
    np.testing.assert_allclose(far, p, atol=1e-6)


def test_relabel_permutation_sorts_means() -> None:
    mu = np.array([[2.0, -1.0, 0.5], [0.0, 1.0, 2.0]])
    perm = relabel_permutation(mu)
    np.testing.assert_array_equal(np.take_along_axis(mu, perm, -1), np.sort(mu, -1))
    np.testing.assert_array_equal(perm[1], [0, 1, 2])


@settings(max_examples=30, deadline=None)
@given(
    seed=st.integers(0, 10_000),
    k=st.integers(2, 4),
    t=st.integers(2, 12),
    cut=st.integers(1, 11),
)
def test_filtering_in_two_parts_equals_filtering_at_once(
    seed: int, k: int, t: int, cut: int
) -> None:
    """The terminal-state contract: continuing from alpha_T is the same as one long pass."""
    cut = min(cut, t - 1)
    rng = np.random.default_rng(seed)
    pi, a, mu, sigma = _params(rng, 2, k)
    y = rng.normal(size=(3, t))
    mask = rng.uniform(size=y.shape) > 0.25
    full = forward_filter(pi, a, mu, sigma, y, mask)
    head = forward_filter(pi, a, mu, sigma, y[:, :cut], mask[:, :cut])
    prior = propagate(head.filtered[:, :, -1], a, np.array([1]))[:, :, 0]
    tail = forward_filter(prior, a, mu, sigma, y[:, cut:], mask[:, cut:])
    np.testing.assert_allclose(tail.filtered, full.filtered[:, :, cut:], atol=1e-10)
    np.testing.assert_allclose(head.loglik + tail.loglik, full.loglik, rtol=1e-9)
    np.testing.assert_allclose(full.filtered.sum(-1), 1.0)
    assert np.all(full.filtered >= 0)
