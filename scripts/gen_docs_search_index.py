"""Generate the documentation index of the web app's global search.

The web app cannot fetch the docs site's search index (its Content-Security-Policy allows
the API origins only), so the app ships a small index made from the docs at build time:

    apps/web/src/features/search/docs-index.gen.json

One entry for each page in the ``nav`` of ``mkdocs.yml``:

- ``t``: the title (front-matter ``title``, else the first H1, else the nav title);
- ``s``: the section tab (the top-level nav entry the page is under);
- ``p``: the path in the built site, ``""`` for the home page, else it ends in ``/``;
- ``h``: the H2 headings as ``[text, anchor]``.

Not in the index: the generated reference (``reference/api/*``, ``reference/generated/*``),
the changelog, and the "What's new" pages older than the newest two.

Run ``uv run python scripts/gen_docs_search_index.py`` after a change to the nav, a page
title or an H2 heading. ``--check`` writes nothing and exits 1 when the file is stale.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MKDOCS = ROOT / "mkdocs.yml"
DOCS = ROOT / "docs"
OUT = ROOT / "apps" / "web" / "src" / "features" / "search" / "docs-index.gen.json"

#: The web app loads the index as one lazy chunk: keep it small.
MAX_BYTES = 120 * 1024
#: "What's new" pages in the index (the newest ones).
WHATS_NEW_KEPT = 2

SKIPPED_PREFIXES = ("reference/api/", "reference/generated/")
_WHATS_NEW = re.compile(r"(?:^|/)whats-new-(\d+)\.(\d+)\.md$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ATTR_LIST = re.compile(r"\s*\{:?\s*([^{}]*?)\s*\}\s*$")
_BADGE = re.compile(r"<span\b[^>]*\bmapi-badge\b[^>]*>.*?</span>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


class _NavLoader(yaml.SafeLoader):
    """``mkdocs.yml`` has ``!!python/name:`` and ``!ENV`` tags: read them as plain nodes."""


def _any_tag(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_NavLoader.add_multi_constructor("tag:yaml.org,2002:python/", _any_tag)
_NavLoader.add_multi_constructor("!", _any_tag)


def _nav_pages(
    nav: Any, section: str | None = None
) -> Iterator[tuple[str, str | None, str | None]]:
    """``(path, nav title, section tab)`` for each Markdown page of ``nav``, in nav order."""
    if isinstance(nav, str):
        yield nav, None, section
    elif isinstance(nav, list):
        for item in nav:
            yield from _nav_pages(item, section)
    elif isinstance(nav, dict):
        for title, value in nav.items():
            tab = section if section is not None else str(title)
            if isinstance(value, str):
                yield value, str(title), tab
            else:
                yield from _nav_pages(value, tab)


def _plain(text: str) -> str:
    """Heading source as plain text: no tags, links, code marks or emphasis marks."""
    text = _TAG.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = text.replace("`", "").replace("**", "").replace("__", "")
    text = re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _slugify(value: str) -> str:
    """The anchor MkDocs gives a heading (``markdown.extensions.toc.slugify``, separator ``-``)."""
    try:
        from markdown.extensions.toc import slugify
    except ImportError:  # the algorithm of Python-Markdown 3.x
        value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        value = re.sub(r"[^\w\s-]", "", value).strip().lower()
        return re.sub(r"[-\s]+", "-", value)
    return str(slugify(value, "-"))


def _split_attrs(raw: str) -> tuple[str, str | None]:
    """A heading without its attribute list, and the ``#id`` of the list when it has one."""
    m = _ATTR_LIST.search(raw)
    if not m:
        return raw, None
    explicit = next(
        (part[1:] for part in m.group(1).split() if part.startswith("#") and len(part) > 1), None
    )
    return raw[: m.start()], explicit


def _headings(body: str) -> Iterator[tuple[int, str, str]]:
    """``(level, title, anchor)`` of each ATX heading outside code fences, with unique anchors."""
    used: set[str] = set()
    fence: str | None = None
    for line in body.splitlines():
        f = _FENCE.match(line)
        if f:
            mark = f.group(1)
            if fence is None:
                fence = mark[0] * 3
            elif mark.startswith(fence):
                fence = None
            continue
        if fence is not None:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        raw, explicit = _split_attrs(m.group(2))
        anchor = explicit or _slugify(_plain(raw))
        if not explicit:
            base, n = anchor, 0
            while anchor in used or not anchor:
                n += 1
                anchor = f"{base}_{n}"
        used.add(anchor)
        title = _plain(_BADGE.sub("", raw))
        if title:
            yield len(m.group(1)), title, anchor


def _page(path: str, nav_title: str | None, section: str | None) -> dict[str, Any] | None:
    source = DOCS / path
    if not source.is_file():
        return None
    text = source.read_text("utf-8")
    front: dict[str, Any] = {}
    m = _FRONT_MATTER.match(text)
    if m:
        try:
            loaded = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            loaded = None
        front = loaded if isinstance(loaded, dict) else {}
        text = text[m.end() :]
    headings = list(_headings(text))
    h1 = next((title for level, title, _ in headings if level == 1), None)
    front_title = front.get("title")
    title = (str(front_title).strip() if front_title else "") or h1 or nav_title or Path(path).stem
    site = re.sub(r"(?:^|/)index\.md$", "/", path) if path.endswith("index.md") else f"{path[:-3]}/"
    site = site.lstrip("/")
    return {
        "t": title,
        "s": section or title,
        "p": site,
        "h": [[t, a] for level, t, a in headings if level == 2],
    }


def _kept_whats_new(paths: Sequence[str]) -> set[str]:
    versions = sorted(
        ((int(m.group(1)), int(m.group(2))), p) for p in paths if (m := _WHATS_NEW.search(p))
    )
    return {p for _, p in versions[-WHATS_NEW_KEPT:]}


def _skipped(path: str, whats_new: set[str]) -> bool:
    if not path.endswith(".md") or "://" in path:
        return True
    if path.startswith(SKIPPED_PREFIXES) or Path(path).stem == "changelog":
        return True
    return bool(_WHATS_NEW.search(path)) and path not in whats_new


def build() -> list[dict[str, Any]]:
    config = yaml.load(MKDOCS.read_text("utf-8"), Loader=_NavLoader)
    entries = list(_nav_pages(config.get("nav") or []))
    whats_new = _kept_whats_new([p for p, _, _ in entries])
    pages: dict[str, dict[str, Any]] = {}
    for path, nav_title, section in entries:
        if _skipped(path, whats_new) or path in pages:
            continue
        page = _page(path, nav_title, section)
        if page is not None:
            pages[path] = page
    return sorted(pages.values(), key=lambda p: p["p"])


def render(pages: list[dict[str, Any]]) -> str:
    """Compact JSON, one page on each line (a readable diff)."""
    rows = [json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in pages]
    return "[\n" + ",\n".join(rows) + "\n]\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="write nothing; exit 1 when the file is stale"
    )
    args = parser.parse_args(argv)
    text = render(build())
    size = len(text.encode("utf-8"))
    rel = OUT.relative_to(ROOT)
    if size > MAX_BYTES:
        print(f"error: {rel} would be {size} bytes (the limit is {MAX_BYTES})", file=sys.stderr)
        return 2
    if args.check:
        if not OUT.is_file() or OUT.read_text("utf-8") != text:
            print(
                f"error: {rel} is stale: run 'uv run python scripts/gen_docs_search_index.py'",
                file=sys.stderr,
            )
            return 1
        return 0
    if not OUT.is_file() or OUT.read_text("utf-8") != text:
        OUT.write_text(text, "utf-8")
        print(f"wrote {rel} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
