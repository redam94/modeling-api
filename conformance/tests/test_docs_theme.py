"""The docs theme (the "Mono" look) is checked like code.

Fast and with no network. One test builds three small pages with the templates in a temporary
directory; no test builds the docs:

* ``mkdocs.yml`` uses the ``overrides/`` directory and does not ask Material for Google Fonts;
* nothing in the theme layer (``overrides/``, ``docs/stylesheets/``, ``docs/javascripts/``,
  ``extra_css``, ``extra_javascript``) names another host, so a page loads from this site only;
* the fonts are self-hosted with their licence, and every ``@font-face`` file exists;
* the vendored mermaid bundle matches the hash pinned in ``scripts/vendor_mermaid.py``;
* the overrides keep the hooks that Material's own JavaScript needs;
* the header row has three columns, so the tabs are in the centre of the page;
* every custom class that a page uses still has a rule;
* the theme only points at brand art that is in the agreed manifest;
* the popular search links point at pages that exist;
* a built page has one ``<h1>`` inside ``<main>``, and "Skip to content" goes to the article;
* the focus rings have room, a link row stays in one piece with code in it, the art of a band
  stays clear of the figures, and print uses the light colours;
* a sequence diagram has no semicolon in a message (mermaid ends the statement there).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
OVERRIDES = ROOT / "overrides"
STYLES = DOCS / "stylesheets"
SCRIPTS = DOCS / "javascripts"
FONTS = DOCS / "assets" / "fonts"
MKDOCS = ROOT / "mkdocs.yml"

#: Hosts that may appear as text because they are XML namespaces, never fetched.
NAMESPACE_HOSTS = {"www.w3.org"}

#: Theme features the site had before the retheme; the retheme must not drop one.
#: (``search.suggest`` is gone on purpose: the Mono search dialog draws no inline suggestion.)
KEPT_FEATURES = {
    "navigation.tabs",
    "navigation.tabs.sticky",
    "navigation.sections",
    "navigation.indexes",
    "navigation.top",
    "navigation.footer",
    "search.highlight",
    "content.code.copy",
    "content.code.annotate",
    "content.tabs.link",
    "toc.follow",
}

#: Custom classes that Markdown pages depend on.
PAGE_CLASSES = (
    "mapi-badge",
    "mapi-badge--soon",
    "mapi-shot",
    "mapi-shots",
    "mapi-hero",
    "mapi-tagline",
    "mapi-personas",
    "mapi-persona",
    "mapi-persona--expert",
    "mapi-promise",
    "mapi-stats",
    "mapi-stat",
    "mapi-path",
    "mapi-compare",
    "mapi-openapi",
)

#: Building blocks that later pages use from Markdown (attr_list / md_in_html).
BLOCK_CLASSES = (
    "mono-band",
    "mono-band--alt",
    "mono-band--dark",
    "mono-band--hero",
    "mono-wrap",
    "mono-wrap--narrow",
    "mono-kicker",
    "mono-display",
    "mono-headline",
    "mono-lead",
    "mono-tiles",
    "mono-tile",
    "mono-icon",
    "mono-actions",
    "mono-figures",
    "mono-figure-num",
    "mono-steps",
    "mono-links",
    "mono-chip",
)

#: The generated art the theme may point at (``docs/assets/brand/``).
BRAND_MANIFEST = {
    *(
        f"hero-{name}.webp"
        for name in ("home", "learn", "guides", "extend", "reference", "operate", "404")
    ),
    *(f"tex-{name}.webp" for name in ("contours", "lattice", "curves")),
    "bg-card.webp",
}
BRAND_ICON = re.compile(r"icons/[a-z0-9-]+\.webp")


class _Loader(yaml.SafeLoader):
    """``mkdocs.yml`` names Python objects (``!!python/name:…``); read them as plain text."""


_Loader.add_multi_constructor(
    "tag:yaml.org,2002:python/", lambda _loader, suffix, _node: f"python/{suffix}"
)


def _config() -> dict[str, Any]:
    return yaml.load(MKDOCS.read_text("utf-8"), Loader=_Loader)  # a SafeLoader subclass


def _theme_files() -> list[Path]:
    files: list[Path] = []
    for base in (OVERRIDES, STYLES, SCRIPTS):
        files += [p for p in sorted(base.rglob("*")) if p.is_file() and p.name != ".DS_Store"]
    return files


def _hosts(text: str) -> set[str]:
    return set(re.findall(r"(?:https?:)?//([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)", text))


def _css() -> str:
    return "\n".join(p.read_text("utf-8") for p in sorted(STYLES.glob("*.css")))


def _load(script: str) -> ModuleType:
    path = ROOT / "scripts" / script
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------------------ mkdocs.yml


def test_theme_uses_the_overrides_and_no_google_fonts() -> None:
    theme = _config()["theme"]
    assert theme["custom_dir"] == "overrides"
    # ``font: false`` stops Material from linking fonts.googleapis.com on every page.
    assert theme["font"] is False
    assert (DOCS / theme["logo"]).is_file() and (DOCS / theme["favicon"]).is_file()
    features = set(theme["features"])
    assert features >= KEPT_FEATURES, f"features dropped: {sorted(KEPT_FEATURES - features)}"
    assert "navigation.path" in features  # breadcrumbs
    # the dialog has no element for the inline suggestion, so the worker must not compute one
    assert "search.suggest" not in features
    search = (OVERRIDES / "partials" / "search.html").read_text("utf-8")
    assert "search-suggest" not in search
    # the light and dark palettes and their toggle stay
    assert [p["scheme"] for p in theme["palette"]] == ["default", "slate"]
    assert all("toggle" in p and "prefers-color-scheme" in p["media"] for p in theme["palette"])
    # the docs tests read this file as text and depend on two-space indentation
    text = MKDOCS.read_text("utf-8")
    assert "\n  custom_dir: overrides\n" in text and "\n  font: false\n" in text


def test_extra_assets_are_local_files() -> None:
    config = _config()
    entries = [*config["extra_css"], *config["extra_javascript"]]
    assert entries
    for entry in entries:
        assert isinstance(entry, str), entry
        assert not re.match(r"(?:[a-z]+:)?//", entry), f"{entry} is not a local path"
        assert (DOCS / entry).is_file(), f"{entry} does not exist under docs/"
    assert any(e.endswith(".css") for e in config["extra_css"])


def test_theme_layer_names_no_other_host() -> None:
    files = _theme_files()
    assert any(p.suffix == ".html" for p in files) and any(p.suffix == ".css" for p in files)
    assert any(p.suffix == ".js" for p in files)
    for path in files:
        if path.suffix not in {".html", ".css", ".js", ".svg", ".json", ".txt"}:
            continue
        hosts = _hosts(path.read_text("utf-8")) - NAMESPACE_HOSTS
        assert not hosts, f"{path.relative_to(ROOT)} names {sorted(hosts)}"


# ----------------------------------------------------------------------------------- fonts


def test_fonts_are_self_hosted_with_their_licence() -> None:
    files = sorted(p.name for p in FONTS.glob("*.woff2"))
    assert any(n.startswith("inter-latin-wght") for n in files), files
    assert any(n.startswith("inter-latin-ext-wght") for n in files), files
    assert any(n.startswith("jetbrains-mono-latin-wght") for n in files), files
    assert any(n.startswith("jetbrains-mono-latin-ext-wght") for n in files), files
    licence = (FONTS / "LICENSE.txt").read_text("utf-8")
    assert "SIL OPEN FONT LICENSE" in licence.upper()
    assert "Inter" in licence and "JetBrains Mono" in licence

    css = _css()
    faces = re.findall(r"@font-face\s*\{[^}]*\}", css)
    referenced = set()
    for face in faces:
        for ref in re.findall(r"url\(([^)]+)\)", face):
            ref = ref.strip("\"'")
            target = (STYLES / ref).resolve()
            assert target.is_file(), f"@font-face points at a missing file: {ref}"
            assert target.parent == FONTS.resolve(), ref
            referenced.add(target.name)
        assert "font-display" in face
    assert referenced == set(files), "every font file is declared, and every declaration exists"
    assert '"Inter Variable"' in css and '"JetBrains Mono Variable"' in css


# --------------------------------------------------------------------------------- mermaid


def test_mermaid_bundle_is_pinned_and_local() -> None:
    vendor = _load("vendor_mermaid.py")
    data = vendor.BUNDLE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == vendor.BUNDLE_SHA256
    assert (vendor.DEST / "LICENSE.txt").read_text("utf-8").startswith("The MIT License")
    local = vendor.BUNDLE.relative_to(DOCS).as_posix()
    # The bundle is 3.5 MB, so it is not in extra_javascript (that loads on each page).
    # main.html loads it on a page that has a diagram, before Material's own script: mermaid
    # must be defined when Material mounts the page, or Material fetches it from unpkg.
    scripts = _config()["extra_javascript"]
    assert not any("mermaid" in str(s) for s in scripts), scripts
    main = (OVERRIDES / "main.html").read_text("utf-8")
    assert "'class=\"mermaid\"' in page.content" in main
    block = main.split("{% block scripts %}", 1)[1].split("{% endblock %}", 1)[0]
    tag = f"<script src=\"{{{{ '{local}' | url }}}}\"></script>"
    assert tag in block, f"main.html does not load {local}"
    assert "{% if mono_has_diagram %}" in block
    assert block.index(tag) < block.index("{{ super() }}"), "mermaid loads before the bundle"
    # the diagrams that the pages have use the fence that makes class="mermaid"
    fenced = [p for p in DOCS.rglob("*.md") if "```mermaid" in p.read_text("utf-8")]
    assert len(fenced) >= 10
    assert "class: mermaid" in MKDOCS.read_text("utf-8")
    for path in _theme_files():
        if path.suffix in {".html", ".js", ".css"}:
            text = path.read_text("utf-8")
            assert "unpkg" not in text and "mermaid@" not in text, path.name


# -------------------------------------------------------------------------------- overrides


def test_overrides_keep_the_hooks_material_needs() -> None:
    for name in ("main.html", "home.html", "404.html"):
        assert (OVERRIDES / name).is_file(), name
    assert '{% extends "base.html" %}' in (OVERRIDES / "main.html").read_text("utf-8")
    assert '{% extends "main.html" %}' in (OVERRIDES / "home.html").read_text("utf-8")
    assert '{% extends "main.html" %}' in (OVERRIDES / "404.html").read_text("utf-8")

    header = (OVERRIDES / "partials" / "header.html").read_text("utf-8")
    for hook in (
        'data-md-component="header"',
        'data-md-component="logo"',
        'for="__drawer"',
        'for="__search"',
        "partials/palette.html",
        "partials/tabs.html",
    ):
        assert hook in header, f"header.html lost {hook}"

    search = (OVERRIDES / "partials" / "search.html").read_text("utf-8")
    for hook in (
        'data-md-component="search"',
        'name="search"',
        'name="query"',
        'data-md-component="search-query"',
        'data-md-component="search-result"',
        'class="md-search-result__meta"',
        'class="md-search-result__list"',
        'for="__search"',
    ):
        assert hook in search, f"search.html lost {hook}"


def test_the_header_tabs_are_centred_on_the_page() -> None:
    """The row has three columns: the brand, the tabs and the tools. The two side columns
    have the same width, so the tabs are in the centre of the page and not in the centre of
    the space that the brand and the tools leave."""
    chrome = (STYLES / "mono-chrome.css").read_text("utf-8")
    desktop = re.search(
        r"@media screen and \(min-width: 76\.25em\) \{\s*"
        r"\.md-header__inner\.mono-header__inner \{([^}]*)\}",
        chrome,
    )
    assert desktop, "the header row has no desktop layout"
    rule = desktop.group(1)
    assert "display: grid" in rule
    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)" in rule
    # the tools go to the end of their column, and the tabs no longer centre with margins
    tools = re.search(r"\.mono-header__tools \{([^}]*)\}", chrome)
    assert tools is not None and "justify-self: end" in tools.group(1)
    tabs = re.search(r"\n\.mono-tabs \{([^}]*)\}", chrome)
    assert tabs is not None and "margin: 0 auto" not in tabs.group(1)
    # below the breakpoint the tabs are in the drawer, as before
    assert re.search(
        r"@media screen and \(max-width: 76\.234375em\) \{\s*\.mono-tabs \{\s*display: none;",
        chrome,
    )


def test_overrides_and_scripts_are_csp_friendly() -> None:
    for path in sorted(OVERRIDES.rglob("*.html")):
        text = path.read_text("utf-8")
        assert not re.search(r"\son[a-z]+\s*=", text), f"{path.name} has an inline handler"
        assert "javascript:" not in text, path.name
        for attrs in re.findall(r"<script\b([^>]*)>", text):
            assert 'type="application/json"' in attrs or "src=" in attrs, (
                f"{path.name} has an inline script"
            )
    scripts = sorted(SCRIPTS.glob("*.js"))
    assert scripts
    for path in scripts:
        text = path.read_text("utf-8")
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert banned not in text, f"{path.name} uses {banned}"
        assert not re.search(r"\bnew Function\b", text), path.name


def test_search_section_labels_are_one_small_map() -> None:
    text = (SCRIPTS / "mono.js").read_text("utf-8")
    match = re.search(r"var SECTION_LABELS = (\{[^}]*\});", text)
    assert match, "mono.js must keep the SECTION_LABELS object for the search section labels"
    labels = json.loads(match.group(1))
    assert labels["guides"] == "Guides" and labels["reference"] == "Reference"
    # every key is a top-level docs folder, so a renamed folder cannot leave a dead entry
    for folder in labels:
        assert (DOCS / folder).is_dir(), f"SECTION_LABELS names a missing folder: {folder}"


def test_search_popular_links_point_at_pages() -> None:
    popular = _config()["extra"]["search_popular"]
    nav = MKDOCS.read_text("utf-8").split("\nnav:", 1)[1]
    assert 4 <= len(popular) <= 8
    for entry in popular:
        assert set(entry) == {"title", "url", "hint"}, entry
        assert entry["title"].strip() and entry["hint"].strip()
        url = entry["url"]
        assert url.endswith("/") and not url.startswith(("/", "http")), url
        stem = url.rstrip("/")
        sources = (f"{stem}.md", f"{stem}/index.md")
        # a page under reference/generated/ exists only after a build, so the nav counts too
        listed = [re.search(rf"[:\-]\s+{re.escape(s)}\s*$", nav, re.M) for s in sources]
        assert any((DOCS / s).is_file() for s in sources) or any(listed), url
    # the template embeds the list as JSON for the search script
    search = (OVERRIDES / "partials" / "search.html").read_text("utf-8")
    assert "config.extra.search_popular" in search and 'type="application/json"' in search


# -------------------------------------------------------------------------------- classes


def test_custom_classes_that_pages_use_keep_a_rule() -> None:
    css = _css()
    pages = "\n".join(p.read_text("utf-8") for p in DOCS.rglob("*.md"))
    used = [c for c in PAGE_CLASSES if re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", pages)]
    assert len(used) >= 10, used
    for name in used:
        assert re.search(rf"\.{re.escape(name)}(?![\w-])", css), f".{name} has no rule"


def test_building_blocks_are_defined() -> None:
    css = _css()
    for name in BLOCK_CLASSES:
        assert re.search(rf"\.{re.escape(name)}(?![\w-])", css), f".{name} has no rule"
    assert "[data-reveal]" in css and "prefers-reduced-motion" in css and "@media print" in css
    assert ":focus-visible" in css
    for scheme in ("default", "slate"):
        assert f'[data-md-color-scheme="{scheme}"]' in css


# ------------------------------------------------------------------------------- brand art


def test_brand_art_paths_are_in_the_manifest() -> None:
    refs: set[str] = set()
    for path in _theme_files():
        if path.suffix in {".html", ".css", ".js"}:
            refs |= set(re.findall(r"assets/brand/([A-Za-z0-9_./-]+)", path.read_text("utf-8")))
    assert refs, "the theme uses no brand art"
    unknown = {r for r in refs if r not in BRAND_MANIFEST and not BRAND_ICON.fullmatch(r)}
    assert not unknown, f"not in the art manifest: {sorted(unknown)}"


# ---------------------------------------------------------------------------- built pages


class _Outline(HTMLParser):
    """The start tags of a page, each with the tags that it is inside."""

    VOID = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "source",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.nodes: list[tuple[str, dict[str, str | None], tuple[str, ...]]] = []
        self.unbalanced: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.nodes.append((tag, dict(attrs), tuple(self.stack)))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.unbalanced.append(f"</{tag}> closes <{self.stack[-1] if self.stack else None}>")
        if tag in self.stack:
            while self.stack.pop() != tag:
                pass

    def find(self, tag: str, **attrs: str) -> list[tuple[dict[str, str | None], tuple[str, ...]]]:
        return [
            (a, inside)
            for name, a, inside in self.nodes
            if name == tag and all(a.get(k.rstrip("_")) == v for k, v in attrs.items())
        ]


def _build_small_site(tmp: Path) -> Path:
    """Three pages with the real templates: a full-bleed home page, a page that starts with
    its title, and a page with no title."""
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    docs = tmp / "docs"
    (docs / "guides").mkdir(parents=True)
    (docs / "index.md").write_text("---\ntemplate: home.html\n---\n\n# Home\n\nText.\n", "utf-8")
    (docs / "guides" / "one.md").write_text(
        "# A guide\n\nText with a [link](../index.md).\n\n## Part one\n\nMore text.\n", "utf-8"
    )
    (docs / "guides" / "bare.md").write_text("Text with no title.\n\n## Part\n", "utf-8")
    theme = _config()["theme"]
    config = {
        "site_name": "test",
        "docs_dir": str(docs),
        "site_dir": str(tmp / "site"),
        "theme": {
            "name": "material",
            "custom_dir": str(OVERRIDES),
            "font": False,
            "features": theme["features"],
        },
        "markdown_extensions": [{"toc": {"permalink": True}}, "attr_list", "md_in_html"],
        "nav": [{"Home": ["index.md"]}, {"Guides": ["guides/one.md", "guides/bare.md"]}],
    }
    (tmp / "mkdocs.yml").write_text(yaml.safe_dump(config), "utf-8")
    build(load_config(str(tmp / "mkdocs.yml")))
    return tmp / "site"


def test_a_built_page_has_its_title_in_main_and_a_skip_link_to_the_article(tmp_path: Path) -> None:
    """The title of a page is in the head band, above the two sidebars. The band is inside
    ``<main>``, and "Skip to content" goes to the article, which is after the sidebars."""
    site = _build_small_site(tmp_path)
    pages = ("guides/one/index.html", "guides/bare/index.html", "index.html", "404.html")
    for name in pages:
        page = _Outline()
        page.feed((site / name).read_text("utf-8"))
        assert not page.unbalanced, f"{name}: {page.unbalanced[:3]}"
        assert len(page.find("main")) == 1, name
        titles = page.find("h1")
        assert len(titles) == 1, f"{name} has {len(titles)} <h1>"
        assert "main" in titles[0][1], f"{name}: the <h1> is not inside <main>"
        # the one link that the reader gets: it points at the article, inside <main>
        skips = [a for a, _ in page.find("a") if "mono-skip" in (a.get("class") or "")]
        assert [a.get("href") for a in skips] == ["#mono-content"], name
        targets = page.find("article", id="mono-content")
        assert len(targets) == 1 and "main" in targets[0][1], name
        assert targets[0][0].get("tabindex") == "-1", "the article takes the focus"
        # the link is before the header, so it is the first stop of the Tab key
        order = [
            n for n, a, _ in page.nodes if n == "header" or "mono-skip" in (a.get("class") or "")
        ]
        assert order[:2] == ["a", "header"], name

    # a page that starts with its title: the title is in the band, before the two sidebars,
    # and the article does not print it again
    one = _Outline()
    one.feed((site / "guides/one/index.html").read_text("utf-8"))
    names = [
        "band" if "mono-pagehead" in (a.get("class") or "").split() else "sidebar"
        for n, a, _ in one.nodes
        if n == "div" and {"mono-pagehead", "md-sidebar"} & set((a.get("class") or "").split())
    ]
    assert names == ["band", "sidebar", "sidebar"]
    band = one.find("div", class_="mono-pagehead")[0]
    assert band[1][-1] == "main", "the band is a child of <main>, outside the grid"
    assert all("article" not in inside for _, inside in one.find("h1"))
    assert one.find("h2", id="part-one"), "the headings keep their anchors"
    # a page with no title in its content gets one in the article, and no band
    bare = _Outline()
    bare.feed((site / "guides/bare/index.html").read_text("utf-8"))
    assert not bare.find("div", class_="mono-pagehead")
    assert "article" in bare.find("h1")[0][1]

    # the stylesheet hides Material's own link, which goes to the title, and the empty grid
    # that main.html leaves before the band
    css = _css()
    assert re.search(r'\[data-md-component="skip"\] \{\s*display: none;', css)
    assert re.search(r"\.md-main__inner:has\(\+ \.mono-pagehead\) \{\s*display: none;", css)
    main = (OVERRIDES / "main.html").read_text("utf-8")
    assert "{% block hero %}" not in main, "the band is not in the hero block (before <main>)"


def _rule(css: str, selector: str) -> str:
    """The declarations of the first rule whose selector list ends with ``selector``."""
    match = re.search(rf"(?:^|[\n,}}]) *{re.escape(selector)} \{{([^}}]*)\}}", css)
    assert match, f"no rule for {selector}"
    return match.group(1)


def _px(rule: str, name: str) -> float:
    match = re.search(rf"(?<![\w-]){re.escape(name)}: (-?[\d.]+)(?:px)?;", rule)
    assert match, f"{name} is not in {rule!r}"
    return float(match.group(1))


def test_focus_rings_are_not_clipped_in_the_header_and_the_sidebars() -> None:
    css = _css()
    ring = _rule(css, ".md-typeset .md-button:focus-visible")
    width = re.search(r"outline: (\d+)px solid", ring)
    assert width, "the ring is an outline"
    reach = _px(ring, "outline-offset") + float(width.group(1))
    # the row of tabs scrolls sideways, so it clips: its padding is the room for the ring
    assert "overflow-x: auto" in _rule(css, ".mono-tabs")
    assert _px(_rule(css, ".mono-tabs__list"), "padding") >= reach
    # the lists of the sidebars clip too: the ring of a link there is inside its own box
    assert _px(_rule(css, ".md-nav__link.focus-visible"), "outline-offset") < 0
    # the panes fade at the top and at the bottom; the padding of a pane holds the two fades,
    # and a link that gets the focus does not stop in a fade
    pane = _rule(css, ".md-sidebar--secondary .md-sidebar__scrollwrap")
    fade = re.search(
        r"mask-image: linear-gradient\(180deg, transparent 0, #000 (\d+)px, "
        r"#000 calc\(100% - (\d+)px\)",
        pane,
    )
    assert fade, "the panes of the sidebars have no fade"
    top, bottom = (float(g) for g in fade.groups())
    assert _px(pane, "padding-top") >= top and _px(pane, "padding-bottom") >= bottom
    pad = re.search(r"scroll-padding: (\d+)px 0 (\d+)px;", pane)
    assert pad and float(pad.group(1)) >= top and float(pad.group(2)) >= bottom


def test_a_link_row_stays_in_one_piece_with_code_in_it() -> None:
    """As a flex column, each inline part of the description (a name in code) was a row."""
    css = _css()
    row = _rule(css, ".md-typeset :is(ul.mono-links, .mono-links > ul) > li > a")
    assert "display: block" in row and "flex" not in row
    title = _rule(css, ".md-typeset :is(ul.mono-links, .mono-links > ul) > li > a :is(b, strong)")
    assert "display: block" in title
    assert "var(--mono-ink-2)" in _rule(
        css, ".md-typeset :is(ul.mono-links, .mono-links > ul) > li > a code"
    )
    # the pages do use code in a row, so the rule has a reader
    rows = [
        line
        for page in ("guides/index.md", "reference/index.md")
        for line in (DOCS / page).read_text("utf-8").splitlines()
        if line.startswith("- [**") and "`" in line
    ]
    assert len(rows) >= 5


def test_the_art_of_a_left_band_stays_clear_of_its_figures() -> None:
    """The backdrops have their art in the right 23% of the file (docs/project/docs-theme.md:
    an empty black centre). The row of figures in such a band ends before the art starts."""
    css = _css()
    figures = _rule(css, ".mono-band--hero.mono-band--left .mono-figures")
    limit = re.search(
        r"max-width: calc\((\d+)vw - max\(var\(--mono-gutter\), 50vw - var\(--mono-wrap\) / 2\)\);",
        figures,
    )
    assert limit, "the row of figures has no limit in a left band"
    assert int(limit.group(1)) <= 75, "the art starts at 77% of the width"
    assert css.index("@media screen and (min-width: 53.76em)") < css.index(figures)
    # the home page is the page that has such a band
    home = (DOCS / "index.md").read_text("utf-8")
    band = re.search(
        r'<section class="([^"]*mono-band--left[^"]*)"[^>]*>(.*?)</section>', home, re.S
    )
    assert band and "mono-band--hero" in band.group(1) and 'class="mono-figures"' in band.group(2)


def test_print_uses_the_light_colours_in_the_dark_scheme() -> None:
    """The dark tokens are for the screen. On paper a page in the dark scheme, and a dark
    band, take the light tokens of ``:root``."""
    mono = (STYLES / "mono.css").read_text("utf-8")
    dark = mono.index('[data-md-color-scheme="slate"],\n  .mono-band--dark {')
    before = mono[:dark]
    assert before.rstrip().endswith("@media not print {"), "the dark tokens are also for print"
    block = mono[dark:].split("\n  }\n", 1)[0]
    light = mono.split('[data-md-color-scheme="default"] {', 1)[1].split("\n}\n", 1)[0]
    names = set(re.findall(r"(--mono-[a-z0-9-]+):", block))
    assert len(names) >= 30 and names <= set(re.findall(r"(--mono-[a-z0-9-]+):", light)), (
        "each dark token has a light value to fall back on"
    )
    # no art and no dark fill on paper: the print rule names each selector that draws them
    blocks = (STYLES / "mono-blocks.css").read_text("utf-8")
    screen, printed = blocks.split("@media print {", 1)
    for selector in (
        '[data-md-color-scheme="slate"] .mono-band--hero',
        ".mono-band--dark.mono-band--hero",
        '[data-md-color-scheme="slate"] .mono-band--hero.mono-band--left',
        ".mono-band--dark.mono-band--hero.mono-band--left",
        '[data-md-color-scheme="slate"] .mono-band:is(.tex-contours, .tex-lattice, .tex-curves)',
        ".mono-band--dark:is(.tex-contours, .tex-lattice, .tex-curves)",
    ):
        assert f"\n{selector}" in screen, f"{selector} draws no art now: remove it here"
        assert re.search(rf"\n  {re.escape(selector)}(?:,| \{{)\n", printed), (
            f"print does not reset {selector}"
        )
    # the last selector of that list opens the rule
    assert "background: none;" in _rule(
        printed, ".mono-band--dark:is(.tex-contours, .tex-lattice, .tex-curves)"
    )


def test_a_title_in_code_is_plain_text_in_the_head_band() -> None:
    """The title of a Python API page is a name in code. In the head band it is plain
    monospace, not a grey chip of 40 px."""
    code = _rule((STYLES / "mono-chrome.css").read_text("utf-8"), ".mono-pagehead__title > h1 code")
    assert "background: none" in code and "padding: 0" in code


def test_a_sequence_diagram_has_no_semicolon_in_a_message() -> None:
    """mermaid ends a statement of a sequence diagram at a semicolon. The rest of the line is
    then a statement that it cannot read, and the page shows the source and not the diagram."""
    fence = re.compile(r"^( *)```mermaid\n(.*?)^\1```", re.M | re.S)
    count = 0
    for page in sorted(DOCS.rglob("*.md")):
        text = page.read_text("utf-8")
        for match in fence.finditer(text):
            body = match.group(2)
            if body.split("\n", 1)[0].strip() != "sequenceDiagram":
                continue
            count += 1
            for line in body.splitlines():
                bare = re.sub(r"#\d+;|&[a-z]+;", "", line)  # a character entity is not a separator
                assert ";" not in bare, f"{page.relative_to(ROOT)}: {line.strip()}"
    assert count >= 5, "the docs have sequence diagrams"
