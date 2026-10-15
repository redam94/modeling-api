"""The documentation's code is real: every snippet runs, every include resolves.

Python examples on the docs site live in ``docs/snippets/*.py`` and are included into pages
with ``--8<-- "file.py:section"`` (pymdownx.snippets). This module runs each snippet file top to
bottom, runs the pytest example as a pytest session, checks that every include in
``docs/**/*.md`` names an existing file and section, and that the catalog generator covers
every installed step and model.
"""

from __future__ import annotations

import importlib.util
import re
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
SNIPPETS = DOCS / "snippets"
SLOW = {"plugin_pymc.py", "pymc_fast.py"}  # tiny MCMC fits

SCRIPTS = sorted(p for p in SNIPPETS.glob("*.py") if not p.name.startswith("test_"))
INCLUDE = re.compile(r'^\s*--8<--\s+"([^"]+)"\s*$', re.M)  # block includes only


def _params() -> list[object]:
    return [
        pytest.param(p, id=p.name, marks=[pytest.mark.slow] if p.name in SLOW else [])
        for p in SCRIPTS
    ]


def test_snippets_exist() -> None:
    assert len(SCRIPTS) >= 8, f"expected the docs snippets in {SNIPPETS}"


@pytest.mark.parametrize("script", _params())
def test_snippet_runs(script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    runpy.run_path(str(script), run_name="__main__")


def test_pytest_snippet_passes(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(SNIPPETS / "test_my_plugin.py"),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout


def _sections(path: Path) -> set[str]:
    text = path.read_text("utf-8")
    starts = set(re.findall(r"--8<-- \[start:([\w-]+)\]", text))
    ends = set(re.findall(r"--8<-- \[end:([\w-]+)\]", text))
    assert starts == ends, f"{path.name}: unbalanced sections {starts ^ ends}"
    return starts


def test_every_include_resolves() -> None:
    seen = 0
    for page in DOCS.rglob("*.md"):
        if "generated" in page.parts or page.name == "architecture-plan.md":
            continue
        for target in INCLUDE.findall(page.read_text("utf-8")):
            name, _, section = target.partition(":")
            source = SNIPPETS / name if (SNIPPETS / name).exists() else ROOT / name
            assert source.exists(), f"{page.relative_to(ROOT)} includes missing {name!r}"
            if section:
                assert section in _sections(source), (
                    f"{page.relative_to(ROOT)} includes missing section {target!r}"
                )
            seen += 1
    assert seen >= 30


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gen_docs_catalog", ROOT / "scripts" / "gen_docs_catalog.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalog_pages_cover_every_step_and_model(tmp_path: Path) -> None:
    from mapi_sdk import default_registry

    gen = _generator()
    changed = gen.generate(tmp_path)
    assert changed, "the generator wrote nothing"
    catalog = default_registry().catalog()
    steps = (tmp_path / "steps-catalog.md").read_text("utf-8")
    models = (tmp_path / "models-catalog.md").read_text("utf-8")
    plots = (tmp_path / "plots-catalog.md").read_text("utf-8")
    assert all(f"`{s.id}`" in steps for s in catalog.steps)
    assert all(f"`{m.id}`" in models for m in catalog.models)
    assert all(f"`{p.id}`" in plots for p in catalog.plots)
    assert "`ModelSpec`" in (tmp_path / "spec-schema.md").read_text("utf-8")
    assert "`HORIZON_EXCEEDS_FIT` | 409" in (tmp_path / "error-codes.md").read_text("utf-8")
    assert (tmp_path / "schemas" / "modelspec.json").exists()
    assert gen.generate(tmp_path) == [], "regenerating unchanged content must not rewrite files"
