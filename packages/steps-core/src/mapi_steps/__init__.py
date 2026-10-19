"""Built-in preprocessing steps for modeling-api (``core.*``).

Every step implements the :class:`mapi_sdk.steps.Step` contract and exports static JSON
(settings schema, templated :class:`~mapi_spec.effects.SchemaEffect`, context need) so the
control plane can validate specs without importing this package. Steps are discovered
through the ``mapi.steps`` entry point (:func:`builtin_steps`); :func:`registry` returns a
ready :class:`~mapi_sdk.registry.Registry`, and ``python -m mapi_steps.catalog`` prints the
catalog JSON.

==========  ===========================================================================
Reshape     resample, count_events, filter_coords, to_long, to_padded, stack, unstack
Value       fx_convert, mean_scale, max_abs_scale, standardize, log1p, logit, impute,
            replace_sentinel, clip
Feature     fourier, trend, calendar, holidays, lag, lag_tensor, one_hot, encode_levels,
            missing_indicator, formula
Precompute  adstock, saturation (point models)
Split       time_holdout, as_of, leave_group_out, last_per_group, mask_block
            (:mod:`mapi_steps.splits`; chosen in ``data.split``, applied by the harness)
==========  ===========================================================================
"""

from __future__ import annotations

from importlib import metadata as _metadata
from typing import Any

from mapi_sdk.registry import Registry
from mapi_sdk.steps import Step

from mapi_steps.feature import (
    CalendarStep,
    EncodeLevelsStep,
    FourierStep,
    HolidaysStep,
    LagStep,
    LagTensorStep,
    MissingIndicatorStep,
    OneHotStep,
    TrendStep,
)
from mapi_steps.formula import FormulaStep
from mapi_steps.precompute import AdstockStep, SaturationStep
from mapi_steps.reshape import (
    CountEventsStep,
    FilterCoordsStep,
    ResampleStep,
    StackStep,
    ToLongStep,
    ToPaddedStep,
    UnstackStep,
)
from mapi_steps.splits import SPLITS, SplitStrategy, split_catalog, split_strategy
from mapi_steps.value import (
    ClipStep,
    FxConvertStep,
    ImputeStep,
    Log1pStep,
    LogitStep,
    MaxAbsScaleStep,
    MeanScaleStep,
    ReplaceSentinelStep,
    StandardizeStep,
)

try:
    __version__ = _metadata.version("mapi-steps")
except _metadata.PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0.9.1"

STEPS: tuple[type[Step[Any, Any]], ...] = (
    # reshape
    ResampleStep,
    CountEventsStep,
    FilterCoordsStep,
    ToLongStep,
    ToPaddedStep,
    StackStep,
    UnstackStep,
    # value
    FxConvertStep,
    MeanScaleStep,
    MaxAbsScaleStep,
    StandardizeStep,
    Log1pStep,
    LogitStep,
    ImputeStep,
    ReplaceSentinelStep,
    ClipStep,
    # feature
    FourierStep,
    TrendStep,
    CalendarStep,
    HolidaysStep,
    LagStep,
    LagTensorStep,
    OneHotStep,
    EncodeLevelsStep,
    MissingIndicatorStep,
    FormulaStep,
    # precompute
    AdstockStep,
    SaturationStep,
)


def builtin_steps() -> list[type[Step[Any, Any]]]:
    """Every built-in step class (the ``mapi.steps`` entry point)."""
    return list(STEPS)


def registry(base: Registry | None = None) -> Registry:
    """A registry holding the built-in steps (added to a copy of ``base`` when given)."""
    reg = base.copy() if base is not None else Registry()
    reg.register(builtin_steps(), replace=True)
    return reg


__all__ = [
    "SPLITS",
    "STEPS",
    "AdstockStep",
    "CalendarStep",
    "ClipStep",
    "CountEventsStep",
    "EncodeLevelsStep",
    "FilterCoordsStep",
    "FormulaStep",
    "FourierStep",
    "FxConvertStep",
    "HolidaysStep",
    "ImputeStep",
    "LagStep",
    "LagTensorStep",
    "Log1pStep",
    "LogitStep",
    "MaxAbsScaleStep",
    "MeanScaleStep",
    "MissingIndicatorStep",
    "OneHotStep",
    "ReplaceSentinelStep",
    "ResampleStep",
    "SaturationStep",
    "SplitStrategy",
    "StackStep",
    "StandardizeStep",
    "ToLongStep",
    "ToPaddedStep",
    "TrendStep",
    "UnstackStep",
    "__version__",
    "builtin_steps",
    "registry",
    "split_catalog",
    "split_strategy",
]
