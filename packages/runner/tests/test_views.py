"""The UI views the runner writes match the ``mapi_spec.views`` contract the web app reads."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_conformance.fixtures.xgb_store_demand import data as xgb
from mapi_runner.worker.ingest import missingness_grid, preview, profile
from mapi_runner.worker.views import tree_index
from mapi_spec.views import (
    MAX_GRID_COORDS,
    PREVIEW_FORMAT,
    PROFILE_FORMAT,
    TREE_INDEX_FORMAT,
    PreviewView,
    ProfileView,
    TreeIndexView,
)

pytestmark = pytest.mark.unit


def test_profile_and_preview_validate_and_carry_their_format_tags() -> None:
    ds, _ = xgb.generate(0, "tiny")
    before = int(ds["price"].isnull().sum())
    ds["price"][0, 0, :3] = np.nan
    prof = profile(ds, xgb.schema(), [], b"raw")
    view = ProfileView.model_validate(json.loads(json.dumps(prof)))
    assert view.format == PROFILE_FORMAT
    price = view.vars["price"]
    assert (
        price.missing == int(ds["price"].isnull().sum()) > before
        and price.count + price.missing == price.size
    )
    assert view.missingness is not None and view.missingness.along == "week"
    assert "price" in view.missingness.labels
    row = view.missingness.fraction[view.missingness.labels.index("price")]
    assert len(row) == ds.sizes["week"] and max(row) > 0
    assert prof["sha256"]  # provenance fields stay alongside the view

    prev = PreviewView.model_validate(preview(ds, 7))
    assert prev.format == PREVIEW_FORMAT and len(prev.rows) == 7
    roles = {c.name: c.role for c in prev.columns}
    assert roles["week"] == "dim" and roles["units_sold"] == "var"
    assert prev.total_rows == ds.sizes["store_id"] * ds.sizes["sku"] * ds.sizes["week"]


def test_missingness_grid_is_binned_for_long_series() -> None:
    n = 2 * MAX_GRID_COORDS + 5
    ds = xr.Dataset(
        {"y": ("t", np.where(np.arange(n) % 2 == 0, np.nan, 1.0))},
        coords={"t": pd.date_range("2020-01-01", periods=n, freq="D")},
    )
    from mapi_spec.schema import DataSchema

    schema = DataSchema.model_validate(
        {
            "dims": {"t": {"kind": "time", "freq": "D", "policy": "extend"}},
            "vars": {"y": {"dims": ["t"], "dtype": "float64", "missing": "allow"}},
        }
    )
    grid = missingness_grid(ds, schema)
    assert grid is not None and len(grid.coords) == MAX_GRID_COORDS
    assert all(0.0 <= f <= 1.0 for f in grid.fraction[0])


def test_tree_index_lists_groups_coords_and_vars_without_arrays() -> None:
    tree = xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"bundle_id": "b1", "blob": {"x": 1}}),
            "/posterior": xr.Dataset(
                {"beta": (("chain", "draw", "geo"), np.zeros((2, 3, 2)), {"units": "EUR"})},
                coords={"geo": ["a", "b"], "chain": [0, 1], "draw": [0, 1, 2]},
            ),
        }
    )
    view = TreeIndexView.model_validate(tree_index(tree))
    assert view.format == TREE_INDEX_FORMAT
    assert view.groups["/"].attrs == {"bundle_id": "b1"}
    post = view.groups["/posterior"]
    assert post.dims == {"chain": 2, "draw": 3, "geo": 2}
    assert post.coords["geo"].values == ["a", "b"]
    assert post.vars["beta"].shape == [2, 3, 2] and post.vars["beta"].units == "EUR"
