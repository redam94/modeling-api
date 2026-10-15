"""``mapi-server``: ``api`` | ``worker`` | ``migrate`` | ``dev``.

* ``api`` — the FastAPI app under uvicorn (127.0.0.1:8000 by default);
* ``worker`` — the orchestrator: procrastinate worker + dispatcher + reconciler + events;
* ``migrate`` — apply Alembic migrations (including the procrastinate schema);
* ``dev`` — migrate, then API and orchestrator in one process (local all-in-one).

Configuration comes from ``MAPI_*`` environment variables (see ``mapi_server.settings``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence

from mapi_server.settings import Settings

__all__ = ["main"]

log = logging.getLogger("mapi_server")


def _setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_migrate(settings: Settings) -> int:
    from mapi_server.db import resolve_database_url
    from mapi_server.db.migrate import upgrade

    url = resolve_database_url(settings)
    upgrade(url)
    log.info("database is at the latest migration")
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
    try:
        await orch.run()
    finally:
        await shutdown(ctx)


def cmd_worker(settings: Settings) -> int:
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


def cmd_dev(settings: Settings, *, host: str | None, port: int | None) -> int:
    cmd_migrate(settings)
    asyncio.run(_dev(settings, host or settings.api_host, port or settings.api_port))
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
    if args.command == "worker":
        return cmd_worker(settings)
    if args.command == "api":
        return cmd_api(settings, host=args.host, port=args.port)
    return cmd_dev(settings, host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
