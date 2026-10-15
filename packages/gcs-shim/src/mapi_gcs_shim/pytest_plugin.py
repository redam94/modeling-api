"""pytest plugin (``pytest11`` entry point): GCS shim fixtures for any workspace test.

* ``gcs_shim`` — a fresh shim on a free port with its own root, per test;
* ``gcs_shim_session`` — one shim shared by the whole session (state persists across tests).

Both yield a :class:`mapi_gcs_shim.server.ShimServer`::

    async def test_upload(gcs_shim):
        async with gcs_shim.store() as store:            # control principal
            meta = await store.put(gcs_shim.buckets.runs, "t/default/x", b"hi")

This module is imported for every pytest run in the workspace, so it imports nothing heavy at
module level.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from mapi_gcs_shim.server import ShimServer


@pytest.fixture
def gcs_shim(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ShimServer]:
    """A GCS shim on ``127.0.0.1:<free port>`` with a fresh root, stopped after the test."""
    from mapi_gcs_shim.server import ShimServer

    server = ShimServer(tmp_path_factory.mktemp("gcs-shim"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def gcs_shim_session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ShimServer]:
    """One GCS shim for the whole test session (state is shared between tests)."""
    from mapi_gcs_shim.server import ShimServer

    server = ShimServer(tmp_path_factory.mktemp("gcs-shim-session"))
    server.start()
    try:
        yield server
    finally:
        server.stop()
