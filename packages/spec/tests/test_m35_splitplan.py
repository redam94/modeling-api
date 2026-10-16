"""M3.5 split plans: fold windows match scikit-learn's reference splitters, edge cases are
refused with SPLIT_INVALID, and expanding folds are exactly the existing as_of split."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import AsOfSplit
from mapi_spec.splitplan import (
    BlockedKFoldPlan,
    ExpandingWindowPlan,
    Fold,
    GeoHoldoutPlan,
    HoldoutPlan,
    NestedPlan,
    PurgedKFoldPlan,
    RollingOriginPlan,
    SplitPlan,
    plan_folds,
    plan_group_folds,
)
from pydantic import TypeAdapter, ValidationError

pytestmark = pytest.mark.unit
PLAN: TypeAdapter[Any] = TypeAdapter(SplitPlan)


def _windows(folds: list[Fold]) -> list[tuple[list[int], list[int]]]:
    out = []
    for f in folds:
        assert f.test_start is not None and f.test_stop is not None
        out.append((f.train_positions, list(range(f.test_start, f.test_stop))))
    return out


@pytest.mark.parametrize(
    ("n", "k", "h", "gap", "max_train"),
    [(100, 5, 8, 0, None), (100, 5, 8, 2, None), (57, 3, 4, 1, None), (100, 6, 4, 0, 30),
     (24, 4, 3, 0, 10)],
)  # fmt: skip
def test_window_plans_match_time_series_split(
    n: int, k: int, h: int, gap: int, max_train: int | None
) -> None:
    sk = pytest.importorskip("sklearn.model_selection")
    import numpy as np

    ref = sk.TimeSeriesSplit(n_splits=k, test_size=h, gap=gap, max_train_size=max_train)
    expected = [(list(tr), list(te)) for tr, te in ref.split(np.zeros(n))]
    common: dict[str, Any] = {"horizons": [h], "gap": gap, "n_folds": k}
    plan = (
        RollingOriginPlan(kind="rolling_origin", window=max_train, **common)
        if max_train
        else ExpandingWindowPlan(kind="expanding_window", **common)
    )
    assert _windows(plan_folds(plan, n)) == expected


@pytest.mark.parametrize(("n", "k"), [(10, 3), (100, 5), (7, 7), (13, 4)])
def test_blocked_kfold_matches_unshuffled_kfold(n: int, k: int) -> None:
    sk = pytest.importorskip("sklearn.model_selection")
    import numpy as np

    expected = [(list(tr), list(te)) for tr, te in sk.KFold(n_splits=k).split(np.zeros(n))]
    got = _windows(plan_folds(BlockedKFoldPlan(kind="blocked_kfold", k=k), n))
    assert got == expected


def test_blocked_gap_purge_and_embargo() -> None:
    folds = plan_folds(BlockedKFoldPlan(kind="blocked_kfold", k=4, gap=2), 20)
    second = folds[1]
    assert (second.test_start, second.test_stop) == (5, 10)
    assert second.train_positions == [0, 1, 2, *range(12, 20)]
    purged = plan_folds(PurgedKFoldPlan(kind="purged_kfold", k=4, purge=1, embargo=3), 20)
    assert purged[1].train_positions == [0, 1, 2, 3, *range(13, 20)]
    assert purged[0].train_positions == list(range(8, 20))  # no purge before the first block
    assert purged[-1].train_positions == list(range(14))  # no embargo after the last


def test_windows_with_steps_horizons_and_start_anchor() -> None:
    plan = ExpandingWindowPlan(
        kind="expanding_window", horizons=[1, 4, 8], step=4, gap=1, n_folds=3, anchor="start",
        min_train=50,
    )  # fmt: skip
    folds = plan_folds(plan, 100)
    assert [(f.train_stop, f.test_start, f.test_stop) for f in folds] == [
        (50, 51, 59), (54, 55, 63), (58, 59, 67),
    ]  # fmt: skip
    assert all(f.horizons == [1, 4, 8] and f.excluded for f in folds)
    auto = plan_folds(
        ExpandingWindowPlan(kind="expanding_window", horizons=[4], min_train=80), 100
    )  # as many folds as fit: (100 - 0 - 4 - 80) // 4 + 1 = 5
    assert len(auto) == 5 and auto[-1].test_stop == 100
    rolling = plan_folds(
        RollingOriginPlan(kind="rolling_origin", horizons=[2], window=10, n_folds=2), 30
    )
    assert [(f.train_start, f.train_stop) for f in rolling] == [(16, 26), (18, 28)]


def test_expanding_folds_are_as_of_splits() -> None:
    coords: list[Any] = [f"2024-01-{d:02d}" for d in range(1, 31)]
    fold = plan_folds(
        ExpandingWindowPlan(kind="expanding_window", horizons=[3], gap=2, n_folds=1), 30
    )[0]
    split = fold.as_of_split("time", coords)
    assert split == AsOfSplit(kind="as_of", dim="time", as_of="2024-01-25", gap=2)
    rolling = plan_folds(
        RollingOriginPlan(kind="rolling_origin", horizons=[3], window=5, n_folds=1), 30
    )[0]
    with pytest.raises(MapiError):
        rolling.as_of_split("time", coords)


def test_nested_plans_shift_inner_folds_into_each_training_window() -> None:
    plan = NestedPlan(
        kind="nested",
        outer=ExpandingWindowPlan(kind="expanding_window", horizons=[4], n_folds=2),
        inner=ExpandingWindowPlan(kind="expanding_window", horizons=[2], n_folds=2),
    )
    folds = plan_folds(plan, 40)
    assert [(f.train_start, f.train_stop) for f in folds] == [(0, 32), (0, 36)]
    assert [(f.test_start, f.test_stop) for f in folds[0].inner] == [(28, 30), (30, 32)]
    with pytest.raises(ValidationError):
        NestedPlan.model_validate({"kind": "nested",
                                   "outer": {"kind": "geo_holdout", "n": 1},
                                   "inner": {"kind": "blocked_kfold", "k": 2}})  # fmt: skip


def test_group_folds_are_seeded_and_explicit_groups_checked() -> None:
    levels: list[Any] = ["de", "fr", "it", "es", "nl"]
    plan = GeoHoldoutPlan(kind="geo_holdout", n=2, n_folds=3, seed=7)
    a, b = plan_group_folds(plan, levels), plan_group_folds(plan, levels)
    assert a == b and len(a) == 3
    for f in a:
        assert f.test_groups is not None and f.train_groups is not None
        assert len(f.test_groups) == 2 and set(f.test_groups) | set(f.train_groups) == set(levels)
    explicit = plan_group_folds(
        GeoHoldoutPlan(kind="geo_holdout", groups=["it"], time_dim="week", test_last=4,
                       include_pre_period=True), levels, n_time=52,
    )  # fmt: skip
    assert explicit[0].test_groups == ["it"]
    assert (explicit[0].test_start, explicit[0].test_stop) == (48, 52)
    assert (explicit[0].train_start, explicit[0].train_stop) == (0, 48)
    with pytest.raises(MapiError):
        plan_group_folds(GeoHoldoutPlan(kind="geo_holdout", groups=["xx"]), levels)
    with pytest.raises(MapiError):
        plan_group_folds(GeoHoldoutPlan(kind="geo_holdout", n=5), levels)


@pytest.mark.parametrize(
    ("plan", "n", "message"),
    [
        ({"kind": "expanding_window", "horizons": [8], "n_folds": 20}, 50, "fit"),
        ({"kind": "rolling_origin", "horizons": [8], "window": 60}, 50, "fit"),
        ({"kind": "blocked_kfold", "k": 6}, 5, "cannot cut"),
        ({"kind": "blocked_kfold", "k": 2, "gap": 10}, 12, "no training data"),
        ({"kind": "expanding_window", "horizons": [1]}, 1, "at least two"),
        ({"kind": "holdout", "split": {"kind": "time_holdout", "dim": "t", "last": 4}}, 50,
         "resolved by its split"),
        ({"kind": "geo_holdout", "n": 1}, 50, "group folds"),
    ],
)  # fmt: skip
def test_impossible_plans_are_split_invalid(plan: dict[str, Any], n: int, message: str) -> None:
    with pytest.raises(MapiError) as exc:
        plan_folds(PLAN.validate_python(plan), n)
    assert exc.value.code is ErrorCode.SPLIT_INVALID and message in exc.value.detail


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "expanding_window", "horizons": [4, 1]},
        {"kind": "expanding_window", "horizons": []},
        {"kind": "geo_holdout"},
        {"kind": "geo_holdout", "groups": ["a"], "n": 1},
        {"kind": "geo_holdout", "groups": ["a"], "n_folds": 2},
        {"kind": "geo_holdout", "n": 1, "test_last": 4},
        {"kind": "blocked_kfold", "k": 1},
        {"kind": "rolling_origin", "horizons": [1]},
        {"kind": "magic"},
    ],
)
def test_plan_documents_are_validated(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PLAN.validate_python(bad)


def test_holdout_plans_wrap_the_existing_split() -> None:
    plan = PLAN.validate_python(
        {"kind": "holdout", "split": {"kind": "as_of", "dim": "week", "as_of": "2024-06-30"}}
    )
    assert isinstance(plan, HoldoutPlan) and isinstance(plan.split, AsOfSplit)
    example = Fold.model_config["json_schema_extra"]["examples"][0]  # type: ignore[index]
    assert Fold.model_validate(example).train_positions == list(range(80))
