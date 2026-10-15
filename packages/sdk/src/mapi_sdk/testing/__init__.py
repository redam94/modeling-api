"""The contract kit for step and model authors (``mapi_sdk.testing``).

* :func:`check_step` / :func:`check_model` run the contract checks of plan §13 and return a
  :class:`ContractReport` (raising :class:`ContractViolation` on failure by default);
* :mod:`mapi_sdk.testing.toy` holds small reference steps and models;
* :mod:`mapi_sdk.testing.strategies` holds Hypothesis strategies (needs ``hypothesis``);
* the pytest plugin (:mod:`mapi_sdk.testing.plugin`, auto-loaded) provides fixtures.
"""

from mapi_sdk.testing import toy
from mapi_sdk.testing.checks import (
    CheckResult,
    ContractReport,
    ContractViolation,
    ModelFixture,
    check_model,
    check_step,
)
from mapi_sdk.testing.toy import panel_dataset, toy_registry

__all__ = [
    "CheckResult",
    "ContractReport",
    "ContractViolation",
    "ModelFixture",
    "check_model",
    "check_step",
    "panel_dataset",
    "toy",
    "toy_registry",
]
