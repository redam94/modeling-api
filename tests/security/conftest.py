"""Fixtures of the upload corpus gate (v0.19 WP12).

* **Import sentinel.** A meta path finder, installed when this conftest loads, fails any
  import of a corpus top-level name (``t_crpsxx_*``: the corpus and probe namespaces) for the
  rest of the session, and the session fails if such a module is ever in ``sys.modules``.
  The corpus is data: it is built in tmp, handed to the inspector and the describe re-check,
  and never imported, installed or extracted outside a sandbox run.
* **Corpus.** ``corpus`` (the parsed ``cases.toml``) and ``build`` (build one case into a
  per-test tmp dir), loaded from ``security/plugin-uploads/build_corpus.py`` by path.
* **Server.** The review cases run against the API with the server test fixtures
  (``pg_admin_url``, ``pg_url``, ``settings``, ``ctx``, ``client``), re-exported from
  ``packages/server/tests/conftest.py``.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = ROOT / "security" / "plugin-uploads"
SERVER_TESTS = ROOT / "packages" / "server" / "tests"

CORPUS_PREFIX = "t_crpsxx_"
"""Every corpus top-level name starts with this (the corpus and probe namespaces)."""


# ============================================================================ import sentinel


class CorpusImportError(ImportError):
    """Something tried to import a corpus module outside a sandbox run."""


class CorpusSentinel(importlib.abc.MetaPathFinder):
    """Refuses (and records) every import of a corpus top-level name."""

    def __init__(self) -> None:
        self.hits: list[str] = []

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> Any:
        if fullname.split(".", 1)[0].startswith(CORPUS_PREFIX):
            self.hits.append(fullname)
            raise CorpusImportError(f"the corpus module {fullname!r} must never be imported")
        return None


SENTINEL = CorpusSentinel()
if not any(isinstance(f, CorpusSentinel) for f in sys.meta_path):
    sys.meta_path.insert(0, SENTINEL)


def corpus_modules() -> list[str]:
    return sorted(m for m in sys.modules if m.split(".", 1)[0].startswith(CORPUS_PREFIX))


@pytest.fixture(autouse=True)
def _no_corpus_imports() -> Any:
    yield
    assert SENTINEL.hits == [], f"corpus imports attempted: {SENTINEL.hits}"
    assert corpus_modules() == [], corpus_modules()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if SENTINEL.hits or corpus_modules():
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        sys.stderr.write(
            f"\nupload corpus imported outside a sandbox: {SENTINEL.hits or corpus_modules()}\n"
        )


# ==================================================================================== corpus


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_corpus = _load("mapi_upload_corpus_builder", CORPUS_DIR / "build_corpus.py")


@pytest.fixture(scope="session")
def corpus() -> Any:
    return build_corpus.load_corpus()


@pytest.fixture
def build(corpus: Any, tmp_path: Path) -> Callable[[str], Any]:
    """Build one case by name into this test's tmp dir."""
    by_name = corpus.by_name()

    def go(name: str) -> Any:
        return build_corpus.build_case(corpus, by_name[name], tmp_path / "corpus")

    return go


def cases_of(*, groups: Sequence[str] = (), artifacts: Sequence[str] = ()) -> list[Any]:
    """Cases for parametrization (read at collection time)."""
    c = build_corpus.load_corpus()
    return [
        case
        for case in c.cases
        if (not groups or case.group in groups) and (not artifacts or case.artifact in artifacts)
    ]


# ==================================================================================== server

_server = _load("mapi_server_tests_conftest", SERVER_TESTS / "conftest.py")
pg_admin_url = _server.pg_admin_url
pg_url = _server.pg_url
settings = _server.settings
ctx = _server.ctx
client = _server.client
