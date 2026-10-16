"""Guided MMM data: a weekly region x channel table as the Guided ingest stores it.

Variables ``revenue(week, region)``, ``spend(week, region, channel)`` and
``avg_price(week, region)`` (the ``geo_mmm`` template's mapping of a ``Week, Region,
Revenue, TV Spend, Search Spend, Avg Price`` table). The simulation uses the model's own
structure: per region, spend relative to its mean over the data, a normalised geometric
carry-over over six lags (the first week repeated before the start, as the template's lag
tensor pads), a logistic curve and a per-channel effect, plus a region intercept, a small
season, a price effect and noise; revenue is that times a region scale.

Spend and price are deterministic (the same at every seed), so the business-prior ranges
and the lift test of ``spec.yaml`` stay true to the simulation for every seed; only the
noise is seeded. :func:`truth` reports the ROI, shares, baseline share and the lift the
lift test measures, computed exactly as the model defines them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec import DataSchema

__all__ = ["LIFT", "SIZES", "GuidedTruth", "generate", "payload_future", "schema", "truth"]

Size = Literal["tiny", "full"]
SIZES: Final[dict[str, dict[str, Any]]] = {
    "tiny": {"regions": ("east", "north", "south"), "channels": ("search", "tv"), "weeks": 40},
    "full": {
        "regions": ("central", "east", "north", "south", "west"),
        "channels": ("search", "social", "tv"),
        "weeks": 104,
    },
}
START: Final = "2024-01-07"  # a Sunday: W-SUN weeks
HORIZON_WEEKS: Final = 4
L_MAX: Final = 6
ALPHA: Final = {"search": 0.2, "social": 0.35, "tv": 0.5}
LAM: Final = {"search": 2.0, "social": 1.5, "tv": 1.2}
BETA: Final = {"search": 0.15, "social": 0.08, "tv": 0.25}
LIFT: Final[dict[str, Any]] = {
    "channel": "tv",
    "geo": "north",
    "start": date(2024, 3, 3),
    "end": date(2024, 3, 24),
    "spend_delta": 4000.0,
}
"""The lift test of ``spec.yaml``: 4000 extra on tv in the north over four weeks."""


def _spend(channel: str, g: int, t: np.ndarray) -> np.ndarray:
    if channel == "tv":
        return np.maximum(0.0, 600 + 250 * np.sin(2 * np.pi * t / 17 + g))
    if channel == "search":
        return 250 + 80 * np.cos(2 * np.pi * t / 11 + 2 * g)
    return 150 + 50 * np.sin(2 * np.pi * t / 7 + g)


def _carry(x: np.ndarray, alpha: float) -> np.ndarray:
    """Normalised geometric carry-over along the last axis, the first value repeated."""
    w = alpha ** np.arange(L_MAX)
    w = w / w.sum()
    out = np.zeros_like(x)
    for lag in range(L_MAX):
        shifted = np.concatenate([np.repeat(x[..., :1], lag, axis=-1), x[..., : x.shape[-1] - lag]],
                                 axis=-1) if lag else x  # fmt: skip
        out = out + w[lag] * shifted
    return out


def _curve(a: np.ndarray, lam: float) -> np.ndarray:
    e = np.exp(-lam * a)
    return (1 - e) / (1 + e)


def _design(size: Size, total: int) -> dict[str, Any]:
    cfg = SIZES[size]
    t = np.arange(total, dtype=float)
    regions, channels = cfg["regions"], cfg["channels"]
    spend = np.stack(
        [np.stack([_spend(c, g, t) for c in channels]) for g in range(len(regions))]
    )  # (region, channel, week)
    price = np.stack([2.5 + 0.2 * np.sin(2 * np.pi * t / 9 + g) for g in range(len(regions))])
    return {"t": t, "regions": regions, "channels": channels, "spend": spend, "price": price}


def _simulate(seed: int, size: Size, total: int) -> tuple[xr.Dataset, dict[str, Any]]:
    weeks = SIZES[size]["weeks"]
    d = _design(size, total)
    regions, channels, spend = d["regions"], d["channels"], d["spend"]
    mean_spend = spend[..., :weeks].mean(axis=-1, keepdims=True)
    x = spend / mean_spend
    contrib = np.stack(
        [BETA[c] * _curve(_carry(x[:, j], ALPHA[c]), LAM[c]) for j, c in enumerate(channels)],
        axis=1,
    )  # model units, (region, channel, week)
    rng = np.random.default_rng(seed)
    g_idx = np.arange(len(regions))[:, None]
    intercept = 0.6 + 0.05 * g_idx
    season = 0.03 * np.sin(2 * np.pi * d["t"] / 52.18)[None, :]
    price = d["price"]
    price_effect = -0.05 * (price - price[:, :weeks].mean(axis=1, keepdims=True))
    noise = rng.normal(0.0, 0.01, size=(len(regions), total))
    y = intercept + contrib.sum(axis=1) + season + price_effect + noise
    scale = 10_000.0 * (1 + 0.2 * g_idx)
    revenue = y * scale
    dates = pd.date_range(START, periods=total, freq="W-SUN")
    ds = xr.Dataset(
        {
            "revenue": (("week", "region"), np.round(revenue.T, 2)),
            "spend": (("week", "region", "channel"), np.round(spend.transpose(2, 0, 1), 4)),
            "avg_price": (("week", "region"), np.round(price.T, 4)),
        },
        coords={"week": dates.values, "region": list(regions), "channel": list(channels)},
    )
    return ds, {"design": d, "contrib": contrib, "scale": scale, "x": x, "mean": mean_spend,
                "revenue": revenue}  # fmt: skip


def generate(seed: int = 0, size: Size = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    """The fit data (no side tables)."""
    ds, _ = _simulate(seed, size, SIZES[size]["weeks"])
    return ds, {}


def schema() -> DataSchema:
    """The schema the Guided ingest gives the table (``region`` new levels, pooled)."""
    return DataSchema.model_validate(
        {
            "dims": {
                "week": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                "region": {"kind": "categorical", "policy": "new_levels",
                           "new_level_strategy": "pooled"},
                "channel": {"kind": "categorical"},
            },
            "vars": {
                "revenue": {"dims": ["week", "region"], "dtype": "float64", "role": "target"},
                "avg_price": {"dims": ["week", "region"], "dtype": "float64", "role": "control"},
                "spend": {"dims": ["week", "region", "channel"], "dtype": "float64",
                          "role": "spend", "future_required": True},
            },
        }
    )  # fmt: skip


def payload_future(data: xr.Dataset, seed: int, size: Size) -> xr.Dataset:
    """Spend and price for the four weeks after the fit window (no revenue)."""
    weeks = SIZES[size]["weeks"]
    ds, _ = _simulate(seed, size, weeks + HORIZON_WEEKS)
    return ds.isel(week=slice(weeks, None)).drop_vars("revenue")


@dataclass(frozen=True, slots=True)
class GuidedTruth:
    """Business quantities of the simulation over the data (revenue units)."""

    roi: dict[str, float]
    share: dict[str, float]
    baseline_share: float
    lift: float


def truth(seed: int = 0, size: Size = "tiny") -> GuidedTruth:
    """ROI and contribution share per channel, the baseline share and the lift the lift
    test measures - the extra spend spread over the window in proportion to the actual
    spend, carried over within the window, as ``mmm.hierarchical_geo`` defines it."""
    weeks = SIZES[size]["weeks"]
    ds, sim = _simulate(seed, size, weeks)
    d = sim["design"]
    regions, channels = list(d["regions"]), list(d["channels"])
    scale = sim["scale"]  # (region, 1)
    contrib = sim["contrib"] * scale[:, None, :]  # revenue units
    total = float(sim["revenue"].sum())
    spend = d["spend"]
    roi = {c: float(contrib[:, j].sum() / spend[:, j].sum()) for j, c in enumerate(channels)}
    share = {c: float(contrib[:, j].sum() / total) for j, c in enumerate(channels)}
    baseline = 1.0 - sum(share.values())
    g, j = regions.index(LIFT["geo"]), channels.index(LIFT["channel"])
    days = pd.DatetimeIndex(ds["week"].values)
    window = np.flatnonzero(
        (days >= pd.Timestamp(LIFT["start"])) & (days <= pd.Timestamp(LIFT["end"]))
    )
    base = spend[g, j, window]
    dx = np.zeros(weeks)
    dx[window] = LIFT["spend_delta"] * base / base.sum() / sim["mean"][g, j, 0]
    x = sim["x"][g, j]
    c = LIFT["channel"]
    gain = _curve(_carry(x + dx, ALPHA[c]), LAM[c]) - _curve(_carry(x, ALPHA[c]), LAM[c])
    lift = float(scale[g, 0] * BETA[c] * gain[window].sum())
    return GuidedTruth(roi=roi, share=share, baseline_share=baseline, lift=lift)
