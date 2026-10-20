"""Shared fixtures of the causal plugin tests.

Before the workspace sync installs ``mapi-plugin-causal`` the package is imported from its
source tree (the entry point and the registry tests then skip).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

if importlib.util.find_spec("mapi_plugin_causal") is None:  # pragma: no cover - before uv sync
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
import xarray as xr
from mapi_plugin_causal.datasets import ImpactTruth, impact_dataset


@pytest.fixture(scope="session")
def impact_data() -> tuple[xr.Dataset, ImpactTruth]:
    return impact_dataset(weeks=60, start_week=44, lift=0.1, seed=0)
