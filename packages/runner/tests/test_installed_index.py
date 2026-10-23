"""``installed_index`` (v0.19, WP2): what the inspect run resolves requirements against is
the image's own installed distributions, read with ``importlib.metadata``."""

from __future__ import annotations

import platform
import re
from pathlib import Path

import pytest
from _wheels import index
from mapi_runner import plugin_inspect
from mapi_runner.plugin_inspect import InstalledIndex, installed_index


def test_indexes_the_running_interpreter() -> None:
    idx = installed_index()
    assert idx.requires_python == platform.python_version()
    # names are PEP 503, versions as installed
    assert idx.dists["mapi-spec"] == "0.16.0"
    assert idx.dists["mapi-runner"] == "0.16.0"
    assert "pydantic" in idx.dists
    assert all(n == n.lower() and "_" not in n and "." not in n for n in idx.dists)
    # top-level import names, from top_level.txt or the RECORD file list
    assert {"mapi_spec", "mapi_runner", "pydantic"} <= idx.top_level
    assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) for n in idx.top_level)
    # each distribution's own requirements, to resolve extras
    assert any(r.startswith("pydantic") for r in idx.requires["mapi-spec"])


def test_digest_prefers_the_image_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = "sha256:" + "cd" * 32
    digest_file = tmp_path / "catalog.digest"
    digest_file.write_text(good + "\n")
    monkeypatch.setattr(plugin_inspect, "_IMAGE_DIGEST_FILE", digest_file)
    assert installed_index().digest() == good
    digest_file.write_text("not a digest")
    fallback = installed_index().digest()
    assert fallback != good and re.fullmatch(r"sha256:[0-9a-f]{64}", fallback)
    monkeypatch.setattr(plugin_inspect, "_IMAGE_DIGEST_FILE", tmp_path / "missing")
    assert installed_index().digest() == fallback  # stable for the same installed set


def test_fallback_digest_follows_the_installed_set() -> None:
    a = InstalledIndex(dists={"x": "1.0"}, top_level=frozenset(), requires_python="3.12.1")
    b = InstalledIndex(dists={"x": "1.1"}, top_level=frozenset(), requires_python="3.12.1")
    c = InstalledIndex(dists={"x": "1.0"}, top_level=frozenset(), requires_python="3.12.2")
    assert len({a.digest(), b.digest(), c.digest()}) == 3
    assert (
        a.digest()
        == InstalledIndex(
            dists={"x": "1.0"}, top_level=frozenset({"x"}), requires_python="3.12.1"
        ).digest()
    )
    assert index().digest() == "sha256:" + "ab" * 32


def test_top_level_from_a_file_list() -> None:
    files = [
        "pkg/__init__.py",
        "mod.py",
        "_native.cpython-312-darwin.so",
        "pkg-1.0.dist-info/METADATA",
        "pkg-1.0.data/scripts/x",
        "../../bin/tool",
        "__pycache__/mod.cpython-312.pyc",
        "hook.pth",
        "",
    ]
    assert plugin_inspect._top_level_of(files) == {"pkg", "mod", "_native"}


def test_editable_installs_resolve_through_their_pth(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("")
    (src / "loose.py").write_text("")
    (src / "notapkg").mkdir()
    (src / "README.md").write_text("")
    pth = tmp_path / "site" / "_editable_pkg.pth"
    pth.parent.mkdir()
    pth.write_text(f"# comment\n{src}\nimport os; os.system('never run')\n../missing\n")
    assert plugin_inspect._editable_top_level(pth) == {"pkg", "loose"}
