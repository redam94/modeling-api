"""Shared fixtures: a registry with the statsmodels plugins and lazily fitted examples."""

from __future__ import annotations

import pytest
from mapi_plugin_statsmodels.examples import Example, examples
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import Registry, default_registry


@pytest.fixture(scope="session")
def registry() -> Registry:
    return default_registry(reload=True)


@pytest.fixture(scope="session")
def cases() -> dict[str, Example]:
    return examples()


class _Bundles(dict[str, Bundle]):
    def __init__(self, registry: Registry) -> None:
        super().__init__()
        self.registry = registry

    def __missing__(self, key: str) -> Bundle:
        case = examples()[key]
        bundle = fit_spec(case.spec, case.data, registry=self.registry, raw_schema=case.schema)
        self[key] = bundle
        return bundle


@pytest.fixture(scope="session")
def bundles(registry: Registry) -> dict[str, Bundle]:
    """Fitted bundles of every example (fitted on first use, once per session)."""
    return _Bundles(registry)
