"""The licence tables, THIRD-PARTY-NOTICES.md and what the images ship stay true to the repo."""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from .conftest import ROOT, load_script

APACHE = "Apache-2.0"
PLATFORM = "AGPL-3.0-only OR LicenseRef-mapi-commercial"
LICENCE_DOCS = ("LICENSE.md", "docs/project/licensing.md")
NOTICES = "THIRD-PARTY-NOTICES.md"
DOC_DIR = "/usr/share/doc/mapi"
IMAGES = ("base-bayes", "base-dl", "base-forecast", "base-ml", "gcs-shim", "server", "web")


@pytest.fixture(scope="module")
def tpn() -> ModuleType:
    return load_script("scripts/third_party_notices.py", "mapi_devtools_third_party_notices")


def _python_components() -> list[tuple[str, str]]:
    """``(path, SPDX licence)`` of every Python package and plugin of the workspace."""
    out = []
    for pyproject in sorted(
        [*ROOT.glob("packages/*/pyproject.toml"), *ROOT.glob("plugins/*/pyproject.toml")]
    ):
        licence = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["license"]
        out.append((pyproject.parent.relative_to(ROOT).as_posix(), licence))
    return out


def _table_paths(doc: str, row_marker: str) -> list[str]:
    """The backticked paths of the table rows of ``doc`` whose licence cell has ``row_marker``."""
    paths: list[str] = []
    for line in (ROOT / doc).read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 3 and row_marker in cells[-1]:
            paths += re.findall(r"`([^`]+)`", cells[-2])
    return paths


@pytest.mark.parametrize("doc", LICENCE_DOCS)
@pytest.mark.parametrize(("path", "licence"), _python_components())
def test_every_package_is_in_the_licence_tables(doc: str, path: str, licence: str) -> None:
    marker = "Apache" if licence == APACHE else "AGPL"
    assert licence in (APACHE, PLATFORM), f"{path}: unexpected licence {licence!r}"
    paths = _table_paths(doc, marker)
    assert any(fnmatch.fnmatch(path, p.rstrip("/")) for p in paths), (
        f"{doc}: {path} ({licence}) is missing from the licence table"
    )
    assert (ROOT / path / "LICENSE").is_file(), f"{path} carries no LICENSE file"


def test_web_apps_are_in_the_platform_row() -> None:
    for doc in LICENCE_DOCS:
        assert "apps/*" in _table_paths(doc, "AGPL"), doc


def test_third_party_notices_match_the_locks(tpn: ModuleType) -> None:
    text = (ROOT / NOTICES).read_text(encoding="utf-8")
    assert tpn.stale(text) == [], "THIRD-PARTY-NOTICES.md is stale: run make notices"


def test_third_party_notices_name_the_m35_dependencies(tpn: ModuleType) -> None:
    sections = tpn.parse((ROOT / NOTICES).read_text(encoding="utf-8"))
    py = {e.name: e.licence for e in sections["python"].values()}
    js = {e.name: e.licence for e in sections["javascript"].values()}
    assert py["mmm-framework"] == "Apache-2.0"
    assert py["statsmodels"] == "BSD-3-Clause"
    assert py["numpyro"] == py["jax"] == py["jaxlib"] == "Apache-2.0"
    assert py["nutpie"] == "MIT"
    assert js["@xyflow/react"] == "MIT"
    # the server's PostgreSQL driver is LGPL: it must be visible, not buried
    assert py["psycopg"] == "LGPL-3.0-only"


def test_closures_follow_the_images_and_the_bundles(tpn: ModuleType) -> None:
    py = tpn.python_closure()
    assert "pytest" not in py and "ruff" not in py, "development tools are not shipped"
    assert not any(n.startswith("mapi-") for n in py), "workspace packages are ours"
    assert "nvidia-nccl-cu13" not in py, "MAPI_NO_INSTALL is honoured"
    js = set(tpn.js_closure().values())
    assert {"@xyflow/react", "react", "vega", "plotly.js-strict-dist-min"} <= js
    assert not {"vite", "vitest", "eslint", "typescript"} & js, "dev tooling is not bundled"


def test_parse_round_trips_render(tpn: ModuleType) -> None:
    e = tpn.Entry("pkg", "1.0", "MIT", notices=["Copyright X\n\nNotice ~~~ body"])
    js = tpn.Entry("@s/js", "2.0", "ISC", licence_texts=["ISC licence text"])
    parsed = tpn.parse(tpn.render([e], [js]))
    assert parsed["python"]["pkg@1.0"].licence == "MIT"
    assert parsed["python"]["pkg@1.0"].notices == ["Copyright X\n\nNotice ~ ~ ~ body"]
    assert parsed["javascript"]["@s/js@2.0"].licence_texts == ["ISC licence text"]


def test_licensing_doc_points_at_the_notices() -> None:
    text = (ROOT / "docs/project/licensing.md").read_text(encoding="utf-8")
    assert NOTICES in text
    for name in ("statsmodels", "NumPyro", "JAX", "nutpie", "@xyflow/react"):
        assert name in text, name


@pytest.mark.parametrize("name", IMAGES)
def test_images_ship_the_licences_and_notices(name: str) -> None:
    text = (ROOT / "images" / name / "Dockerfile").read_text(encoding="utf-8")
    joined = re.sub(r"\\\n\s*", " ", text)  # fold continuation lines into one instruction
    copies = [line for line in joined.splitlines() if line.startswith("COPY") and DOC_DIR in line]
    # base-bayes has two final stages (runtime and the compose worker); both ship them
    expected = 2 if name == "base-bayes" else 1
    assert len(copies) == expected, f"{name}: {copies}"
    for line in copies:
        for f in ("LICENSE.md", "LICENSE-APACHE.txt", "LICENSE-AGPL.txt", NOTICES):
            assert f in line.split(), f"{name}: {f} is not copied into {DOC_DIR}"


def test_notices_file_is_not_ignored_by_the_build_contexts() -> None:
    for path in [
        Path(".dockerignore"),
        *(Path("images") / n / "Dockerfile.dockerignore" for n in IMAGES),
    ]:
        rules = (ROOT / path).read_text(encoding="utf-8").splitlines()
        assert NOTICES not in rules and "*.md" not in rules, path
