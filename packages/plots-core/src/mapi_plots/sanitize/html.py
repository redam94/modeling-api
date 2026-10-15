"""Plotly pseudo-HTML stripping.

Plotly renders a small HTML subset inside text attributes (titles, annotations, hover
labels). Only ``<b>``, ``<i>``, ``<br>``, ``<sub>`` and ``<sup>`` survive, always without
attributes; every other tag (``<a href>``, ``<span style>``, ``<img>``, ``<script>`` ...) and
every HTML comment is removed. Any ``<`` that is not the start of a kept tag is escaped as
``&lt;``, so the output contains no ``<`` except in the kept tags. The function is idempotent.
"""

from __future__ import annotations

import re

__all__ = ["ALLOWED_TAGS", "HOVERTEMPLATE_TAGS", "strip_html"]

ALLOWED_TAGS = frozenset({"b", "i", "br", "sub", "sup"})
#: ``<extra>`` is Plotly template syntax (the secondary hover box), not HTML.
HOVERTEMPLATE_TAGS = ALLOWED_TAGS | {"extra"}

_TOKEN = re.compile(
    r"<!--.*?(?:-->|$)"  # comment (also unterminated)
    r"|<(/?)([A-Za-z][A-Za-z0-9]*)(?:\s[^<>]*|/)?>"  # a tag
    r"|<",  # stray '<'
    re.S,
)


def strip_html(text: str, allowed: frozenset[str] = ALLOWED_TAGS) -> str:
    """Remove every tag except ``allowed`` (attributes dropped); escape stray ``<``."""
    if "<" not in text:
        return text

    def repl(m: re.Match[str]) -> str:
        whole = m.group(0)
        if whole == "<":
            return "&lt;"
        if whole.startswith("<!--"):
            return ""
        close, name = m.group(1), m.group(2).lower()
        if name not in allowed:
            return ""
        if name == "br":
            return "<br>"
        return f"<{close}{name}>"

    return _TOKEN.sub(repl, text)
