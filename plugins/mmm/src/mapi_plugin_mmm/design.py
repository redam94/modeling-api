"""Model-stage inputs of ``mmm.hierarchical_geo`` as plain arrays.

The PyMC graph (fit and score) and the NumPy derived quantities (contributions, response
curves, marginal cost, the decision objective) read the data through these helpers, so
both always see the same spend tensor, the same control features and the same order.

Canonical names: dims ``geo``, ``channel``, ``time``; target ``y``; spend ``spend`` over
``(geo, channel, time, <lag_dim>)``. Controls are every other numeric variable along
``time`` (or the explicit list in the settings); each control variable is flattened into
one feature per label of its extra dims (``fourier[fourier_mode=sin_1]``), features
sorted by name within a variable so the order never depends on the payload.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError

__all__ = [
    "BASELINE",
    "CHANNEL",
    "GEO",
    "SPEND",
    "TARGET",
    "TIME",
    "ControlDesign",
    "components",
    "control_design",
    "control_vars",
    "spend_tensor",
]

GEO = "geo"
CHANNEL = "channel"
TIME = "time"
TARGET = "y"
SPEND = "spend"
BASELINE = "baseline"

Aligner = Callable[[xr.DataArray], xr.DataArray]


def _identity(da: xr.DataArray) -> xr.DataArray:
    return da


def _schema_error(message: str, pointer: str = "") -> MapiError:
    return MapiError(message, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


def spend_tensor(
    ds: xr.Dataset,
    lag_dim: str,
    *,
    align: Aligner = _identity,
    fill_missing: bool = False,
) -> np.ndarray:
    """``spend`` as a float array ``(geo, channel, time, lag)``.

    Missing values are an error unless ``fill_missing`` (score time: context rows of geos
    unseen at fit are missing by construction and contribute no spend).
    """
    if SPEND not in ds:
        raise MapiError(
            "the model needs the variable 'spend' (bind your spend variable to it)",
            code=ErrorCode.MISSING_FUTURE_COVARIATE if fill_missing else ErrorCode.SCHEMA_INVALID,
            pointer="/data/spend",
        )
    da = align(ds[SPEND])
    want = {GEO, CHANNEL, TIME, lag_dim}
    if set(map(str, da.dims)) != want:
        raise _schema_error(
            f"'spend' must have dims {sorted(want)} (a core.lag_tensor over time), got "
            f"{list(map(str, da.dims))}",
            "/data/spend",
        )
    arr = np.asarray(da.transpose(GEO, CHANNEL, TIME, lag_dim).values, dtype=np.float64)
    bad = ~np.isfinite(arr)
    if bad.any():
        if not fill_missing:
            raise _schema_error(
                f"'spend' has {int(bad.sum())} missing or infinite value(s); impute them in "
                "the pipeline",
                "/data/spend",
            )
        arr = np.where(bad, 0.0, arr)
    if (arr < 0).any():
        raise _schema_error("'spend' must be non-negative", "/data/spend")
    return arr


def control_vars(ds: xr.Dataset, explicit: Sequence[str] | None) -> list[str]:
    """Control variables: ``explicit`` (checked) or every other numeric var along time."""
    if explicit is not None:
        missing = [v for v in explicit if v not in ds]
        if missing:
            raise MapiError(
                f"control variable(s) {missing} are not in the data",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer="/model/settings/controls",
            )
        for v in explicit:
            if TIME not in ds[v].dims:
                raise _schema_error(f"control {v!r} has no {TIME!r} dim")
        return list(explicit)
    out = []
    for name in sorted(map(str, ds.data_vars)):
        da = ds[name]
        if name in (TARGET, SPEND) or TIME not in da.dims or da.dtype.kind not in "fiub":
            continue
        out.append(name)
    return out


@dataclass(frozen=True, slots=True)
class ControlDesign:
    """Control features ``(geo, time, feature)`` and the feature slice of each variable."""

    values: np.ndarray
    features: list[str]
    blocks: dict[str, slice]

    @property
    def empty(self) -> bool:
        return not self.features


def control_design(
    ds: xr.Dataset,
    names: Sequence[str],
    *,
    align: Aligner = _identity,
    fill_missing: bool = False,
) -> ControlDesign:
    """Flatten the control variables into ``(geo, time, feature)``."""
    n_geo, n_time = int(ds.sizes[GEO]), int(ds.sizes[TIME])
    frame = xr.DataArray(
        np.zeros((n_geo, n_time)),
        dims=[GEO, TIME],
        coords={GEO: ds[GEO].values, TIME: ds[TIME].values},
    )
    columns: list[np.ndarray] = []
    features: list[str] = []
    blocks: dict[str, slice] = {}
    for name in names:
        da = align(ds[name])
        extra = [str(d) for d in da.dims if d not in (GEO, TIME)]
        if CHANNEL in extra:
            raise _schema_error(f"control {name!r} must not vary by channel")
        labels = [
            np.asarray(da[d].values) if d in da.coords else np.arange(da.sizes[d]) for d in extra
        ]
        cols: dict[str, np.ndarray] = {}
        for combo in np.ndindex(*[len(lab) for lab in labels]) if extra else [()]:
            sel = {d: int(i) for d, i in zip(extra, combo, strict=True)}
            label = name
            if extra:
                parts = [f"{d}={labels[k][i]}" for k, (d, i) in enumerate(sel.items())]
                label = f"{name}[{','.join(parts)}]"
            col = da.isel(sel) if sel else da
            col = col.drop_vars([c for c in col.coords if c not in (GEO, TIME)])
            cols[label] = np.asarray(
                col.broadcast_like(frame).transpose(GEO, TIME).values, dtype=np.float64
            )
        start = len(features)
        for label in sorted(cols):
            features.append(label)
            columns.append(cols[label])
        blocks[name] = slice(start, len(features))
    if not features:
        return ControlDesign(np.zeros((n_geo, n_time, 0)), [], {})
    values = np.stack(columns, axis=-1)
    bad = ~np.isfinite(values)
    if bad.any():
        if not fill_missing:
            raise _schema_error(
                f"control variables have {int(bad.sum())} missing value(s); impute them in "
                "the pipeline"
            )
        values = np.where(bad, 0.0, values)
    return ControlDesign(values, features, blocks)


def components(channels: Sequence[str], controls: Sequence[str]) -> list[str]:
    """Labels of the ``component`` dim: channels, ``baseline``, then the controls."""
    labels = [*map(str, channels), BASELINE, *controls]
    seen: set[str] = set()
    dups = [x for x in labels if x in seen or seen.add(x)]  # type: ignore[func-returns-value]
    if dups:
        raise _schema_error(
            f"channel and control names must be distinct and not {BASELINE!r}: {sorted(set(dups))}"
        )
    return labels
