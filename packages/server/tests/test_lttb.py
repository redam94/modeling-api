"""LTTB downsampling of metric histories (pure Python: the control plane has no numpy)."""

from __future__ import annotations

import math

import pytest
from mapi_server.services.tracking import lttb


def test_short_series_and_large_budgets_keep_every_point() -> None:
    assert lttb([], [], 10) == []
    assert lttb([0.0], [1.0], 10) == [0]
    assert lttb([0.0, 1.0], [1.0, 2.0], 1) == [0, 1]
    xs = [float(i) for i in range(50)]
    assert lttb(xs, xs, 50) == list(range(50))
    assert lttb(xs, xs, 500) == list(range(50))


def test_endpoints_are_kept_and_indices_increase() -> None:
    xs = [float(i) for i in range(1000)]
    ys = [math.sin(i / 25.0) for i in range(1000)]
    keep = lttb(xs, ys, 100)
    assert len(keep) == 100
    assert keep[0] == 0 and keep[-1] == 999
    assert keep == sorted(set(keep))


def test_a_spike_survives_downsampling() -> None:
    """LTTB keeps the point that makes the largest triangle: a single outlier stays."""
    xs = [float(i) for i in range(101)]
    ys = [0.0] * 101
    ys[37] = 50.0
    keep = lttb(xs, ys, 10)
    assert 37 in keep


def test_known_small_example() -> None:
    xs = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    ys = [0.0, 1.0, 0.0, 5.0, 0.0, 1.0, 0.0]
    # n = 3: one middle bucket (points 1..5); the spike at 3 makes the largest triangle
    assert lttb(xs, ys, 3) == [0, 3, 6]
    assert lttb(xs, ys, 7) == list(range(7))


def test_mismatched_lengths_are_refused() -> None:
    with pytest.raises(ValueError, match="same length"):
        lttb([0.0, 1.0], [0.0], 2)
