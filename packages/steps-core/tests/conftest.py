"""Shared datasets and side tables for the built-in step tests."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_sdk.schema import infer_schema
from mapi_spec import DataSchema

GEOS = ("NL", "DE", "UK")
CHANNELS = ("tv", "search", "social")


def make_weekly(weeks: int = 30, seed: int = 0, geos: tuple[str, ...] = GEOS) -> xr.Dataset:
    """Weekly canonical panel: y(geo, time) target, spend(geo, channel, time), and friends."""
    rng = np.random.default_rng(seed)
    time = pd.date_range("2024-01-07", periods=weeks, freq="W-SUN").as_unit("ns")
    g, c = len(geos), len(CHANNELS)
    spend = rng.gamma(2.0, 50.0, size=(g, c, weeks))
    y = 300 + spend.sum(axis=1) * 0.8 + rng.normal(0, 5, size=(g, weeks))
    price = rng.uniform(5, 10, size=(g, weeks))
    price[0, 3:6] = np.nan
    price[1, -2:] = np.nan
    price_raw = price.copy()
    price_raw[np.isnan(price_raw)] = -99.0
    share = rng.uniform(0.05, 0.95, size=(g, weeks))
    promo = np.array(["none", "bogo", "discount"])[rng.integers(0, 3, size=weeks)]
    return xr.Dataset(
        {
            "y": (("geo", "time"), y, {"units": "EUR"}),
            "spend": (("geo", "channel", "time"), spend, {"units": "EUR"}),
            "price": (("geo", "time"), price),
            "price_raw": (("geo", "time"), price_raw),
            "share": (("geo", "time"), share),
            "promo_type": (("time",), promo),
        },
        coords={"geo": list(geos), "channel": list(CHANNELS), "time": time},
    )


def make_daily(days: int = 7 * 12, seed: int = 1, geos: tuple[str, ...] = GEOS) -> xr.Dataset:
    """Daily local-currency panel starting on a Monday (full W-SUN weeks)."""
    rng = np.random.default_rng(seed)
    time = pd.date_range("2024-01-01", periods=days, freq="D").as_unit("ns")
    g, c = len(geos), len(CHANNELS)
    spend = rng.gamma(2.0, 20.0, size=(g, c, days))
    y = 100 + spend.sum(axis=1) * 0.7 + rng.normal(0, 3, size=(g, days))
    return xr.Dataset(
        {
            "y": (("geo", "time"), y, {"units": "LCU"}),
            "spend": (("geo", "channel", "time"), spend, {"units": "LCU"}),
        },
        coords={"geo": list(geos), "channel": list(CHANNELS), "time": time},
    )


def make_fx(start: str = "2023-12-01", days: int = 500) -> xr.Dataset:
    """Daily FX table rate(date, currency): units of EUR per unit of local currency."""
    dates = pd.date_range(start, periods=days, freq="D").as_unit("ns")
    t = np.arange(days)
    rates = np.stack([1.15 + 0.02 * np.sin(t / 20.0), 0.09 + 0.001 * np.cos(t / 15.0)], axis=1)
    return xr.Dataset(
        {"rate": (("date", "currency"), rates)},
        coords={"date": dates, "currency": ["GBP", "SEK"]},
    )


def make_holidays(start: str = "2023-12-01", days: int = 500) -> xr.Dataset:
    """Daily holiday indicator table holiday(date)."""
    dates = pd.date_range(start, periods=days, freq="D").as_unit("ns")
    flag = np.zeros(days)
    md = list(zip(dates.month, dates.day, strict=True))
    for i, (m, d) in enumerate(md):
        if (m, d) in {(1, 1), (4, 1), (5, 1), (12, 25), (12, 26)}:
            flag[i] = 1.0
    return xr.Dataset({"holiday": (("date",), flag)}, coords={"date": dates})


def make_events(n: int = 400, seed: int = 2) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    t0 = np.datetime64("2024-03-01T00:00:00", "ns")
    offsets = np.sort(rng.integers(0, 30 * 24 * 3600, size=n)).astype("timedelta64[s]")
    return xr.Dataset(
        {
            "timestamp": (("event",), (t0 + offsets).astype("datetime64[ns]")),
            "store": (("event",), np.array(["s1", "s2", "s3"])[rng.integers(0, 3, size=n)]),
            "amount": (("event",), rng.uniform(1, 50, size=n)),
        },
        coords={"event": np.arange(n)},
    )


def make_padded(seed: int = 3) -> xr.Dataset:
    """Ragged households × trip positions, padded, with a mask."""
    rng = np.random.default_rng(seed)
    lengths = [5, 3, 6, 4]
    h, w = len(lengths), max(lengths)
    mask = np.arange(w)[None, :] < np.asarray(lengths)[:, None]
    y = np.where(mask, rng.uniform(1, 5, size=(h, w)), np.nan)
    x = np.where(mask, rng.uniform(0, 1, size=(h, w)), np.nan)
    return xr.Dataset(
        {
            "y": (("hh", "trip"), y),
            "x": (("hh", "trip"), x),
            "mask": (("hh", "trip"), mask),
        },
        coords={"hh": ["h1", "h2", "h3", "h4"], "trip": np.arange(w)},
    )


def make_long(seed: int = 4) -> xr.Dataset:
    """Long ragged rows: obs with a household label variable, interleaved groups."""
    rng = np.random.default_rng(seed)
    hh = np.array(["h1", "h2", "h1", "h3", "h2", "h1", "h3", "h3", "h2", "h1"])
    n = hh.size
    return xr.Dataset(
        {
            "hh_id": (("obs",), hh),
            "y": (("obs",), rng.uniform(1, 5, size=n)),
            "x": (("obs",), rng.uniform(0, 1, size=n)),
        },
        coords={"obs": np.arange(n)},
    )


class Data:
    """Dataset builders (fixtures cannot be imported under importlib mode)."""

    weekly = staticmethod(make_weekly)
    daily = staticmethod(make_daily)
    fx = staticmethod(make_fx)
    holidays = staticmethod(make_holidays)
    events = staticmethod(make_events)
    padded = staticmethod(make_padded)
    long = staticmethod(make_long)
    geos = GEOS
    channels = CHANNELS


@pytest.fixture
def data() -> type[Data]:
    return Data


@pytest.fixture
def weekly() -> xr.Dataset:
    return make_weekly()


@pytest.fixture
def daily() -> xr.Dataset:
    return make_daily()


@pytest.fixture
def fx() -> xr.Dataset:
    return make_fx()


@pytest.fixture
def holidays() -> xr.Dataset:
    return make_holidays()


@pytest.fixture
def target_schema() -> Callable[[xr.Dataset], DataSchema]:
    return lambda ds: infer_schema(ds, targets=[t for t in ("y",) if t in ds])
