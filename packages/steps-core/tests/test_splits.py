"""The split catalog (core.<kind>) and its direct split API."""

from __future__ import annotations

import mapi_steps as ms
import numpy as np
import pytest
import xarray as xr
from jsonschema import Draft202012Validator
from mapi_sdk.schema import infer_schema
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import TimeHoldoutSplit
from mapi_steps.splits import split_catalog
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def test_catalog_entries() -> None:
    entries = split_catalog()
    assert [e.id for e in entries] == [
        "core.time_holdout",
        "core.as_of",
        "core.leave_group_out",
        "core.last_per_group",
        "core.mask_block",
    ]
    for e in entries:
        assert e.ref.endswith("@1.0.0")
        assert "kind" not in e.settings_schema.get("properties", {})
        Draft202012Validator.check_schema(e.settings_schema)
    holdout = entries[0]
    Draft202012Validator(holdout.settings_schema).validate({"dim": "time", "last": 4})


def test_time_holdout_and_as_of(weekly: xr.Dataset) -> None:
    res = ms.split_strategy("core.time_holdout").split(weekly, {"dim": "time", "last": 4})
    assert res.train.sizes["time"] == 26 and res.holdout.sizes["time"] == 4
    res = ms.split_strategy("as_of@1").split(
        weekly, {"dim": "time", "as_of": "2024-03-31", "gap": 1}
    )
    assert str(res.train["time"].values[-1])[:10] == "2024-03-31"
    assert str(res.holdout["time"].values[0])[:10] == "2024-04-14"
    parsed = ms.split_strategy("time_holdout").parse(
        TimeHoldoutSplit(kind="time_holdout", dim="t", last=2)
    )
    assert parsed.last == 2
    with pytest.raises(ValidationError):
        ms.split_strategy("time_holdout").parse({"dim": "time"})
    with pytest.raises(KeyError):
        ms.split_strategy("core.random")


def test_group_and_cell_splits(weekly: xr.Dataset) -> None:
    res = ms.split_strategy("leave_group_out").split(weekly, {"dim": "geo", "groups": ["UK"]})
    assert list(res.holdout["geo"].values) == ["UK"] and "UK" not in res.train["geo"].values
    schema = infer_schema(weekly, targets=["y"])
    res = ms.split_strategy("last_per_group").split(
        weekly, {"dim": "time", "within": ["geo"], "last": 2, "min_len": 5}, schema=schema
    )
    assert res.holdout_mask is not None and int(res.holdout_mask.sum()) == 2 * 3
    assert int(res.train["y"].isnull().sum()) == 6
    res = ms.split_strategy("mask_block").split(
        weekly,
        {"dims": ["geo", "time"], "fraction": 0.2, "block": {"time": 3}, "seed": 1},
        schema=schema,
    )
    assert res.is_masked and 0 < float(res.holdout_mask.mean()) < 0.5
    np.testing.assert_array_equal(
        res.train["y"].isnull().values, res.holdout_mask.transpose("geo", "time").values
    )
    with pytest.raises(MapiError) as err:
        ms.split_strategy("mask_block").split(weekly, {"dims": ["geo"], "fraction": 0.5})
    assert err.value.code is ErrorCode.SPLIT_INVALID
