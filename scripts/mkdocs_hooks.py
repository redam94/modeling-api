"""MkDocs hooks: regenerate the catalog reference pages before every build.

Registered in ``mkdocs.yml`` under ``hooks:``. The generator only rewrites files whose
content changed, so ``mkdocs serve`` does not loop on its own output.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("mkdocs.hooks.gen_docs_catalog")
_SCRIPT = Path(__file__).with_name("gen_docs_catalog.py")


def on_pre_build(config: Any, **kwargs: Any) -> None:
    spec = importlib.util.spec_from_file_location("gen_docs_catalog", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    changed = module.generate(Path(config["docs_dir"]) / "reference" / "generated")
    if changed:
        log.info("regenerated %d catalog file(s)", len(changed))
