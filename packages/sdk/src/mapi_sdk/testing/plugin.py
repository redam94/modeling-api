"""pytest plugin for plugin authors (auto-loaded through the ``pytest11`` entry point).

Fixtures:

``mapi_check_step`` / ``mapi_check_model``
    The contract-kit functions (:func:`mapi_sdk.testing.check_step` / ``check_model``).
``mapi_toy_registry``
    A registry with the toy reference steps and models.
``mapi_panel``
    ``(data, fx)``: a small daily markets × channels panel and its FX side table.
``mapi_check_tree_model`` / ``mapi_tree``
    The tree-input model checks (:func:`mapi_sdk.testing.check_tree_model`) and a small
    weekly tree dataset (:func:`mapi_sdk.testing.tree_dataset`, M4).

The ``mapi_contract`` marker tags contract-kit tests. Imports are lazy so that loading the
plugin costs nothing for test suites that do not use it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "mapi_contract: mapi contract-kit checks")


@pytest.fixture
def mapi_check_step() -> Callable[..., Any]:
    from mapi_sdk.testing.checks import check_step

    return check_step


@pytest.fixture
def mapi_check_model() -> Callable[..., Any]:
    from mapi_sdk.testing.checks import check_model

    return check_model


@pytest.fixture
def mapi_toy_registry() -> Any:
    from mapi_sdk.testing.toy import toy_registry

    return toy_registry()


@pytest.fixture
def mapi_panel() -> Any:
    from mapi_sdk.testing.toy import panel_dataset

    return panel_dataset(days=7 * 24)


@pytest.fixture
def mapi_check_tree_model() -> Callable[..., Any]:
    from mapi_sdk.testing.tree_checks import check_tree_model

    return check_tree_model


@pytest.fixture
def mapi_tree() -> Any:
    from mapi_sdk.testing.toy import tree_dataset

    return tree_dataset()
