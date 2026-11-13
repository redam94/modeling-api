"""The docs theme (the "Mono" look) is checked like code.

Fast, no network and no build:

* ``mkdocs.yml`` uses the ``overrides/`` directory and does not ask Material for Google Fonts;
* nothing in the theme layer (``overrides/``, ``docs/stylesheets/``, ``docs/javascripts/``,
  ``extra_css``, ``extra_javascript``) names another host, so a page loads from this site only;
* the fonts are self-hosted with their licence, and every ``@font-face`` file exists;
* the vendored mermaid bundle matches the hash pinned in ``scripts/vendor_mermaid.py``;
* the overrides keep the hooks that Material's own JavaScript needs;
* every custom class that a page uses still has a rule;
* the theme only points at brand art that is in the agreed manifest;
* the popular search links point at pages that exist.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
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
