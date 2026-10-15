"""E33 data: synthetic daily local-currency spend and revenue with known parameters.

``tiny``: 3 markets x 2 channels x 30 weeks (PR runs); ``full``: 4 markets (one in GBP) x
3 channels x 104 weeks (nightly). Side tables: daily FX (EUR per GBP) and holidays,
covering the data plus the forecast horizon. The generator lives in
:mod:`mapi_plugin_mmm.examples`; this module adapts it to the corpus layout.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import xarray as xr
from mapi_plugin_mmm.examples import (
    CHANNELS,
    MARKETS,
    E33Truth,
    e33_dataset,
    e33_raw_schema,
    e33_side_tables,
    future_daily,
)
from mapi_spec import DataSchema

__all__ = ["HORIZON_WEEKS", "SIZES", "generate", "payload", "raw_schema", "truth"]

Size = Literal["tiny", "full"]
SIZES: dict[str, dict[str, Any]] = {
    "tiny": {"markets": ("NL", "DE", "UK"), "channels": ("tv", "search"), "weeks": 30},
    "full": {"markets": MARKETS, "channels": CHANNELS, "weeks": 104},
}
HORIZON_WEEKS = 8
NEW_MARKET = "FR"


def generate(seed: int = 0, size: Size = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    """The fit data and its side tables (``fx``, ``holidays``)."""
    data, _ = e33_dataset(seed=seed, **SIZES[size])
    return data, e33_side_tables()


def truth(seed: int = 0, size: Size = "tiny") -> E33Truth:
    """Simulation parameters and weekly EUR truth, including the forecast horizon."""
    _, t = e33_dataset(seed=seed, future_weeks=HORIZON_WEEKS, **SIZES[size])
    return t


def raw_schema(data: xr.Dataset) -> DataSchema:
    return e33_raw_schema(data)


def payload(kind: str, *, seed: int = 0, size: Size = "tiny") -> xr.Dataset:
    """Score data for a request in ``score_requests.yaml``.

    * ``future`` - daily local-currency spend for the horizon after the fit window;
    * ``future_new_market`` - the same plus a market unseen at fit;
    * ``weekly_plan`` - the horizon's spend as weekly EUR totals (``inputs_stage: weekly``);
    * ``window`` - the last four weeks of the fit data (with revenue).
    """
    cfg = SIZES[size]
    if kind == "future":
        return future_daily(future_weeks=HORIZON_WEEKS, seed=seed, **cfg)
    if kind == "future_new_market":
        fut = future_daily(future_weeks=HORIZON_WEEKS, seed=seed, **cfg)
        new = fut.sel(market=[cfg["markets"][0]]).assign_coords(market=[NEW_MARKET]) * 1.1
        return xr.concat([fut, new], dim="market")
    if kind == "weekly_plan":
        weekly = truth(seed, size).weekly["spend_eur"].isel(week=slice(cfg["weeks"], None))
        plan = weekly.rename(week="date").astype(np.float64)
        plan.attrs = {"units": "EUR"}
        return xr.Dataset({"spend_local": plan})
    if kind == "window":
        data, _ = generate(seed, size)
        return data.isel(date=slice(-28, None))
    raise ValueError(f"unknown E33 payload {kind!r}")
