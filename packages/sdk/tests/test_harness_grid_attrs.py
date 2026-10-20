"""``grid`` attrs on the gridded outputs of a score tree (the output's ``GridDecl``)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, ClassVar

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.binding import BindingMap
from mapi_sdk.harness import _stamp_grids, fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.testing import toy
from mapi_spec import ModelSpec, load_score_request
from mapi_spec.capabilities import GridDecl, ModelManifest, OutputDecl

REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/toy\n"
#: an lgcp-like output: an intensity per period on a row x col grid, and its total per period
LGCP_GRID = GridDecl(y="row", x="col", frame="period", origin="lower", colormap="magma", log=True)


class GridLinear(toy.LinearModel):
    """The toy linear model with its outputs declared as fields over (geo, time)."""

    id = "toy.grid_linear"
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {"dims": ["geo", "time"], "units_of": "y", "grid": {"y": "geo", "x": "time"}},
        "contributions": {
            "dims": ["component", "geo", "time"],
            "units_of": "y",
            "grid": {"y": "geo", "x": "time", "frame": "component"},
        },
    }


def _manifest() -> ModelManifest:
    outputs = {
        "rate": OutputDecl(dims=["period", "row", "col"], grid=LGCP_GRID),
        "total": OutputDecl(dims=["period"]),
    }
    return GridLinear.manifest().model_copy(update={"outputs": outputs})


def _groups() -> dict[str, xr.Dataset]:
    rng = np.random.default_rng(0)
    rate = xr.DataArray(
        rng.gamma(2.0, size=(2, 5, 3, 4, 6)), dims=["chain", "draw", "period", "row", "col"]
    )
    pred = xr.Dataset({"rate": rate, "total": rate.sum(["row", "col"])})
    stats = xr.concat([pred.mean(["chain", "draw"]), pred.std(["chain", "draw"])], dim="stat")
    return {"summary": stats.assign_coords(stat=["mean", "sd"]), "predictions": pred}


def _grid(ds: xr.Dataset, var: str) -> dict[str, Any] | None:
    raw = ds[var].attrs.get("grid")
    return None if raw is None else json.loads(raw)


def test_grid_attr_follows_the_binding_to_dataset_names() -> None:
    groups = _groups()
    stamped = _stamp_grids(groups, _manifest())
    binding = BindingMap({"dims": {"period": "quarter"}})
    named = {k: binding.reverse(v) for k, v in stamped.items()}
    for group in ("summary", "predictions"):
        grid = _grid(named[group], "rate")
        assert grid == {
            "y": "row",
            "x": "col",
            "frame": "quarter",
            "origin": "lower",
            "colormap": "magma",
            "log": True,
        }, group
        assert _grid(named[group], "total") is None, group
    # the input groups are untouched: the stamped variables carry fresh attrs dicts
    assert "grid" not in groups["predictions"]["rate"].attrs
    assert "grid" not in groups["summary"]["rate"].attrs


def test_no_grid_attr_once_a_raster_dim_is_aggregated_away() -> None:
    groups = {k: v.sum("row") for k, v in _groups().items()}
    stamped = _stamp_grids(groups, _manifest())
    for ds in stamped.values():
        assert _grid(ds, "rate") is None


def test_aggregated_frame_is_left_out_of_the_attr() -> None:
    groups = {k: v.sum("period") for k, v in _groups().items()}
    stamped = _stamp_grids(groups, _manifest())
    grid = _grid(stamped["predictions"], "rate")
    assert grid is not None and "frame" not in grid
    assert (grid["y"], grid["x"]) == ("row", "col")


def test_a_model_without_grids_leaves_the_groups_alone() -> None:
    groups = _groups()
    manifest = _manifest().model_copy(update={"outputs": {"total": OutputDecl(dims=["period"])}})
    assert _stamp_grids(groups, manifest) is groups


@pytest.fixture
def reg(registry: Registry) -> Registry:
    registry.register_model(GridLinear)
    return registry


def test_score_tree_carries_grid_attrs_in_dataset_names(
    reg: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    bundle = fit_spec(
        e33_spec(model="toy.grid_linear@^1", metrics="rmse, mape"), data, {"fx": fx}, reg
    )
    window = data.isel(date=slice(-28, None))
    tree = score_bundle(
        bundle,
        load_score_request(REQ + "mode: predict\n"),
        window,
        registry=reg,
        return_full=True,
    )
    for group in ("summary", "predictions"):
        ds = tree[group].to_dataset()
        assert _grid(ds, "y_hat") == {
            "y": "market",
            "x": "date",
            "origin": "upper",
            "colormap": "viridis",
            "log": False,
        }
        frame = _grid(ds, "contributions")
        assert frame is not None and frame["frame"] == "component"
    agg = score_bundle(
        bundle,
        load_score_request(
            REQ + "mode: predict\noutput: {aggregate: {over: [market], how: sum}}\n"
        ),
        window,
        registry=reg,
        return_full=True,
    )
    for group in ("summary", "predictions"):
        ds = agg[group].to_dataset()
        assert _grid(ds, "y_hat") is None and _grid(ds, "contributions") is None
