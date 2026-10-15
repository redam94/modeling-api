"""Fixtures for the runner tests: the real local GCS emulator (``shimgcs.ShimGcs``)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from shimgcs import ShimGcs


@pytest.fixture
def gcs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ShimGcs]:
    shim = ShimGcs(tmp_path_factory.mktemp("gcs")).start()
    try:
        yield shim
    finally:
        shim.stop()
