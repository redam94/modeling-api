"""The control plane never opens, unpickles or imports an upload (v0.19 hard rule).

Uploaded wheels are parsed and imported only inside the sandbox (``plugin_inspect``,
``plugin_describe`` and runs that mount an overlay). This AST scan of ``mapi_server`` refuses:

* imports of ``zipfile``, ``tarfile``, ``pickle``, ``marshal`` and ``shelve`` (in any form);
* ``importlib.import_module`` and ``importlib.metadata`` (entry-point loading), however they
  are reached (``import importlib.metadata``, ``from importlib import metadata``,
  ``importlib.import_module(...)``, ``__import__(...)``).

One allowlisted exception: ``services/gdpr_export.py`` imports ``zipfile`` to *write* the GDPR
export. There, every ``zipfile.ZipFile(...)`` call must open in mode ``"w"`` and no other
``zipfile`` entry point may be used. ``importlib.resources`` stays allowed (samples).

Import-linter cannot express this (stdlib modules are not in its graph), hence a test.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import mapi_server
import pytest

pytestmark = pytest.mark.unit

SERVER_SRC = Path(mapi_server.__file__).resolve().parent
FORBIDDEN_MODULES = frozenset({"zipfile", "tarfile", "pickle", "marshal", "shelve"})
FORBIDDEN_IMPORTLIB = frozenset({"import_module", "metadata"})
ZIP_WRITER = "services/gdpr_export.py"
"""The only module that may import ``zipfile`` (write mode only)."""
ZIP_WRITE_NAMES = frozenset({"ZipFile", "ZIP_DEFLATED", "ZIP_STORED", "ZipInfo"})


def _sources() -> list[Path]:
    return sorted(p for p in SERVER_SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(SERVER_SRC).as_posix()


def _root(name: str) -> str:
    return name.split(".", 1)[0]


def violations(tree: ast.AST, rel: str) -> Iterator[str]:
    """Every forbidden construct in one module's AST (as ``"<rel>:<line>: <what>"``)."""
    zip_allowed = rel == ZIP_WRITER
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root(alias.name)
                if root in FORBIDDEN_MODULES and not (zip_allowed and alias.name == "zipfile"):
                    yield f"{rel}:{node.lineno}: import {alias.name}"
                if alias.name.startswith("importlib.") and alias.name != "importlib.resources":
                    yield f"{rel}:{node.lineno}: import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _root(module) in FORBIDDEN_MODULES:
                yield f"{rel}:{node.lineno}: from {module} import ..."
            if module == "importlib":
                for alias in node.names:
                    if alias.name in FORBIDDEN_IMPORTLIB or alias.name == "*":
                        yield f"{rel}:{node.lineno}: from importlib import {alias.name}"
            elif module.startswith("importlib.") and module != "importlib.resources":
                yield f"{rel}:{node.lineno}: from {module} import ..."
        elif isinstance(node, ast.Attribute):
            value = node.value
            if (
                isinstance(value, ast.Name)
                and value.id == "importlib"
                and node.attr in FORBIDDEN_IMPORTLIB
            ):
                yield f"{rel}:{node.lineno}: importlib.{node.attr}"
            if (
                zip_allowed
                and isinstance(value, ast.Name)
                and value.id == "zipfile"
                and node.attr not in ZIP_WRITE_NAMES
            ):
                yield f"{rel}:{node.lineno}: zipfile.{node.attr} (only writing is allowed)"
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "__import__":
                yield f"{rel}:{node.lineno}: __import__(...)"
            if (
                zip_allowed
                and isinstance(func, ast.Attribute)
                and func.attr in ("ZipFile", "open")
                and _zip_mode(node) != "w"
            ):
                yield f"{rel}:{node.lineno}: zipfile.{func.attr}(...) not in write mode"


def _zip_mode(call: ast.Call) -> str | None:
    """The literal mode of a ``ZipFile(file, mode, ...)`` call (``"r"`` when absent)."""
    for kw in call.keywords:
        if kw.arg == "mode":
            return kw.value.value if isinstance(kw.value, ast.Constant) else None
    if len(call.args) >= 2:
        arg = call.args[1]
        return arg.value if isinstance(arg, ast.Constant) else None
    return "r"


def _scan(source: str, rel: str = "services/example.py") -> list[str]:
    return list(violations(ast.parse(source), rel))


def test_the_control_plane_never_parses_unpickles_or_imports_uploads() -> None:
    found: list[str] = []
    for path in _sources():
        found.extend(violations(ast.parse(path.read_text("utf-8"), str(path)), _rel(path)))
    assert not found, "\n".join(found)


def test_the_scan_covers_the_server_and_the_allowlisted_writer_exists() -> None:
    rels = {_rel(p) for p in _sources()}
    assert len(rels) > 50
    assert ZIP_WRITER in rels
    assert "zipfile.ZipFile" in (SERVER_SRC / ZIP_WRITER).read_text("utf-8")


@pytest.mark.parametrize(
    "source",
    [
        "import zipfile",
        "import tarfile as t",
        "from zipfile import ZipFile",
        "import pickle",
        "from pickle import loads",
        "import marshal",
        "import shelve",
        "import importlib.metadata",
        "from importlib import metadata",
        "from importlib import import_module",
        "from importlib.metadata import entry_points",
        "import importlib\nimportlib.import_module('x')",
        "import importlib\nimportlib.metadata.entry_points()",
        "__import__('pickle')",
        "from importlib import *",
    ],
)
def test_every_forbidden_form_is_caught(source: str) -> None:
    assert _scan(source), source


@pytest.mark.parametrize(
    "source",
    [
        "from importlib import resources",
        "import importlib.resources",
        "import json, hashlib",
        "pickled = 'never unpickled'",
    ],
)
def test_allowed_forms_pass(source: str) -> None:
    assert not _scan(source), source


def test_the_gdpr_writer_may_only_write() -> None:
    ok = "import zipfile\nz = zipfile.ZipFile(f, 'w', compression=zipfile.ZIP_DEFLATED)"
    assert not _scan(ok, ZIP_WRITER)
    assert not _scan("import zipfile\nz = zipfile.ZipFile(f, mode='w')", ZIP_WRITER)
    for bad in (
        "import zipfile\nz = zipfile.ZipFile(f)",
        "import zipfile\nz = zipfile.ZipFile(f, 'r')",
        "import zipfile\nz = zipfile.ZipFile(f, mode=m)",
        "import zipfile\nzipfile.is_zipfile(f)",
        "import tarfile",
    ):
        assert _scan(bad, ZIP_WRITER), bad
    # the allowance is for that one file only
    assert _scan("import zipfile", "services/other.py")
