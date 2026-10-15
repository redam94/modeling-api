"""Fixtures over the synthetic trees in :mod:`mapi_plots.examples`."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("xarray")


@pytest.fixture(scope="session")
def bundle() -> Any:
    from mapi_plots.examples import bundle_tree

    return bundle_tree()


@pytest.fixture(scope="session")
def scored() -> Any:
    from mapi_plots.examples import score_tree

    return score_tree()


@pytest.fixture(scope="session")
def cf_tree() -> Any:
    from mapi_plots.examples import counterfactual_tree

    return counterfactual_tree()
