"""Last-layer Laplace and draw selection."""

from __future__ import annotations

import numpy as np
import pytest
from mapi_jax.uncertainty import (
    LaplacePosterior,
    draw_positions,
    fit_laplace,
    gauss_newton,
    laplace_logits,
    laplace_thetas,
    n_draws,
)


def _data(n: int = 60, f: int = 3, k: int = 3) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    return rng.normal(size=(n, f)), rng.normal(size=(f, k)), rng.normal(size=k)


def test_full_and_diag_agree_on_the_diagonal() -> None:
    feats, w, b = _data()
    theta = np.concatenate([w, b[None]]).reshape(-1)
    full = gauss_newton(feats, theta, 3, task="classification", full=True)
    diag = gauss_newton(feats, theta, 3, task="classification", full=False)
    assert full.shape == (12, 12) and diag.shape == (12,)
    np.testing.assert_allclose(np.diag(full), diag, rtol=1e-10)
    np.testing.assert_allclose(full, full.T, atol=1e-10)
    assert np.linalg.eigvalsh(full).min() > -1e-8  # positive semi-definite


@pytest.mark.parametrize("hessian", ["full", "diag"])
def test_laplace_draws_are_probabilities(hessian: str) -> None:
    feats, w, b = _data()
    post = fit_laplace(feats, w, b, task="classification", hessian=hessian)  # type: ignore[arg-type]
    assert post.hessian == hessian and 1e-4 <= post.prior_precision <= 1e4
    assert (post.chol is not None) == (hessian == "full")
    thetas = laplace_thetas(post, np.arange(5), seed=1)
    logits = laplace_logits(feats[:4], thetas, 3)
    assert logits.shape == (5, 4, 3)
    z = np.exp(logits - logits.max(-1, keepdims=True))
    probs = z / z.sum(-1, keepdims=True)
    np.testing.assert_allclose(probs.sum(-1), 1.0)
    assert np.std(logits, axis=0).mean() > 0  # draws differ
    # a draw is reproducible on its own (any subset gives the same values)
    np.testing.assert_array_equal(laplace_thetas(post, np.array([3]), seed=1)[0], thetas[3])
    again = LaplacePosterior.from_tensors(
        post.tensors(), prior_precision=post.prior_precision, n_out=3
    )
    np.testing.assert_array_equal(laplace_thetas(again, np.arange(5), seed=1), thetas)


def test_auto_switches_to_diag_above_the_cap() -> None:
    rng = np.random.default_rng(0)
    feats = rng.normal(size=(30, 2100))
    post = fit_laplace(feats, rng.normal(size=(2100, 1)), np.zeros(1), task="regression")
    assert post.hessian == "diag"


def test_given_prior_precision_is_kept_and_regression_noise() -> None:
    feats, w, b = _data(k=1)
    post = fit_laplace(feats, w, b, task="regression", prior_precision=3.0, noise_var=0.5)
    assert post.prior_precision == 3.0
    # more noise -> wider posterior
    wide = fit_laplace(feats, w, b, task="regression", prior_precision=3.0, noise_var=50.0)
    assert np.trace(wide.chol @ wide.chol.T) > np.trace(post.chol @ post.chol.T)  # type: ignore[operator]


def test_draw_counts_and_positions() -> None:
    assert n_draws("none", 50, 200) == 1
    assert n_draws("laplace", 7, 200) == 7
    assert n_draws("laplace", None, 5000) == 1000
    assert n_draws("mc_dropout", None, None) == 100
    np.testing.assert_array_equal(draw_positions(4, None), [0, 1, 2, 3])
    pairs = np.array([[0, 2], [1, 0], [0, 9], [0, 1], [0, 2]])
    np.testing.assert_array_equal(draw_positions(4, pairs), [1, 2])  # ascending
    np.testing.assert_array_equal(draw_positions(3, np.array([7])), [0, 1, 2])


def test_network_draws_by_method() -> None:
    from mapi_jax.models.base import Normalize, network_logits
    from mapi_jax.nets import ArchSpec, build_network

    arch = ArchSpec(kind="resnet", channels=1, widths=[4], blocks=[1], groups=2, dropout=0.5)
    net = build_network(arch, 3, seed=0)
    raw = np.random.default_rng(0).integers(0, 255, size=(5, 8, 8, 1)).astype(np.uint8)
    unit = Normalize((0.0,), (1.0,), "unit")

    def run(method: str, positions: np.ndarray) -> np.ndarray:
        return network_logits(
            net,
            [raw[:3], raw[3:]],
            unit,
            4,
            method=method,
            positions=positions,
            seed=0,
            laplace=None,
            n_out=3,
        )

    none = run("none", np.zeros(1, dtype=np.int64))
    assert none.shape == (1, 5, 3)
    mc = run("mc_dropout", np.arange(4))
    assert mc.shape == (4, 5, 3) and float(mc.std(axis=0).mean()) > 0
    # each draw is reproducible on its own
    np.testing.assert_array_equal(run("mc_dropout", np.array([2]))[0], mc[2])


def _reference_ggn(feats: np.ndarray, theta: np.ndarray, k: int) -> np.ndarray:
    """The full GGN from per-row Kronecker products (small inputs only)."""
    x = np.concatenate([feats, np.ones((feats.shape[0], 1))], axis=1)
    z = x @ theta.reshape(x.shape[1], k)
    prob = np.exp(z - z.max(1, keepdims=True))
    prob /= prob.sum(1, keepdims=True)
    return sum(
        np.kron(np.outer(xn, xn), np.diag(pn) - np.outer(pn, pn))
        for xn, pn in zip(x, prob, strict=True)
    )


def test_the_full_ggn_matches_its_definition() -> None:
    feats, w, b = _data(n=40, f=4, k=3)
    theta = np.concatenate([w, b[None]]).reshape(-1)
    full = gauss_newton(feats, theta, 3, task="classification", full=True)
    np.testing.assert_allclose(full, _reference_ggn(feats, theta, 3), atol=1e-10)


def test_the_full_ggn_of_a_wide_head_stays_small_in_memory() -> None:
    """P = 1024 x 2 = 2048 (``hessian: auto`` picks full): no (rows, F^2) work array (that
    was 17 GB at 2048 rows); the peak is the P x P result plus small chunks."""
    import tracemalloc

    rng = np.random.default_rng(0)
    feats, theta = rng.normal(size=(300, 1023)), rng.normal(size=2048) * 0.01
    tracemalloc.start()
    try:
        full = gauss_newton(feats, theta, 2, task="classification", full=True)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert full.shape == (2048, 2048)
    assert peak < 160 * 1024**2, peak  # the result is 32 MiB
    # the same values as the per-output blocks of a small slice
    sub = gauss_newton(feats[:5, :3], theta[:8], 2, task="classification", full=True)
    np.testing.assert_allclose(sub, _reference_ggn(feats[:5, :3], theta[:8], 2), atol=1e-10)
