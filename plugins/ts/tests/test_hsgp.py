"""HSGP numerics: basis, spectral densities and the domain fixed by anticipated coords."""

from __future__ import annotations

import numpy as np
import pytensor.tensor as pt
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_plugin_ts.hsgp import (
    HSGPDomain,
    hsgp_basis,
    kernel_covariance,
    spectral_density,
    sqrt_eigenvalues,
)

KERNELS = ("expquad", "matern52", "matern32")


def test_basis_is_orthonormal_on_the_interval() -> None:
    L = 1.5
    z = np.linspace(-L, L, 20001)[1:-1]
    phi = hsgp_basis(z, 8, L)
    gram = np.trapezoid(phi[:, :, None] * phi[:, None, :], z, axis=0)
    np.testing.assert_allclose(gram, np.eye(8), atol=1e-3)


@pytest.mark.parametrize("kernel", KERNELS)
def test_approximation_matches_the_exact_covariance_inside_the_domain(kernel: str) -> None:
    L, m, ls, eta = 2.0, 80, 0.4, 1.3
    z = np.linspace(-1.0, 1.0, 41)
    phi = hsgp_basis(z, m, L)
    psd = spectral_density(kernel, sqrt_eigenvalues(m, L), ls, eta)  # type: ignore[arg-type]
    approx = (phi * psd) @ phi.T
    exact = kernel_covariance(kernel, z, z, ls, eta)  # type: ignore[arg-type]
    assert np.abs(approx - exact).max() < 0.03 * eta**2


@pytest.mark.parametrize("kernel", KERNELS)
def test_spectral_density_numpy_and_pytensor_agree(kernel: str) -> None:
    w = np.linspace(0.1, 6.0, 7)
    ls = pt.scalar("ls")
    eta = pt.scalar("eta")
    graph = spectral_density(kernel, w, ls, eta)  # type: ignore[arg-type]
    got = graph.eval({ls: 0.3, eta: 2.0})
    want = spectral_density(kernel, w, 0.3, 2.0)  # type: ignore[arg-type]
    np.testing.assert_allclose(got, want, rtol=1e-10)


def test_spectral_density_integrates_to_the_variance() -> None:
    # S is the Fourier transform of k: (1/pi) * int_0^inf S(w) dw == k(0) == eta^2
    w = np.linspace(0.0, 400.0, 400001)
    for kernel in KERNELS:
        s = spectral_density(kernel, w, 0.5, 1.7)  # type: ignore[arg-type]
        assert np.trapezoid(s, w) / np.pi == pytest.approx(1.7**2, rel=2e-3)


def test_unknown_kernel_and_bad_inputs() -> None:
    with pytest.raises(ValueError, match="unknown kernel"):
        spectral_density("rq", np.ones(2), 1.0, 1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown kernel"):
        kernel_covariance("rq", np.ones(2), np.ones(2), 1.0, 1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="inside"):
        hsgp_basis(np.array([2.0]), 4, 1.5)
    with pytest.raises(ValueError, match="at least 1"):
        sqrt_eigenvalues(0, 1.5)
    with pytest.raises(ValueError, match="greater than 1"):
        HSGPDomain.from_positions(np.arange(5.0), 1.0)
    with pytest.raises(ValueError, match="at least one"):
        HSGPDomain.from_positions(np.array([]), 1.5)


def test_domain_maps_the_anticipated_range_onto_unit_interval() -> None:
    anticipated = np.arange(0, 120 + 24, dtype=float)  # 10 years + 24 months
    dom = HSGPDomain.from_positions(anticipated, 1.5)
    z = dom.scale(anticipated)
    assert z.min() == pytest.approx(-1.0) and z.max() == pytest.approx(1.0)
    assert dom.L == 1.5
    assert dom.contains(anticipated)
    # [-c, c] reaches (c - 1) half-widths (a quarter of the range here) past each end
    assert dom.contains(np.array([anticipated.max() + 0.2 * 143]))
    assert not dom.contains(np.array([anticipated.max() + 0.3 * 143]))
    single = HSGPDomain.from_positions(np.array([3.0]), 1.2)
    assert single.half_width == 1.0 and single.scale(np.array([3.0]))[0] == 0.0


@settings(max_examples=40, deadline=None)
@given(
    m=st.integers(1, 40),
    c=st.floats(1.05, 4.0),
    zs=st.lists(st.floats(-1.0, 1.0), min_size=1, max_size=30),
)
def test_basis_shape_and_bound(m: int, c: float, zs: list[float]) -> None:
    phi = hsgp_basis(np.asarray(zs), m, c)
    assert phi.shape == (len(zs), m)
    assert np.all(np.abs(phi) <= np.sqrt(1.0 / c) + 1e-12)
    eig = sqrt_eigenvalues(m, c)
    assert np.all(np.diff(eig) > 0) and eig[0] == pytest.approx(np.pi / (2 * c))
