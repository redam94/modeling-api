"""Adstock and saturation curves: NumPy and PyTensor agree; shape properties hold."""

from __future__ import annotations

import numpy as np
import pytensor.tensor as pt
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_plugin_mmm.transforms import adstock_weights, saturate, saturation_params

pytestmark = pytest.mark.unit

unit = st.floats(min_value=0.01, max_value=0.95, allow_nan=False)
positive = st.floats(min_value=0.05, max_value=8.0, allow_nan=False)


@given(alpha=st.lists(unit, min_size=1, max_size=4), l_max=st.integers(1, 12))
@settings(max_examples=50, deadline=None)
def test_normalised_geometric_weights_sum_to_one_and_decay(alpha: list[float], l_max: int) -> None:
    a = np.asarray(alpha)[:, None]
    w = adstock_weights("geometric", np.arange(l_max), alpha=a, normalize=True)
    assert w.shape == (len(alpha), l_max)
    np.testing.assert_allclose(w.sum(-1), 1.0)
    assert (np.diff(w, axis=-1) <= 1e-12).all()


@given(alpha=unit, theta=st.floats(0.0, 5.0), l_max=st.integers(2, 10))
@settings(max_examples=50, deadline=None)
def test_delayed_weights_peak_near_theta(alpha: float, theta: float, l_max: int) -> None:
    w = adstock_weights(
        "delayed", np.arange(l_max), alpha=np.asarray([[alpha]]), theta=np.asarray([[theta]])
    )[0]
    peak = int(np.argmax(w))
    assert abs(peak - min(theta, l_max - 1)) <= 0.5 + 1e-9


def test_delayed_needs_theta() -> None:
    with pytest.raises(ValueError, match="theta"):
        adstock_weights("delayed", np.arange(3), alpha=0.5)


@given(lam=positive, x=st.lists(st.floats(0.0, 50.0), min_size=2, max_size=20))
@settings(max_examples=50, deadline=None)
def test_logistic_is_monotone_and_bounded(lam: float, x: list[float]) -> None:
    xs = np.sort(np.asarray(x))
    y = saturate(xs, "logistic", lam=lam)
    assert (y >= 0).all() and (y < 1 + 1e-12).all()
    assert (np.diff(y) >= -1e-12).all()
    assert saturate(np.zeros(1), "logistic", lam=lam)[0] == 0.0


@given(slope=positive, half=positive)
@settings(max_examples=50, deadline=None)
def test_hill_hits_one_half_at_the_half_point(slope: float, half: float) -> None:
    y = saturate(np.asarray([half]), "hill", slope=slope, half=half)
    np.testing.assert_allclose(y, 0.5, rtol=1e-6)


@pytest.mark.parametrize(
    ("kind", "params"),
    [("logistic", {"lam": 1.7}), ("hill", {"slope": 2.0, "half": 0.8}), ("none", {})],
)
def test_numpy_and_pytensor_agree(kind: str, params: dict[str, float]) -> None:
    x = np.linspace(0.0, 3.0, 13)
    want = saturate(x, kind, **params)  # type: ignore[arg-type]
    got = saturate(pt.as_tensor_variable(x), kind, xp=pt, **params).eval()  # type: ignore[arg-type]
    np.testing.assert_allclose(got, want, rtol=1e-10)
    assert set(saturation_params(kind)) == set(params)  # type: ignore[arg-type]
    alpha = np.asarray([[0.3], [0.7]])
    w_np = adstock_weights("geometric", np.arange(5), alpha=alpha)
    w_pt = adstock_weights("geometric", np.arange(5), alpha=pt.as_tensor_variable(alpha), xp=pt)
    np.testing.assert_allclose(w_pt.eval(), w_np, rtol=1e-12)
