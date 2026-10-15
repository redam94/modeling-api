#!/usr/bin/env python3
"""Pre-release gate (``make release-check``): is the tree consistent enough to tag?

Checks, each reported with every offending file:

1. **One version everywhere.** The root ``pyproject.toml`` version must equal every workspace
   member's ``[project].version``, every hard-coded ``__version__ = "..."`` in their ``src/``,
   every ``apps/*/package.json`` and root ``package.json`` version, and the ``MAPI_VERSION``
   default image tag in ``docker-compose.yml``.
2. **A changelog entry.** ``CHANGELOG.md`` has a dated ``## [X.Y.Z] ... YYYY-MM-DD`` heading for
   that version, with at least one line of content under it.
3. **No plan files tracked.** ``plans/``, ``docs/architecture-plan.md`` and ``*.plan.md`` are
   never committed (``git ls-files``).

Exit 0 when every check passes, 1 otherwise. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.-]+)?$")
_DUNDER_RE = re.compile(r'^\s*__version__\s*=\s*"(?P<v>[^"]+)"', re.M)
_COMPOSE_RE = re.compile(r"\$\{MAPI_VERSION:-(?P<v>[^}]+)\}")
PLAN_PATHSPECS = ("plans", "docs/architecture-plan.md", "*.plan.md", "**/*.plan.md")


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.errors.append(message)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def workspace_members(root: Path) -> list[Path]:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    members: list[Path] = []
    for pattern in data.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", []):
        members += sorted(p for p in root.glob(pattern) if (p / "pyproject.toml").exists())
    return members


def version_sources(root: Path) -> list[tuple[str, str]]:
    """``(location, version)`` for every place a release version is written down."""
    out: list[tuple[str, str]] = []
    root_meta = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    out.append(("pyproject.toml", str(root_meta["project"]["version"])))
    for member in workspace_members(root):
        meta = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))
        version = meta.get("project", {}).get("version")
        if version is None:
            out.append((_rel(member / "pyproject.toml", root), "<dynamic or missing>"))
        else:
            out.append((_rel(member / "pyproject.toml", root), str(version)))
        for init in sorted((member / "src").glob("*/__init__.py")):
            for m in _DUNDER_RE.finditer(init.read_text(encoding="utf-8")):
                out.append((_rel(init, root), m["v"]))
    for pkg in [root / "package.json", *sorted(root.glob("apps/*/package.json"))]:
        if pkg.exists():
            version = json.loads(pkg.read_text(encoding="utf-8")).get("version")
            if version is not None:
                out.append((_rel(pkg, root), str(version)))
    compose = root / "docker-compose.yml"
    if compose.exists():
        for m in _COMPOSE_RE.finditer(compose.read_text(encoding="utf-8")):
            out.append(("docker-compose.yml (MAPI_VERSION default)", m["v"]))
    return out


def check_versions(root: Path, report: Report) -> str:
    sources = version_sources(root)
    expected = sources[0][1]
    if not _VERSION_RE.match(expected):
        report.fail(f"pyproject.toml: version {expected!r} is not X.Y.Z")
    for where, version in sources[1:]:
        if version != expected:
            report.fail(f"{where}: version {version!r} != {expected!r} (pyproject.toml)")
    report.notes.append(f"version {expected} in {len(sources)} places")
    return expected


def check_changelog(root: Path, version: str, report: Report) -> None:
    path = root / "CHANGELOG.md"
    if not path.exists():
        report.fail("CHANGELOG.md is missing")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    heading = re.compile(rf"^## \[{re.escape(version)}\](?P<rest>.*)$")
    for i, line in enumerate(lines):
        m = heading.match(line)
        if not m:
            continue
        if not re.search(r"\b\d{4}-\d{2}-\d{2}\b", m["rest"]):
            report.fail(f"CHANGELOG.md:{i + 1}: the [{version}] heading has no YYYY-MM-DD date")
        body = []
        for nxt in lines[i + 1 :]:
            if nxt.startswith("## "):
                break
            if nxt.strip():
                body.append(nxt)
        if not body:
            report.fail(f"CHANGELOG.md:{i + 1}: the [{version}] section is empty")
        return
    report.fail(f"CHANGELOG.md has no '## [{version}] ... YYYY-MM-DD' section")


GitLsFiles = Callable[[Sequence[str]], list[str]]


def _git_ls_files(pathspecs: Sequence[str]) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--", *pathspecs],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def check_no_plans(report: Report, ls_files: GitLsFiles = _git_ls_files) -> None:
    tracked = ls_files(PLAN_PATHSPECS)
    for path in tracked:
        report.fail(f"{path}: plan files must never be committed (git rm --cached it)")


def run(
    root: Path = ROOT, *, ls_files: GitLsFiles = _git_ls_files, checks: Iterable[str] = ()
) -> Report:
    report = Report()
    wanted = set(checks) or {"versions", "changelog", "plans"}
    version = check_versions(root, report) if "versions" in wanted or "changelog" in wanted else ""
    if "changelog" in wanted:
        check_changelog(root, version, report)
    if "plans" in wanted:
        check_no_plans(report, ls_files)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pre-release consistency checks")
    parser.add_argument("--only", action="append", choices=["versions", "changelog", "plans"])
    args = parser.parse_args(argv)
    report = run(checks=args.only or ())
    for note in report.notes:
        print(f"ok: {note}")
    for error in report.errors:
        print(f"release-check: {error}", file=sys.stderr)
    if report.errors:
        print(f"release-check failed: {len(report.errors)} problem(s)", file=sys.stderr)
        return 1
    print("release-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
