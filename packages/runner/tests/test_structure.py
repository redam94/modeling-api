"""Structure detection of the Data Explorer on the ``sci.*`` case studies (built in memory
from the synthetic data builders of ``mapi_plugin_science``), on time-series panels and on
small hand-made datasets."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_runner.worker.structure import (
    angle_period,
    detect_structure,
    is_label_index,
    time_strength,
)
from mapi_spec.explore import StructureHints
from mapi_spec.schema import DataSchema
from mapi_spec.views import DATA_SECTIONS

pytestmark = [pytest.mark.unit, pytest.mark.filterwarnings("ignore::FutureWarning")]

science = pytest.importorskip("mapi_plugin_science.export")

# case -> [(kind, vars, dims)] best first, then (geometry vars, time dim, sequence dim)
EXPECTED: dict[str, tuple[list[tuple[str, tuple[str, ...], dict[str, str]]], set[str], Any]] = {
    "e07": (
        [
            ("image", ("quakes",), {"y": "row", "x": "col", "frame": "quarter"}),
            ("timeseries", ("quakes",), {"time": "quarter"}),
        ],
        set(),
        ("quarter", None),
    ),
    "e50": (
        [
            ("profile", ("cells",), {"along": "position", "frame": "hours"}),
            ("sequence", ("cells",), {"time": "hours"}),
        ],
        set(),
        (None, "hours"),
    ),
    "e60": (
        [("vector_field", ("displacement",), {"y": "row", "x": "col", "component": "component"})],
        set(),
        (None, None),
    ),
    "e80": (
        [
            ("image", ("image", "weight"), {"y": "row", "x": "col"}),
            ("image", ("psf",), {"y": "psf_row", "x": "psf_col"}),
        ],
        set(),
        (None, None),
    ),
    "e51": (
        [("image", ("heater_w", "reading_c"), {"y": "y_cm", "x": "x_cm", "frame": "run"})],
        set(),
        (None, None),
    ),
    "e61": (
        [
            ("image", ("activator",), {"y": "y", "x": "x", "frame": "t"}),
            ("sequence", ("activator",), {"time": "t"}),
        ],
        set(),
        (None, "t"),
    ),
    "e58": (
        [("image", ("frames",), {"y": "row", "x": "col", "frame": "frame"})],
        set(),
        (None, None),
    ),
    "e31": (
        [("image", ("pixels",), {"y": "row", "x": "col", "frame": "digit"})],
        set(),
        (None, None),
    ),
    "e77": (
        [("points", ("cie_l", "cie_a", "cie_b"), {"point": "chip"})],
        {"wcs_row", "wcs_col"},
        (None, None),
    ),
    "e79": (
        [
            ("image", ("temperature", "noise_sd"), {"y": "row", "x": "col"}),
            ("profile", ("reference_dl", "pixel_window"), {"along": "multipole"}),
        ],
        set(),
        (None, None),
    ),
    "e18": (
        [("areal_map", ("cases", "expected", "aff"), {"area": "district", "vertex": "vertex"})],
        {"ring_x", "ring_y", "adjacency"},
        (None, None),
    ),
    "e21": (
        [
            ("network", ("cowork",), {"node": "lawyer"}),
            ("network", ("homophily",), {"node": "lawyer"}),
        ],
        set(),
        (None, None),
    ),
    "e19": (
        [
            ("network", ("consensus",), {"node": "protein"}),
            ("table", ("level",), {"item": "cell", "columns": "protein"}),
        ],
        {"consensus"},
        (None, None),
    ),
    "e23": (
        [("tree", ("brain", "body", "group_size"), {"node": "node", "tip": "species"})],
        {"parent", "branch_length", "is_tip", "tip_label"},
        (None, None),
    ),
    "e81": (
        [("circular", ("wind_from", "hour_lst", "day_of_year"), {"item": "report"})],
        set(),
        (None, None),
    ),
    "e81b": ([("circular", ("clock_time",), {"item": "record"})], set(), (None, None)),
}


def case_data(case: str) -> tuple[xr.Dataset, DataSchema]:
    c = science.EXPORTS[case]
    ds = c.synthetic()
    assert isinstance(ds, xr.Dataset) and isinstance(c.schema, DataSchema)
    return ds, c.schema


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_the_shapes_of_every_case_study(case: str) -> None:
    ds, schema = case_data(case)
    s = detect_structure(ds, schema)
    shapes, geometry, (time_dim, seq_dim) = EXPECTED[case]
    got = [(x.kind, x.vars, dict(x.dims)) for x in s.shapes]
    assert got == shapes
    assert s.geometry_vars == geometry
    assert (s.time_dim, s.sequence_dim) == (time_dim, seq_dim)
    assert not s.plain
    assert s.recommended[:2] == ("overview", "structure")
    assert set(s.recommended).isdisjoint(s.not_applicable)
    assert set(s.recommended) | set(s.not_applicable) <= set(DATA_SECTIONS)
    for shape in s.shapes:
        if shape.kind not in ("timeseries", "sequence", "table"):
            assert shape.section in s.recommended, shape.kind
            assert shape.label_plain


def test_the_lens_tree_nodes_have_their_own_shapes() -> None:
    from mapi_plugin_science import lens_data

    tree = lens_data.to_tree(science.EXPORTS["e80"].synthetic())
    found = {}
    for path, node_schema in lens_data.LENS_TREE_SCHEMA.nodes.items():
        if node_schema.vars:
            s = detect_structure(tree[path].to_dataset(), node_schema)
            found[path] = [(x.kind, x.vars) for x in s.shapes]
    assert found == {"/image": [("image", ("image", "weight"))], "/psf": [("image", ("psf",))]}


def test_specific_facts_of_the_case_studies() -> None:
    ds, schema = case_data("e81b")
    s = detect_structure(ds, schema)
    assert s.group_vars == {"species_idx": "species"} and "species_idx" in s.non_data
    assert s.not_applicable["timeseries"] == "the data has no time dimension"
    assert "groups" not in s.not_applicable
    ds, schema = case_data("e60")
    s = detect_structure(ds, schema)
    assert s.mask_vars == {"force_mask"} and s.var_use["force_mask"] == "mask"
    ds, schema = case_data("e79")
    s = detect_structure(ds, schema)
    prof = s.first("profile")
    assert prof is not None and prof.extra["log_x"] is True
    assert s.var_use["noise_sd"] == "weight"
    ds, schema = case_data("e51")
    assert detect_structure(ds, schema).categorical_dims == ("run",)
    ds, schema = case_data("e77")
    pts = detect_structure(ds, schema).first("points")
    assert pts is not None and pts.extra["lattice"] and pts.extra["answers"] == "response"
    assert pts.extra["lab"] == ("cie_l", "cie_a", "cie_b")


def _panel() -> tuple[xr.Dataset, DataSchema]:
    t = pd.date_range("2022-01-02", periods=60, freq="W-SUN")
    rng = np.random.default_rng(0)
    ds = xr.Dataset(
        {
            "sales": (("week", "geo"), rng.normal(size=(60, 3))),
            "spend": (("week", "geo", "channel"), rng.gamma(2, 1, size=(60, 3, 2))),
        },
        coords={"week": t, "geo": ["a", "b", "c"], "channel": ["tv", "search"]},
    )
    schema = DataSchema.model_validate(
        {
            "dims": {
                "week": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                "geo": {"kind": "categorical"},
                "channel": {"kind": "categorical"},
            },
            "vars": {
                "sales": {"dims": ["week", "geo"], "dtype": "float64", "role": "target"},
                "spend": {"dims": ["week", "geo", "channel"], "dtype": "float64", "role": "spend"},
            },
        }
    )
    return ds, schema


def test_a_time_series_panel_is_plain_and_keeps_todays_order() -> None:
    ds, schema = _panel()
    s = detect_structure(ds, schema)
    assert s.plain and s.kinds == ["timeseries"] and s.time_dim == "week"
    assert s.recommended[:4] == ("overview", "quality", "distributions", "timeseries")
    assert s.recommended[-1] == "structure"
    assert set(s.not_applicable) == {
        "images", "profiles", "map", "network", "tree", "circular", "points",
    }  # fmt: skip
    assert s.categorical_dims == ("geo", "channel")


def test_an_observation_index_is_not_time() -> None:
    ds = xr.Dataset({"y": ("obs", np.arange(100.0))}, coords={"obs": np.arange(100)})
    schema = DataSchema.model_validate(
        {
            "dims": {"obs": {"kind": "integer", "policy": "extend"}},
            "vars": {"y": {"dims": ["obs"], "dtype": "float64"}},
        }
    )
    assert is_label_index(ds, schema, "obs") and time_strength(ds, schema, "obs") == 0
    s = detect_structure(ds, schema)
    assert s.time_dim is None and s.sequence_dim is None
    # an extend dim with real steps is a sequence; a request can make it time
    ds2 = ds.assign_coords(obs=np.arange(100) * 10 + 5)
    assert time_strength(ds2, schema, "obs") == 1
    assert detect_structure(ds2, schema).sequence_dim == "obs"
    assert detect_structure(ds, schema, time_dim="obs").time_dim == "obs"


def test_hints_override_the_rules() -> None:
    rng = np.random.default_rng(1)
    ds = xr.Dataset(
        {
            "level": (("a", "b"), rng.normal(size=(20, 30))),
            "bearing_deg": ("n", rng.uniform(0, 360, 50)),
            "turn": ("n", rng.uniform(0, 6, 50)),
        },
        coords={"a": np.arange(20) * 0.5, "b": np.arange(30) * 0.5},
    )
    plain = detect_structure(ds, None)
    assert [x.kind for x in plain.of("image")] == ["image"]  # last two numeric dims
    assert plain.first("image").score < 0.9  # type: ignore[union-attr]
    assert plain.first("circular") is None  # no units
    hinted = detect_structure(
        ds, None, StructureHints(y_dim="b", x_dim="a", angle_vars={"turn": "rad"})
    )
    img = hinted.first("image")
    assert img is not None and img.dims == {"y": "b", "x": "a"}
    circ = hinted.first("circular")
    assert circ is not None and circ.vars == ("turn",)
    assert angle_period(ds, None, "turn", StructureHints(angle_vars={"turn": "rad"})) == (
        2 * np.pi,
        "rad",
    )


def test_angle_units_need_the_name_for_hours_and_days() -> None:
    ds = xr.Dataset(
        {
            "duration": ("n", np.linspace(0, 20, 30)),
            "hour_of_day": ("n", np.linspace(0, 23, 30)),
            "share": ("n", np.linspace(0, 1, 30)),
            "aspect": ("n", np.linspace(-10, 400, 30)),
        }
    )
    for v in ds.data_vars:
        ds[v].attrs["units"] = "h"
    ds["aspect"].attrs["units"] = "deg"
    assert angle_period(ds, None, "duration") is None
    assert angle_period(ds, None, "hour_of_day") == (24.0, "h")
    assert angle_period(ds, None, "share") is None
    assert angle_period(ds, None, "aspect") is None  # values outside the circle


def test_a_tree_with_a_cycle_or_without_a_root_is_not_a_tree() -> None:
    ds = xr.Dataset({"parent": ("node", np.array([1, 2, 0]))})
    assert detect_structure(ds, None).first("tree") is None
    ok = xr.Dataset({"parent": ("node", np.array([-1, 0, 0]))})
    assert detect_structure(ok, None).first("tree") is not None


# ------------------------------------------------- review: MMM and tabular data stay plain


def _weekly(n: int = 104) -> pd.DatetimeIndex:
    return pd.date_range("2022-01-03", periods=n, freq="W-MON")


def test_a_target_named_y_and_merge_suffixes_are_data_not_positions() -> None:
    from mapi_sdk.schema import infer_schema

    rng = np.random.default_rng(0)
    ds = xr.Dataset(
        {v: ("date", rng.gamma(2, 1, 104)) for v in ("y", "x", "price")},
        coords={"date": _weekly()},
    )
    s = detect_structure(ds, infer_schema(ds, targets=["y"]))
    assert s.plain and not s.geometry_vars and s.first("points") is None
    # pandas merge suffixes and compass suffixes on a table of stores
    for a, b in (("sales_x", "sales_y"), ("sales_east", "sales_north")):
        stores = xr.Dataset(
            {a: ("store", rng.gamma(2, 1, 40)), b: ("store", rng.gamma(2, 1, 40))},
            coords={"store": [f"s{i}" for i in range(40)]},
        )
        assert detect_structure(stores, None).first("points") is None, a
    # x and y of a regression on observations are not positions without a proof
    reg = xr.Dataset({"x": ("obs", rng.random(50)), "y": ("obs", rng.random(50))})
    assert detect_structure(reg, None).first("points") is None
    # ... with length units, or integer lattice positions, they are
    for v in ("x", "y"):
        reg[v].attrs["units"] = "m"
    assert detect_structure(reg, None).first("points") is not None
    lat = xr.Dataset(
        {
            "lat": ("store", rng.uniform(30, 45, 50), {"units": "degrees"}),
            "lon": ("store", rng.uniform(-120, -75, 50), {"units": "degrees"}),
            "revenue": ("store", rng.gamma(2, 1, 50)),
        }
    )
    pts = detect_structure(lat, None).first("points")
    assert pts is not None and (pts.extra["x"], pts.extra["y"]) == ("lon", "lat")
    assert detect_structure(lat, None).first("circular") is None  # degrees, not angles


def test_an_mmm_panel_with_integer_codes_is_not_an_image() -> None:
    from mapi_runner.worker.explore_data import data_context, iter_series
    from mapi_sdk.schema import infer_schema

    rng = np.random.default_rng(0)
    ds = xr.Dataset(
        {
            "spend": (("date", "geo", "channel"), rng.gamma(2, 1, (104, 6, 5))),
            "sales": (("date", "geo"), rng.gamma(2, 1, (104, 6))),
        },
        coords={"date": _weekly(), "geo": [501, 502, 503, 504, 505, 506], "channel": np.arange(5)},
    )
    schema = infer_schema(ds, targets=["sales"], roles={"spend": "spend"})
    s = detect_structure(ds, schema)
    assert s.plain and s.kinds == ["timeseries"]
    c = data_context(ds, schema)
    assert len(list(iter_series(c, "spend"))) == 30  # one per geo and channel, not summed
    # a design matrix (obs, feature) and an integer week x geo table are not images
    xm = xr.Dataset(
        {"x": (("obs", "feature"), rng.normal(size=(200, 8)))},
        coords={"obs": np.arange(200), "feature": np.arange(8)},
    )
    assert detect_structure(xm, None).first("image") is None
    # an unnamed square grid without time is still an image
    grid = xr.Dataset({"z": (("a", "b"), rng.normal(size=(32, 32)))})
    assert detect_structure(grid, None).first("image") is not None


def test_an_integer_week_or_step_that_extends_is_time_or_a_sequence() -> None:
    from mapi_runner.worker.explore_data import data_context
    from mapi_sdk.schema import infer_schema

    rng = np.random.default_rng(0)
    for coords in (np.arange(1, 105), np.arange(104)):
        ds = xr.Dataset(
            {"sales": (("week", "geo"), rng.gamma(2, 1, (104, 3))),
             "spend": (("week", "geo"), rng.gamma(2, 1, (104, 3)))},
            coords={"week": coords, "geo": list("abc")},
        )  # fmt: skip
        schema = infer_schema(
            ds, targets=["sales"], roles={"spend": "spend"}, policies={"week": "extend"}
        )
        c = data_context(ds, schema)
        assert c.time_dim == "week" and c.strong_time, coords[:2]
        assert "timeseries" not in c.shapes.not_applicable
    # a step number (E24) is a sequence: Over time applies, the lag views do not
    steps = xr.Dataset(
        {"step_length": (("animal", "step"), rng.gamma(2, 1, (4, 40)))},
        coords={"animal": list("abcd"), "step": np.arange(40)},
    )
    sch = DataSchema.model_validate(
        {
            "dims": {
                "animal": {"kind": "categorical"},
                "step": {"kind": "integer", "policy": "extend"},
            },
            "vars": {"step_length": {"dims": ["animal", "step"], "dtype": "float64"}},
        }
    )
    assert time_strength(steps, sch, "step") == 1
    assert detect_structure(steps, sch).sequence_dim == "step"


def test_durations_temperatures_and_positions_are_not_circular() -> None:
    n = 60
    ds = xr.Dataset(
        {
            "hours_open": ("n", np.linspace(8, 12, n), {"units": "hours"}),
            "man_hours": ("n", np.linspace(1, 20, n), {"units": "h"}),
            "months_active": ("n", np.linspace(1, 11, n), {"units": "months"}),
            "temperature": ("n", np.linspace(20, 95, n), {"units": "degrees"}),
            "lat": ("n", np.linspace(30, 45, n), {"units": "degrees"}),
            "hour_lst": ("n", np.linspace(0, 23, n), {"units": "h"}),
            "wind_from": ("n", np.linspace(0, 350, n), {"units": "deg"}),
            "month": ("n", np.linspace(1, 12, n), {"units": "month"}),
        }
    )
    for v in ("hours_open", "man_hours", "months_active", "temperature", "lat"):
        assert angle_period(ds, None, v) is None, v
    assert angle_period(ds, None, "hour_lst") == (24.0, "h")
    assert angle_period(ds, None, "wind_from") == (360.0, "deg")
    assert angle_period(ds, None, "month") == (12.0, "month")


def test_a_dim_that_only_layers_a_network_has_no_groups() -> None:
    rng = np.random.default_rng(2)
    ds = xr.Dataset(
        {
            "tie": (("lawyer", "lawyer_"), (rng.random((20, 20)) > 0.8).astype(float)),
            "same": (("lawyer", "lawyer_", "covariate"), rng.random((20, 20, 3))),
        },
        coords={"covariate": ["office", "practice", "gender"]},
    )
    s = detect_structure(ds, None)
    assert "groups" in s.not_applicable and "covariate" not in s.categorical_dims
