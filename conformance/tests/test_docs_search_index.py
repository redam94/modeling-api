"""The web app's documentation index is fresh and points at real pages.

``scripts/gen_docs_search_index.py`` makes ``apps/web/src/features/search/docs-index.gen.json``
from the ``nav`` of ``mkdocs.yml``. The global search of the web app links to these paths, so a
stale index sends a person to a page that is not there.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
SCRIPT = ROOT / "scripts" / "gen_docs_search_index.py"
INDEX = ROOT / "apps" / "web" / "src" / "features" / "search" / "docs-index.gen.json"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_docs_search_index", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pages() -> list[dict[str, object]]:
    pages = json.loads(INDEX.read_text("utf-8"))
    assert isinstance(pages, list) and pages, "the index has no page"
    return pages


def _source(site_path: str) -> Path | None:
    """The Markdown file of a site path: ``guides/x/`` is ``guides/x.md`` or its ``index.md``."""
    stem = site_path.rstrip("/")
    candidates = (
        [DOCS / stem / "index.md"] if not stem else [DOCS / f"{stem}.md", DOCS / stem / "index.md"]
    )
    return next((c for c in candidates if c.is_file()), None)


def test_the_index_is_fresh() -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr or done.stdout


def test_every_path_is_a_page_of_the_docs() -> None:
    for page in _pages():
        path = page["p"]
        assert isinstance(path, str)
        assert path == "" or (path.endswith("/") and not path.startswith("/")), path
        assert _source(path) is not None, f"no docs page for {path!r}"


def test_every_entry_has_a_title_a_section_and_headings_with_anchors() -> None:
    pages = _pages()
    assert [p["p"] for p in pages] == sorted(p["p"] for p in pages), "the index is sorted by path"
    assert len({p["p"] for p in pages}) == len(pages), "a path is in the index two times"
    for page in pages:
        assert set(page) == {"t", "s", "p", "h"}
        assert isinstance(page["t"], str) and page["t"].strip() and "<" not in page["t"], page
        assert isinstance(page["s"], str) and page["s"].strip(), page
        headings = page["h"]
        assert isinstance(headings, list)
        for heading in headings:
            text, anchor = heading
            assert isinstance(text, str) and text.strip() and "<" not in text, page["p"]
            assert isinstance(anchor, str) and anchor and " " not in anchor and "#" not in anchor, (
                page["p"]
            )
        anchors = [h[1] for h in headings]
        assert len(set(anchors)) == len(anchors), f"an anchor is two times in {page['p']!r}"


def test_the_index_leaves_out_generated_reference_changelog_and_old_news() -> None:
    paths = [str(p["p"]) for p in _pages()]
    assert not [p for p in paths if p.startswith(("reference/api/", "reference/generated/"))]
    assert not [p for p in paths if p.rstrip("/").endswith("changelog")]
    assert len([p for p in paths if "whats-new-" in p]) <= 2
    assert INDEX.stat().st_size <= 120 * 1024


def test_check_fails_on_a_stale_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    out = tmp_path / "docs-index.gen.json"
    monkeypatch.setattr(module, "OUT", out)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    assert module.main(["--check"]) == 1
    assert "is stale" in capsys.readouterr().err
    assert not out.exists(), "--check writes nothing"
    assert module.main([]) == 0
    assert module.main(["--check"]) == 0
    out.write_text(out.read_text("utf-8").replace('"t":', '"t" :', 1), "utf-8")
    assert module.main(["--check"]) == 1


def test_titles_and_anchors_follow_mkdocs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    docs = tmp_path / "docs"
    (docs / "guides").mkdir(parents=True)
    (docs / "index.md").write_text(
        "---\ntitle: Start\n---\n\n# A hero line { .display }\n\n## Do a task. { .headline }\n",
        "utf-8",
    )
    (docs / "guides" / "index.md").write_text("# Guides\n", "utf-8")
    (docs / "guides" / "one.md").write_text(
        "\n".join(
            [
                '# One page <span class="mapi-badge">v0.9</span>',
                "## The `core.resample` step",
                "```bash",
                "## not a heading",
                "```",
                "## Same",
                "### Deeper",
                "## Same",
                "## With an id { #chosen }",
                "## A [link](x.md) and **bold**",
                "",
            ]
        ),
        "utf-8",
    )
    (docs / "whats-new-0.9.md").write_text("# News 0.9\n", "utf-8")
    (docs / "whats-new-0.10.md").write_text("# News 0.10\n", "utf-8")
    (docs / "whats-new-0.11.md").write_text("# News 0.11\n", "utf-8")
    (docs / "reference").mkdir()
    (docs / "reference" / "generated").mkdir()
    (docs / "reference" / "generated" / "cli.md").write_text("# CLI\n", "utf-8")
    (docs / "changelog.md").write_text("# Changelog\n", "utf-8")
    mkdocs = tmp_path / "mkdocs.yml"
    mkdocs.write_text(
        "\n".join(
            [
                "site_name: x",
                "markdown_extensions:",
                "  - pymdownx.emoji:",
                "      emoji_index: !!python/name:material.extensions.emoji.twemoji",
                "nav:",
                "  - Home:",
                "      - index.md",
                "      - News:",
                "          - whats-new-0.11.md",
                "          - whats-new-0.10.md",
                "          - whats-new-0.9.md",
                "      - Changelog: changelog.md",
                "      - Elsewhere: https://example.test/",
                "  - Guides:",
                "      - guides/index.md",
                "      - First: guides/one.md",
                "      - Missing: guides/missing.md",
                "  - Reference:",
                "      - CLI: reference/generated/cli.md",
                "",
            ]
        ),
        "utf-8",
    )
    monkeypatch.setattr(module, "MKDOCS", mkdocs)
    monkeypatch.setattr(module, "DOCS", docs)
    pages = {p["p"]: p for p in module.build()}
    assert list(pages) == ["", "guides/", "guides/one/", "whats-new-0.10/", "whats-new-0.11/"]
    assert pages[""] == {"t": "Start", "s": "Home", "p": "", "h": [["Do a task.", "do-a-task"]]}
    assert pages["guides/"]["t"] == "Guides"
    assert pages["guides/one/"] == {
        "t": "One page",
        "s": "Guides",
        "p": "guides/one/",
        "h": [
            ["The core.resample step", "the-coreresample-step"],
            ["Same", "same"],
            ["Same", "same_1"],
            ["With an id", "chosen"],
            ["A link and bold", "a-link-and-bold"],
        ],
    }
    assert pages["whats-new-0.11/"]["s"] == "Home"
