"""Test models and data (re-exported from :mod:`mapi_workflows.testing`)."""

from __future__ import annotations

from mapi_sdk.registry import Registry
from mapi_workflows.testing import multi_series, test_registry, weekly_series

__all__ = ["multi_series", "registry", "weekly_series"]


def registry() -> Registry:
    return test_registry()
