#!/usr/bin/env python3
"""Build-time guard: fail the image build if the environment contains the wrong packages.

Run with the image's own interpreter after ``uv sync``::

    python -I assert_dists.py --absent numpy xarray pandas --present mapi-server

``--absent`` names must be neither installed as a distribution nor importable as a top-level
module (the module name is the distribution name with ``-`` replaced by ``_``, plus any extra
``--absent-module`` names). ``--present`` names must be installed distributions. This is the
runtime counterpart of ``scripts/lock_closure.py`` and of the import-linter contracts: those
prove the *lock* and the *code* are clean, this proves the *image* is.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import importlib.util
import re
import sys
from collections.abc import Callable, Iterable, Sequence


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_distributions() -> set[str]:
    return {_norm(d.metadata["Name"]) for d in md.distributions() if d.metadata["Name"]}


def violations(
    present: Iterable[str],
    absent: Iterable[str],
    absent_modules: Iterable[str],
    *,
    dists: set[str],
    find_module: Callable[[str], object | None],
) -> list[str]:
    out: list[str] = []
    for name in present:
        if _norm(name) not in dists:
            out.append(f"required distribution {name!r} is not installed")
    modules = set(absent_modules)
    for name in absent:
        if _norm(name) in dists:
            out.append(f"forbidden distribution {name!r} is installed")
        modules.add(_norm(name).replace("-", "_"))
    for module in sorted(modules):
        if find_module(module) is not None:
            out.append(f"forbidden module {module!r} is importable")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="assert which distributions an image contains")
    parser.add_argument("--present", nargs="*", default=[])
    parser.add_argument("--absent", nargs="*", default=[])
    parser.add_argument("--absent-module", nargs="*", default=[], dest="absent_modules")
    args = parser.parse_args(argv)
    problems = violations(
        args.present,
        args.absent,
        args.absent_modules,
        dists=installed_distributions(),
        find_module=importlib.util.find_spec,
    )
    for p in problems:
        print(f"image check failed: {p}", file=sys.stderr)
    if not problems:
        print(
            f"image check ok: {len(args.present)} required present, "
            f"{len(args.absent) + len(args.absent_modules)} forbidden absent"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
