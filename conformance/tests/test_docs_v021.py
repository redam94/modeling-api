"""The v0.21 docs are real: the release page and the contributor page are in the nav, the home
page and the README point at the release page, the changelog has the release block, each
label that the pages show as a control exists in the web app or in the docs theme, and the
docs tests of the theme, the nav and the search index run with ``make docs-test``.

Fast, text only: no build, no browser and no network.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
WEB = ROOT / "apps" / "web" / "src"
OVERRIDES = ROOT / "overrides"
MKDOCS = ROOT / "mkdocs.yml"

WHATS_NEW = DOCS / "whats-new-0.21.md"
WEB_UI = DOCS / "guides" / "web-ui.md"
THEME_PAGE = DOCS / "project" / "docs-theme.md"
LICENSING = DOCS / "project" / "licensing.md"
ROADMAP = DOCS / "roadmap" / "expert-plugins.md"
TITLE = "### One look, and a search that finds your work (v0.21)"

APP_LABELS = {
    WHATS_NEW: [
        "Search",
        "Find a page, a name or an id",
        "Recent",
        "Clear recent items",
        "Go to",
        "Actions",
        "See all in",
        "Clear the search",
        "Help",
        "Read the docs",
        "Keyboard shortcuts",
        "Theme",
    ],
    WEB_UI: [
        "Search",
        "Find a page, a name or an id",
        "Recent",
        "Clear recent items",
        "See all in",
        "Clear the search",
        "Help",
        "Read the docs",
        "Keyboard shortcuts",
        "Theme",
        "System",
        "Light",
        "Dark",
        "API connected",
    ],
}
"""Text that the pages tell the reader to look for or to select, per page."""

#: Labels of the docs site itself (the header, the search dialog), not of the web app.
DOCS_SITE_LABELS = ["Try", "Search guides, concepts and reference"]

#: Page titles that changed in v0.21. A link must not use the old title as its text.
OLD_LINK_TEXT = {
    "Authentication and tenant isolation": None,
    "Your first analysis in Guided mode": None,
    "Getting started": None,
    "Authentication": "authentication.md",
    "Measure the effect of a change": "causal-effects.md",
    "Workflows": "concepts/workflows.md",
}
"""Old title -> the page that it named (``None``: the text was the title of one page only)."""

#: The building blocks that the contributor page must show (docs/stylesheets/mono.css).
BLOCKS = (
    "mono-band",
    "mono-wrap",
    "mono-kicker",
    "mono-display",
    "mono-headline",
    "mono-lead",
    "mono-tiles",
    "mono-icon",
    "mono-actions",
    "mono-figures",
    "mono-steps",
    "mono-links",
    "mono-chip",
    "data-reveal",
)


def _text(path: Path) -> str:
    return path.read_text("utf-8")


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", _text(path))


def _web() -> str:
    return "\n".join(
        p.read_text("utf-8")
        for p in WEB.rglob("*.ts*")
        if p.is_file() and ".test." not in p.name and "mocks" not in p.parts
    )


def _docs_site() -> str:
    """What the docs site itself shows: the templates, the script and the configuration."""
    files = [*OVERRIDES.rglob("*.html"), DOCS / "javascripts" / "mono.js", MKDOCS]
    return "\n".join(p.read_text("utf-8") for p in files)


def _body(path: Path) -> str:
    """A page without its front matter, its code blocks and its HTML tags."""
    text = _text(path)
    if text.startswith("---\n"):
        text = text.split("---\n", 2)[2]
    text = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)
    return re.sub(r"<[^>]+>", "", text)


def _bold(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", b) for b in re.findall(r"\*\*([^*]+)\*\*", text)]


def _hand_written() -> list[Path]:
    return [p for p in sorted(DOCS.rglob("*.md")) if "generated" not in p.parts]


def _nav() -> str:
    return _text(MKDOCS).split("\nnav:\n", 1)[1]


# ------------------------------------------------------------------------------- the pages


def test_the_pages_are_in_the_nav() -> None:
    nav = _nav()
    for page in (WHATS_NEW, THEME_PAGE):
        rel = page.relative_to(DOCS).as_posix()
        assert page.is_file(), rel
        assert re.search(rf"[:\-]\s+{re.escape(rel)}\s*$", nav, re.M), f"{rel} is not in the nav"
    home = nav.split("  - Home:", 1)[1].split("\n  - ", 1)[0]
    # the newest release is the first entry of the group
    releases = re.findall(r"whats-new-[\d.]+\.md", home)
    assert releases[:2] == ["whats-new-0.21.md", "whats-new-0.20.md"]
    project = home.split("      - Project:\n", 1)[1]
    assert "project/docs-theme.md" in project, "the page is under Home > Project"


def test_the_release_page_follows_the_pattern() -> None:
    text = _text(WHATS_NEW)
    meta = yaml.safe_load(text.split("---\n", 2)[1])
    assert 50 <= len(meta["description"]) <= 300
    assert re.search(r'^# What\'s new in 0\.21 <span class="mapi-badge">v0\.21</span>$', text, re.M)
    # built from the building blocks of the new look, not from the old cards
    assert "mono-tiles" in text and "mono-links" in text
    assert "grid cards" not in text and ":material-" not in text and "octicons" not in text
    icons = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
    assert len(icons) >= 4
    for ref in icons:
        assert ref.startswith("assets/brand/icons/"), f"{ref}: no screenshot on this page yet"
        assert (DOCS / ref).is_file(), f"{ref} does not exist"
    for link in re.findall(r"(?<!\!)\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        if not re.match(r"[a-z]+:", link):
            assert (DOCS / link).is_file(), f"the page links a missing file: {link}"
    for heading in ("## Known gaps", "## For developers"):
        assert f"\n{heading}\n" in text, heading
    # the copy rules of the site
    for banned in ("→", "·", " via ", "e.g.", "simply", "please", "Please"):
        assert banned not in text, f"the page uses {banned!r}"


def test_the_release_page_says_what_is_true_about_the_limits() -> None:
    flat = _flat(WHATS_NEW)
    gaps = flat.split("## Known gaps", 1)[1]
    assert "200" in flat.split("## Known gaps", 1)[0], "the limit is also where search is described"
    for item in ("200", "Chromium", "Redoc", "screenshots"):
        assert item in gaps, f"Known gaps does not name {item}"
    # the limits in the code are the ones the page states
    rows = re.search(
        r"const GROUP_ROWS = (\d+);", _text(WEB / "components/shell/command-palette.tsx")
    )
    recents = re.search(r"RECENT_LIMIT = (\d+);", _text(WEB / "features/search/recents.ts"))
    assert rows is not None and recents is not None
    assert f"{rows.group(1)} results" in flat and f"last {recents.group(1)}" in flat
    remote = _text(WEB / "features/search/remote-sources.tsx")
    assert "200" in remote, "the search reads at most 200 items of a kind"


def test_the_home_page_names_the_new_release() -> None:
    home = _text(DOCS / "index.md")
    band = home.split('<p class="mono-kicker">What\'s new</p>', 1)[1].split("</section>", 1)[0]
    assert "(whats-new-0.21.md)" in band and "0.21" in band
    assert "whats-new-0.20.md" not in home


def test_the_readme_points_at_the_newest_release_page() -> None:
    readme = _text(ROOT / "README.md")
    assert "docs/whats-new-0.21.md" in readme
    assert "MEI / ODS" not in readme, "the look of v0.16 is not the look of the app now"


# ---------------------------------------------------------------------------------- labels


@pytest.mark.parametrize("page", list(APP_LABELS), ids=lambda p: p.name)
def test_the_pages_name_what_the_app_shows(page: Path) -> None:
    text, web = _flat(page), _web()
    for label in APP_LABELS[page]:
        assert label in text, f"{page.name} does not name {label!r}"
        assert label in web, f"{page.name} names {label!r}, which the web app does not show"


def test_each_bold_label_of_the_release_page_exists() -> None:
    """On this page bold text is a control: a button, a field, a group or a menu item."""
    web, site = _web(), _docs_site()
    # in a link row (mono-links) the bold text is the title of the page that the row opens
    body = re.sub(r"\[[^\]]*\]\([^)]*\)", "", _body(WHATS_NEW))
    labels = _bold(body)
    assert len(labels) >= 12
    for label in labels:
        for part in label.split(" / "):
            assert part in web or part in site, (
                f"whats-new-0.21.md shows {part!r} as a control, which the app and the docs "
                "site do not have"
            )
    flat = _flat(WHATS_NEW)
    for label in DOCS_SITE_LABELS:
        assert label in flat and label in site, label


def test_each_bold_label_of_the_shell_section_exists() -> None:
    web = _web()
    shell = _text(WEB_UI).split("\n## The shell\n", 1)[1].split("\n## ", 1)[0]
    labels = _bold(shell)
    assert len(labels) >= 10
    for label in labels:
        for part in label.split(" / "):
            assert part in web, (
                f"web-ui.md shows {part!r} in the shell, which the app does not have"
            )


def test_the_docs_name_no_jump_to_label() -> None:
    for page in _hand_written():
        text = _text(page)
        assert "Jump to" not in text, f"{page.relative_to(ROOT)} names the old label"
    # the app has no such label, so a page that named it would send the reader nowhere
    assert "Jump to" not in _web()
    for page in (WEB_UI, DOCS / "reference" / "web-app.md"):
        assert "command palette" not in _text(page).lower(), page.name


def test_the_docs_do_not_call_the_old_looks_current() -> None:
    for page in (WEB_UI, DOCS / "reference" / "web-app.md", DOCS / "guides" / "start-here.md"):
        text = _text(page)
        for old in ("MEI / ODS", "Studio look", "Bricolage", "lime"):
            assert old not in text, f"{page.name} names {old!r}"


def test_no_link_uses_an_old_page_title() -> None:
    link = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
    for page in _hand_written():
        if page.name.startswith("whats-new-0.") and page != WHATS_NEW:
            continue  # a release page keeps the words of its release
        folder = page.parent.relative_to(DOCS).as_posix()
        for label, target in link.findall(_text(page)):
            if label not in OLD_LINK_TEXT:
                continue
            named = OLD_LINK_TEXT[label]
            path = target.split("#", 1)[0]
            resolved = (page.parent / path).resolve().relative_to(DOCS.resolve()).as_posix()
            stale = named is None or resolved.endswith(named)
            # "Authentication" was the title of the guide and a short name of the operator page
            if label == "Authentication":
                stale = resolved in ("guides/authentication.md", "operations/authentication.md")
            assert not stale, f"{folder}/{page.name}: [{label}]({target}) uses an old title"


def test_the_roadmap_page_calls_released_work_released() -> None:
    text = _text(ROADMAP)
    head = text.split("\n", 1)[0]
    assert "mapi-badge--soon" not in head and "planned" not in head
    assert "upload is planned" not in text
    # the anchors that other pages and old links use stay
    assert "{ #planned-for-v020 }" in text and 'id="planned-for-v018"' in text
    flat = _flat(ROADMAP)
    assert "whats-new-0.19.md" in flat and "whats-new-0.20.md" in flat


# ------------------------------------------------------------------- the contributor page


def test_the_contributor_page_shows_each_building_block() -> None:
    text = _text(THEME_PAGE)
    css = "\n".join(p.read_text("utf-8") for p in sorted((DOCS / "stylesheets").glob("*.css")))
    for block in BLOCKS:
        assert block in text, f"docs-theme.md does not show {block}"
        assert block in css, f"{block} has no rule"
    # each class that an example on the page uses has a rule
    # (a stylesheet such as mono-blocks.css is a file name, not a class)
    for name in set(re.findall(r"(?<![\w-])(mono-[a-z0-9-]+|tex-[a-z]+)(?![\w-]|\.css)", text)):
        if name.endswith("-"):
            continue
        assert re.search(rf"\.{re.escape(name)}(?![\w-])", css), f".{name} has no rule"
    for item in (
        "docs/stylesheets/mono.css",
        "apps/web/src/app.css",
        "TASK_PAGES",
        "search_popular",
        "SECTION_LABELS",
        "mono_sections",
        "PROVENANCE.md",
        "scripts/vendor_mermaid.py",
        "OFL-1.1",
        "gen_docs_search_index.py",
    ):
        assert item in text, f"docs-theme.md does not name {item}"
    assert "Chrome is monochrome. Colour is for data." in _flat(THEME_PAGE)


def test_the_contributor_page_names_files_that_exist() -> None:
    text = _text(THEME_PAGE)
    paths = set(
        re.findall(
            r"`((?:docs|overrides|scripts|conformance|apps)/[A-Za-z0-9_./-]+\.[a-z0-9]+)`", text
        )
    )
    assert len(paths) >= 12
    for path in sorted(paths):
        assert (ROOT / path).is_file(), f"docs-theme.md names a missing file: {path}"
    for image in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
        assert (THEME_PAGE.parent / image).resolve().is_file(), image


def test_the_licensing_page_names_the_vendored_assets_and_the_art() -> None:
    flat = _flat(LICENSING)
    for item in (
        "Inter",
        "JetBrains Mono",
        "OFL-1.1",
        "mermaid",
        "MIT",
        "Redoc",
        "docs/assets/fonts/LICENSE.txt",
        "docs/assets/brand/PROVENANCE.md",
        "apps/web/src/assets/brand/PROVENANCE.md",
    ):
        assert item in flat, f"licensing.md does not name {item}"
    for path in re.findall(r"`((?:docs|apps)/[A-Za-z0-9_./-]+\.(?:md|txt))`", _text(LICENSING)):
        assert (ROOT / path).is_file(), f"licensing.md names a missing file: {path}"
    assert "(docs-theme.md)" in _text(DOCS / "project" / "contributing.md")


def test_the_contributor_page_is_last_in_search() -> None:
    """A reader who types "theme" wants the theme of the app, not the page for contributors."""
    path = ROOT / "scripts" / "mkdocs_hooks.py"
    spec = importlib.util.spec_from_file_location("mkdocs_hooks_v021", path)
    assert spec is not None and spec.loader is not None
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    assert hooks.search_boost("project/docs-theme.md", DOCS) == 0.2
    # the pages beside it keep the default, and the two newest releases too
    assert hooks.search_boost("project/writing-style.md", DOCS) is None
    assert hooks.search_boost("whats-new-0.21.md", DOCS) is None
    assert hooks.search_boost("whats-new-0.20.md", DOCS) is None
    assert hooks.search_boost("whats-new-0.19.md", DOCS) == 0.2


# ---------------------------------------------------------------------- make and changelog


def test_make_docs_test_runs_the_theme_the_nav_and_the_release_tests() -> None:
    makefile = _text(ROOT / "Makefile")
    docs_test = makefile.split("\ndocs-test:", 1)[1].split("\n\n", 1)[0]
    for name in (
        "test_docs_theme.py",
        "test_docs_nav.py",
        "test_docs_search_index.py",
        "test_docs_v021.py",
    ):
        assert f"conformance/tests/{name}" in docs_test, name
        assert (ROOT / "conformance" / "tests" / name).is_file(), name
    # the files that the target ran before are still there
    for name in ("test_docs_snippets.py", "test_docs_platform.py", "test_docs_chat_mcp.py"):
        assert f"conformance/tests/{name}" in docs_test, name


def test_changelog_has_the_v021_section() -> None:
    text = _text(ROOT / "CHANGELOG.md")
    unreleased = text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert TITLE in unreleased
    assert unreleased.index("(v0.21)") < unreleased.index("(v0.20)")
    section = unreleased.split(TITLE, 1)[1].split("\n### ", 1)[0]
    headings = re.findall(r"^#### (.+)$", section, re.M)
    assert headings == ["Added", "Changed", "Known gaps"]
    flat = re.sub(r"\s+", " ", section)
    changed = flat.split("#### Changed", 1)[1].split("#### Known gaps", 1)[0]
    for item in (
        "`mapi_plots.palette`",
        "Bricolage Grotesque",
        "mermaid",
        "page titles",
        "separator",
        '"Jump to…"',
        "illustration",
    ):
        assert item in changed, f"Changed does not name {item}"
    gaps = flat.split("#### Known gaps", 1)[1]
    for item in ("200", "screenshots", "Redoc", "Chromium"):
        assert item in gaps, f"Known gaps does not name {item}"
