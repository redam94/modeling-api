"""Fixtures for the runner tests: the signed-URL GCS fake (``fakegcs``)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakegcs import FakeGcs  # noqa: E402


@pytest.fixture
def gcs() -> Iterator[FakeGcs]:
    fake = FakeGcs().start()
    try:
        yield fake
    finally:
        fake.stop()
