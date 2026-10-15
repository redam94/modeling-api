#!/usr/bin/env python3
"""Dependency closures from ``uv.lock`` — what an image built from a package set will contain.

Each container image installs a fixed set of workspace packages (``MAPI_PACKAGES`` in its
Dockerfile) with ``uv sync --frozen``. This module answers, without installing anything,
which distributions that pulls in, so tests can assert image invariants statically: the
control-plane image has no numpy, ``base-ml`` has no PyMC, and so on. The Dockerfiles repeat
the same assertions at build time against the real environment (``images/common/assert_dists.py``).

Usage::

    python scripts/lock_closure.py mapi-server[docker]
    python scripts/lock_closure.py mapi-runner mapi-steps mapi-sklearn --lacks pymc
    python scripts/lock_closure.py import-linter --requirements   # hash-pinned, for pip/uv
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
_REQ_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)(?:\[(?P<extras>[^\]]*)\])?$")

# Markers we evaluate for a Linux x86_64/aarch64 CPython 3.12 container. Anything we cannot
# decide is treated as *included* (conservative for "must not contain" assertions).
_FALSE_MARKERS = (
    "sys_platform == 'win32'",
    "sys_platform == 'darwin'",
    "platform_system == 'Windows'",
    "platform_system == 'Darwin'",
    "os_name == 'nt'",
    "python_full_version < '3.12'",
    "python_version < '3.12'",
    "implementation_name == 'pypy'",
    "platform_python_implementation == 'PyPy'",
)


def normalize(name: str) -> str:
    """PEP 503 normalised distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True, slots=True)
class Requirement:
    name: str
    extras: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, text: str) -> Requirement:
        m = _REQ_RE.match(text.strip())
        if not m:
            raise ValueError(f"not a requirement: {text!r} (use name or name[extra,...])")
        extras = frozenset(normalize(e) for e in (m["extras"] or "").split(",") if e.strip())
        return cls(normalize(m["name"]), extras)


@dataclass
class Lock:
    """The parts of ``uv.lock`` the closure needs."""

    deps: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    optional: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)
    workspace: set[str] = field(default_factory=set)
    versions: dict[str, str] = field(default_factory=dict)
    hashes: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = ROOT / "uv.lock") -> Lock:
        return cls.from_mapping(tomllib.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Lock:
        lock = cls()
        for pkg in data.get("package", []):
            name = normalize(pkg["name"])
            lock.deps.setdefault(name, []).extend(pkg.get("dependencies", []))
            for extra, reqs in pkg.get("optional-dependencies", {}).items():
                lock.optional.setdefault(name, {}).setdefault(normalize(extra), []).extend(reqs)
            source = pkg.get("source", {})
            if "editable" in source or "virtual" in source or "directory" in source:
                lock.workspace.add(name)
            if "version" in pkg:
                lock.versions[name] = str(pkg["version"])
            artifacts = [*pkg.get("wheels", []), *([pkg["sdist"]] if "sdist" in pkg else [])]
            lock.hashes[name] = sorted({a["hash"] for a in artifacts if "hash" in a})
        return lock

    def requirements(self, names: Iterable[str]) -> str:
        """Hash-pinned requirement lines (``--require-hashes`` compatible) for ``names``."""
        lines: list[str] = []
        for name in sorted(names):
            if name in self.workspace:
                raise ValueError(f"{name} is a workspace package; install it with uv sync")
            hashes = self.hashes.get(name) or []
            if name not in self.versions or not hashes:
                raise ValueError(f"{name} has no pinned version/hashes in uv.lock")
            pins = " ".join(f"--hash={h}" for h in hashes)
            lines.append(f"{name}=={self.versions[name]} {pins}")
        return "\n".join(lines) + "\n"


def _marker_applies(marker: str | None) -> bool:
    if not marker:
        return True
    clauses = [c.strip() for c in marker.split(" or ")]
    # An "or" applies if any clause may apply; a clause is false only if it is a pure,
    # known-false comparison (possibly inside an "and" chain).
    for clause in clauses:
        parts = [p.strip().strip("()") for p in clause.split(" and ")]
        if not any(p in _FALSE_MARKERS for p in parts):
            return True
    return False


def closure(lock: Lock, roots: Iterable[Requirement]) -> set[str]:
    """Every distribution name (normalised) installed for ``roots``, roots included."""
    seen: set[tuple[str, frozenset[str]]] = set()
    names: set[str] = set()
    stack = list(roots)
    while stack:
        req = stack.pop()
        key = (req.name, req.extras)
        if key in seen:
            continue
        seen.add(key)
        if req.name not in lock.deps and req.name not in lock.optional:
            raise KeyError(f"{req.name} is not in uv.lock")
        names.add(req.name)
        edges = list(lock.deps.get(req.name, []))
        for extra in req.extras:
            extra_reqs = lock.optional.get(req.name, {}).get(extra)
            if extra_reqs is None:
                raise KeyError(f"{req.name} has no extra {extra!r} in uv.lock")
            edges.extend(extra_reqs)
        for dep in edges:
            if not _marker_applies(dep.get("marker")):
                continue
            extras = frozenset(normalize(e) for e in dep.get("extra", []))
            stack.append(Requirement(normalize(dep["name"]), extras))
    return names


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the uv.lock closure of package roots.")
    parser.add_argument("roots", nargs="+", help="name or name[extra,...]")
    parser.add_argument("--has", action="append", default=[], help="exit 1 unless present")
    parser.add_argument("--lacks", action="append", default=[], help="exit 1 if present")
    parser.add_argument(
        "--requirements", action="store_true", help="print hash-pinned requirement lines"
    )
    args = parser.parse_args(argv)
    lock = Lock.load()
    names = closure(lock, [Requirement.parse(r) for r in args.roots])
    if args.requirements:
        sys.stdout.write(lock.requirements(names))
    else:
        for n in sorted(names):
            print(n)
    bad = [h for h in args.has if normalize(h) not in names]
    bad += [f"!{x}" for x in args.lacks if normalize(x) in names]
    if bad:
        print(f"closure check failed: {', '.join(bad)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
