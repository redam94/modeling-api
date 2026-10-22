"""The library service (v0.18): analyst-made model recipes, prep presets, chart templates and
team workflow templates, stored as JSON documents with immutable revisions and compiled by
trusted :mod:`mapi_spec` code. Nothing here imports or runs tenant code.

* :mod:`.store` — items, revisions, scopes, caps, promotion, the sample seed;
* :mod:`.compile` — checking a document against the current catalog, ``:validate``;
* :mod:`.use` — ``:use``: spec fragments, prep steps, applied charts, workflow documents;
* :mod:`.charts` — chart templates applied to a model version's plot, and previews.
"""

from __future__ import annotations

from mapi_server.services.library.charts import apply_chart, preview_chart
from mapi_server.services.library.compile import Checked, check_document, parse_body, validate_doc
from mapi_server.services.library.store import (
    archive_item,
    create_item,
    create_item_system,
    item_dto,
    list_items,
    list_revisions,
    load_item,
    load_revision,
    promote_item,
    refuse_project_refs,
    resolve_ref,
    revise_item,
    revision_dto,
)
from mapi_server.services.library.use import use_item

__all__ = [
    "Checked",
    "apply_chart",
    "archive_item",
    "check_document",
    "create_item",
    "create_item_system",
    "item_dto",
    "list_items",
    "list_revisions",
    "load_item",
    "load_revision",
    "parse_body",
    "preview_chart",
    "promote_item",
    "refuse_project_refs",
    "resolve_ref",
    "revise_item",
    "revision_dto",
    "use_item",
    "validate_doc",
]
