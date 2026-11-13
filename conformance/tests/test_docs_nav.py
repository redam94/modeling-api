"""The docs navigation, the home page and the search tuning are checked like code.

Fast, no network and no build (text, YAML and the hook functions only):

* the nav has six tabs in a fixed order, and each page is in it exactly one time;
* the Guides and Reference tabs open on a landing page that links to each page of its tab;
* the home page uses the full-bleed template, and each link and icon on it resolves;
* the "Try" list of the search dialog points at pages in the nav;
* ``scripts/mkdocs_hooks.py`` sets the search boost of a page from its path, keeps two pages
  out of the index, and keeps version badges out of the titles that the search shows;
* the search separator splits the names that readers type (``share_link``, ``core.calendar``).
"""

from __future__ import annotations

import importlib.util
import posixpath
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml
from pathspec.gitignore import GitIgnoreSpec

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
OVERRIDES = ROOT / "overrides"
MKDOCS = ROOT / "mkdocs.yml"

TABS = ["Home", "Learn", "Guides", "Extend", "Reference", "Operate"]
#: Tabs with a landing page written for them, and that page.
LANDING = {"Guides": "guides/index.md", "Reference": "reference/index.md"}
#: The page that each tab opens (``navigation.indexes``: the first entry of the tab).
TAB_OPENS = {
    "Home": "index.md",
    "Learn": "getting-started/index.md",
    "Guides": "guides/index.md",
    "Extend": "extend/index.md",
    "Reference": "reference/index.md",
    "Operate": "operations/index.md",
}
#: Pages that exist only after a build (git-ignored output of the generators).
GENERATED = "reference/generated/"

LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


class _Loader(yaml.SafeLoader):
    """``mkdocs.yml`` names Python objects (``!!python/name:…``); read them as plain text."""


_Loader.add_multi_constructor(
    "tag:yaml.org,2002:python/", lambda _loader, suffix, _node: f"python/{suffix}"
)


def _config() -> dict[str, Any]:
    return yaml.load(MKDOCS.read_text("utf-8"), Loader=_Loader)  # a SafeLoader subclass


def _pages(items: list[Any]) -> list[str]:
    """Each page path below a nav node, in order."""
    found: list[str] = []
    for item in items:
        if isinstance(item, str):
            found.append(item)
            continue
        for value in item.values():
            found += [value] if isinstance(value, str) else _pages(value)
    return found


def _tabs() -> dict[str, list[str]]:
    tabs: dict[str, list[str]] = {}
    for item in _config()["nav"]:
        assert isinstance(item, dict) and len(item) == 1, item
        ((title, children),) = item.items()
        assert isinstance(children, list), f"the tab {title} must be a group"
        tabs[title] = _pages(children)
    return tabs


def _front_matter(path: Path) -> dict[str, Any]:
    text = path.read_text("utf-8")
    if not text.startswith("---\n"):
        return {}
    return yaml.safe_load(text.split("---\n", 2)[1]) or {}


def _targets(page: str, pattern: re.Pattern[str]) -> set[str]:
    """The docs-relative files that the Markdown links (or images) of a page point at."""
    out: set[str] = set()
    for ref in pattern.findall((DOCS / page).read_text("utf-8")):
        if re.match(r"[a-z]+:|#", ref):
            continue
        target = ref.split("#", 1)[0]
        out.add(posixpath.normpath(posixpath.join(posixpath.dirname(page), target)))
    return out


def _hooks() -> ModuleType:
    path = ROOT / "scripts" / "mkdocs_hooks.py"
    spec = importlib.util.spec_from_file_location("mkdocs_hooks_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _page(src_uri: str, meta: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(file=SimpleNamespace(src_uri=src_uri, src_path=src_uri), meta=meta or {})


def _boost(src_uri: str, meta: dict[str, Any] | None = None) -> Any:
    page = _page(src_uri, meta)
    _hooks().on_page_markdown("# Title\n", page=page, config={"docs_dir": str(DOCS)}, files=None)
    return (page.meta.get("search") or {}).get("boost")


# ------------------------------------------------------------------------------------- nav


def test_the_nav_has_six_tabs_in_order() -> None:
    assert list(_tabs()) == TABS
    # the other docs tests read the file as text and depend on two-space indentation
    nav = MKDOCS.read_text("utf-8").split("\nnav:\n", 1)[1].split("\nvalidation:", 1)[0]
    assert re.findall(r"^  - ([^:\n]+):$", nav, re.M) == TABS


def test_each_tab_opens_on_its_index_page() -> None:
    assert "navigation.indexes" in _config()["theme"]["features"]
    nav = {next(iter(item)): next(iter(item.values())) for item in _config()["nav"]}
    for tab, page in TAB_OPENS.items():
        # a bare path as the first entry is the page of the section itself
        assert nav[tab][0] == page, f"{tab} must open {page}"
        assert (DOCS / page).is_file(), page


def test_each_page_is_in_the_nav_one_time() -> None:
    config = _config()
    listed = [page for pages in _tabs().values() for page in pages]
    twice = sorted({page for page in listed if listed.count(page) > 1})
    assert not twice, f"in the nav more than one time: {twice}"

    excluded = GitIgnoreSpec.from_lines(config["exclude_docs"].splitlines())
    hidden = GitIgnoreSpec.from_lines(config["not_in_nav"].splitlines())
    on_disk = {p.relative_to(DOCS).as_posix() for p in DOCS.rglob("*.md")}
    expected = {p for p in on_disk if not excluded.match_file(p) and not hidden.match_file(p)}
    missing = sorted(expected - set(listed))
    assert not missing, f"not in the nav (add them, or to exclude_docs / not_in_nav): {missing}"
    # a nav entry names a file; only the generated reference pages can be absent before a build
    dead = sorted(p for p in listed if p not in on_disk and not p.startswith(GENERATED))
    assert not dead, f"the nav names missing files: {dead}"
    for page in listed:
        assert not excluded.match_file(page) and not hidden.match_file(page), page


def test_the_releases_are_newest_first_in_home() -> None:
    releases = [p for p in _tabs()["Home"] if p.startswith("whats-new-")]
    assert len(releases) >= 8
    versions = [tuple(int(n) for n in re.findall(r"\d+", p)) for p in releases]
    assert versions == sorted(versions, reverse=True), releases
    assert sorted(releases) == sorted(p.name for p in DOCS.glob("whats-new-*.md"))


def test_labels_in_one_tab_are_distinct() -> None:
    def labels(items: list[Any]) -> list[str]:
        out: list[str] = []
        for item in items:
            if isinstance(item, dict):
                ((title, value),) = item.items()
                out.append(title)
                if isinstance(value, list):
                    out += labels(value)
        return out

    for item in _config()["nav"]:
        ((tab, children),) = item.items()
        names = labels(children)
        twice = sorted({n for n in names if names.count(n) > 1})
        assert not twice, f"{tab} has the same label two times: {twice}"
    text = MKDOCS.read_text("utf-8").split("\nnav:", 1)[1]
    # plugin upload, dashboard chat and MCP are released (0.19, 0.20)
    assert "Planned:" not in text


# --------------------------------------------------------------------------- landing pages


@pytest.mark.parametrize("tab", sorted(LANDING))
def test_a_landing_page_links_to_each_page_of_its_tab(tab: str) -> None:
    landing = LANDING[tab]
    assert (DOCS / landing).is_file(), f"docs/{landing} does not exist"
    pages = _tabs()[tab]
    assert pages[0] == landing
    linked = _targets(landing, LINK)
    missing = [p for p in pages[1:] if p not in linked]
    assert not missing, f"{landing} does not link to: {missing}"
    for target in sorted(linked):
        exists = (DOCS / target).is_file() or target.startswith(GENERATED)
        assert exists, f"{landing} links to a missing page: {target}"
    for icon in sorted(_targets(landing, IMAGE)):
        assert (DOCS / icon).is_file(), f"{landing} uses a missing image: {icon}"
    meta = _front_matter(DOCS / landing)
    assert meta.get("description"), f"{landing} needs a description"
    text = (DOCS / landing).read_text("utf-8")
    assert "mono-tiles" in text or "mono-links" in text, "use the building blocks"


# ------------------------------------------------------------------------------- home page


def test_the_home_page_uses_the_home_template_and_its_links_resolve() -> None:
    meta = _front_matter(DOCS / "index.md")
    assert meta.get("template") == "home.html"
    assert (OVERRIDES / "home.html").is_file()
    # a real title and description: the browser tab must not read "modeling-api - modeling-api"
    assert meta.get("title") and meta["title"] != _config()["site_name"]
    assert 50 <= len(meta.get("description", "")) <= 200

    in_nav = {page for pages in _tabs().values() for page in pages}
    links = _targets("index.md", LINK)
    assert len(links) >= 20, "the home page routes to the roles and the tasks"
    for target in sorted(links):
        assert target in in_nav, f"index.md links to a page that is not in the nav: {target}"
    images = _targets("index.md", IMAGE)
    icons = {i for i in images if i.startswith("assets/brand/icons/")}
    assert {
        f"assets/brand/icons/{n}.webp" for n in ("analyst", "developer", "operator", "client")
    } <= icons
    for image in sorted(images):
        assert (DOCS / image).is_file(), f"index.md uses a missing image: {image}"

    text = (DOCS / "index.md").read_text("utf-8")
    assert "mono-band--hero" in text and "mono-hero--home" in text
    assert text.count("{ .mono-display") == 1, "one page headline"
    # the newest release page that exists is the one the home page names
    releases = sorted(
        DOCS.glob("whats-new-*.md"), key=lambda p: tuple(int(n) for n in re.findall(r"\d+", p.name))
    )
    assert releases[-1].name in links
    # the copy rules of the site: no arrows and no middle dots as separators
    for banned in ("→", "·", "octicons-arrow-right"):
        assert banned not in text, f"index.md uses {banned!r}"


def test_the_home_page_is_short() -> None:
    lines = (DOCS / "index.md").read_text("utf-8").splitlines()
    assert len(lines) < 320, "the home page routes; the detail is on the pages it links"
    shots = [ln for ln in lines if "assets/screenshots/" in ln]
    assert len(shots) <= 2
    # the tour keeps its four steps; the full path is in the tutorial that it links
    tour = "\n".join(lines).split('<div class="mono-steps" markdown>', 1)[1].split("</div>", 1)[0]
    assert len(re.findall(r"^\d\.  ### ", tour, re.M)) == 4


# ---------------------------------------------------------------------------------- search


def test_the_search_try_list_points_at_nav_pages() -> None:
    popular = _config()["extra"]["search_popular"]
    assert 6 <= len(popular) <= 8
    in_nav = {page for pages in _tabs().values() for page in pages}
    for entry in popular:
        stem = entry["url"].rstrip("/")
        sources = {f"{stem}.md", f"{stem}/index.md"}
        assert sources & in_nav, f"{entry['url']} is not a page in the nav"
    assert len({e["url"] for e in popular}) == len(popular)
    assert len({e["title"] for e in popular}) == len(popular)


def test_the_search_separator_splits_the_names_readers_type() -> None:
    search = next(
        p["search"] for p in _config()["plugins"] if isinstance(p, dict) and "search" in p
    )
    separator = re.compile(search["separator"])

    def tokens(text: str) -> list[str]:
        return [t for t in separator.split(text) if t]

    assert tokens("share_link") == ["share", "link"]
    assert tokens("core.calendar") == ["core", "calendar"]
    # a class name stays one token, so a query for it does not match "model" on every page
    assert tokens("ModelSpec") == ["ModelSpec"]
    assert tokens("client-dashboards") == ["client", "dashboards"]
    assert tokens("share link") == ["share", "link"]
    # a version number stays one token
    assert tokens("v0.20") == ["v0.20"]


def test_the_hook_sets_the_search_boost_from_the_path() -> None:
    # home, the landing pages and the task pages of the home page come first
    for page in ("index.md", "guides/index.md", "reference/index.md", "operations/index.md"):
        assert _boost(page) == 2, page
    for page in ("guides/client-dashboards.md", "guides/sharing-dashboards-securely.md"):
        assert _boost(page) == 2, page
    # generated catalogs are long lists: below the guides
    assert _boost("reference/generated/models-catalog.md") == 0.5
    assert _boost("reference/generated/plots-catalog.md") == 0.5
    # ... but the error codes are a task of the home page
    assert _boost("reference/generated/error-codes.md") == 2
    # the Python API, old releases, the roadmap and the verification records come last
    assert _boost("reference/api/spec.md") == 0.2
    assert _boost("roadmap/index.md") == 0.2
    assert _boost("operations/m35-verification.md") == 0.2
    # the science tutorials have rare words in their titles that match the start of other words
    assert _boost("tutorials/pymc-challenges/e19-protein-signalling.md") == 0.5
    assert _boost("tutorials/pymc-challenges/index.md") is None
    # "sign in" must find the sign-in page first
    assert _boost("guides/authentication.md") == 4
    # an ordinary guide keeps the default
    assert _boost("guides/tracking.md") is None
    assert _boost("concepts/pipelines.md") is None
    # the front matter of a page wins
    assert _boost("reference/api/spec.md", {"search": {"boost": 3}}) == 3


def test_the_hook_boosts_each_task_page_of_the_home_page() -> None:
    hooks = _hooks()
    in_nav = {page for pages in _tabs().values() for page in pages}
    links = _targets("index.md", LINK)
    assert len(hooks.TASK_PAGES) >= 10
    for page in hooks.TASK_PAGES:
        assert page in in_nav, f"TASK_PAGES names a page that is not in the nav: {page}"
        assert page in links, f"TASK_PAGES names a page that the home page does not link: {page}"


def test_the_hook_lowers_all_releases_but_the_newest_two() -> None:
    releases = sorted(
        (p.name for p in DOCS.glob("whats-new-*.md")),
        key=lambda name: tuple(int(n) for n in re.findall(r"\d+", name)),
    )
    for name in releases[-2:]:
        assert _boost(name) is None, name
    for name in releases[:-2]:
        assert _boost(name) == 0.2, name


def test_the_hook_keeps_two_pages_out_of_the_search() -> None:
    hooks = _hooks()
    for src in ("project/changelog.md", "extend/upload-roadmap.md"):
        page = _page(src)
        hooks.on_page_markdown("text\n", page=page, config={"docs_dir": str(DOCS)}, files=None)
        assert page.meta["search"]["exclude"] is True, src
    page = _page("guides/tracking.md")
    hooks.on_page_markdown("text\n", page=page, config={"docs_dir": str(DOCS)}, files=None)
    assert not (page.meta.get("search") or {}).get("exclude")


def test_the_hook_still_embeds_generated_fragments(tmp_path: Path) -> None:
    """The behaviour that the hook had before the search rules: it must still hold."""
    from mkdocs.exceptions import PluginError

    hooks = _hooks()
    fragments = tmp_path / "reference" / "generated" / "fragments"
    fragments.mkdir(parents=True)
    (fragments / "roles.md").write_text("| a | b |\n", "utf-8")
    config = {"docs_dir": str(tmp_path)}
    page = _page("guides/administration.md")
    out = hooks.on_page_markdown(
        "Before\n<!-- generated: roles -->\nAfter\n", page=page, config=config, files=None
    )
    assert out == "Before\n| a | b |\nAfter\n"
    with pytest.raises(PluginError, match="no generated fragment 'absent'"):
        hooks.on_page_markdown("<!-- generated: absent -->\n", page=page, config=config, files=None)


def test_version_badges_stay_out_of_search_titles() -> None:
    """The index that Material builds from the page must not carry the badge text."""
    from material.plugins.search.plugin import SearchIndex

    hooks = _hooks()
    html = (
        '<h1 id="client-dashboards">Client dashboards <span class="mapi-badge">v0.16</span> '
        '<span class="mapi-badge mapi-badge--soon">planned</span></h1>\n'
        "<p>Publish a dashboard.</p>\n"
        '<h2 id="share">Share a link <span class="mapi-badge">v0.17</span></h2>\n'
        "<p>Make a link.</p>\n"
    )
    page = SimpleNamespace(
        file=SimpleNamespace(src_uri="guides/client-dashboards.md"),
        meta={"search": {"boost": 2}},
        toc=[],
        url="guides/client-dashboards/",
        title="Build a client dashboard",
    )
    page.content = hooks.on_page_content(html, page=page, config={}, files=None)
    # the reader still sees the badge
    assert page.content.count('class="mapi-badge') == 3 and ">v0.16</span>" in page.content

    index = SearchIndex(lang=["en"], separator=r"[\s\-]+", pipeline=[], fields={})
    index.add_entry_from_context(page)
    titles = [entry["title"] for entry in index.entries]
    assert titles == ["Client dashboards", "Share a link"]
    assert all(entry["boost"] == 2 for entry in index.entries)
    assert "Publish a dashboard." in index.entries[0]["text"]

    # without the hook the badge text is in the title: this is what the hook repairs
    page.content = html
    plain = SearchIndex(lang=["en"], separator=r"[\s\-]+", pipeline=[], fields={})
    plain.add_entry_from_context(page)
    assert plain.entries[0]["title"] == "Client dashboards v0.16 planned"


# -------------------------------------------------------------------------- section labels


def test_the_search_script_gets_the_tab_of_each_page_that_is_not_in_its_folder() -> None:
    """A page such as ``guides/authentication.md`` is in the Operate tab, not in Guides."""
    search = (OVERRIDES / "partials" / "search.html").read_text("utf-8")
    script = (DOCS / "javascripts" / "mono.js").read_text("utf-8")
    assert "mono_page_sections" in search and '"sections"' in search
    assert "data.sections" in script
    main = (OVERRIDES / "main.html").read_text("utf-8")
    folders = dict(
        re.findall(r'"([a-z-]+)":\s*"([a-z]+)"', main.split("mono_sections = {")[1].split("}")[0])
    )
    assert set(folders.values()) <= {t.lower() for t in TABS}
    # the two maps name the same folders (mono.js: labels of results; main.html: head band art)
    labels = re.search(r"var SECTION_LABELS = (\{[^}]*\});", script)
    assert labels is not None
    assert set(yaml.safe_load(labels.group(1))) == set(folders)
    for folder, tab in folders.items():
        assert yaml.safe_load(labels.group(1))[folder].lower() == tab


def test_the_home_figures_match_the_catalogs() -> None:
    """The home page states four counts. Each count is the count in the page that it links."""
    text = (DOCS / "index.md").read_text("utf-8")
    figures = dict(
        (target, int(number))
        for number, target in re.findall(
            r'<p class="mono-figure-num[^"]*">(\d+)</p>\s+[^\n]*\]\(([^)]+)\)', text
        )
    )
    assert len(figures) >= 3, "each figure links the page that backs it"
    nodes = (DOCS / "reference" / "workflow-nodes.md").read_text("utf-8")
    assert figures["reference/workflow-nodes.md"] == len(re.findall(r"^### `", nodes, re.M))
    # the generated catalogs exist only after a build
    for target, count in figures.items():
        page = DOCS / target
        if "/generated/" not in target:
            continue
        if not page.is_file():
            pytest.skip(f"{target} is not built yet (run the docs build first)")
        head = page.read_text("utf-8").split("\n## ", 1)[0]
        assert count == len(re.findall(r"^\| \[`[^`]+`\]\(#", head, re.M)), target
