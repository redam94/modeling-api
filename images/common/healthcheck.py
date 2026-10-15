#!/usr/bin/env python3
"""Container health probe (stdlib only, so it works in every Python image).

Used by ``HEALTHCHECK`` in the Dockerfiles and by ``healthcheck:`` in docker-compose::

    healthcheck.py http http://127.0.0.1:8000/healthz      # 2xx within the timeout
    healthcheck.py tcp 127.0.0.1:4443                      # a TCP connect succeeds
    healthcheck.py env-tcp DATABASE_URL MAPI_GCS_ENDPOINT  # TCP connect to each URL's host:port
    healthcheck.py import mapi_runner                      # the module imports cleanly

Exit 0 when healthy, 1 otherwise, with one line on stderr saying why. Never prints secrets:
URLs read from the environment are reduced to ``host:port`` before being reported.
"""

from __future__ import annotations

import argparse
import importlib
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence

_DEFAULT_PORTS = {"http": 80, "https": 443, "postgres": 5432, "postgresql": 5432}


class Unhealthy(Exception):
    """A probe failed; the message is safe to print."""


def host_port(url: str) -> tuple[str, int]:
    """``(host, port)`` of a URL such as ``postgresql+psycopg://u:p@db:5432/x``."""
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.split("+", 1)[0].lower()
    host = parsed.hostname
    if not host:
        # libpq-style "postgresql:///db?host=/socket/dir" has no TCP endpoint.
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("host", [""])[0].startswith("/"):
            raise Unhealthy(f"{scheme} URL points at a unix socket; nothing to probe over TCP")
        raise Unhealthy(f"{scheme} URL has no host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise Unhealthy(f"invalid port in {scheme} URL for host {host}") from exc
    if port is None:
        if scheme not in _DEFAULT_PORTS:
            raise Unhealthy(f"no port in {scheme} URL for host {host} and no default known")
        port = _DEFAULT_PORTS[scheme]
    return host, port


def probe_tcp(host: str, port: int, timeout: float) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except OSError as exc:
        raise Unhealthy(f"tcp {host}:{port} unreachable: {exc.strerror or exc}") from exc


def probe_http(url: str, timeout: float) -> None:
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise Unhealthy(f"refusing non-http(s) URL scheme {scheme!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "mapi-healthcheck"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # scheme checked above
            status = resp.status
    except urllib.error.HTTPError as exc:
        raise Unhealthy(f"GET {url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise Unhealthy(f"GET {url} failed: {getattr(exc, 'reason', exc)}") from exc
    if not 200 <= status < 300:
        raise Unhealthy(f"GET {url} returned HTTP {status}")


def probe_env_tcp(names: Sequence[str], environ: Mapping[str, str], timeout: float) -> None:
    for name in names:
        value = environ.get(name, "").strip()
        if not value:
            raise Unhealthy(f"{name} is not set")
        host, port = host_port(value)
        try:
            probe_tcp(host, port, timeout)
        except Unhealthy as exc:
            raise Unhealthy(f"{name}: {exc}") from exc


def probe_import(modules: Sequence[str]) -> None:
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as exc:  # any import-time failure means unhealthy
            raise Unhealthy(f"import {module} failed: {type(exc).__name__}: {exc}") from exc


def run(argv: Sequence[str], environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="healthcheck", description="container health probe")
    parser.add_argument("--timeout", type=float, default=3.0)
    sub = parser.add_subparsers(dest="kind", required=True)
    sub.add_parser("http").add_argument("url")
    sub.add_parser("tcp").add_argument("address", help="host:port")
    sub.add_parser("env-tcp").add_argument("names", nargs="+")
    sub.add_parser("import").add_argument("modules", nargs="+")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        if args.kind == "http":
            probe_http(args.url, args.timeout)
        elif args.kind == "tcp":
            host, _, port = args.address.rpartition(":")
            if not host or not port.isdigit():
                raise Unhealthy(f"expected host:port, got {args.address!r}")
            probe_tcp(host, int(port), args.timeout)
        elif args.kind == "env-tcp":
            probe_env_tcp(args.names, env, args.timeout)
        else:
            probe_import(args.modules)
    except Unhealthy as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
