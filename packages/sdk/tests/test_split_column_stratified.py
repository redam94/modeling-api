"""v0.14 row splits: ``column`` (a fixed train/test assignment read from the data) and
``stratified`` (a random share of each class)."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.split import split_dataset
from mapi_spec import spec as spec_mod
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import ColumnSplit, StratifiedSplit
from pydantic import TypeAdapter

pytestmark = pytest.mark.unit


def _images(labels: list[str], split: list[str] | None = None) -> xr.Dataset:
    n = len(labels)
    ids = [f"{i:03d}" for i in range(n)]
    coords: dict[str, object] = {"image": ids, "y": [0, 1], "x": [0, 1]}
    if split is not None:
        coords["split"] = ("image", np.array(split))
    return xr.Dataset(
        {
            "pixels": (("image", "y", "x"), np.arange(n * 4, dtype=np.uint8).reshape(n, 2, 2)),
            "label": (("image",), np.array(labels)),
        },
        coords=coords,
    )


def _column(**kw: object) -> ColumnSplit:
    return ColumnSplit.model_validate({"kind": "column", "dim": "image", **kw})


def _strat(**kw: object) -> StratifiedSplit:
    return StratifiedSplit.model_validate({"kind": "stratified", "dim": "image", **kw})


# ---------------------------------------------------------------------------------- column


def test_column_split_holds_out_the_test_rows_by_default() -> None:
    ds = _images(["a", "b", "a", "b"], split=["train", "test", "train", "test"])
    res = split_dataset(ds, _column(column="split"))
    assert res.kind == "column" and not res.is_masked
    assert list(res.holdout.image.values) == ["001", "003"]
    assert list(res.train.image.values) == ["000", "002"]


def test_column_split_with_train_drops_the_other_rows() -> None:
    ds = _images(["a"] * 5, split=["train", "test", "valid", "train", "test"])
    res = split_dataset(ds, _column(column="split", holdout=["test"], train=["train"]))
    assert list(res.train.image.values) == ["000", "003"]
    assert list(res.holdout.image.values) == ["001", "004"]


def test_column_split_reads_a_data_variable_and_compares_numbers_as_text() -> None:
    ds = _images(["a", "b", "c"])
    ds["fold"] = ("image", np.array([1.0, 2.0, 1.0]))
    res = split_dataset(ds, _column(column="fold", holdout=[2]))
    assert list(res.holdout.image.values) == ["001"]
    res = split_dataset(ds, _column(column="fold", holdout=["1"]))
    assert list(res.holdout.image.values) == ["000", "002"]


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"column": "split", "holdout": ["nope"]}, "no holdout data"),
        ({"column": "split", "holdout": ["train", "test"]}, "no training data"),
        ({"column": "pixels"}, "is not a coordinate or a 1-D variable"),
        ({"column": "missing"}, "is not a coordinate or a 1-D variable"),
    ],
)
def test_column_split_errors(kw: dict[str, object], message: str) -> None:
    ds = _images(["a", "b"], split=["train", "test"])
    with pytest.raises(MapiError, match=message) as err:
        split_dataset(ds, _column(**kw))
    assert err.value.code is ErrorCode.SPLIT_INVALID


def test_column_split_on_an_unknown_dim() -> None:
    ds = _images(["a", "b"], split=["train", "test"])
    with pytest.raises(MapiError, match="does not exist"):
        split_dataset(ds, ColumnSplit(kind="column", dim="row", column="split"))


# ------------------------------------------------------------------------------ stratified


def test_stratified_holds_out_the_same_share_of_each_class() -> None:
    labels = ["cat"] * 40 + ["dog"] * 20 + ["bird"] * 8
    ds = _images(labels)
    res = split_dataset(ds, _strat(by="label", fraction=0.25, seed=3))
    held = list(res.holdout["label"].values)
    assert held.count("cat") == 10 and held.count("dog") == 5 and held.count("bird") == 2
    assert res.train.sizes["image"] + res.holdout.sizes["image"] == len(labels)
    assert not set(res.train.image.values) & set(res.holdout.image.values)
    # the rows keep their order
    assert list(res.holdout.image.values) == sorted(res.holdout.image.values)


def test_stratified_keeps_single_rows_and_missing_labels_in_training() -> None:
    labels = ["a", "a", "a", "a", "lonely", "", ""]
    res = split_dataset(_images(labels), _strat(by="label", fraction=0.5, seed=0))
    held = list(res.holdout["label"].values)
    assert held == ["a", "a"]
    assert "lonely" in res.train["label"].values
    assert list(res.train["label"].values).count("") == 2


def test_stratified_is_deterministic_by_seed_and_uses_the_fit_seed() -> None:
    ds = _images(["a"] * 30 + ["b"] * 30)
    pick = lambda res: list(res.holdout.image.values)  # noqa: E731
    one = pick(split_dataset(ds, _strat(by="label", fraction=0.3, seed=7)))
    assert one == pick(split_dataset(ds, _strat(by="label", fraction=0.3, seed=7)))
    assert one != pick(split_dataset(ds, _strat(by="label", fraction=0.3, seed=8)))
    by_fit = pick(split_dataset(ds, _strat(by="label", fraction=0.3), seed=7))
    assert by_fit == one


def test_stratified_on_a_coordinate_with_integer_classes() -> None:
    ds = _images(["x"] * 12)
    ds = ds.assign_coords(cls=("image", np.array([0, 1, 2] * 4)))
    res = split_dataset(ds, _strat(by="cls", fraction=0.5, seed=1))
    assert sorted(int(v) for v in res.holdout.cls.values) == [0, 0, 1, 1, 2, 2]


def test_stratified_errors() -> None:
    ds = _images(["a", "b", "c"])
    with pytest.raises(MapiError, match="no holdout data"):
        split_dataset(ds, _strat(by="label", fraction=0.4))
    with pytest.raises(MapiError, match="is not a coordinate or a 1-D variable"):
        split_dataset(ds, _strat(by="pixels", fraction=0.4))


def test_the_split_union_parses_both_kinds() -> None:
    adapter: TypeAdapter[object] = TypeAdapter(spec_mod.Split)
    col = adapter.validate_python({"kind": "column", "dim": "image", "column": "split"})
    assert (
        isinstance(col, ColumnSplit) and col.holdout == ["test"] and col.split_dims() == ["image"]
    )
    st = adapter.validate_python(
        {"kind": "stratified", "dim": "image", "by": "label", "fraction": 0.2}
    )
    assert isinstance(st, StratifiedSplit) and st.split_dims() == ["image"]
