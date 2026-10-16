"""The test shim records each request before its response reaches the client.

The recorder used to append in a ``finally`` after the wrapped app had sent the whole
response, so a client could get its answer (and a test could read
:attr:`ShimGcs.requests`) before the server thread wrote the record. That made
``test_ranged_download_verifies_hashes`` flaky under load.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from shimgcs import Recorded, ShimGcs
from starlette.types import ASGIApp, Message, Receive, Scope, Send

pytestmark = pytest.mark.unit

_SCOPE: Scope = {"type": "http", "method": "GET", "path": "/b/o"}


async def _receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


def _drive(gcs: ShimGcs, app: ASGIApp, seen: list[list[Recorded]]) -> None:
    async def send(message: Message) -> None:
        # What the client could observe the moment each message goes out.
        seen.append(list(gcs.requests))

    asyncio.run(gcs._record(app)(_SCOPE, _receive, send))


def test_request_is_recorded_before_the_response_is_sent(tmp_path: Path) -> None:
    gcs = ShimGcs(tmp_path)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 206, "headers": []})
        await send({"type": "http.response.body", "body": b"x", "more_body": False})

    seen: list[list[Recorded]] = []
    _drive(gcs, app, seen)
    assert seen[0] == [Recorded("GET", "/b/o", 206)]
    assert gcs.requests == [Recorded("GET", "/b/o", 206)]


def test_request_that_fails_before_responding_is_still_recorded(tmp_path: Path) -> None:
    gcs = ShimGcs(tmp_path)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _drive(gcs, app, [])
    assert gcs.requests == [Recorded("GET", "/b/o", 0)]
