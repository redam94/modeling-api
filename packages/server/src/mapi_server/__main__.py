"""``mapi-server``: ``api`` | ``worker`` | ``migrate`` | ``dev``.

* ``api`` — the FastAPI app under uvicorn (127.0.0.1:8000 by default);
* ``worker`` — the orchestrator: procrastinate worker + dispatcher + reconciler + events;
* ``migrate`` — apply Alembic migrations (including the procrastinate schema);
* ``dev`` — migrate, then API and orchestrator in one process (local all-in-one); starts an
  embedded GCS shim when ``MAPI_OBJECT_STORE=shim`` and nothing listens at
  ``MAPI_GCS_ENDPOINT`` (like the embedded Postgres when ``DATABASE_URL`` is unset).

Configuration comes from ``MAPI_*`` environment variables (see ``mapi_server.settings``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import socket
import sys
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from mapi_server.hardening import harden_process
from mapi_server.settings import ObjectStoreKind, Settings

if TYPE_CHECKING:
    from mapi_gcs_shim.server import ShimServer

__all__ = ["main", "start_embedded_shim"]

log = logging.getLogger("mapi_server")


def _setup_logging(settings: Settings) -> None:
    from mapi_server.observability import setup_observability

    setup_observability(settings.obs_settings())


def cmd_migrate(settings: Settings) -> int:
    """Prepare the durable state: database migrations, then the bundle-signing key.

    The key is created here, once, before ``api`` and ``worker`` start: both must load the
    *same* key (the worker signs bundle manifests, the API verifies them).
    """
    from mapi_server.db import resolve_database_url
    from mapi_server.db.migrate import upgrade
    from mapi_server.services.secrets import load_server_secrets
    from mapi_server.services.signing import LocalSigner
    from mapi_server.settings import SignerKind

    url = resolve_database_url(settings)
    upgrade(url, app_role=settings.db_app_role, system_role=settings.db_system_role)
    log.info("database is at the latest migration")
    load_server_secrets(settings, create=True)  # token pepper and audit key; shared by all
    log.info("server secret ready (%s)", "environment" if settings.server_secret else "file")
    if settings.signer is SignerKind.KMS:
        log.info("bundle manifests are signed by KMS key %s", settings.kms_key_version)
        return 0
    signer = LocalSigner.from_file(settings.key_path, create=True)
    log.info("bundle-signing key %s at %s", signer.key_id, settings.key_path)
    return 0


def _install_stop(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def _worker(settings: Settings) -> None:
    from mapi_server.app import build_context, shutdown, startup
    from mapi_server.executors import build_executor
    from mapi_server.orchestrator import Orchestrator

    ctx = build_context(settings)
    await startup(ctx, listen=False)
    orch = Orchestrator(ctx, build_executor(settings))
    _install_stop(orch.stop)
    metrics = _worker_metrics_server(settings)
    serving = asyncio.create_task(metrics.serve(), name="metrics") if metrics else None
    try:
        await orch.run()
    finally:
        if metrics is not None and serving is not None:
            metrics.should_exit = True
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await serving
        await shutdown(ctx)


def _worker_metrics_server(settings: Settings) -> Any:
    """A small HTTP server for the worker's ``GET /metrics`` (``MAPI_WORKER_METRICS_PORT``):
    the dispatcher, reconciler, finalizer and pipe event ingestion record their series in
    this process, not in the API's. Same bearer rule as the API's endpoint."""
    if not (settings.metrics_enabled and settings.worker_metrics_port):
        return None
    import uvicorn
    from fastapi import FastAPI

    from mapi_server.observability.middleware import add_metrics_endpoint
    from mapi_server.settings import Profile

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    add_metrics_endpoint(
        app,
        token=settings.metrics_token,
        require_token=settings.profile is Profile.MULTI_TENANT,
    )

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:  # the worker owns SIGINT/SIGTERM
            yield

    config = uvicorn.Config(
        app,
        host=settings.worker_metrics_host,
        port=settings.worker_metrics_port,
        log_level="warning",
        lifespan="off",
        access_log=False,
    )
    return _Server(config)


def cmd_worker(settings: Settings) -> int:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_worker(settings))
    return 0


def cmd_api(settings: Settings, *, host: str | None, port: int | None) -> int:
    import uvicorn

    from mapi_server.app import create_app

    uvicorn.run(
        create_app(settings),
        host=host or settings.api_host,
        port=port or settings.api_port,
        log_level=settings.log_level,
        proxy_headers=False,
    )
    return 0


async def _dev(settings: Settings, host: str, port: int) -> None:
    import uvicorn

    from mapi_server.app import build_context, create_app, shutdown, startup
    from mapi_server.executors import build_executor
    from mapi_server.orchestrator import Orchestrator

    ctx = build_context(settings)
    await startup(ctx)
    orch = Orchestrator(ctx, build_executor(settings))
    app = create_app(settings, ctx=ctx, manage_context=False)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level=settings.log_level, lifespan="on")
    )
    # uvicorn owns SIGINT/SIGTERM; when it stops, the orchestrator stops too
    serve = asyncio.create_task(server.serve(), name="uvicorn")
    work = asyncio.create_task(orch.run(), name="orchestrator")
    try:
        await asyncio.wait([serve, work], return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.should_exit = True
        orch.stop.set()
        for task in (serve, work):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        await shutdown(ctx)


_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def start_embedded_shim(settings: Settings) -> ShimServer | None:
    """Start the GCS shim in this process if the store is the shim and nothing listens at
    ``gcs_endpoint`` (loopback only). Returns the server to stop, or ``None``."""
    if settings.object_store is not ObjectStoreKind.SHIM:
        return None
    parts = urlsplit(settings.gcs_endpoint)
    host, port = parts.hostname or "127.0.0.1", parts.port or 80
    if _listening(host, port):
        return None
    if host not in _LOOPBACK:
        raise RuntimeError(
            f"nothing listens at MAPI_GCS_ENDPOINT={settings.gcs_endpoint} and it is not a "
            "loopback address, so no embedded shim is started; start the object store first"
        )
    from mapi_gcs_shim.server import ShimServer

    shim = ShimServer(
        settings.resolved_gcs_root(),
        host=host,
        port=port,
        env=settings.env,
        sweep_interval=300.0,
        cors_origins=[settings.web_origin],
    )
    shim.start()
    log.info("embedded GCS shim serving %s at %s", settings.resolved_gcs_root(), shim.endpoint)
    return shim


def cmd_dev(settings: Settings, *, host: str | None, port: int | None) -> int:
    cmd_migrate(settings)
    shim = start_embedded_shim(settings)
    try:
        with contextlib.suppress(KeyboardInterrupt):  # uvicorn re-raises the SIGINT it captured
            asyncio.run(_dev(settings, host or settings.api_host, port or settings.api_port))
    finally:
        if shim is not None:
            shim.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mapi-server", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_ in (
        ("api", "serve the HTTP API"),
        ("dev", "migrate, then API + orchestrator in one process"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--host", default=None)
        p.add_argument("--port", type=int, default=None)
    sub.add_parser("worker", help="dispatcher + reconciler + events + procrastinate worker")
    sub.add_parser("migrate", help="apply database migrations")
    args = parser.parse_args(argv)
    settings = Settings()
    _setup_logging(settings)
    if args.command == "migrate":
        return cmd_migrate(settings)
    # Long-running: under the local-process executor, runs share this uid. Non-dumpable,
    # they cannot ptrace this process or read its environment (DATABASE_URL) or memory.
    if not harden_process():
        log.debug("not marked non-dumpable (prctl is Linux-only)")
    if args.command == "worker":
        return cmd_worker(settings)
    if args.command == "api":
        return cmd_api(settings, host=args.host, port=args.port)
    return cmd_dev(settings, host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
