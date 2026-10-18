"""The contract kit for step and model authors (``mapi_sdk.testing``).

* :func:`check_step` / :func:`check_model` run the contract checks of plan §13 and return a
  :class:`ContractReport` (raising :class:`ContractViolation` on failure by default);
* :func:`check_tree_model` runs the checks of tree-input models (M4) on a
  :class:`TreeModelFixture` (:func:`tree_dataset` is a small tree fixture);
* :mod:`mapi_sdk.testing.toy` holds small reference steps and models;
* :mod:`mapi_sdk.testing.strategies` holds Hypothesis strategies (needs ``hypothesis``);
* the pytest plugin (:mod:`mapi_sdk.testing.plugin`, auto-loaded) provides fixtures.

Attributes are imported lazily so that loading the pytest plugin stays cheap.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mapi_sdk.testing import toy
    from mapi_sdk.testing.checks import (
        CheckResult,
        ContractReport,
        ContractViolation,
        ModelFixture,
        check_about,
        check_model,
        check_step,
    )
    from mapi_sdk.testing.toy import panel_dataset, toy_registry, tree_dataset
    from mapi_sdk.testing.tree_checks import TreeModelFixture, check_tree_model

_LAZY: dict[str, tuple[str, str | None]] = {
    "CheckResult": ("mapi_sdk.testing.checks", "CheckResult"),
    "ContractReport": ("mapi_sdk.testing.checks", "ContractReport"),
    "ContractViolation": ("mapi_sdk.testing.checks", "ContractViolation"),
    "ModelFixture": ("mapi_sdk.testing.checks", "ModelFixture"),
    "check_about": ("mapi_sdk.testing.checks", "check_about"),
    "check_model": ("mapi_sdk.testing.checks", "check_model"),
    "check_step": ("mapi_sdk.testing.checks", "check_step"),
    "panel_dataset": ("mapi_sdk.testing.toy", "panel_dataset"),
    "toy_registry": ("mapi_sdk.testing.toy", "toy_registry"),
    "tree_dataset": ("mapi_sdk.testing.toy", "tree_dataset"),
    "TreeModelFixture": ("mapi_sdk.testing.tree_checks", "TreeModelFixture"),
    "check_tree_model": ("mapi_sdk.testing.tree_checks", "check_tree_model"),
    "toy": ("mapi_sdk.testing.toy", None),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'mapi_sdk.testing' has no attribute {name!r}")
    module = importlib.import_module(target[0])
    value = module if target[1] is None else getattr(module, target[1])
    globals()[name] = value
    return value


__all__ = [
    "CheckResult",
    "ContractReport",
    "ContractViolation",
    "ModelFixture",
    "TreeModelFixture",
    "check_about",
    "check_model",
    "check_step",
    "check_tree_model",
    "panel_dataset",
    "toy",
    "toy_registry",
    "tree_dataset",
]
