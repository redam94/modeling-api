"""``POST /v1/library/{item_id}:use``: compile an item for use (v0.18 D1, D8, D9).

Nothing is saved: the web puts the result where the analyst is working.

* a **model recipe** → a ModelSpec fragment (``data.bind``, ``pipeline``, ``model``,
  ``outputs``, ``metadata.labels``) compiled against the current catalog, with the dataset's
  schema when one is named (binding, shape step, effect chain), the use-time values of the
  preset's ``exposed`` settings and of its prep preset's slots;
* a **prep preset** → its steps, slots filled, ids prefixed ``p_``;
* a **chart template** → the chart applied to a model version's plot of the source recipe;
* a **workflow template** → the workflow document, re-validated with the merged node
  contracts, its ``metadata.project`` set to the caller's project; the web posts it to
  ``POST /v1/workflows``.

Lineage is recorded with labels, never with a spec schema change (D9):
``mapi.library/model``, ``mapi.library/prep``, ``mapi.library/template`` =
``lib_<id>-r<revision>``. Error issues refuse the use (``EXTENSION_UNAVAILABLE`` 409 when the
catalog lacks a base model, step, recipe or node; ``LIBRARY_DATA_SHAPE`` and
``LIBRARY_INVALID`` 422); warnings and notes come back with the result.
"""

from __future__ import annotations

import copy
from typing import Any

from mapi_spec.errors import ErrorCode, Issue, MapiError, has_errors
from mapi_spec.library import (
    LABEL_PREP,
    LABEL_TEMPLATE,
    ChartTemplateDoc,
    LibraryDoc,
    LibraryUseRequest,
    LibraryUseResult,
    ModelRecipeDoc,
    PrepPresetDoc,
    WorkflowTemplateDoc,
    library_kind,
    library_label,
    parse_library_doc,
    workflow_template_issues,
)
from mapi_spec.model_recipes import compile_model_recipe
from mapi_spec.prep_presets import apply_prep_preset
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.library.charts import apply_chart
from mapi_server.services.library.compile import dataset_schema, ref_key, refusal
from mapi_server.services.library.store import load_revision, read_refs
from mapi_server.services.workflows.node_catalog import contracts_for

__all__ = ["PREP_ID_PREFIX", "use_item"]

PREP_ID_PREFIX = "p"
"""Step-id prefix of a used prep preset (``p_<step id>``)."""


def _refuse_errors(issues: list[Issue], what: str) -> None:
    if has_errors(issues):
        raise refusal(issues, what)


async def use_item(
    ctx: ServerContext,
    session: AsyncSession,
    item: m.LibraryItem,
    body: LibraryUseRequest,
) -> LibraryUseResult:
    """Compile revision ``body.revision`` (the latest by default) of ``item`` for use in the
    caller's project. The caller checked the permissions (``spec`` create for a model
    recipe)."""
    rev = await load_revision(session, item, body.revision)
    doc: LibraryDoc = parse_library_doc(rev.document)
    label = library_label(item.id, rev.revision)
    scope = ctx.scope
    snapshot = await ctx.catalogs.require(session)
    kind = library_kind(doc)

    if isinstance(doc, ChartTemplateDoc):
        if body.model_id is None or body.model_version is None:
            raise MapiError(
                "a chart template is applied to a model version: give model_id and model_version",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/model_id" if body.model_id is None else "/model_version",
            )
        plot, notes = await apply_chart(
            session,
            scope,
            doc,
            model_id=body.model_id,
            version=body.model_version,
            author=label,
        )
        return LibraryUseResult(kind=kind, plot=plot, issues=notes)

    schema, issues = await dataset_schema(session, scope, body.dataset)
    _refuse_errors(issues, "the dataset cannot be read")

    if isinstance(doc, ModelRecipeDoc):
        refs = await read_refs(session, scope, doc)

        def resolve(ref: Any, _kind: Any) -> LibraryDoc | None:
            found = refs.get(ref_key(ref))
            return found[1] if found is not None else None

        compiled = compile_model_recipe(
            doc,
            snapshot.catalog,
            item_ref=label,
            exposed=body.exposed,
            slots=body.slots,
            resolve=resolve,
            schema=schema,
        )
        _refuse_errors(compiled.issues, "the model recipe cannot be used")
        return LibraryUseResult(
            kind=kind,
            spec_fragment=compiled.spec_fragment(),
            labels=dict(compiled.labels),
            issues=compiled.issues,
        )

    if isinstance(doc, PrepPresetDoc):
        steps, issues = apply_prep_preset(
            doc,
            body.slots,
            schema=schema,
            id_prefix=PREP_ID_PREFIX,
            catalog=snapshot.catalog,
        )
        _refuse_errors(issues, "the prep preset cannot be used")
        pipeline: list[dict[str, JsonValue]] = [
            s.model_dump(mode="json", by_alias=True, exclude_defaults=True) for s in steps
        ]
        return LibraryUseResult(
            kind=kind, pipeline=pipeline, labels={LABEL_PREP: label}, issues=issues
        )

    assert isinstance(doc, WorkflowTemplateDoc)
    issues = workflow_template_issues(doc, nodes=contracts_for(snapshot), catalog=snapshot.catalog)
    _refuse_errors(issues, "the workflow template cannot be used")
    document: dict[str, Any] = copy.deepcopy(dict(doc.document))
    metadata = document.get("metadata")
    if isinstance(metadata, dict):
        metadata["project"] = scope.project_id
        labels = metadata.get("labels")
        metadata["labels"] = {**(labels if isinstance(labels, dict) else {}), LABEL_TEMPLATE: label}
    return LibraryUseResult(
        kind=kind,
        workflow_document=document,
        labels={LABEL_TEMPLATE: label},
        issues=issues,
    )
