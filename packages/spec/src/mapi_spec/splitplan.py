"""Split plans: multi-fold train/test designs for workflows and backtests (M3.5).

A ``ModelSpec`` holds one train/holdout :data:`~mapi_spec.spec.Split` (``data.split``).
A :data:`SplitPlan` describes *many* folds - a backtest - and is what workflow ``split``
nodes produce (port type ``split_plan``) and ``model``/``evaluate`` nodes consume:

====================  ======================================================================
kind                  folds
====================  ======================================================================
``holdout``           one fold: an existing ``Split`` (``time_holdout``, ``as_of``, ...)
``rolling_origin``    a fixed-length training window that slides forward; test windows of
                      ``max(horizons)`` periods after an optional ``gap``
``expanding_window``  like rolling origin, but training always starts at the beginning
``blocked_kfold``     ``k`` contiguous blocks; each is the test once, with ``gap`` periods
                      left out of training on both sides
``purged_kfold``      blocked k-fold with ``purge`` periods before and ``embargo`` after
                      each test block dropped from training (overlapping labels)
``geo_holdout``       whole groups (geos, stores) held out, optionally only over the last
                      ``test_last`` periods; repeated over seeded random draws
``nested``            an ``inner`` plan run inside every training window of an ``outer``
                      plan (tune on inner folds, score on outer ones)
====================  ======================================================================

Folds are computed from metadata only (:func:`plan_folds` for ordered plans, from the
number of positions along ``dim``; :func:`plan_group_folds` for group plans, from the
levels), so the control plane can show them before anything runs. Positions are
0-based half-open windows ``[start, stop)``. ``expanding_window`` and ``rolling_origin``
match scikit-learn's ``TimeSeriesSplit`` (``test_size=max(horizons)``, ``gap``,
``max_train_size``) when ``step == max(horizons)`` and ``anchor="end"``; ``blocked_kfold``
with ``gap=0`` matches ``KFold(shuffle=False)``. An expanding fold is exactly an
``as_of`` split (:meth:`Fold.as_of_split`), so the existing split machinery runs it.
"""

from __future__ import annotations

import random
from typing import Annotated, Final, Literal

from pydantic import Field, model_validator

from mapi_spec._dto import ResponseModel, dto_examples
from mapi_spec._types import Name, Scalar, StrictModel
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import AsOfSplit, Split

__all__ = [
    "MAX_FOLDS",
    "BlockedKFoldPlan",
    "ExpandingWindowPlan",
    "Fold",
    "GeoHoldoutPlan",
    "HoldoutPlan",
    "NestedPlan",
    "PurgedKFoldPlan",
    "RollingOriginPlan",
    "SplitPlan",
    "plan_folds",
    "plan_group_folds",
]

MAX_FOLDS: Final = 200


def _bad(message: str) -> MapiError:
    return MapiError(message, code=ErrorCode.SPLIT_INVALID)


class Fold(ResponseModel):
    """One fold. Ordered plans fill the position windows; group plans fill the groups.

    ``excluded`` are extra windows left out of training (gaps, purges, embargoes); the
    first ``gap`` positions of the test window's lead are scored for continuity only when
    the plan says so (``as_of`` semantics), never counted in metrics.
    """

    index: int = Field(..., ge=0)
    train_start: int | None = Field(None, ge=0)
    train_stop: int | None = Field(None, ge=0)
    test_start: int | None = Field(None, ge=0)
    test_stop: int | None = Field(None, ge=0)
    excluded: list[tuple[int, int]] = Field(default_factory=list)
    horizons: list[int] = Field(default_factory=list)
    train_groups: list[Scalar] | None = None
    test_groups: list[Scalar] | None = None
    inner: list[Fold] = Field(default_factory=list, description="nested plans: inner folds")

    model_config = dto_examples(
        {
            "index": 0,
            "train_start": 0,
            "train_stop": 80,
            "test_start": 82,
            "test_stop": 90,
            "excluded": [[80, 82]],
            "horizons": [1, 4, 8],
        }
    )

    @property
    def train_positions(self) -> list[int]:
        """Training positions (ordered folds), with ``excluded`` windows removed."""
        if self.train_start is None or self.train_stop is None:
            return []
        return [
            i
            for i in range(self.train_start, self.train_stop)
            if not any(a <= i < b for a, b in self.excluded)
        ]

    def as_of_split(self, dim: str, coords: list[Scalar]) -> AsOfSplit:
        """The existing ``as_of`` split this expanding fold stands for (``train_start`` must
        be 0): train up to and including ``coords[train_stop - 1]``, skip the gap."""
        if self.train_start != 0 or self.train_stop is None or self.test_start is None:
            raise _bad("only folds that train from the first period are as_of splits")
        if not 0 < self.train_stop <= len(coords):
            raise _bad("the fold does not fit the coordinates")
        return AsOfSplit(
            kind="as_of", dim=dim, as_of=coords[self.train_stop - 1],
            gap=self.test_start - self.train_stop,
        )  # fmt: skip


class _PlanBase(StrictModel):
    after_step: str | None = Field(
        None, description="Stage the folds are cut at (a step id or 'raw'), as data.split"
    )


class HoldoutPlan(_PlanBase):
    """One fold: the existing train/holdout split."""

    kind: Literal["holdout"]
    split: Split


class _WindowPlan(_PlanBase):
    dim: Name = "time"
    horizons: list[int] = Field(
        ..., min_length=1, max_length=32, description="Forecast horizons scored per fold"
    )
    step: int | None = Field(None, ge=1, description="Periods between origins (default: max)")
    gap: int = Field(0, ge=0, description="Periods between training and test (not scored)")
    n_folds: int | None = Field(None, ge=1, le=MAX_FOLDS)
    min_train: int = Field(1, ge=1, description="Fewest training periods a fold may have")
    anchor: Literal["end", "start"] = Field(
        "end", description="end: the last test window ends at the last period"
    )

    @model_validator(mode="after")
    def _horizons(self) -> _WindowPlan:
        if sorted(set(self.horizons)) != self.horizons or self.horizons[0] < 1:
            raise ValueError("horizons must be unique, ascending and >= 1")
        return self

    @property
    def test_size(self) -> int:
        return self.horizons[-1]

    @property
    def stride(self) -> int:
        return self.step or self.test_size


class RollingOriginPlan(_WindowPlan):
    """A fixed-length training ``window`` sliding forward."""

    kind: Literal["rolling_origin"]
    window: int = Field(..., ge=1, description="Training periods per fold")


class ExpandingWindowPlan(_WindowPlan):
    """Training from the first period up to each origin."""

    kind: Literal["expanding_window"]


class BlockedKFoldPlan(_PlanBase):
    """``k`` contiguous blocks; ``gap`` periods on each side of the test block are dropped."""

    kind: Literal["blocked_kfold"]
    dim: Name = "time"
    k: int = Field(..., ge=2, le=MAX_FOLDS)
    gap: int = Field(0, ge=0)


class PurgedKFoldPlan(_PlanBase):
    """Blocked k-fold with ``purge`` periods before and ``embargo`` after each test block
    removed from training (labels that overlap the test window)."""

    kind: Literal["purged_kfold"]
    dim: Name = "time"
    k: int = Field(..., ge=2, le=MAX_FOLDS)
    purge: int = Field(0, ge=0)
    embargo: int = Field(0, ge=0)


class GeoHoldoutPlan(_PlanBase):
    """Hold out whole groups along ``dim``: the explicit ``groups`` (one fold), or ``n``
    seeded random groups in each of ``n_folds`` folds. With ``time_dim``/``test_last``, the
    held-out groups are tested on their last periods only, and ``include_pre_period``
    trains on their earlier periods too (a geo experiment readout)."""

    kind: Literal["geo_holdout"]
    dim: Name = "geo"
    groups: list[Scalar] | None = Field(None, min_length=1, max_length=10_000)
    n: int | None = Field(None, ge=1)
    n_folds: int = Field(1, ge=1, le=MAX_FOLDS)
    seed: int = Field(0, ge=0, lt=2**32)
    time_dim: Name | None = None
    test_last: int | None = Field(None, ge=1)
    include_pre_period: bool = False

    @model_validator(mode="after")
    def _one(self) -> GeoHoldoutPlan:
        if (self.groups is None) == (self.n is None):
            raise ValueError("geo_holdout needs exactly one of groups or n")
        if self.groups is not None and self.n_folds != 1:
            raise ValueError("explicit groups make one fold")
        if (self.time_dim is None) != (self.test_last is None):
            raise ValueError("time_dim and test_last go together")
        if self.include_pre_period and self.test_last is None:
            raise ValueError("include_pre_period needs test_last")
        return self


_Flat = Annotated[
    HoldoutPlan
    | RollingOriginPlan
    | ExpandingWindowPlan
    | BlockedKFoldPlan
    | PurgedKFoldPlan
    | GeoHoldoutPlan,
    Field(discriminator="kind"),
]


class NestedPlan(_PlanBase):
    """An ``inner`` plan inside every training window of an ``outer`` ordered plan."""

    kind: Literal["nested"]
    outer: _Flat
    inner: _Flat

    @model_validator(mode="after")
    def _ordered(self) -> NestedPlan:
        if self.outer.kind in ("holdout", "geo_holdout") or self.inner.kind in (
            "holdout",
            "geo_holdout",
        ):
            raise ValueError("nested plans combine ordered plans (windows or k-folds)")
        return self


SplitPlan = Annotated[
    HoldoutPlan
    | RollingOriginPlan
    | ExpandingWindowPlan
    | BlockedKFoldPlan
    | PurgedKFoldPlan
    | GeoHoldoutPlan
    | NestedPlan,
    Field(discriminator="kind"),
]
"""A train/test design with one or more folds (workflow port type ``split_plan``)."""

OrderedPlan = RollingOriginPlan | ExpandingWindowPlan | BlockedKFoldPlan | PurgedKFoldPlan


def _window_folds(plan: RollingOriginPlan | ExpandingWindowPlan, n: int) -> list[Fold]:
    h, s, g = plan.test_size, plan.stride, plan.gap
    rolling = isinstance(plan, RollingOriginPlan)
    min_train = max(plan.min_train, plan.window if isinstance(plan, RollingOriginPlan) else 1)
    possible = (n - g - h - min_train) // s + 1 if n - g - h - min_train >= 0 else 0
    k = possible if plan.n_folds is None else plan.n_folds
    if k < 1 or k > possible:
        raise _bad(
            f"{plan.kind}: {n} periods fit {max(possible, 0)} fold(s) of {h} test periods "
            f"(gap {g}, step {s}, at least {min_train} training periods); asked for {k}"
        )
    if k > MAX_FOLDS:
        raise _bad(f"{plan.kind}: at most {MAX_FOLDS} folds")
    folds: list[Fold] = []
    for j in range(k):
        end_anchored = plan.anchor == "end"
        test_stop = n - (k - 1 - j) * s if end_anchored else min_train + g + h + j * s
        test_start = test_stop - h
        train_stop = test_start - g
        train_start = max(0, train_stop - plan.window) if rolling else 0  # type: ignore[union-attr]
        folds.append(
            Fold(
                index=j,
                train_start=train_start,
                train_stop=train_stop,
                test_start=test_start,
                test_stop=test_stop,
                excluded=[(train_stop, test_start)] if g else [],
                horizons=list(plan.horizons),
            )
        )
    return folds


def _kfold(n: int, k: int, before: int, after: int) -> list[Fold]:
    if k > n:
        raise _bad(f"cannot cut {n} periods into {k} blocks")
    base, extra = divmod(n, k)
    folds: list[Fold] = []
    start = 0
    for i in range(k):
        size = base + (1 if i < extra else 0)
        stop = start + size
        lo, hi = max(0, start - before), min(n, stop + after)
        excluded = [w for w in ((lo, start), (stop, hi)) if w[0] < w[1]]
        if (start - lo) + (hi - stop) + size >= n:
            raise _bad(f"fold {i} leaves no training data (gap, purge or embargo too large)")
        folds.append(
            Fold(
                index=i, train_start=0, train_stop=n, test_start=start, test_stop=stop,
                excluded=[(start, stop), *excluded],
            )
        )  # fmt: skip
        start = stop
    return folds


def plan_folds(plan: HoldoutPlan | OrderedPlan | NestedPlan, n: int) -> list[Fold]:
    """Folds of an ordered plan over ``n`` positions along its ``dim`` (pure).

    k-fold folds train on ``[0, n)`` minus ``excluded`` (which includes the test block);
    window folds train on ``[train_start, train_stop)``. ``holdout`` plans need the data to
    resolve their split and are refused here (``SPLIT_INVALID``); so are group plans
    (:func:`plan_group_folds`). Raises ``SPLIT_INVALID`` when the data is too short.
    """
    if n < 2:
        raise _bad("a split plan needs at least two periods")
    match plan:
        case RollingOriginPlan() | ExpandingWindowPlan():
            return _window_folds(plan, n)
        case BlockedKFoldPlan():
            return _kfold(n, plan.k, plan.gap, plan.gap)
        case PurgedKFoldPlan():
            return _kfold(n, plan.k, plan.purge, plan.embargo)
        case NestedPlan():
            outer = plan_folds(plan.outer, n)  # type: ignore[arg-type]
            out: list[Fold] = []
            for f in outer:
                if f.train_start is None or f.train_stop is None:
                    raise _bad("nested plans need ordered outer folds")
                size = f.train_stop - f.train_start
                inner = [
                    _shift(x, f.train_start)
                    for x in plan_folds(plan.inner, size)  # type: ignore[arg-type]
                ]
                out.append(f.model_copy(update={"inner": inner}))
            return out
        case HoldoutPlan():
            raise _bad("a holdout plan is resolved by its split on the data, not by position")
    raise _bad(f"{plan.kind} plans have group folds; use plan_group_folds")


def _shift(fold: Fold, by: int) -> Fold:
    def s(v: int | None) -> int | None:
        return None if v is None else v + by

    return fold.model_copy(
        update={
            "train_start": s(fold.train_start),
            "train_stop": s(fold.train_stop),
            "test_start": s(fold.test_start),
            "test_stop": s(fold.test_stop),
            "excluded": [(a + by, b + by) for a, b in fold.excluded],
        }
    )


def plan_group_folds(
    plan: GeoHoldoutPlan, levels: list[Scalar], *, n_time: int | None = None
) -> list[Fold]:
    """Folds of a ``geo_holdout`` plan over the group ``levels`` (pure, seeded).

    With ``test_last``, ``n_time`` (positions along ``time_dim``) is required and every fold
    tests its groups on ``[n_time - test_last, n_time)``; ``include_pre_period`` trains on
    their earlier periods too.
    """
    if len(set(map(repr, levels))) != len(levels):
        raise _bad("group levels must be unique")
    if plan.groups is not None:
        unknown = [g for g in plan.groups if g not in levels]
        if unknown:
            raise _bad(f"{len(unknown)} held-out group(s) are not in the data")
        draws = [list(plan.groups)]
    else:
        assert plan.n is not None
        if plan.n >= len(levels):
            raise _bad("geo_holdout would hold out every group")
        rng = random.Random(plan.seed)
        draws = [sorted(rng.sample(range(len(levels)), plan.n)) for _ in range(plan.n_folds)]
        draws = [[levels[i] for i in d] for d in draws]  # type: ignore[index]
    window: tuple[int, int] | None = None
    if plan.test_last is not None:
        if n_time is None or plan.test_last >= n_time:
            raise _bad("test_last needs fewer periods than the data has (n_time)")
        window = (n_time - plan.test_last, n_time)
    folds: list[Fold] = []
    for i, held in enumerate(draws):
        keep = [lv for lv in levels if lv not in held]
        folds.append(
            Fold(
                index=i,
                train_groups=keep,
                test_groups=list(held),
                test_start=window[0] if window else None,
                test_stop=window[1] if window else None,
                train_start=0 if window and plan.include_pre_period else None,
                train_stop=window[0] if window and plan.include_pre_period else None,
            )
        )
    return folds
