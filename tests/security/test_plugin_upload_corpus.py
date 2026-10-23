"""The static upload corpus (v0.19 WP12; T19.2-T19.9, T19.14): every hostile wheel is refused
by ``plugin_inspect`` with its stable :data:`~mapi_spec.plugins.InspectCheck` and a plain
reason, and nothing from it is imported, extracted or written next to it.

The wheels come from ``security/plugin-uploads/build_corpus.py`` (built in tmp, inert member
contents); the base image is the fixed synthetic one in ``cases.toml``.
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path
from typing import Any, get_args

import pytest
from mapi_spec.plugins import InspectCheck

pytestmark = pytest.mark.unit

builder = sys.modules["mapi_upload_corpus_builder"]
CORPUS = builder.load_corpus()
INSPECT_CASES = [c for c in CORPUS.cases if c.stage == "inspect"]


inspect_artifact = builder.inspect_artifact


@pytest.fixture
def no_extract(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    hits: list[str] = []

    def refuse(*_a: Any, **_k: Any) -> Any:
        hits.append("extract")
        raise AssertionError("a corpus member was extracted")

    monkeypatch.setattr(zipfile.ZipFile, "extract", refuse)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", refuse)
    return hits


# ================================================================================ the cases


def test_the_corpus_lists_every_group_and_valid_results() -> None:
    groups = {c.group for c in CORPUS.cases}
    assert groups == set(builder.GROUPS)
    checks = set(get_args(InspectCheck))
    for case in INSPECT_CASES:
        assert case.expect in checks, case.name
        assert case.artifact == "wheel", case.name
    assert CORPUS.namespace.startswith("t_crpsxx_")
    assert CORPUS.probe_namespace.startswith("t_crpsxx_")


@pytest.mark.parametrize("case", INSPECT_CASES, ids=lambda c: c.name)
def test_inspect_refuses_the_case(
    case: Any, corpus: Any, build: Any, no_extract: list[str], tmp_path: Path
) -> None:
    art = build(case.name)
    before = sorted(p.name for p in art.path.parent.iterdir())
    cwd = os.getcwd()
    os.chdir(art.path.parent)
    try:
        insp = inspect_artifact(corpus, art)
    finally:
        os.chdir(cwd)
    found = [(p.check, p.path, p.message) for p in insp.problems]
    assert not insp.ok, case.name
    hits = [p for p in insp.problems if p.check == case.expect]
    assert hits, f"{case.name}: expected {case.expect!r}, got {found}"
    if case.path is not None and case.name not in ("path_dotdot", "path_absolute"):
        want = corpus.fill(case.path)
        assert any(p.path == want for p in hits), (case.name, want, found)
    elif case.path is not None:
        assert any(p.path == case.path for p in hits), (case.name, found)
    if case.says is not None:
        assert any(case.says in p.message for p in hits), (case.name, case.says, found)
    # every reason is platform-authored plain text within its cap
    assert all(0 < len(p.message) <= 500 for p in insp.problems)
    # a refused wheel never yields an SBOM that could be mistaken for a clean one
    if case.expect in ("zip_structure", "encrypted", "compression_method", "link_member"):
        assert insp.sbom is None, case.name
    # nothing was written next to the wheel, nothing extracted
    assert sorted(p.name for p in art.path.parent.iterdir()) == before
    assert no_extract == []


def test_the_inert_base_wheel_passes(corpus: Any, tmp_path: Path) -> None:
    """The corpus's well-formed wheel passes, so every refusal above is the case's own."""
    path = builder.write_wheel(tmp_path / "base.whl", builder.members(corpus))
    insp = inspect_artifact(corpus, builder.Artifact(case=None, path=path))
    assert insp.ok, [(p.check, p.path, p.message) for p in insp.problems]
    assert insp.top_level == [corpus.namespace]


def test_runtime_cases_are_not_built_by_the_static_builder(corpus: Any, tmp_path: Path) -> None:
    for case in corpus.cases:
        if case.group == "runtime":
            with pytest.raises(builder.NotBuilt):
                builder.build_case(corpus, case, tmp_path)
