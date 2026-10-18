"""Binding: dataset names <-> canonical names."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.binding import BindingMap
from mapi_spec import DataSchema
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import Binding

BIND = {"dims": {"geo": "market", "time": "date"}, "vars": {"y": "revenue"}}


def _ds() -> xr.Dataset:
    return xr.Dataset(
        {
            "revenue": (("market", "date"), np.ones((2, 3))),
            "spend": (("market", "date"), np.ones((2, 3))),
        },
        coords={"market": ["a", "b"], "date": [1, 2, 3]},
    )


def test_apply_and_reverse_roundtrip() -> None:
    bm = BindingMap(BIND)
    canon = bm.apply(_ds())
    assert set(canon.dims) == {"geo", "time"}
    assert set(canon.data_vars) == {"y", "spend"}
    back = bm.reverse(canon)
    xr.testing.assert_identical(back, _ds())
    assert bm.to_canonical("market") == "geo"
    assert bm.to_dataset("geo") == "market"
    assert bm.to_canonical("other") == "other"
    assert bm.canonical_keys({"market": 1, "x": 2}) == {"geo": 1, "x": 2}
    assert not bm.is_identity
    assert BindingMap().is_identity
    assert "geo" in repr(bm)
    assert BindingMap(Binding.model_validate(BIND)).binding == bm.binding


def test_reverse_handles_output_only_names_and_trees() -> None:
    bm = BindingMap(BIND)
    out = xr.Dataset({"y_hat": (("chain", "geo"), np.zeros((1, 2)))}, coords={"geo": ["a", "b"]})
    rev = bm.reverse(out)
    assert set(rev.dims) == {"chain", "market"}
    tree = xr.DataTree.from_dict({"/predictions": out, "/": xr.Dataset(attrs={"k": 1})})
    rtree = bm.reverse_tree(tree)
    assert set(rtree["predictions"].dims) == {"chain", "market"}
    assert rtree.attrs == {"k": 1}
    again = bm.apply_tree(rtree)
    assert set(again["predictions"].dims) == {"chain", "geo"}


def test_missing_and_collisions() -> None:
    bm = BindingMap(BIND)
    no_target = _ds().drop_vars("revenue")
    with pytest.raises(MapiError) as err:
        bm.apply(no_target)
    assert err.value.code is ErrorCode.BINDING_INVALID
    assert set(bm.apply(no_target, missing="ignore").data_vars) == {"spend"}
    clash = _ds().assign(y=(("market",), [1.0, 2.0]))
    with pytest.raises(MapiError, match="collides"):
        bm.apply(clash)
    out = xr.Dataset({"y": (("x",), [1.0]), "revenue": (("x",), [2.0])})
    with pytest.raises(MapiError, match="already uses"):
        bm.reverse(out)


def test_schema_renames() -> None:
    raw = DataSchema.model_validate(
        {
            "dims": {"market": {"kind": "categorical"}, "date": {"kind": "time"}},
            "vars": {"revenue": {"dims": ["market", "date"], "dtype": "float64"}},
        }
    )
    bm = BindingMap(BIND)
    canon = bm.apply_schema(raw)
    assert set(canon.dims) == {"geo", "time"} and set(canon.vars) == {"y"}
    assert bm.reverse_schema(canon) == raw


def test_reverse_and_apply_rename_grid_attrs() -> None:
    """A ``grid`` attr names dims, so it follows the dims through a rename (JSON or dict)."""
    import json

    bm = BindingMap({"dims": {"y": "row", "x": "col"}})
    grid = {"y": "y", "x": "x", "frame": "digit", "origin": "upper"}
    out = xr.Dataset(
        {
            "img": (("digit", "y", "x"), np.zeros((1, 2, 3)), {"grid": json.dumps(grid)}),
            "raw": (("y", "x"), np.zeros((2, 3)), {"grid": dict(grid), "units": "1"}),
            "bad": (("y",), np.zeros(2), {"grid": "not json"}),
        }
    )
    rev = bm.reverse(out)
    assert json.loads(rev["img"].attrs["grid"]) == {**grid, "y": "row", "x": "col"}
    assert rev["raw"].attrs == {"grid": {**grid, "y": "row", "x": "col"}, "units": "1"}
    assert rev["bad"].attrs["grid"] == "not json"
    # the input is not mutated
    assert json.loads(out["img"].attrs["grid"]) == grid
    assert out["raw"].attrs["grid"] == grid
    back = bm.apply(rev, missing="ignore")
    assert json.loads(back["img"].attrs["grid"]) == grid
