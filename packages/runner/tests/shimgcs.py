"""The runner tests' object store: the real local GCS emulator (``mapi_gcs_shim``).

:class:`ShimGcs` runs a :class:`~mapi_gcs_shim.server.ShimServer` on a free port and offers a
small synchronous façade over :class:`~mapi_gcs_shim.client.ShimObjectStore` (the *control*
principal) for arranging inputs and inspecting outputs. The runner itself only ever sees
what a real execution sees: signed GET/PUT URLs, a V4 POST policy and the bootstrap object,
which it reads and deletes as the shim's *exec* principal.

Every HTTP request the shim serves is recorded in :attr:`ShimGcs.requests` (method, path,
status) so tests can assert on the wire behaviour (ranged reads, bootstrap GET-then-DELETE).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TypeVar

from mapi_gcs_shim import PRINCIPAL_EXEC, ShimObjectStore
from mapi_gcs_shim.server import ShimServer
from mapi_spec.storage import (
    Buckets,
    NotFound,
    ObjectMeta,
    PostPolicy,
    SignedGet,
    SignedPut,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["Recorded", "ShimGcs"]

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Recorded:
    """One request the shim answered."""

    method: str
    path: str
    status: int


class ShimGcs:
    """A real GCS shim plus synchronous helpers for tests (``env='test'`` buckets)."""

    def __init__(self, root: Path, *, env: str = "test") -> None:
        self.requests: list[Recorded] = []
        self._lock = threading.Lock()
        self.server = ShimServer(root, env=env, wrap=self._record)

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> ShimGcs:
        self.server.start()
        return self

    def stop(self) -> None:
        self.server.stop()

    def _record(self, app: ASGIApp) -> ASGIApp:
        async def recorder(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await app(scope, receive, send)
                return
            status = 0

            async def capture(message: Message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                await send(message)

            try:
                await app(scope, receive, capture)
            finally:
                with self._lock:
                    self.requests.append(Recorded(scope["method"], scope["path"], status))

        return recorder

    # ------------------------------------------------------------------ topology
    @property
    def endpoint(self) -> str:
        return self.server.endpoint

    @property
    def buckets(self) -> Buckets:
        return self.server.buckets

    @property
    def exec_token(self) -> str:
        return self.server.token(PRINCIPAL_EXEC)

    def bootstrap_template(self) -> str:
        return f"{self.endpoint}/storage/v1/b/{self.buckets.bootstrap}/o/{{nonce}}"

    # --------------------------------------------------------------------- store
    def _call(self, fn: Callable[[ShimObjectStore], Awaitable[T]]) -> T:
        async def go() -> T:
            async with self.server.store() as store:
                return await fn(store)

        return asyncio.run(go())

    def put(
        self, bucket: str, name: str, data: bytes, content_type: str | None = None
    ) -> ObjectMeta:
        return self._call(lambda s: s.put(bucket, name, data, content_type=content_type))

    def get(self, bucket: str, name: str) -> bytes:
        return self._call(lambda s: s.get(bucket, name))

    def stat(self, bucket: str, name: str) -> ObjectMeta:
        return self._call(lambda s: s.stat(bucket, name))

    def exists(self, bucket: str, name: str) -> bool:
        try:
            self.stat(bucket, name)
        except NotFound:
            return False
        return True

    def delete(self, bucket: str, name: str) -> None:
        self._call(lambda s: s.delete(bucket, name))

    def list(self, bucket: str, prefix: str = "") -> list[ObjectMeta]:
        async def every(store: ShimObjectStore) -> list[ObjectMeta]:
            out: list[ObjectMeta] = []
            token: str | None = None
            while True:
                page = await store.list(bucket, prefix=prefix or None, page_token=token)
                out.extend(page.items)
                token = page.next_page_token
                if not token:
                    return sorted(out, key=lambda m: m.name)

        return self._call(every)

    # ------------------------------------------------------------------- signing
    def sign_get(self, bucket: str, name: str, ttl: float = 3600) -> SignedGet:
        return self._call(lambda s: s.sign_get(bucket, name, expires_in=timedelta(seconds=ttl)))

    def sign_put(self, bucket: str, name: str, *, max_bytes: int = 1 << 20) -> SignedPut:
        return self._call(
            lambda s: s.sign_put(bucket, name, expires_in=timedelta(hours=1), max_bytes=max_bytes)
        )

    def post_policy(self, bucket: str, prefix: str, *, max_bytes: int = 1 << 30) -> PostPolicy:
        return self._call(
            lambda s: s.post_policy(
                bucket, prefix, expires_in=timedelta(hours=1), max_bytes=max_bytes
            )
        )
