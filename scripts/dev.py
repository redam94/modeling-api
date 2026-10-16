#!/usr/bin/env python3
"""Run the whole modeling-api stack locally, without Docker.

``make dev`` (``uv run python scripts/dev.py up``) starts, in dependency order:

==========  ===============================================  =================================
service     command                                          ready when
==========  ===============================================  =================================
postgres    embedded PostgreSQL 16 (``pgserver``) in         ``pg_ctl`` reports it running
            ``.local/pg`` (skipped when ``DATABASE_URL``
            is set)
gcs-shim    ``mapi-gcs-shim serve --host H --port 4443       TCP port accepts connections
            --root .local/gcs`` (CORS for the web origin)
migrate     ``mapi-server migrate`` (one-shot)               exits 0
api         ``mapi-server api --host H --port 8000``         ``GET /healthz`` returns 2xx
worker      ``mapi-server worker``                           started
web         ``pnpm --filter @mapi/web dev --host H --port    TCP port accepts connections
            5173 --strictPort``
plots       ``pnpm --filter @mapi/plot-sandbox dev ...       TCP port accepts connections
            --port 5174 --strictPort``
==========  ===============================================  =================================

The servers run in the ``dev`` profile unless ``MAPI_STACK_PROFILE`` says otherwise
(``single-tenant`` or ``multi-tenant``): multi-tenant with the built-in development identity
provider at ``/dev/idp``, the ``default`` tenant and project, and the seeded accounts of
``MAPI_DEVIDP_USERS`` (``admin@dev.local`` is the tenant admin and a platform operator). The
web app signs in through it; scripts use :func:`mapi_client.oidc.dev_idp_login`. The variable
is not ``MAPI_PROFILE`` because the CLI reads that one as its config-file profile. Outside the
single-tenant profile ``POST /internal/events`` needs a bearer: a random
``MAPI_INTERNAL_TOKEN`` is kept in ``$MAPI_DATA_DIR/dev/internal-token`` (mode 0600).

Addresses come from :class:`mapi_spec.localdev.LocalDev` (``MAPI_API_URL``,
``MAPI_GCS_ENDPOINT``, ``MAPI_WEB_ORIGIN``, ``MAPI_PLOTS_ORIGIN``, ``MAPI_DATA_DIR``,
``MAPI_ENV``, ``DATABASE_URL``), and every service gets the same resolved values in its
environment. Output is prefixed with the service name (coloured on a terminal). Ctrl-C, or
``make stop`` from another shell, stops the services in reverse order: SIGTERM to each
process group, then SIGKILL after a grace period. If a service dies unexpectedly, the whole
stack is stopped and the exit status is non-zero. Nothing is restarted behind your back.

Any service's command can be replaced with ``MAPI_DEV_<NAME>_CMD`` (shell-quoted), e.g.
``MAPI_DEV_API_CMD="uvicorn mapi_server.app:app --reload --port 8000"``.

Sub-commands: ``up``, ``stop``, ``status``, ``env``, ``migrate``, ``db-shell``, ``clean``,
``outbox`` (the dev mailer's newest e-mails, e.g. sign-up verification links).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import importlib.util
import json
import os
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, TextIO

ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = "dev"
EXECUTOR_ENV = "MAPI_EXECUTOR"
STACK_PROFILE_ENV = "MAPI_STACK_PROFILE"
STACK_PROFILES = ("dev", "single-tenant", "multi-tenant")
DEFAULT_STACK_PROFILE = "dev"
"""Executor backend for ``mapi-server worker`` (``local-process`` in dev and compose)."""

_COLORS = ("36", "32", "33", "35", "34", "96", "92", "93", "95", "94")
_RESET = "\033[0m"


# ================================================================================ configuration


@dataclass(frozen=True, slots=True)
class Endpoint:
    """``host:port`` parsed from one of the LocalDev URLs."""

    host: str
    port: int

    @classmethod
    def from_url(cls, url: str) -> Endpoint:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"expected an http(s) URL with a host, got {url!r}")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return cls(parts.hostname, port)


@dataclass(frozen=True, slots=True)
class DevConfig:
    """Everything ``dev.py`` derives from the environment (see ``mapi_spec.localdev``)."""

    env: str
    database_url: str | None
    gcs_endpoint: str
    api_url: str
    web_origin: str
    plots_origin: str
    data_dir: Path
    profile: str = DEFAULT_STACK_PROFILE
    """The servers' ``MAPI_PROFILE`` (from ``MAPI_STACK_PROFILE``)."""

    @classmethod
    def from_environ(cls, environ: Mapping[str, str], root: Path = ROOT) -> DevConfig:
        from mapi_spec.localdev import LocalDev  # the documented contract for ports and names

        local = LocalDev.from_environ(environ)
        data_dir = Path(local.data_dir)
        if not data_dir.is_absolute():
            data_dir = root / data_dir
        profile = environ.get(STACK_PROFILE_ENV, "").strip() or DEFAULT_STACK_PROFILE
        if profile not in STACK_PROFILES:
            raise ValueError(
                f"{STACK_PROFILE_ENV}={profile!r}: expected one of {', '.join(STACK_PROFILES)}"
            )
        return cls(
            env=local.env,
            database_url=local.database_url,
            gcs_endpoint=local.gcs_endpoint,
            api_url=local.api_url,
            web_origin=local.web_origin,
            plots_origin=local.plots_origin,
            data_dir=data_dir,
            profile=profile,
        )

    @property
    def pg_dir(self) -> Path:
        return self.data_dir / "pg"

    @property
    def gcs_dir(self) -> Path:
        return self.data_dir / "gcs"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "dev" / "supervisor.json"

    @property
    def internal_token_file(self) -> Path:
        return self.data_dir / "dev" / "internal-token"

    @property
    def outbox_dir(self) -> Path:
        """Where the dev profile's outbox mailer writes e-mail (sign-up verification links);
        ``GET /dev/outbox`` and ``dev.py outbox`` read it."""
        return self.data_dir / "outbox"

    def internal_token(self) -> str:
        """The stack's ``MAPI_INTERNAL_TOKEN`` (created once, mode 0600)."""
        path = self.internal_token_file
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        return token

    def service_env(self, database_url: str | None) -> dict[str, str]:
        """The variables every service receives (on top of the caller's environment)."""
        from mapi_spec.localdev import (
            ENV_API_URL,
            ENV_DATA_DIR,
            ENV_DATABASE_URL,
            ENV_ENV,
            ENV_GCS_ENDPOINT,
            ENV_PLOTS_ORIGIN,
            ENV_WEB_ORIGIN,
        )

        env = {
            ENV_ENV: self.env,
            ENV_GCS_ENDPOINT: self.gcs_endpoint,
            ENV_API_URL: self.api_url,
            ENV_WEB_ORIGIN: self.web_origin,
            ENV_PLOTS_ORIGIN: self.plots_origin,
            ENV_DATA_DIR: str(self.data_dir),
            "PYTHONUNBUFFERED": "1",
            # overrides a MAPI_PROFILE the caller may have exported for the CLI
            "MAPI_PROFILE": self.profile,
        }
        if self.profile != "single-tenant":
            env["MAPI_INTERNAL_TOKEN"] = self.internal_token()
        if self.profile == "dev":
            # the dev profile mails through the outbox (read at GET /dev/outbox)
            env["MAPI_OUTBOX_DIR"] = str(self.outbox_dir)
        if database_url:
            env[ENV_DATABASE_URL] = database_url
        return env


# ===================================================================================== services


@dataclass(frozen=True, slots=True)
class HttpProbe:
    url: str

    def check(self, timeout: float = 1.0) -> bool:
        try:
            with urllib.request.urlopen(self.url, timeout=timeout) as resp:
                return 200 <= int(resp.status) < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def describe(self) -> str:
        return f"GET {self.url}"


@dataclass(frozen=True, slots=True)
class TcpProbe:
    host: str
    port: int

    def check(self, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True
        except OSError:
            return False

    def describe(self) -> str:
        return f"tcp {self.host}:{self.port}"


Probe = HttpProbe | TcpProbe


@dataclass(frozen=True, slots=True)
class Requirement:
    """Something that must exist before a service can start (checked up front)."""

    kind: Literal["module", "exe", "file"]
    target: str
    hint: str

    def problem(self, root: Path, path: str | None = None) -> str | None:
        if self.kind == "module":
            try:
                found = importlib.util.find_spec(self.target) is not None
            except (ImportError, ValueError):
                found = False
            ok = found
        elif self.kind == "exe":
            ok = shutil.which(self.target, path=path) is not None
        else:
            ok = (root / self.target).exists()
        return None if ok else f"{self.kind} {self.target!r} not found - {self.hint}"


@dataclass(frozen=True, slots=True)
class Service:
    name: str
    argv: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    ready: Probe | None = None
    oneshot: bool = False
    env: Mapping[str, str] = field(default_factory=dict)
    requires: tuple[Requirement, ...] = ()
    ready_timeout: float = 60.0
    port: Endpoint | None = None
    """The address this service binds (checked to be free before starting)."""


def _console_script(name: str) -> str:
    """A console script of this virtualenv, or the bare name (resolved through PATH)."""
    candidate = Path(sys.executable).parent / name
    return str(candidate) if candidate.exists() else name


def build_services(cfg: DevConfig, environ: Mapping[str, str]) -> list[Service]:
    """The full service table for ``cfg`` (before ``--only``/``--without`` selection)."""
    shim, api = Endpoint.from_url(cfg.gcs_endpoint), Endpoint.from_url(cfg.api_url)
    web, plots = Endpoint.from_url(cfg.web_origin), Endpoint.from_url(cfg.plots_origin)
    server = _console_script("mapi-server")
    server_req = Requirement(
        "module", "mapi_server.__main__", "run `uv sync --all-packages` (packages/server)"
    )
    shim_req = Requirement(
        "module", "mapi_gcs_shim.__main__", "run `uv sync --all-packages` (packages/gcs-shim)"
    )
    runner_req = Requirement(
        "module", "mapi_runner.__main__", "run `uv sync --all-packages` (packages/runner)"
    )
    pnpm_req = Requirement("exe", "pnpm", "install pnpm (corepack enable)")
    js_req = Requirement("file", "node_modules/.modules.yaml", "run `pnpm install` first")
    services = [
        Service(
            "gcs-shim",
            (
                _console_script("mapi-gcs-shim"),
                "serve",
                "--host",
                shim.host,
                "--port",
                str(shim.port),
                "--cors-origin",
                cfg.web_origin,
                "--root",
                str(cfg.gcs_dir),
            ),
            ready=TcpProbe(shim.host, shim.port),
            requires=(shim_req,),
            port=shim,
        ),
        Service("migrate", (server, "migrate"), oneshot=True, requires=(server_req,)),
        Service(
            "api",
            (server, "api", "--host", api.host, "--port", str(api.port)),
            depends_on=("migrate", "gcs-shim"),
            ready=HttpProbe(cfg.api_url.rstrip("/") + "/healthz"),
            requires=(server_req,),
            port=api,
        ),
        Service(
            "worker",
            (server, "worker"),
            depends_on=("migrate", "gcs-shim"),
            env={EXECUTOR_ENV: environ.get(EXECUTOR_ENV, "local-process")},
            requires=(server_req, runner_req),
        ),
        Service(
            "web",
            (
                "pnpm",
                "--filter",
                "@mapi/web",
                "dev",
                "--host",
                web.host,
                "--port",
                str(web.port),
                "--strictPort",
            ),
            depends_on=("api",),
            env={
                "VITE_API_BASE": cfg.api_url.rstrip("/"),
                "VITE_PLOTS_ORIGIN": cfg.plots_origin.rstrip("/"),
                "VITE_STORAGE_ORIGINS": cfg.gcs_endpoint.rstrip("/"),
            },
            ready=TcpProbe(web.host, web.port),
            requires=(
                pnpm_req,
                js_req,
                Requirement("file", "apps/web/package.json", "apps/web is not built yet"),
            ),
            ready_timeout=120.0,
            port=web,
        ),
        Service(
            "plots",
            (
                "pnpm",
                "--filter",
                "@mapi/plot-sandbox",
                "dev",
                "--host",
                plots.host,
                "--port",
                str(plots.port),
                "--strictPort",
            ),
            # the embed page (/embed/<token>) fetches the public view from the API
            env={
                "VITE_APP_ORIGINS": cfg.web_origin.rstrip("/"),
                "VITE_API_URL": cfg.api_url.rstrip("/"),
            },
            ready=TcpProbe(plots.host, plots.port),
            requires=(
                pnpm_req,
                js_req,
                Requirement(
                    "file", "apps/plot-sandbox/package.json", "apps/plot-sandbox is not built yet"
                ),
            ),
            ready_timeout=120.0,
            port=plots,
        ),
    ]
    return [apply_override(s, environ) for s in services]


def override_var(name: str) -> str:
    return f"MAPI_DEV_{name.upper().replace('-', '_')}_CMD"


def apply_override(service: Service, environ: Mapping[str, str]) -> Service:
    """Replace the command with ``MAPI_DEV_<NAME>_CMD`` when that variable is set."""
    raw = environ.get(override_var(service.name), "").strip()
    if not raw:
        return service
    argv = tuple(shlex.split(raw))
    reqs = tuple(r for r in service.requires if r.kind == "file")
    reqs += (Requirement("exe", argv[0], f"from {override_var(service.name)}"),)
    return dataclasses.replace(service, argv=argv, requires=reqs)


class SelectionError(ValueError):
    pass


def select(
    services: Sequence[Service], only: Iterable[str] = (), without: Iterable[str] = ()
) -> list[Service]:
    """``--only`` (plus everything those depend on) minus ``--without``, in dependency order."""
    by_name = {s.name: s for s in services}
    only, without = list(only), set(without)
    for name in [*only, *without]:
        if name not in by_name:
            raise SelectionError(f"unknown service {name!r} (known: {', '.join(by_name)})")
    both = sorted(set(only) & without)
    if both:
        raise SelectionError(f"{', '.join(both)} is both selected and excluded")
    wanted: set[str] = set()
    stack = list(only) if only else list(by_name)
    while stack:
        name = stack.pop()
        if name not in wanted:
            wanted.add(name)
            stack.extend(by_name[name].depends_on)
    chosen = [s for s in services if s.name in wanted and s.name not in without]
    for s in chosen:
        excluded = [d for d in s.depends_on if d in without]
        if excluded:
            raise SelectionError(f"{s.name} depends on {', '.join(excluded)}; exclude {s.name} too")
    return topo_order(chosen)


def topo_order(services: Sequence[Service]) -> list[Service]:
    """Stable topological order (input order among independent services); rejects cycles."""
    by_name = {s.name: s for s in services}
    for s in services:
        for dep in s.depends_on:
            if dep not in by_name:
                raise SelectionError(f"{s.name} depends on {dep!r}, which is not selected")
    order: list[Service] = []
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(s: Service, path: tuple[str, ...]) -> None:
        mark = state.get(s.name)
        if mark == 2:
            return
        if mark == 1:
            raise SelectionError(f"dependency cycle: {' -> '.join((*path, s.name))}")
        state[s.name] = 1
        for dep in s.depends_on:
            visit(by_name[dep], (*path, s.name))
        state[s.name] = 2
        order.append(s)

    for s in services:
        visit(s, ())
    return order


def preflight(services: Sequence[Service], root: Path = ROOT, path: str | None = None) -> list[str]:
    """Every missing requirement and busy port, as messages naming the service."""
    problems: list[str] = []
    for s in services:
        problems += [f"{s.name}: {p}" for r in s.requires if (p := r.problem(root, path))]
        if s.port and port_in_use(s.port.host, s.port.port):
            problems.append(
                f"{s.name}: {s.port.host}:{s.port.port} is already in use "
                "(another `make dev`? `make stop`, or change the matching MAPI_* URL)"
            )
    return problems


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


# ======================================================================================= output


def use_color(stream: TextIO, environ: Mapping[str, str]) -> bool:
    if environ.get("NO_COLOR"):
        return False
    if environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)()) and environ.get("TERM") != "dumb"


def format_line(name: str, width: int, color: str | None, line: str) -> str:
    label = name.ljust(width)
    if color:
        return f"\033[{color}m{label}{_RESET} | {line}"
    return f"{label} | {line}"


class Output:
    """Line-oriented, prefixed writer shared by every service."""

    def __init__(self, stream: TextIO, names: Sequence[str], color: bool) -> None:
        self.stream = stream
        self.width = max((len(n) for n in [SUPERVISOR, *names]), default=len(SUPERVISOR))
        self.colors = {n: _COLORS[i % len(_COLORS)] for i, n in enumerate(names)} if color else {}
        self.bold = "1" if color else None

    def line(self, name: str, text: str) -> None:
        color = self.bold if name == SUPERVISOR else self.colors.get(name)
        self.stream.write(format_line(name, self.width, color, text) + "\n")
        self.stream.flush()

    def say(self, text: str) -> None:
        self.line(SUPERVISOR, text)


# =================================================================================== supervisor


class ServiceFailed(RuntimeError):
    pass


@dataclass
class Running:
    service: Service
    proc: asyncio.subprocess.Process
    pump: asyncio.Task[None]


class Supervisor:
    """Starts services in order, waits for readiness, streams output, stops them cleanly."""

    def __init__(
        self,
        services: Sequence[Service],
        base_env: Mapping[str, str],
        output: Output,
        *,
        cwd: Path = ROOT,
        grace: float = 10.0,
        poll: float = 0.2,
        on_started: Callable[[dict[str, int]], None] | None = None,
    ) -> None:
        self.services = list(services)
        self.base_env = dict(base_env)
        self.out = output
        self.cwd = cwd
        self.grace = grace
        self.poll = poll
        self.on_started = on_started
        self.running: dict[str, Running] = {}
        self.stop_requested = asyncio.Event()
        self.force = False

    # -- control
    def request_stop(self) -> None:
        if self.stop_requested.is_set():
            self.force = True
            self.out.say("second interrupt: killing everything now")
        self.stop_requested.set()

    def pids(self) -> dict[str, int]:
        return {n: r.proc.pid for n, r in self.running.items() if r.proc.returncode is None}

    # -- lifecycle
    async def run(self) -> int:
        try:
            for svc in self.services:
                if self.stop_requested.is_set():
                    return 0
                await self._start(svc)
            if any(not s.oneshot for s in self.services):
                self.out.say(
                    f"all {len(self.services)} services up - Ctrl-C or `make stop` stops them"
                )
            return await self._watch()
        except ServiceFailed as exc:
            self.out.say(f"error: {exc}")
            return 1
        finally:
            await self.shutdown()

    async def _start(self, svc: Service) -> None:
        env = {**self.base_env, **svc.env}
        self.out.say(f"starting {svc.name}: {shlex.join(svc.argv)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *svc.argv,
                cwd=self.cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                limit=1 << 20,
            )
        except OSError as exc:
            raise ServiceFailed(f"{svc.name} could not start: {exc}") from exc
        assert proc.stdout is not None
        pump = asyncio.create_task(self._pump(svc.name, proc.stdout))
        self.running[svc.name] = Running(svc, proc, pump)
        if self.on_started:
            self.on_started(self.pids())
        if svc.oneshot:
            rc = await self._wait_or_stop(proc)
            if rc is None:
                return
            await pump
            if rc != 0:
                raise ServiceFailed(f"{svc.name} exited with status {rc}")
            self.out.say(f"{svc.name} finished")
            return
        if svc.ready is not None:
            await self._wait_ready(svc, proc)
        self.out.say(f"{svc.name} is up" + (f" ({svc.ready.describe()})" if svc.ready else ""))

    async def _wait_or_stop(self, proc: asyncio.subprocess.Process) -> int | None:
        wait = asyncio.create_task(proc.wait())
        stop = asyncio.create_task(self.stop_requested.wait())
        done, _ = await asyncio.wait({wait, stop}, return_when=asyncio.FIRST_COMPLETED)
        if wait in done:
            stop.cancel()
            return wait.result()
        wait.cancel()
        return None

    async def _wait_ready(self, svc: Service, proc: asyncio.subprocess.Process) -> None:
        assert svc.ready is not None
        deadline = time.monotonic() + svc.ready_timeout
        while True:
            if proc.returncode is not None:
                raise ServiceFailed(
                    f"{svc.name} exited with status {proc.returncode} before it was ready"
                )
            if self.stop_requested.is_set():
                return
            if await asyncio.to_thread(svc.ready.check):
                return
            if time.monotonic() > deadline:
                raise ServiceFailed(
                    f"{svc.name} not ready after {svc.ready_timeout:.0f}s ({svc.ready.describe()})"
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_requested.wait(), timeout=self.poll)

    async def _watch(self) -> int:
        """Block until a stop is requested (0) or a long-running service exits (1)."""
        waits = {
            asyncio.create_task(r.proc.wait()): name
            for name, r in self.running.items()
            if not r.service.oneshot
        }
        if not waits:  # only one-shot services were selected, and they all succeeded
            return 0
        stop = asyncio.create_task(self.stop_requested.wait())
        try:
            done, _ = await asyncio.wait({stop, *waits}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in [stop, *waits]:
                t.cancel()
        if stop in done:
            return 0
        for task in done:
            name = waits[task]
            rc = self.running[name].proc.returncode
            self.out.say(f"{name} exited unexpectedly with status {rc}; stopping the stack")
        return 1

    async def _pump(self, name: str, stream: asyncio.StreamReader) -> None:
        while True:
            try:
                raw = await stream.readline()
            except ValueError:  # a line longer than the limit: emit what we have
                raw = await stream.read(1 << 20)
            if not raw:
                return
            self.out.line(name, raw.decode("utf-8", "replace").rstrip("\r\n"))

    async def shutdown(self) -> None:
        """SIGTERM each live process group in reverse start order, SIGKILL after ``grace``."""
        for name in reversed(list(self.running)):
            run = self.running[name]
            if run.proc.returncode is None:
                self.out.say(f"stopping {name}")
                await self._terminate(run)
            with contextlib.suppress(asyncio.CancelledError):
                await run.pump

    async def _terminate(self, run: Running) -> None:
        proc = run.proc
        _signal_group(proc.pid, signal.SIGTERM)
        deadline = time.monotonic() + (0 if self.force else self.grace)
        while proc.returncode is None and time.monotonic() < deadline and not self.force:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=self.poll)
        if proc.returncode is None:
            self.out.say(f"{run.service.name} did not stop in {self.grace:.0f}s: SIGKILL")
            _signal_group(proc.pid, signal.SIGKILL)
            await proc.wait()
        else:
            # The leader exited; make sure no stragglers survive in its group.
            _signal_group(proc.pid, signal.SIGKILL)


def _signal_group(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


# ====================================================================================== postgres


class Postgres(Protocol):
    def start(self) -> str: ...
    def stop(self) -> None: ...
    def running(self) -> bool: ...


class EmbeddedPostgres:
    """PostgreSQL 16 from the ``pgserver`` wheel, data in ``<data_dir>/pg``, unix socket only."""

    DATABASE = "mapi"

    def __init__(self, pgdata: Path) -> None:
        self.pgdata = pgdata

    @staticmethod
    def bin_dir() -> Path:
        try:
            from pgserver._commands import POSTGRES_BIN_PATH
        except ImportError as exc:  # pragma: no cover - dev dependency missing
            raise RuntimeError("pgserver is not installed: run `uv sync --all-packages`") from exc
        return Path(POSTGRES_BIN_PATH)

    def start(self) -> str:
        import pgserver

        self.pgdata.parent.mkdir(parents=True, exist_ok=True)
        server = pgserver.get_server(self.pgdata, cleanup_mode=None)
        exists = server.psql(f"SELECT 1 FROM pg_database WHERE datname = '{self.DATABASE}';")
        if "(1 row)" not in exists:
            server.psql(f"CREATE DATABASE {self.DATABASE};")
        return str(server.get_uri(database=self.DATABASE))

    def running(self) -> bool:
        if not (self.pgdata / "PG_VERSION").exists():
            return False
        result = subprocess.run(
            [str(self.bin_dir() / "pg_ctl"), "status", "-D", str(self.pgdata)],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def stop(self) -> None:
        if self.running():
            pg_ctl = str(self.bin_dir() / "pg_ctl")
            subprocess.run(
                [pg_ctl, "stop", "-D", str(self.pgdata), "-m", "fast", "-w"],
                capture_output=True,
                check=True,
            )


def libpq_url(url: str) -> str:
    """A SQLAlchemy URL (``postgresql+psycopg://``) as a plain libpq URL for ``psql``."""
    scheme, sep, rest = url.partition("://")
    if not sep or not scheme.startswith(("postgresql", "postgres")):
        raise ValueError("DATABASE_URL must be a postgresql:// URL")
    return "postgresql://" + rest


# ======================================================================================== state


@dataclass
class State:
    pid: int
    started_at: float
    services: dict[str, int]
    database_url: str | None
    embedded_pg: bool

    @classmethod
    def read(cls, path: Path) -> State | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**data)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return None

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(self), indent=1), encoding="utf-8")
        os.replace(tmp, path)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_state(path: Path) -> State | None:
    state = State.read(path)
    return state if state is not None and pid_alive(state.pid) else None


# ===================================================================================== commands


def _postgres_for(cfg: DevConfig) -> EmbeddedPostgres | None:
    return None if cfg.database_url else EmbeddedPostgres(cfg.pg_dir)


def cmd_up(args: argparse.Namespace, cfg: DevConfig, environ: Mapping[str, str]) -> int:
    out_names = [s.name for s in build_services(cfg, environ)]
    out = Output(sys.stdout, ["postgres", *out_names], use_color(sys.stdout, environ))
    if (state := live_state(cfg.state_file)) is not None:
        out.say(f"already running (supervisor pid {state.pid}); `make stop` first")
        return 1
    try:
        services = select(build_services(cfg, environ), args.only, args.without)
    except SelectionError as exc:
        out.say(f"error: {exc}")
        return 2
    problems = preflight(services, ROOT, environ.get("PATH"))
    if problems:
        out.say("cannot start - fix these first (or skip services with --without NAME):")
        for p in problems:
            out.say(f"  - {p}")
        return 1
    pg = _postgres_for(cfg)
    database_url = cfg.database_url
    if pg is not None:
        out.line("postgres", f"starting embedded PostgreSQL in {cfg.pg_dir}")
        try:
            database_url = pg.start()
        except Exception as exc:
            out.line("postgres", f"failed to start: {exc}")
            return 1
        out.line("postgres", f"ready: {database_url}")
    else:
        out.line("postgres", "using DATABASE_URL (external server)")

    base_env = {**environ, **cfg.service_env(database_url)}
    state = State(os.getpid(), time.time(), {}, database_url, pg is not None)
    state.write(cfg.state_file)

    def on_started(pids: dict[str, int]) -> None:
        state.services = pids
        state.write(cfg.state_file)

    sup = Supervisor(services, base_env, out, grace=args.grace, on_started=on_started)

    async def main() -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, sup.request_stop)
        return await sup.run()

    try:
        rc = asyncio.run(main())
    finally:
        if pg is not None and not args.keep_db:
            out.line("postgres", "stopping")
            try:
                pg.stop()
            except subprocess.CalledProcessError as exc:
                out.line("postgres", f"pg_ctl stop failed: {exc.stderr.decode(errors='replace')}")
                rc = 1
        cfg.state_file.unlink(missing_ok=True)
    out.say("stopped" if rc == 0 else f"stopped with errors (exit {rc})")
    return rc


def cmd_stop(args: argparse.Namespace, cfg: DevConfig, environ: Mapping[str, str]) -> int:
    state = State.read(cfg.state_file)
    if state is None or not pid_alive(state.pid):
        print("dev stack is not running")
        cfg.state_file.unlink(missing_ok=True)
    else:
        print(f"stopping dev stack (supervisor pid {state.pid})")
        os.kill(state.pid, signal.SIGTERM)
        deadline = time.monotonic() + args.timeout
        while pid_alive(state.pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if pid_alive(state.pid):
            print(f"supervisor still alive after {args.timeout:.0f}s: killing its services")
            for pid in state.services.values():
                _signal_group(pid, signal.SIGKILL)
            os.kill(state.pid, signal.SIGKILL)
            cfg.state_file.unlink(missing_ok=True)
    pg = _postgres_for(cfg)
    if pg is not None and pg.running():
        print(f"stopping embedded PostgreSQL ({cfg.pg_dir})")
        pg.stop()
    return 0


def cmd_status(args: argparse.Namespace, cfg: DevConfig, environ: Mapping[str, str]) -> int:
    state = live_state(cfg.state_file)
    print(f"supervisor: {f'running, pid {state.pid}' if state else 'not running'}")
    pg = _postgres_for(cfg)
    if pg is None:
        print("postgres:   external (DATABASE_URL)")
    else:
        print(f"postgres:   {'running' if pg.running() else 'stopped'} ({cfg.pg_dir})")
    for svc in build_services(cfg, environ):
        if svc.ready is None:
            pid = state.services.get(svc.name) if state else None
            alive = pid is not None and pid_alive(pid)
            print(f"{svc.name + ':':<11} {'running' if alive else 'not running'}")
        else:
            up = svc.ready.check()
            print(f"{svc.name + ':':<11} {'up' if up else 'down'} ({svc.ready.describe()})")
    return 0


def _database_url(cfg: DevConfig, start: bool) -> tuple[str | None, EmbeddedPostgres | None]:
    """The URL to use and, when we had to start the embedded server, its handle."""
    if cfg.database_url:
        return cfg.database_url, None
    pg = EmbeddedPostgres(cfg.pg_dir)
    if pg.running():
        return pg.start(), None  # attaches to the running server
    if not start:
        return None, None
    return pg.start(), pg


def cmd_env(args: argparse.Namespace, cfg: DevConfig, environ: Mapping[str, str]) -> int:
    url, _ = _database_url(cfg, start=False)
    if url is None:
        print("# embedded PostgreSQL is not running: DATABASE_URL omitted (`make dev` first)")
    print(f"# servers run with MAPI_PROFILE={cfg.profile} (not exported: the CLI reads it as")
    print("# its config-file profile; set MAPI_STACK_PROFILE to change it)")
    for key, value in sorted(cfg.service_env(url).items()):
        if key == "MAPI_PROFILE":
            continue
        print(f"export {key}={shlex.quote(value)}")
    return 0


def cmd_migrate(args: argparse.Namespace, cfg: DevConfig, environ: Mapping[str, str]) -> int:
    service = next(s for s in build_services(cfg, environ) if s.name == "migrate")
    problems = preflight([service], ROOT, environ.get("PATH"))
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        return 1
    url, started = _database_url(cfg, start=True)
    try:
        env = {**environ, **cfg.service_env(url), **service.env}
        return subprocess.run(list(service.argv), env=env, cwd=ROOT, check=False).returncode
    finally:
        if started is not None:
            started.stop()


def cmd_db_shell(args: argparse.Namespace, cfg: DevConfig, environ: Mapping[str, str]) -> int:
    url, started = _database_url(cfg, start=True)
    assert url is not None
    psql = EmbeddedPostgres.bin_dir() / "psql"
    exe = str(psql) if psql.exists() else (shutil.which("psql") or "")
    if not exe:
        print("psql not found (install the dev dependencies or PostgreSQL)", file=sys.stderr)
        return 1
    try:
        return subprocess.run([exe, libpq_url(url)], check=False).returncode
    finally:
        if started is not None:
            started.stop()


def cmd_clean(args: argparse.Namespace, cfg: DevConfig, environ: Mapping[str, str]) -> int:
    target = cfg.data_dir.resolve()
    if live_state(cfg.state_file) is not None:
        print("the dev stack is running: `make stop` first", file=sys.stderr)
        return 1
    pg = EmbeddedPostgres(cfg.pg_dir)
    if pg.running():
        print("embedded PostgreSQL is running: `make stop` first", file=sys.stderr)
        return 1
    root = ROOT.resolve()
    if target == root or root not in target.parents:
        print(f"refusing to delete {target}: not inside {root}", file=sys.stderr)
        return 1
    if not target.exists():
        print(f"nothing to clean ({target} does not exist)")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("pass --yes to delete without a prompt", file=sys.stderr)
            return 1
        answer = input(f"delete {target} (database, objects, logs)? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    shutil.rmtree(target)
    print(f"deleted {target}")
    return 0


def cmd_outbox(args: argparse.Namespace, cfg: DevConfig, environ: Mapping[str, str]) -> int:
    """Print the newest e-mails of the dev outbox (verification links first)."""
    from mapi_server.services.mail import read_outbox

    directory = Path(environ.get("MAPI_OUTBOX_DIR") or cfg.outbox_dir)
    messages = read_outbox(directory, to=args.to, limit=args.limit)
    if not messages:
        print(f"no e-mail in {directory}" + (f" to {args.to}" if args.to else ""))
        return 0
    for msg in messages:
        tags = msg.get("tags") or {}
        print(f"{msg.get('sent_at', '?')}  to {msg.get('to', '?')}: {msg.get('subject', '')}")
        if isinstance(tags, dict) and tags.get("link"):
            print(f"    {tags['link']}")
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace, DevConfig, Mapping[str, str]], int]] = {
    "up": cmd_up,
    "stop": cmd_stop,
    "status": cmd_status,
    "env": cmd_env,
    "migrate": cmd_migrate,
    "db-shell": cmd_db_shell,
    "clean": cmd_clean,
    "outbox": cmd_outbox,
}


def _names(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dev.py", description="Run the modeling-api stack locally without Docker."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="start the stack in the foreground")
    up.add_argument("--only", type=_names, default=[], help="comma list (deps are added)")
    up.add_argument("--without", type=_names, default=[], help="comma list to skip")
    up.add_argument("--keep-db", action="store_true", help="leave PostgreSQL running on exit")
    up.add_argument("--grace", type=float, default=10.0, help="seconds before SIGKILL")
    stop = sub.add_parser("stop", help="stop a running stack (from another shell)")
    stop.add_argument("--timeout", type=float, default=30.0)
    sub.add_parser("status", help="show what is running")
    sub.add_parser("env", help="print the resolved environment as shell exports")
    sub.add_parser("migrate", help="run database migrations once")
    sub.add_parser("db-shell", help="open psql on the dev database")
    clean = sub.add_parser("clean", help="delete MAPI_DATA_DIR (database, objects, logs)")
    clean.add_argument("--yes", action="store_true")
    outbox = sub.add_parser("outbox", help="show the dev mailer's newest e-mails and links")
    outbox.add_argument("--to", default=None, help="only e-mail to this address")
    outbox.add_argument("--limit", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    try:
        cfg = DevConfig.from_environ(env)
    except ValueError as exc:
        print(f"invalid local configuration: {exc}", file=sys.stderr)
        return 2
    return COMMANDS[args.command](args, cfg, env)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DevConfig",
    "EmbeddedPostgres",
    "Endpoint",
    "HttpProbe",
    "Output",
    "Requirement",
    "SelectionError",
    "Service",
    "ServiceFailed",
    "State",
    "Supervisor",
    "TcpProbe",
    "apply_override",
    "build_services",
    "format_line",
    "libpq_url",
    "main",
    "preflight",
    "select",
    "topo_order",
    "use_color",
]
