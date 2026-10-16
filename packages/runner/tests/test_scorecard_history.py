"""The ``data.history`` check measures years of history from the model stage's time dim,
whatever its frequency (daily, weekly, monthly)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from mapi_runner.worker.scorecard import history_years

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("freq", "periods", "declared", "years"),
    [
        ("D", 105, "D", 105 / 365),
        ("W-SUN", 104, "W-SUN", 2.0),
        ("MS", 30, "MS", 2.5),
        ("D", 730, None, 2.0),  # no declared frequency: inferred from the labels
        ("W-SUN", 52, None, 1.0),
    ],
)
def test_history_years(freq: str, periods: int, declared: str | None, years: float) -> None:
    labels = pd.date_range("2024-01-07", periods=periods, freq=freq).values
    got = history_years(labels, declared)
    assert got is not None and got[0] == pytest.approx(years)
    assert f"{periods} periods" in got[1]


def test_history_without_a_known_frequency_is_not_guessed() -> None:
    assert history_years(np.arange(40), None) is None
    irregular = pd.to_datetime(["2024-01-01", "2024-01-05", "2024-03-01"]).values
    assert history_years(irregular, None) is None
