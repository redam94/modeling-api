"""Run the shim: blocking (CLI) or in a background thread (tests, dev scripts)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

from mapi_spec.localdev import DEFAULT_ENV, LOCALHOST
from mapi_spec.storage import Buckets

from mapi_gcs_shim.app import ShimApp
from mapi_gcs_shim.backend import FsBackend
from mapi_gcs_shim.config import (
    PRINCIPAL_CONTROL,
    ShimConfig,
    ShimCredentials,
    init_root,
    write_key_files,
)

if TYPE_CHECKING:
    import uvicorn

    from mapi_gcs_shim.client import ShimObjectStore

__all__ = ["ShimServer", "bind_socket", "build_app", "serve"]


def build_app(
    root: str | Path,
    *,
    env: str = DEFAULT_ENV,
    endpoint: str | None = None,
    sweep_interval: float | None = 300.0,
    clock: Callable[[], datetime] | None = None,
) -> ShimApp:
    """Initialise ``root`` (config, keys, buckets) and return the ASGI app.

    The caller owns ``app.backend`` and must ``close()`` it to release the root lock.
    """
    root = Path(root)
    config = init_root(root, env=env)
    write_key_files(root, config, endpoint=endpoint)
    backend = FsBackend(root)
    try:
        return ShimApp(backend, config, clock=clock, sweep_interval=sweep_interval)
    except BaseException:
        backend.close()
        raise


def bind_socket(host: str = LOCALHOST, port: int = 0) -> socket.socket:
    """A listening TCP socket (``port=0`` picks a free port without a probe race)."""
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def _endpoint(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def _uvicorn_config(app: ShimApp, log_level: str) -> uvicorn.Config:
    import uvicorn

    return uvicorn.Config(
        app,
        log_level=log_level,
        lifespan="on",
        access_log=False,
        timeout_graceful_shutdown=5,
        h11_max_incomplete_event_size=64 * 1024,
    )


def serve(
    root: str | Path,
    *,
    host: str = LOCALHOST,
    port: int = 4443,
    env: str = DEFAULT_ENV,
    sweep_interval: float | None = 300.0,
    log_level: str = "info",
    on_ready: Callable[[str], None] | None = None,
) -> None:
    """Serve until interrupted (SIGINT/SIGTERM)."""
    import uvicorn

    sock = bind_socket(host, port)
    endpoint = _endpoint(host, sock.getsockname()[1])
    app = build_app(root, env=env, endpoint=endpoint, sweep_interval=sweep_interval)
    try:
        if on_ready is not None:
            on_ready(endpoint)
        uvicorn.Server(_uvicorn_config(app, log_level)).run(sockets=[sock])
    finally:
        app.backend.close()
        sock.close()


class ShimServer:
    """The shim on a background thread::

    with ShimServer(tmp_path / "gcs") as shim:
        async with shim.store() as store:
            await store.put(shim.buckets.runs, "t/x/obj", b"...")
    """

    def __init__(
        self,
        root: str | Path,
        *,
        host: str = LOCALHOST,
        port: int = 0,
        env: str = DEFAULT_ENV,
        sweep_interval: float | None = None,
        clock: Callable[[], datetime] | None = None,
        log_level: str = "warning",
    ) -> None:
        self.root = Path(root)
        self.host = host
        self.port = port
        self.env = env
        self.sweep_interval = sweep_interval
        self.clock = clock
        self.log_level = log_level
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._app: ShimApp | None = None
        self._sock: socket.socket | None = None
        self._error: BaseException | None = None
        self.endpoint = ""

    # ---------------------------------------------------------------- lifecycle

    def start(self, timeout: float = 15.0) -> str:
        """Start serving; returns the endpoint (``http://127.0.0.1:<port>``)."""
        import uvicorn

        if self._thread is not None:
            raise RuntimeError("the shim server is already running")
        self._sock = bind_socket(self.host, self.port)
        self.port = self._sock.getsockname()[1]
        self.endpoint = _endpoint(self.host, self.port)
        self._app = build_app(
            self.root,
            env=self.env,
            endpoint=self.endpoint,
            sweep_interval=self.sweep_interval,
            clock=self.clock,
        )
        self._server = uvicorn.Server(_uvicorn_config(self._app, self.log_level))
        server, sock = self._server, self._sock

        def run() -> None:
            try:
                server.run(sockets=[sock])
            except BaseException as exc:  # pragma: no cover - surfaced by start()/stop()
                self._error = exc

        self._thread = threading.Thread(target=run, name=f"gcs-shim:{self.port}", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError(f"the GCS shim failed to start: {self._error!r}")
            time.sleep(0.01)
        return self.endpoint

    def stop(self, timeout: float = 15.0) -> None:
        """Stop serving and release the root lock (idempotent)."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():  # pragma: no cover - defensive
                if self._server is not None:
                    self._server.force_exit = True
                self._thread.join(timeout)
        if self._app is not None:
            self._app.backend.close()
        if self._sock is not None:
            self._sock.close()
        self._server = self._thread = self._app = self._sock = None

    def __enter__(self) -> ShimServer:
        self.start()
        return self

    def __exit__(
        self,
        et: type[BaseException] | None,
        ev: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # ---------------------------------------------------------------- conveniences

    @property
    def app(self) -> ShimApp:
        if self._app is None:
            raise RuntimeError("the shim server is not running")
        return self._app

    @property
    def config(self) -> ShimConfig:
        return self.app.config

    @property
    def buckets(self) -> Buckets:
        return Buckets(self.config.env)

    def credentials(self, principal: str = PRINCIPAL_CONTROL) -> ShimCredentials:
        """The principal's key (as written to ``{root}/_shim/keys/{principal}.json``)."""
        return ShimCredentials.from_principal(
            self.config.principal(principal), endpoint=self.endpoint
        )

    def token(self, principal: str = PRINCIPAL_CONTROL) -> str:
        return self.credentials(principal).token.get_secret_value()

    def auth_headers(self, principal: str = PRINCIPAL_CONTROL) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(principal)}"}

    def store(self, principal: str = PRINCIPAL_CONTROL, **kwargs: Any) -> ShimObjectStore:
        """A :class:`~mapi_gcs_shim.client.ShimObjectStore` acting as ``principal``.

        Use it as an async context manager (``async with shim.store() as store``).
        """
        from mapi_gcs_shim.client import ShimObjectStore

        return ShimObjectStore(self.endpoint, self.credentials(principal), **kwargs)
