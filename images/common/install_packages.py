#!/usr/bin/env python3
"""Install a set of workspace packages into one environment, exactly as ``uv.lock`` pins them.

Each image declares the workspace packages it ships (``MAPI_PACKAGES``). ``uv sync`` selects
one workspace package per call, so this runs one ``uv sync --frozen --inexact`` per package
into the same environment (``UV_PROJECT_ENVIRONMENT``); ``--inexact`` keeps what earlier calls
installed. Every version comes from the lock, so the union is consistent by construction.

    install_packages.py --deps-only mapi-runner "mapi-server[docker]" --no-install nvidia-nccl-cu13
    install_packages.py mapi-runner "mapi-server[docker]" --no-install nvidia-nccl-cu13

``--deps-only`` installs third-party dependencies only (``--no-install-workspace``), which is
the cacheable layer built from the manifests alone; the second call adds the workspace
packages themselves, non-editable. ``--dry-run`` prints the commands instead of running them.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

_SPEC_RE = re.compile(r"^(?P<name>[a-z0-9][a-z0-9._-]*)(?:\[(?P<extras>[a-z0-9._,-]+)\])?$")


@dataclass(frozen=True, slots=True)
class PackageSpec:
    name: str
    extras: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> PackageSpec:
        m = _SPEC_RE.match(text.strip().lower())
        if not m:
            raise ValueError(f"invalid package spec {text!r}; expected name or name[extra,...]")
        extras = tuple(e for e in (m["extras"] or "").split(",") if e)
        return cls(m["name"], extras)


def uv_commands(
    specs: Sequence[PackageSpec], *, deps_only: bool, no_install: Sequence[str] = ()
) -> list[list[str]]:
    if not specs:
        raise ValueError("no packages given")
    commands: list[list[str]] = []
    for spec in specs:
        cmd = ["uv", "sync", "--frozen", "--no-dev", "--inexact", "--package", spec.name]
        for extra in spec.extras:
            cmd += ["--extra", extra]
        for name in no_install:
            cmd += ["--no-install-package", name]
        cmd.append("--no-install-workspace" if deps_only else "--no-editable")
        commands.append(cmd)
    return commands


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("packages", nargs="+", help="name or name[extra,...]")
    parser.add_argument("--deps-only", action="store_true")
    parser.add_argument("--no-install", nargs="*", default=[], help="never install these")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    specs = [PackageSpec.parse(p) for p in args.packages]
    for cmd in uv_commands(specs, deps_only=args.deps_only, no_install=args.no_install):
        print("+ " + shlex.join(cmd), flush=True)
        if not args.dry_run:
            subprocess.run(cmd, check=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1) from None
