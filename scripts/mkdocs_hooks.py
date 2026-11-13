"""MkDocs hooks: regenerate the generated reference pages before every build.

Registered in ``mkdocs.yml`` under ``hooks:``. Three generators write into
``docs/reference/generated/``: ``gen_docs_catalog.py`` (steps, models, plots, spec schema,
error codes), ``gen_docs_reference.py`` (CLI reference, server settings, the OpenAPI
document) and ``gen_docs_platform.py`` (permissions, environment variables, and the fragments
below). All only rewrite files whose content changed, so ``mkdocs serve`` does not loop on its
own output.

A hand-written page embeds a generated table with a line of its own:
``<!-- generated: NAME -->`` is replaced by ``generated/fragments/NAME.md`` before the page is
rendered. A fragment that does not exist fails the build.

The hooks also tune the search, in one place, because the generated pages have no front
matter to edit:

* ``on_page_markdown`` sets ``search.boost`` of a page from its path (the front matter of a
  page wins) and keeps the pages in ``SEARCH_EXCLUDE`` out of the index. The search plugin
  reads ``page.meta`` later, when it builds the index entry of the page.
* ``on_page_content`` marks each version badge with ``data-search-exclude``. The search
  plugin then leaves the badge text out of the index, so a result reads "Client dashboards"
  and not "Client dashboards v0.16 v0.17". The plugin removes the attribute from the page
  before the page is written.
"""

from __future__ import annotations

import importlib.util
import logging
import re
from pathlib import Path
from typing import Any

from mkdocs.exceptions import PluginError

log = logging.getLogger("mkdocs.hooks.generated_reference")
_SCRIPTS = ("gen_docs_catalog.py", "gen_docs_reference.py", "gen_docs_platform.py")
_FRAGMENT = re.compile(r"^<!-- generated: ([a-z0-9-]+) -->$", re.M)
_BADGE = re.compile(r'<span class="(mapi-badge(?:\s[^"]*)?)"')
#: The science tutorials: fifteen long pages with the same numbered headings, and rare words
#: in their titles ("signalling", "induced") that match the start of common queries.
_SCIENCE_TUTORIAL = re.compile(r"tutorials/pymc-challenges/e\d+-[a-z0-9-]+\.md")
_RELEASE = re.compile(r"whats-new-(\d+)\.(\d+)\.md")

#: Pages that the search does not index: the changelog repeats each release page, and the
#: old upload roadmap is a pointer to the pages that replaced it.
SEARCH_EXCLUDE = frozenset({"project/changelog.md", "extend/upload-roadmap.md"})

#: The task pages that the home page links ("Do a task" and the role tiles). They rank first.
TASK_PAGES = (
    "getting-started/index.md",
    "getting-started/quickstart.md",
    "guides/guided-tutorial.md",
    "guides/start-here.md",
    "guides/authentication.md",
    "guides/guided-data.md",
    "guides/choosing-a-model.md",
    "guides/null-tests.md",
    "guides/run-comparison.md",
    "guides/client-dashboards.md",
    "guides/sharing-dashboards-securely.md",
    "guides/for-clients.md",
    "guides/ask-about-your-dashboard.md",
    "guides/workflow-cookbook.md",
    "extend/upload.md",
    "reference/spec-format.md",
    "reference/generated/error-codes.md",
    "operations/troubleshooting.md",
)

_BOOST_FIRST = 2
#: "sign in" is two short words, and the search matches the start of a word: "in" also finds
#: "interpreting", "install" and "integrations" in other titles. The sign-in page must still
#: come first for that query, so it gets more than the other task pages.
_BOOST_BY_PAGE = {"guides/authentication.md": 4}
_BOOST_CATALOG = 0.5
_BOOST_LAST = 0.2
#: Section landing pages: the page that a tab or a group opens.
_LANDING = frozenset(
    {
        "index.md",
        "guides/index.md",
        "reference/index.md",
        "extend/index.md",
        "operations/index.md",
        "tutorials/index.md",
        "concepts/index.md",
    }
)
_LAST_PREFIXES = ("reference/api/", "roadmap/")
_LAST_PAGES = frozenset(
    {
        "operations/m2-verification.md",
        "operations/m3-verification.md",
        "operations/m35-verification.md",
    }
)


def _load(name: str) -> Any:
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _generated(config: Any) -> Path:
    return Path(config["docs_dir"]) / "reference" / "generated"


def on_pre_build(config: Any, **kwargs: Any) -> None:
    out = _generated(config)
    for script in _SCRIPTS:
        changed = _load(script).generate(out)
        if changed:
            log.info("%s: regenerated %d file(s)", script, len(changed))


def _old_releases(docs_dir: Path) -> set[str]:
    """Each ``whats-new-*.md`` page but the newest two."""
    releases = []
    for path in docs_dir.glob("whats-new-*.md"):
        match = _RELEASE.fullmatch(path.name)
        if match:
            releases.append(((int(match.group(1)), int(match.group(2))), path.name))
    return {name for _version, name in sorted(releases)[:-2]}


def search_boost(src_uri: str, docs_dir: Path) -> float | None:
    """The search boost of a page from its path; ``None`` keeps the default (1)."""
    if src_uri in _BOOST_BY_PAGE:
        return _BOOST_BY_PAGE[src_uri]
    if src_uri in _LANDING or src_uri in TASK_PAGES:
        return _BOOST_FIRST
    if src_uri.startswith(_LAST_PREFIXES) or src_uri in _LAST_PAGES:
        return _BOOST_LAST
    if src_uri in _old_releases(docs_dir):
        return _BOOST_LAST
    if src_uri.startswith("reference/generated/") or _SCIENCE_TUTORIAL.fullmatch(src_uri):
        return _BOOST_CATALOG
    return None


def _tune_search(page: Any, config: Any) -> None:
    src_uri = page.file.src_uri
    search = dict(page.meta.get("search") or {})
    if src_uri in SEARCH_EXCLUDE:
        search.setdefault("exclude", True)
    boost = search_boost(src_uri, Path(config["docs_dir"]))
    if boost is not None:
        search.setdefault("boost", boost)
    if search:
        page.meta["search"] = search


def on_page_markdown(markdown: str, page: Any, config: Any, **kwargs: Any) -> str:
    _tune_search(page, config)
    fragments = _generated(config) / "fragments"

    def embed(match: re.Match[str]) -> str:
        path = fragments / f"{match.group(1)}.md"
        if not path.is_file():
            raise PluginError(f"{page.file.src_path}: no generated fragment {match.group(1)!r}")
        return path.read_text("utf-8").rstrip("\n")

    return _FRAGMENT.sub(embed, markdown)


def on_page_content(html: str, page: Any, config: Any, **kwargs: Any) -> str:
    return _BADGE.sub(r'<span data-search-exclude="badge" class="\1"', html)
