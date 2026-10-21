"""Extending a dataset version with uploaded periods (v0.16 dashboard refits): the base
keeps its history, an uploaded period replaces the base's, variables the upload lacks are
missing for its periods, and anything the base does not have is refused."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_runner.worker.extend_ingest import extend_dataset
from mapi_runner.worker.tabular import IngestLimits
from mapi_spec.errors import MapiError

T = pd.date_range("2024-01-01", periods=4, freq="W-MON")


def _base() -> xr.Dataset:
    return xr.Dataset(
        {
            "y": (("t", "geo"), np.arange(8, dtype=float).reshape(4, 2)),
            "spend": (("t", "geo"), np.full((4, 2), 10.0)),
            "price": (("t",), np.array([1.0, 1.1, 1.2, 1.3])),
            "population": (("geo",), np.array([5.0, 7.0])),
        },
        coords={"t": T, "geo": ["de", "fr"]},
        attrs={"mapi.note": "kept"},
    )


def test_new_periods_are_added_and_an_uploaded_period_replaces_the_base() -> None:
    new_t = pd.DatetimeIndex([T[-1], T[-1] + pd.Timedelta(weeks=1)])
    new = xr.Dataset(
        {"y": (("t", "geo"), [[100.0, 101.0], [102.0, 103.0]]),
         "spend": (("t", "geo"), [[20.0, 21.0], [22.0, 23.0]])},
        coords={"t": new_t, "geo": ["de", "fr"]},
    )  # fmt: skip
    out = extend_dataset(_base(), new, "t", IngestLimits())
    assert out.sizes["t"] == 5 and list(out.data_vars) == ["y", "spend", "price", "population"]
    assert out["y"].sel(t=T[0]).values.tolist() == [0.0, 1.0]  # history kept
    assert out["y"].sel(t=T[-1]).values.tolist() == [100.0, 101.0]  # replaced
    assert out["y"].isel(t=-1).values.tolist() == [102.0, 103.0]  # added
    assert np.isnan(out["price"].isel(t=-1).item())  # not in the upload: missing
    assert out["price"].sel(t=T[0]).item() == 1.0
    assert out["population"].values.tolist() == [5.0, 7.0]  # static: the base's
    assert pd.DatetimeIndex(out["t"].values).is_monotonic_increasing
    assert out.attrs == {"mapi.note": "kept"}


def test_an_upload_with_fewer_geos_is_missing_for_the_others() -> None:
    new = xr.Dataset(
        {"y": (("t", "geo"), [[50.0]])},
        coords={"t": [T[-1] + pd.Timedelta(weeks=1)], "geo": ["fr"]},
    )
    out = extend_dataset(_base(), new, "t", IngestLimits())
    last = out["y"].isel(t=-1)
    assert np.isnan(last.sel(geo="de").item()) and last.sel(geo="fr").item() == 50.0


def test_a_variable_broadcast_by_the_long_table_is_reduced_to_its_dims() -> None:
    """The server sends the upload as a long CSV (one row per ``t`` x ``geo``), so
    ``price(t)`` arrives as ``price(t, geo)``, repeated per geo or blank in some rows."""
    new_t = [T[-1] + pd.Timedelta(weeks=1), T[-1] + pd.Timedelta(weeks=2)]
    new = xr.Dataset(
        {"y": (("t", "geo"), [[1.0, 2.0], [3.0, 4.0]]),
         "price": (("t", "geo"), [[1.5, 1.5], [np.nan, 1.6]])},
        coords={"t": new_t, "geo": ["de", "fr"]},
    )  # fmt: skip
    out = extend_dataset(_base(), new, "t", IngestLimits())
    assert out["price"].dims == ("t",)
    assert out["price"].values[-2:].tolist() == [1.5, 1.6]
    assert out["y"].isel(t=-1).values.tolist() == [3.0, 4.0]


@pytest.mark.parametrize(
    ("new", "message"),
    [
        (xr.Dataset({"z": (("t",), [1.0])}, coords={"t": [T[0]]}), "variables the dataset"),
        (xr.Dataset({"y": (("t", "geo"), [[1.0]])}, coords={"t": [T[0]], "geo": ["it"]}),
         "labels of 'geo'"),
        (xr.Dataset({"y": (("geo",), [1.0])}, coords={"geo": ["de"]}), "no 't' dimension"),
        (xr.Dataset({"price": (("t", "geo"), [[1.0, 2.0]])},
                    coords={"t": [T[0]], "geo": ["de", "fr"]}),
         "has dims"),  # differs along a dim the dataset's variable does not have
    ],
)  # fmt: skip
def test_anything_the_base_does_not_have_is_refused(new: xr.Dataset, message: str) -> None:
    with pytest.raises(MapiError, match=message):
        extend_dataset(_base(), new, "t", IngestLimits())


def test_the_extended_dataset_is_capped() -> None:
    new = xr.Dataset({"y": (("t", "geo"), [[1.0, 2.0]])}, coords={"t": [T[0]], "geo": ["de", "fr"]})
    with pytest.raises(MapiError, match="decodes to more than"):
        extend_dataset(_base(), new, "t", IngestLimits(max_uncompressed_bytes=16))
