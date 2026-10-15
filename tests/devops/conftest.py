"""Helpers for the repository-level devops tests (scripts/, images/, compose, Makefile)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_script(relpath: str, name: str) -> ModuleType:
    """Import a standalone script (not a package module) by path, once per test session."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses need the module registered while executing
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def dev() -> ModuleType:
    return load_script("scripts/dev.py", "mapi_devtools_dev")
