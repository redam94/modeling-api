"""The Python client wired to the ASGI app and the in-memory store (tests only).

:func:`asgi_client` returns a :class:`mapi_client.AsyncClient` whose API requests go to the
real app through ``httpx.ASGITransport`` and whose signed storage links are served from the
context's in-memory object store by :func:`~.store_http.store_app`, so downloads are verified
end to end (size and CRC32C). ``sleep`` replaces the client's waits: a test can make the
platform progress while the client polls.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import httpx
import uvicorn
from mapi_client import AsyncClient, ClientConfig
from mapi_server.app import create_app
from mapi_server.context import ServerContext

from .builders import EXEC_TOKEN
from .store_http import store_app

__all__ = ["BASE", "SessionAuth", "asgi_client", "serve_api"]

BASE = "http://127.0.0.1:8000"

Sleep = Callable[[float], Awaitable[None]]


async def _nosleep(_: float) -> None:
    return None


def asgi_client(ctx: ServerContext, *, sleep: Sleep | None = None, **kw: Any) -> AsyncClient:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    storage = store_app(
        ctx.store,  # type: ignore[arg-type]
        exec_token=EXEC_TOKEN,
        bootstrap_bucket=ctx.buckets.bootstrap,
    )
    return AsyncClient(
        config=ClientConfig(base_url=BASE, max_retries=0),
        transport=httpx.ASGITransport(app=app),
        storage_transport=httpx.ASGITransport(app=storage),
        sleep=sleep or _nosleep,
        **kw,
    )


class SessionAuth(httpx.Auth):
    """A signed-in browser session (cookie, CSRF and project headers) as client auth."""

    def __init__(self, headers: dict[str, str], cookies: dict[str, str]) -> None:
        self.headers = headers
        self.cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())

    def auth_flow(self, request: httpx.Request) -> Any:
        request.headers.update(self.headers)
        request.headers["Cookie"] = self.cookie
        yield request


class _Quiet(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@contextlib.asynccontextmanager
async def serve_api(ctx: ServerContext) -> AsyncIterator[str]:
    """Serve the app over real HTTP on a free 127.0.0.1 port (for the sync client, the CLI
    and the docs snippets); yields the base URL."""
    port = _free_port()
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    server = _Quiet(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
