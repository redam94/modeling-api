"""CoordPlan, coordinate canonicalisation and horizon generation."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_sdk.context import ContextMark
from mapi_sdk.coords import (
    CoordPlan,
    canonicalise_coords,
    horizon_coords,
    horizon_frame,
    next_label,
    payload_issues,
    required_future_vars,
)
from mapi_sdk.pipeline import Pipeline
from mapi_sdk.registry import Registry
from mapi_sdk.schema import infer_schema
from mapi_spec import DataSchema, ModelSpec, load_score_request, validate_spec
from mapi_spec.errors import CoordPlanError, ErrorCode, MapiError, Severity

SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "geo": {"kind": "categorical", "policy": "new_levels", "new_level_strategy": "pooled"},
            "channel": {"kind": "categorical", "policy": "fixed"},
            "time": {"kind": "time", "policy": "extend", "freq": "W-SUN", "max_extension": 6},
        },
        "vars": {
            "y": {
                "dims": ["geo", "time"],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
            },
            "x": {"dims": ["geo", "channel", "time"], "dtype": "float64", "future_required": True},
        },
    }
)
TIME = pd.date_range("2024-01-07", periods=10, freq="W-SUN").as_unit("ns")
FIT = xr.Dataset(coords={"geo": ["a", "b", "c"], "channel": ["tv", "web"], "time": TIME})


def _data(geo: list[str], channel: list[str], time: np.ndarray) -> xr.Dataset:
    rng = np.random.default_rng(0)
    return xr.Dataset(
        {
            "y": (("geo", "time"), rng.normal(size=(len(geo), len(time)))),
            "x": (("geo", "channel", "time"), rng.normal(size=(len(geo), len(channel), len(time)))),
        },
        coords={"geo": geo, "channel": channel, "time": time},
    )


def test_fixed_dims_are_reordered_padded_and_restored() -> None:
    ds = _data(["c", "a"], ["web"], TIME[:3])
    out, plan = canonicalise_coords(ds, FIT, SCHEMA)
    assert list(out.geo.values) == ["a", "b", "c"]
    assert list(out.channel.values) == ["tv", "web"]
    padded = out["x"].sel(channel="tv", time=TIME[:3])
    assert padded.values.tolist() == [[0.0] * 3] * 3  # padded with pad_fill
    assert np.isnan(out["y"].sel(geo="b")).all()  # targets padded with NaN
    assert out["geo__requested"].values.tolist() == [True, False, True]
    assert out["geo__is_new"].values.tolist() == [False, False, False]
    assert list(out.time.values) == list(TIME)  # extend dims padded to the full fit range
    restored = plan.restore(out)
    xr.testing.assert_identical(restored, ds)
    assert plan.new_levels("geo") == [] and plan.new_levels("nope") == []
    with pytest.raises(CoordPlanError) as err:
        canonicalise_coords(_data(["a"], ["radio"], TIME[:2]), FIT, SCHEMA)
    assert err.value.code is ErrorCode.UNSEEN_LEVELS and err.value.untrusted_text == "radio"


def test_new_levels_are_appended_and_flagged() -> None:
    ds = _data(["zz", "b"], ["tv", "web"], TIME[-2:])
    out, plan = canonicalise_coords(ds, FIT, SCHEMA, pad_extend=False)
    assert list(out.geo.values) == ["a", "b", "c", "zz"]
    assert out["geo__is_new"].values.tolist() == [False, False, False, True]
    assert plan.new_levels("geo") == ["zz"]
    assert list(out.time.values) == list(TIME[-2:])
    xr.testing.assert_identical(plan.restore(out), ds)
    strict = SCHEMA.model_copy(
        update={
            "dims": {
                **SCHEMA.dims,
                "geo": SCHEMA.dims["geo"].model_copy(update={"new_level_strategy": "error"}),
            }
        }
    )
    with pytest.raises(CoordPlanError, match="strategy is 'error'"):
        canonicalise_coords(ds, FIT, strict)


def test_extend_dim_errors() -> None:
    before = pd.date_range("2023-12-31", periods=2, freq="W-SUN").as_unit("ns").values
    with pytest.raises(CoordPlanError) as err:
        canonicalise_coords(_data(["a"], ["tv"], before), FIT, SCHEMA)
    assert err.value.code is ErrorCode.COORD_NOT_ALLOWED
    gap = (
        pd.date_range(TIME[-1] + pd.Timedelta(weeks=2), periods=2, freq="W-SUN")
        .as_unit("ns")
        .values
    )
    with pytest.raises(CoordPlanError) as err:
        canonicalise_coords(_data(["a"], ["tv"], gap), FIT, SCHEMA)
    assert err.value.code is ErrorCode.NON_CONTIGUOUS_EXTENSION
    far = (
        pd.date_range(TIME[-1] + pd.Timedelta(weeks=1), periods=7, freq="W-SUN")
        .as_unit("ns")
        .values
    )
    with pytest.raises(CoordPlanError) as err:
        canonicalise_coords(_data(["a"], ["tv"], far), FIT, SCHEMA)
    assert err.value.code is ErrorCode.HORIZON_EXCEEDS_FIT
    ok = far[:6]
    out, plan = canonicalise_coords(_data(["a"], ["tv"], ok), FIT, SCHEMA)
    assert out.sizes["time"] == 16 and plan.dims["time"].padded
    no_freq = SCHEMA.model_copy(
        update={
            "dims": {**SCHEMA.dims, "time": SCHEMA.dims["time"].model_copy(update={"freq": None})}
        }
    )
    irregular = FIT.assign_coords(
        time=np.concatenate([TIME[:5], TIME[6:]]).astype("datetime64[ns]")[:9]
    )
    with pytest.raises(CoordPlanError, match="frequency is unknown"):
        canonicalise_coords(_data(["a"], ["tv"], ok[:1]), irregular.isel(time=slice(0, 9)), no_freq)


def test_context_labels_are_not_requested() -> None:
    ds = _data(["a"], ["tv"], TIME[-3:])
    mark = ContextMark(dim="time", labels=TIME[-3:-1].values)
    out, plan = canonicalise_coords(ds, FIT, SCHEMA, marks=[mark])
    assert plan.restore(out).sizes["time"] == 1
    ints = xr.Dataset({"v": (("k",), [1.0, 2.0])}, coords={"k": [10, 12]})
    schema_k = DataSchema.model_validate(
        {
            "dims": {"k": {"kind": "integer", "policy": "extend"}},
            "vars": {"v": {"dims": ["k"], "dtype": "float64"}},
        }
    )
    fit_k = xr.Dataset(coords={"k": [0, 2, 4, 6, 8]})
    out_k, _ = canonicalise_coords(ints, fit_k, schema_k)
    assert out_k["k"].values.tolist() == [0, 2, 4, 6, 8, 10, 12]
    untouched, plan_u = canonicalise_coords(ints, fit_k, None)
    assert untouched is ints and not plan_u.dims


@settings(max_examples=40, deadline=None)
@given(
    order=st.permutations(["a", "b", "c"]),
    keep=st.integers(1, 3),
    new=st.booleans(),
    weeks=st.integers(1, 4),
)
def test_property_canonicalise_restore_roundtrip(
    order: list[str], keep: int, new: bool, weeks: int
) -> None:
    geos = list(order[:keep]) + (["new_geo"] if new else [])
    ds = _data(geos, ["web", "tv"], TIME[-weeks:].values)
    out, plan = canonicalise_coords(ds, FIT, SCHEMA)
    assert list(out.geo.values)[:3] == ["a", "b", "c"]
    xr.testing.assert_identical(plan.restore(out), ds)


def test_horizon_coords_and_frames() -> None:
    weekly = horizon_coords(TIME, 3)
    assert [str(v)[:10] for v in weekly] == ["2024-03-17", "2024-03-24", "2024-03-31"]
    months = horizon_coords(pd.date_range("2024-01-01", periods=3, freq="MS").values, 2)
    assert [str(v)[:10] for v in months] == ["2024-04-01", "2024-05-01"]
    assert horizon_coords(np.array([0, 5, 10]), 2).tolist() == [15, 20]
    with pytest.raises(CoordPlanError):
        horizon_coords(np.array([]), 2)
    with pytest.raises(CoordPlanError, match="frequency"):
        next_label(np.datetime64("2024-01-01"))
    frame = horizon_frame(FIT, "time", periods=2)
    assert frame.sizes == {"geo": 3, "channel": 2, "time": 2}
    until = horizon_frame(FIT, "time", until="2024-03-31", drop=["channel"])
    assert until.sizes["time"] == 3 and "channel" not in until.dims
    with pytest.raises(CoordPlanError):
        horizon_frame(FIT, "nope", periods=1)
    with pytest.raises(ValueError, match="periods or until"):
        horizon_frame(FIT, "time")
    assert required_future_vars(SCHEMA) == ["x"] and required_future_vars(None) == []


def _resolved(registry: Registry, spec: ModelSpec, data: xr.Dataset):  # type: ignore[no-untyped-def]
    raw = infer_schema(data, targets=["revenue_local"], policies={"market": "new_levels"})
    raw = raw.model_copy(
        update={
            "dims": {
                **raw.dims,
                "market": raw.dims["market"].model_copy(update={"new_level_strategy": "pooled"}),
            }
        }
    )
    return validate_spec(spec, registry.catalog(), raw).raise_for_errors()


def test_coordplan_checks_score_requests(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    from mapi_sdk.binding import BindingMap

    data, fx = panel
    spec = e33_spec(model="toy.linear@^1", metrics="rmse")
    rs = _resolved(registry, spec, data)
    pipe = Pipeline.from_spec(spec, registry, rs)
    canon = BindingMap(rs.binding).apply(data)
    trace = pipe.fit(canon, side_tables={"fx": fx})
    base = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/x\n"
    plan = CoordPlan(load_score_request(base + "mode: simulate\n"), rs, trace.coords)
    with pytest.raises(MapiError) as err:
        plan.check(None)
    assert err.value.code is ErrorCode.MODE_NOT_SUPPORTED
    ok = CoordPlan(load_score_request(base + "mode: predict\n"), rs, trace.coords)
    new_market = canon.isel(geo=[0]).assign_coords(geo=["XX"])
    issues = ok.check(new_market)
    assert any(i.code is ErrorCode.UNSEEN_LEVELS and i.severity is Severity.INFO for i in issues)
    assert payload_issues(issues) == issues
    radio = canon.isel(channel=[0]).assign_coords(channel=["radio"])
    with pytest.raises(CoordPlanError) as err2:
        ok.check(radio)
    assert err2.value.code is ErrorCode.UNSEEN_LEVELS
    assert ok.metadata().fit_coords["weekly"]["time"].freq == "W-SUN"


LONG = DataSchema.model_validate(
    {
        "layout": "long",
        "dims": {
            "obs": {"kind": "index", "policy": "extend"},
            "county": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
            },
        },
        "vars": {
            "y": {"dims": ["obs"], "dtype": "float64", "role": "target", "missing": "allow"},
            "county_idx": {
                "dims": ["obs"],
                "dtype": "int64",
                "role": "index",
                "index_of": "county",
            },
        },
    }
)


def test_long_layout_index_variables_follow_the_canonical_order() -> None:
    fit = xr.Dataset(coords={"obs": np.arange(4), "county": ["a", "b", "c"]})
    payload = xr.Dataset(
        {"county_idx": (("obs",), np.array([0, 1, 2, 0]))},
        coords={"obs": np.arange(4, 8), "county": ["c", "new", "a"]},
    )
    canon, plan = canonicalise_coords(payload, fit, LONG, pad_extend=False)
    assert list(canon["county"].values) == ["a", "b", "c", "new"]
    labels = canon["county"].values[canon["county_idx"].values]
    # every observation still points at the county it named in the payload
    assert list(labels) == ["c", "new", "a", "c"]
    assert canon["county_idx"].dtype == np.int64
    assert list(plan.dims["county"].new) == ["new"]
