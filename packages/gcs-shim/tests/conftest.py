"""Shared fixtures. The ``gcs_shim`` fixture itself comes from the package's pytest11 plugin."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        yield client
