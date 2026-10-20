#!/usr/bin/env python3
"""Generate ``THIRD-PARTY-NOTICES.md``: every third-party library the images ship, with its licence.

The list is computed from the lock files, never by hand:

* **Python**: the ``uv.lock`` closure (``scripts/lock_closure.py``) of every image's
  ``MAPI_PACKAGES`` / ``MAPI_WORKER_PACKAGES`` (minus ``MAPI_NO_INSTALL``), workspace
  packages excluded.
* **JavaScript**: the production closure in ``pnpm-lock.yaml`` of ``apps/web`` and
  ``apps/plot-sandbox`` (``dependencies`` only; development tooling is never bundled).

Licences come from the installed distributions (``License-Expression``, an SPDX-like
``License`` field, or one unambiguous classifier) and from ``pnpm licenses list --prod``;
``OVERRIDES`` settles the packages whose metadata is ambiguous. NOTICE files (Apache-2.0
section 4(d)) are carried verbatim, and so are the licence texts of the JavaScript packages,
because the web bundle keeps none of them. Entries for packages not installed on this
machine (a Linux-only wheel) are kept from the previous file.

Usage::

    uv sync --all-packages && pnpm install --frozen-lockfile
    python scripts/third_party_notices.py            # rewrite THIRD-PARTY-NOTICES.md
    python scripts/third_party_notices.py --check    # exit 1 if it is stale (no installs needed)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
NOTICES = ROOT / "THIRD-PARTY-NOTICES.md"
JS_APPS = ("apps/web", "apps/plot-sandbox")

#: Licences the metadata leaves ambiguous (a bare "BSD" classifier, a full licence text in
#: ``License``, several classifiers). Checked against each project's LICENSE file; re-check
#: when the lock bumps one of them.
OVERRIDES: dict[str, str] = {
    "altair": "BSD-3-Clause",
    "cycler": "BSD-3-Clause",
    "h5netcdf": "BSD-3-Clause",
    "jinja2": "BSD-3-Clause",
    "kiwisolver": "BSD-3-Clause",
    "matplotlib": "PSF-2.0 (Matplotlib licence)",
    "multipledispatch": "BSD-3-Clause",
    "numba": "BSD-2-Clause",
    "pandas": "BSD-3-Clause",
    "patsy": "BSD-2-Clause",
    "protobuf": "BSD-3-Clause",
    "pymc-extras": "Apache-2.0",
    "python-dateutil": "Apache-2.0 AND BSD-3-Clause",
    "scipy": "BSD-3-Clause",
    "skops": "MIT",
    "tzdata": "Apache-2.0",
    "uvloop": "MIT OR Apache-2.0",
}

_CLASSIFIERS = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
}
_SHORT_NAMES = {
    "MIT License": "MIT",
    "Apache License, Version 2.0": "Apache-2.0",
    "Apache License 2.0": "Apache-2.0",
}
_SPDXISH = re.compile(r"^[A-Za-z0-9.+-]+(?: (?:AND|OR|WITH) [A-Za-z0-9.+-]+)*$")
_NOTICE_FILE = re.compile(r"^NOTICE(?:\.(?:txt|md))?$", re.IGNORECASE)
_LICENSE_FILE = re.compile(r"^(?:LICEN[CS]E|COPYING)(?:[.-].*)?$", re.IGNORECASE)
_ROW = re.compile(r"^\| `(?P<name>[^`]+)` \| (?P<version>[^|]+?) \| (?P<licence>[^|]+?) \|$")
_LANG = {"py": "python", "js": "javascript"}  # text-block key prefix -> section
_TEXT = re.compile(
    r"^<!-- (?P<kind>notice|licence): (?P<keys>[^>]+?) -->\n~~~text\n(?P<body>.*?)\n~~~$",
    re.MULTILINE | re.DOTALL,
)


@dataclass
class Entry:
    name: str
    version: str
    licence: str = "UNKNOWN"
    notices: list[str] = field(default_factory=list)
    licence_texts: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"


def _lock_closure() -> ModuleType:
    name = "mapi_devtools_lock_closure"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "lock_closure.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ================================================================================== Python


def image_roots(root: Path = ROOT) -> dict[str, tuple[list[str], list[str]]]:
    """``{image: (packages, no_install)}`` from the ``ARG`` defaults of ``images/*/Dockerfile``."""
    out: dict[str, tuple[list[str], list[str]]] = {}
    arg = re.compile(r'^ARG (MAPI_PACKAGES|MAPI_WORKER_PACKAGES|MAPI_NO_INSTALL)="([^"]*)"', re.M)
    for dockerfile in sorted((root / "images").glob("*/Dockerfile")):
        packages: list[str] = []
        no_install: list[str] = []
        for m in arg.finditer(dockerfile.read_text(encoding="utf-8")):
            (no_install if m[1] == "MAPI_NO_INSTALL" else packages).extend(m[2].split())
        if packages:
            out[dockerfile.parent.name] = (packages, no_install)
    return out


def python_closure(root: Path = ROOT) -> dict[str, str]:
    """``{distribution: locked version}`` of every third-party Python package an image ships."""
    lc = _lock_closure()
    lock = lc.Lock.load(root / "uv.lock")
    names: set[str] = set()
    for packages, no_install in image_roots(root).values():
        found = lc.closure(lock, [lc.Requirement.parse(p) for p in packages])
        names |= found - {lc.normalize(n) for n in no_install}
    return {n: lock.versions[n] for n in sorted(names - lock.workspace)}


def _python_licence(name: str, meta: Any) -> str:
    if name in OVERRIDES:
        return OVERRIDES[name]
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return str(expression)
    short = (meta.get("License") or "").strip()
    short = _SHORT_NAMES.get(short, short)
    if short and _SPDXISH.match(short) and short != "BSD":
        return str(short)
    classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if len(classifiers) == 1 and classifiers[0] in _CLASSIFIERS:
        return _CLASSIFIERS[classifiers[0]]
    return "UNKNOWN"


def python_entries(previous: Mapping[str, Entry], root: Path = ROOT) -> list[Entry]:
    entries: list[Entry] = []
    for name, version in python_closure(root).items():
        entry = Entry(name, version)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            dist = None
        if dist is not None and dist.version == version:
            entry.licence = _python_licence(name, dist.metadata)
            for f in sorted(dist.files or [], key=str):
                if _NOTICE_FILE.match(f.name):
                    entry.notices.append(Path(str(f.locate())).read_text(encoding="utf-8"))
        elif entry.key in previous:
            old = previous[entry.key]
            entry.licence, entry.notices = old.licence, old.notices
        elif name in OVERRIDES:
            entry.licence = OVERRIDES[name]
        entries.append(entry)
    return entries


# ============================================================================== JavaScript


def _split_key(key: str) -> tuple[str, str]:
    """``'@scope/pkg@1.2.3(peer@4)'`` -> ``('@scope/pkg', '1.2.3')``."""
    bare = key.split("(", 1)[0]
    name, _, version = bare.rpartition("@")
    return name, version


def js_closure(root: Path = ROOT) -> dict[str, str]:
    """``{name@version: name}`` of the production dependencies bundled into the web image."""
    import yaml

    lock = yaml.safe_load((root / "pnpm-lock.yaml").read_text(encoding="utf-8"))
    importers: dict[str, Any] = lock.get("importers", {})
    snapshots: dict[str, Any] = lock.get("snapshots", {})
    stack: list[str] = []
    seen_apps: set[str] = set()
    apps = list(JS_APPS)
    while apps:
        app = apps.pop()
        if app in seen_apps:
            continue
        seen_apps.add(app)
        for name, dep in (importers.get(app, {}).get("dependencies") or {}).items():
            version = str(dep["version"])
            if version.startswith("link:"):
                target = (root / app / version.removeprefix("link:")).resolve()
                apps.append(target.relative_to(root.resolve()).as_posix())
            else:
                stack.append(f"{name}@{version}")
    seen: set[str] = set()
    out: dict[str, str] = {}
    while stack:
        key = stack.pop()
        if key in seen:
            continue
        seen.add(key)
        name, version = _split_key(key)
        out[f"{name}@{version}"] = name
        snap = snapshots.get(key) or {}
        for section in ("dependencies", "optionalDependencies"):
            for dep_name, dep_version in (snap.get(section) or {}).items():
                stack.append(f"{dep_name}@{dep_version}")
    return dict(sorted(out.items()))


def _pnpm_licences(root: Path) -> dict[str, tuple[str, Path]]:
    """``{name@version: (licence, package dir)}`` from ``pnpm licenses list --prod``."""
    found: dict[str, tuple[str, Path]] = {}
    for app in JS_APPS:
        raw = subprocess.run(
            ["pnpm", "licenses", "list", "--prod", "--json"],
            cwd=root / app,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for licence, packages in json.loads(raw).items():
            for pkg in packages:
                for version, path in zip(pkg["versions"], pkg["paths"], strict=False):
                    found[f"{pkg['name']}@{version}"] = (licence, Path(path))
    return found


def _texts(directory: Path, pattern: re.Pattern[str]) -> list[str]:
    if not directory.is_dir():
        return []
    files = sorted(p for p in directory.iterdir() if p.is_file() and pattern.match(p.name))
    return [p.read_text(encoding="utf-8", errors="replace").strip() for p in files]


def js_entries(previous: Mapping[str, Entry], root: Path = ROOT) -> list[Entry]:
    known = _pnpm_licences(root)
    entries: list[Entry] = []
    for key, name in js_closure(root).items():
        entry = Entry(name, key[len(name) + 1 :])
        if key in known:
            entry.licence, directory = known[key]
            entry.notices = _texts(directory, _NOTICE_FILE)
            entry.licence_texts = _texts(directory, _LICENSE_FILE)
        elif key in previous:
            old = previous[key]
            entry.licence, entry.notices = old.licence, old.notices
            entry.licence_texts = old.licence_texts
        entries.append(entry)
    return entries


# ================================================================================ the file


def parse(text: str) -> dict[str, dict[str, Entry]]:
    """The entries of an existing notices file, by section (``python``/``javascript``)."""
    sections: dict[str, dict[str, Entry]] = {"python": {}, "javascript": {}}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            title = line[3:].strip().lower()
            current = next((s for s in sections if title.startswith(s)), None)
        elif current is not None and (m := _ROW.match(line)):
            entry = Entry(m["name"], m["version"].strip(), m["licence"].strip())
            sections[current][entry.key] = entry
    for m in _TEXT.finditer(text):
        for tagged in m["keys"].split():
            lang, _, key = tagged.partition(":")
            owner = sections.get(_LANG.get(lang, ""), {}).get(key)
            if owner is not None:
                body = m["body"]
                (owner.notices if m["kind"] == "notice" else owner.licence_texts).append(body)
    return sections


def _clean(body: str) -> str:
    return body.strip("\n").replace("~~~", "~ ~ ~")


def _block(kind: str, keys: Iterable[str], body: str) -> str:
    return f"<!-- {kind}: {' '.join(keys)} -->\n~~~text\n{_clean(body)}\n~~~"


def render(python: Iterable[Entry], javascript: Iterable[Entry]) -> str:
    py, js = list(python), list(javascript)
    out = [
        "# Third-party notices",
        "",
        "<!-- Generated by scripts/third_party_notices.py from uv.lock and pnpm-lock.yaml;"
        " do not edit by hand. -->",
        "",
        "modeling-api ships the third-party libraries below in its container images. Each is",
        "used under its own licence, named here as an SPDX expression. The modeling-api",
        "components themselves are licensed as described in `LICENSE.md`.",
        "",
        "The Python packages are installed with their own licence files (in each",
        "`*.dist-info` directory of the image's virtual environment). The JavaScript packages",
        "are compiled into the web app and the plot sandbox, so their licence texts (where",
        "the package ships one) are reproduced at the end of this file. NOTICE files are",
        "reproduced verbatim.",
        "",
        f"## Python packages ({len(py)})",
        "",
        "Shipped in the runner, worker, API and GCS-shim images (`uv.lock`).",
        "",
        "| Package | Version | Licence |",
        "|---|---|---|",
        *(f"| `{e.name}` | {e.version} | {e.licence} |" for e in py),
        "",
        f"## JavaScript packages ({len(js)})",
        "",
        "Bundled into the web image (`apps/web`, `apps/plot-sandbox`; `pnpm-lock.yaml`).",
        "",
        "| Package | Version | Licence |",
        "|---|---|---|",
        *(f"| `{e.name}` | {e.version} | {e.licence} |" for e in js),
        "",
        "## NOTICE files",
        "",
    ]
    for lang, entries in (("py", py), ("js", js)):
        for e in entries:
            for body in e.notices:
                block = _block("notice", [f"{lang}:{e.key}"], body)
                out += [f"### {e.name} {e.version}", "", block, ""]
    out += [
        "## JavaScript licence texts",
        "",
        "Each text is shown once, under every package that ships it word for word.",
        "",
    ]
    # many packages of one project carry the same text: show it once, in first-seen order
    users: dict[str, list[Entry]] = {}
    for e in js:
        for body in e.licence_texts:
            users.setdefault(_clean(body), []).append(e)
    for body, entries in users.items():
        names = ", ".join(f"`{e.name}` {e.version}" for e in entries)
        block = _block("licence", [f"js:{e.key}" for e in entries], body)
        out += [
            f"### {entries[0].name} {entries[0].version}",
            "",
            f"Used by {names}.",
            "",
            block,
            "",
        ]
    return "\n".join(out).rstrip("\n") + "\n"


def stale(text: str, root: Path = ROOT) -> list[str]:
    """Why ``text`` does not describe the current lock files (empty when it does)."""
    sections = parse(text)
    want = {
        "python": {f"{n}@{v}" for n, v in python_closure(root).items()},
        "javascript": set(js_closure(root)),
    }
    problems: list[str] = []
    for title, keys in want.items():
        have = set(sections[title])
        problems += [f"{title}: {key} is missing" for key in sorted(keys - have)]
        problems += [f"{title}: {key} is no longer shipped" for key in sorted(have - keys)]
        problems += [
            f"{title}: {e.key} has no licence"
            for e in sections[title].values()
            if e.licence in ("", "UNKNOWN")
        ]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is stale")
    args = parser.parse_args(argv)
    current = NOTICES.read_text(encoding="utf-8") if NOTICES.exists() else ""
    if args.check:
        problems = stale(current) if current else [f"{NOTICES.name} does not exist"]
        for p in problems:
            print(p, file=sys.stderr)
        if problems:
            print("run: python scripts/third_party_notices.py", file=sys.stderr)
        return 1 if problems else 0
    previous = parse(current)
    py = python_entries(previous["python"])
    js = js_entries(previous["javascript"])
    NOTICES.write_text(render(py, js), encoding="utf-8")
    unknown = [e.key for e in (*py, *js) if e.licence == "UNKNOWN"]
    for key in unknown:
        print(f"no licence found for {key}: add it to OVERRIDES", file=sys.stderr)
    print(f"wrote {NOTICES.relative_to(ROOT)}: {len(py)} Python, {len(js)} JavaScript packages")
    return 1 if unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
