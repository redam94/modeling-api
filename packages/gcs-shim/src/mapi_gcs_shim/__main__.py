"""``mapi-gcs-shim`` / ``python -m mapi_gcs_shim``: run or initialise the local GCS emulator.

    mapi-gcs-shim [serve] [--root .local/gcs] [--host 127.0.0.1] [--port 4443] [--env local]
    mapi-gcs-shim init    [--root .local/gcs] [--env local]
    mapi-gcs-shim key     [--root .local/gcs] PRINCIPAL [--field path|token|email]

Defaults come from the local-dev environment (``MAPI_DATA_DIR``, ``MAPI_ENV``,
``MAPI_GCS_ENDPOINT``), so a bare ``mapi-gcs-shim`` serves ``.local/gcs`` on port 4443.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from mapi_spec.localdev import GCS_SHIM_PORT, LOCALHOST, LocalDev

from mapi_gcs_shim.config import (
    ShimCredentials,
    default_root,
    init_root,
    key_file_path,
    write_key_files,
)

_COMMANDS = ("serve", "init", "key")


def _defaults() -> tuple[LocalDev, str, int]:
    local = LocalDev.from_environ(os.environ)
    parts = urlsplit(local.gcs_endpoint)
    return local, parts.hostname or LOCALHOST, parts.port or GCS_SHIM_PORT


def _parser() -> argparse.ArgumentParser:
    local, host, port = _defaults()
    p = argparse.ArgumentParser(prog="mapi-gcs-shim", description="Local GCS emulator")
    sub = p.add_subparsers(dest="command")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--root",
            type=Path,
            default=default_root(local.data_dir),
            help="state directory (default: $MAPI_DATA_DIR/gcs)",
        )
        sp.add_argument(
            "--env", default=local.env, help="environment for default bucket names (mapi-{env}-*)"
        )

    serve = sub.add_parser("serve", help="run the HTTP server (default command)")
    common(serve)
    serve.add_argument("--host", default=host)
    serve.add_argument("--port", type=int, default=port, help="0 picks a free port")
    serve.add_argument(
        "--sweep-interval",
        type=float,
        default=300.0,
        help="lifecycle sweep period in seconds (0 disables)",
    )
    serve.add_argument("--log-level", default="info")

    init = sub.add_parser("init", help="write the default config and key files, then exit")
    common(init)
    init.add_argument(
        "--endpoint", default=local.gcs_endpoint, help="endpoint recorded in the key files"
    )

    key = sub.add_parser("key", help="print a principal's key-file path, token or email")
    common(key)
    key.add_argument("principal")
    key.add_argument("--field", choices=("path", "token", "email"), default="path")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or args_list[0] not in (*_COMMANDS, "-h", "--help"):
        args_list.insert(0, "serve")
    args = _parser().parse_args(args_list)

    if args.command == "init":
        cfg = init_root(args.root, env=args.env)
        paths = write_key_files(args.root, cfg, endpoint=args.endpoint)
        for principal, path in sorted(paths.items()):
            print(f"{principal}: {path}")
        return 0

    if args.command == "key":
        path = key_file_path(args.root, args.principal)
        if not path.exists():
            print(f"no key file at {path}; run 'mapi-gcs-shim init' first", file=sys.stderr)
            return 1
        creds = ShimCredentials.load(path)
        if args.field == "token":
            print(creds.token.get_secret_value())
        elif args.field == "email":
            print(creds.client_email)
        else:
            print(path)
        return 0

    from mapi_gcs_shim.server import serve

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    def ready(endpoint: str) -> None:
        print(f"mapi-gcs-shim serving {args.root} at {endpoint}", flush=True)
        print(f"key files: {args.root / '_shim' / 'keys'}", flush=True)

    serve(
        args.root,
        host=args.host,
        port=args.port,
        env=args.env,
        sweep_interval=args.sweep_interval or None,
        log_level=args.log_level.lower(),
        on_ready=ready,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
