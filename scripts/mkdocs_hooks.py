"""MkDocs hooks: regenerate the generated reference pages before every build.

Registered in ``mkdocs.yml`` under ``hooks:``. Two generators write into
``docs/reference/generated/``: ``gen_docs_catalog.py`` (steps, models, plots, spec schema,
error codes) and ``gen_docs_reference.py`` (CLI reference, server settings, the OpenAPI
document). Both only rewrite files whose content changed, so ``mkdocs serve`` does not loop
on its own output.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("mkdocs.hooks.generated_reference")
_SCRIPTS = ("gen_docs_catalog.py", "gen_docs_reference.py")


def _load(name: str) -> Any:
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def on_pre_build(config: Any, **kwargs: Any) -> None:
    out = Path(config["docs_dir"]) / "reference" / "generated"
    for script in _SCRIPTS:
        changed = _load(script).generate(out)
        if changed:
            log.info("%s: regenerated %d file(s)", script, len(changed))
