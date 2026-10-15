"""pymc-marketing fixture data: the E33 simulation (daily local-currency spend, a GBP
market, FX and holiday tables) with markets as a fixed panel dim."""

from __future__ import annotations

from typing import Literal

import xarray as xr
from mapi_plugin_mmm.examples import e33_dataset, e33_side_tables, future_daily
from mapi_spec import DataSchema

__all__ = ["HORIZON_WEEKS", "SIZES", "generate", "payload_future", "payload_window", "schema"]

Size = Literal["tiny", "full"]
SIZES = {
    "tiny": {"markets": ("NL", "DE", "UK"), "channels": ("tv", "search"), "weeks": 30},
    "full": {
        "markets": ("NL", "DE", "IT", "UK"),
        "channels": ("tv", "search", "social"),
        "weeks": 104,
    },
}
HORIZON_WEEKS = 4


def generate(seed: int = 0, size: Size = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    data, _ = e33_dataset(seed=seed, **SIZES[size])  # type: ignore[arg-type]
    return data, e33_side_tables()


def schema() -> DataSchema:
    """Markets are fixed: pymc-marketing cannot score panel levels unseen at fit."""
    return DataSchema.model_validate(
        {
            "dims": {
                "market": {"kind": "categorical", "policy": "fixed"},
                "channel": {"kind": "categorical", "policy": "fixed"},
                "date": {"kind": "time", "policy": "extend", "freq": "D"},
            },
            "vars": {
                "revenue_local": {"dims": ["market", "date"], "dtype": "float64", "role": "target"},
                "spend_local": {
                    "dims": ["market", "channel", "date"],
                    "dtype": "float64",
                    "role": "spend",
                    "future_required": True,
                },
            },
        }
    )


def payload_future(data: xr.Dataset, seed: int, size: Size) -> xr.Dataset:
    """Daily local-currency spend for the horizon after the fit window (no revenue)."""
    return future_daily(future_weeks=HORIZON_WEEKS, seed=seed, **SIZES[size])  # type: ignore[arg-type]


def payload_window(data: xr.Dataset, seed: int, size: Size) -> xr.Dataset:
    """The last four weeks of the fit data."""
    return data.isel(date=slice(-28, None))
