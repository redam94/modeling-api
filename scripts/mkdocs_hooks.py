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


def on_page_markdown(markdown: str, page: Any, config: Any, **kwargs: Any) -> str:
    fragments = _generated(config) / "fragments"

    def embed(match: re.Match[str]) -> str:
        path = fragments / f"{match.group(1)}.md"
        if not path.is_file():
            raise PluginError(f"{page.file.src_path}: no generated fragment {match.group(1)!r}")
        return path.read_text("utf-8").rstrip("\n")

    return _FRAGMENT.sub(embed, markdown)
