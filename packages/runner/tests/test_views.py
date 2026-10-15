"""The UI views the runner writes match the ``mapi_spec.views`` contract the web app reads."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_conformance.fixtures.xgb_store_demand import data as xgb
from mapi_runner.worker.ingest import missingness_grid, preview, profile
from mapi_runner.worker.views import preprocess_view, tree_index
from mapi_sdk.steps import StepState
from mapi_spec.effects import StepKind
from mapi_spec.validate import ResolvedStep
from mapi_spec.views import (
    MAX_GRID_COORDS,
    MAX_STATE_ARRAY_CELLS,
    PREPROCESS_FORMAT,
    PREVIEW_FORMAT,
    PROFILE_FORMAT,
    TREE_INDEX_FORMAT,
    PreprocessView,
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


def _step(sid: str, use: str, kind: StepKind, *, stateful: bool) -> ResolvedStep:
    return ResolvedStep(
        id=sid,
        use=f"{use}@^1",
        resolved=f"{use}@1.0.0",
        kind=kind,
        stateful=stateful,
        invertible=False,
        depends_on_target=False,
        input_stage="raw",
    )


class _OriginState(StepState):
    origin: str | None = None


def test_preprocess_view_lists_every_step_with_learned_state_and_small_arrays() -> None:
    scale = xr.DataArray(
        [[120.1, 340.2], [98.4, np.nan], [77.0, 150.3]],
        dims=("geo", "channel"),
        coords={"geo": ["de", "fr", "it"], "channel": ["search", "tv"]},
        attrs={"units": "EUR"},
    )
    big = xr.DataArray(np.zeros(MAX_STATE_ARRAY_CELLS + 1), dims=("t",))
    cube = xr.DataArray(np.zeros((2, 2, 2)), dims=("a", "b", "c"))
    steps = [
        _step("weekly", "core.resample", StepKind.RESHAPE, stateful=False),
        _step("scale", "core.mean_scale", StepKind.VALUE, stateful=True),
        _step("season", "core.fourier", StepKind.FEATURE, stateful=True),
    ]
    states = {
        "weekly": StepState(step_id="weekly"),
        "scale": StepState(
            step_id="scale",
            settings_hash="h",
            arrays=xr.Dataset(
                {
                    "scale__spend": scale,
                    "level": xr.DataArray(2.5),
                    "too_big": big,
                    "cube": cube,
                }
            ),
        ),
        "season": _OriginState(step_id="season", learned={"period": 52.18}, origin="2024-01-07"),
    }
    raw = preprocess_view(steps, states)
    view = PreprocessView.model_validate(json.loads(json.dumps(raw, allow_nan=False)))
    assert view.format == PREPROCESS_FORMAT
    assert [s.id for s in view.steps] == ["weekly", "scale", "season"]
    weekly, sc, season = view.steps
    assert weekly.use == "core.resample@1.0.0" and weekly.kind == "reshape"
    assert not weekly.stateful and weekly.arrays == [] and weekly.learned == {}
    assert sc.stateful and sc.kind == "value"
    arrays = {a.name: a for a in sc.arrays}
    heat = arrays["scale__spend"]
    assert heat.dims == ["geo", "channel"]
    assert heat.coords == {"geo": ["de", "fr", "it"], "channel": ["search", "tv"]}
    assert heat.values == [[120.1, 340.2], [98.4, None], [77.0, 150.3]]
    assert heat.units == "EUR"
    assert arrays["level"].dims == [] and arrays["level"].values == [2.5]
    assert sorted(sc.omitted) == ["cube", "too_big"]
    assert season.learned == {"period": 52.18, "origin": "2024-01-07"}


def test_preprocess_view_caps_learned_json() -> None:
    steps = [_step("enc", "core.encode_levels", StepKind.FEATURE, stateful=True)]
    levels = {f"level_{i}": "x" * 100 for i in range(2000)}
    raw = preprocess_view(steps, {"enc": StepState(learned={"small": 1, "levels": levels})})
    step = PreprocessView.model_validate(raw).steps[0]
    assert step.learned == {"small": 1} and step.learned_truncated
    assert len(json.dumps(raw)) < 64 * 1024
