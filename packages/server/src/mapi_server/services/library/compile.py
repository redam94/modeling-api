"""Checking and compiling library documents against the current catalog (v0.18 D1).

A library document is JSON data; this module runs the pure :mod:`mapi_spec` compilers over it
and never executes tenant content:

* a **model recipe** compiles to a ModelSpec fragment (:func:`~mapi_spec.model_recipes
  .compile_model_recipe`); its ``prep`` and ``charts`` references must name library items of
  the right kind that the caller can read;
* a **prep preset** fills its slots and checks its steps (:func:`~mapi_spec.prep_presets
  .apply_prep_preset`). When the document is saved, the slots have no values yet: each slot
  gets a placeholder name, so the step settings are checked without the data;
* a **chart template** is checked against its source recipe's catalog example
  (:func:`mapi_plots.chart_templates.check_chart_template`; JSON only, the example's Arrow
  datasets are never decoded);
* a **workflow template** validates with the merged node contracts (built-ins plus the
  catalog's plugin nodes).

:func:`check_with` is the pure part (catalog, node contracts, example and references passed
in); :func:`check_document` gathers them for a request; :func:`validate_doc` serves
``POST /v1/library:validate``. Every issue points into the library document.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from mapi_plots.chart_templates import check_chart_template
from mapi_spec.catalog import Catalog
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity, has_errors, json_pointer
from mapi_spec.library import (
    LIBRARY_DOC_MAX_BYTES,
    ChartTemplateDoc,
    LibraryDoc,
    LibraryKind,
    LibraryRef,
    LibraryValidation,
    ModelRecipeDoc,
    PrepPresetDoc,
    WorkflowTemplateDoc,
    parse_library_doc,
    workflow_template_issues,
)
from mapi_spec.loader import Limits, parse_text
from mapi_spec.model_recipes import compile_model_recipe
from mapi_spec.plots import PlotSpec
from mapi_spec.prep_presets import apply_prep_preset
from mapi_spec.refs import DatasetRef
from mapi_spec.schema import DataSchema
from mapi_spec.workflow_nodes import NodeImplementation
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.errors import ProblemWithHeaders
from mapi_server.services.catalog import CatalogSnapshot
from mapi_server.services.quotas import count_requests
from mapi_server.services.runs import Scope, now
from mapi_server.services.specs import resolve_dataset
from mapi_server.services.workflows.node_catalog import contracts_for

__all__ = [
    "Checked",
    "RefMap",
    "check_document",
    "check_with",
    "dataset_schema",
    "parse_body",
    "per_minute",
    "ref_key",
    "refusal",
    "source_example",
    "validate_doc",
]

_TEXT_LIMITS: Final = Limits(max_bytes=LIBRARY_DOC_MAX_BYTES)

RefKey = tuple[str, int | str]
RefMap = Mapping[RefKey, tuple[LibraryKind, LibraryDoc] | None]
"""Referenced library items, read beforehand (``None``: not found or not readable)."""

_REFUSAL_CODES: Final = frozenset(
    {
        ErrorCode.EXTENSION_UNAVAILABLE,
        ErrorCode.LIBRARY_DATA_SHAPE,
        ErrorCode.PLOT_UNSAFE,
        ErrorCode.SCHEMA_UNSAFE,
    }
)


@dataclass(frozen=True, slots=True)
class Checked:
    """The outcome of a check: the issues, the compiled output (model recipes, and prep
    presets without slots) and the catalog it was checked against."""

    issues: list[Issue] = field(default_factory=list)
    compiled: dict[str, JsonValue] | None = None
    catalog_digest: str | None = None

    @property
    def valid(self) -> bool:
        return not has_errors(self.issues)


def ref_key(ref: LibraryRef) -> RefKey:
    return (ref.item, ref.revision)


def _issue(
    pointer: str,
    message: str,
    *,
    code: ErrorCode = ErrorCode.LIBRARY_INVALID,
    severity: Severity = Severity.ERROR,
) -> Issue:
    return Issue(code=code, severity=severity, pointer=pointer, message=message)


def refusal(issues: list[Issue], what: str) -> MapiError:
    """The error that refuses a document or a use: the code of the first error issue when it
    is one of the specific refusals (``EXTENSION_UNAVAILABLE`` 409, ``LIBRARY_DATA_SHAPE``,
    ``PLOT_UNSAFE``), else ``LIBRARY_INVALID``; every issue is attached."""
    errors = [i for i in issues if i.severity is Severity.ERROR]
    first = errors[0] if errors else None
    code = (
        first.code if first is not None and first.code in _REFUSAL_CODES else None
    ) or ErrorCode.LIBRARY_INVALID
    detail = what if first is None else f"{what}: {first.message}"
    return MapiError(
        detail[:2000], code=code, pointer=first.pointer if first else "", issues=issues
    )


# --------------------------------------------------------------------------------- parsing


def parse_body(
    document: Mapping[str, JsonValue] | None, text: str | None
) -> tuple[LibraryDoc, dict[str, JsonValue]]:
    """The typed document of a request body (``document`` or YAML/JSON ``text``, parsed with
    the event-budgeted loader) and its stored form: the fields the author set, as JSON.

    Raises ``LIBRARY_INVALID`` with pointers into the document (``SPEC_PARSE_ERROR`` and
    ``PAYLOAD_TOO_LARGE`` for text the parser refuses)."""
    data = parse_text(text, _TEXT_LIMITS) if text is not None else document
    doc = parse_library_doc(data)
    stored: dict[str, JsonValue] = doc.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return doc, stored


# -------------------------------------------------------------------------------- the check


def _ref_issues(doc: ModelRecipeDoc, refs: RefMap | None) -> list[Issue]:
    """A model recipe's ``prep`` and ``charts`` must name readable items of the right kind."""
    if refs is None:
        return []
    wanted: list[tuple[str, LibraryRef, LibraryKind, str]] = []
    if doc.prep is not None:
        wanted.append(("/prep/item", doc.prep, "prep_preset", "prep preset"))
    for i, ref in enumerate(doc.charts):
        wanted.append((json_pointer("charts", i, "item"), ref, "chart_template", "chart template"))
    issues: list[Issue] = []
    for pointer, ref, kind, label in wanted:
        found = refs.get(ref_key(ref))
        if found is None:
            issues.append(_issue(pointer, f"The {label} {ref.item} is not in your library."))
        elif found[0] != kind:
            issues.append(_issue(pointer, f"The library item {ref.item} is not a {label}."))
    return issues


def _placeholder_slots(doc: PrepPresetDoc) -> dict[str, JsonValue]:
    """A valid value for every slot (the slot's own name), so a saved preset's steps can be
    checked before anyone chooses the data."""
    return {name: [name] if slot.kind == "var_list" else name for name, slot in doc.slots.items()}


def _slots_from_roles(doc: PrepPresetDoc, schema: DataSchema) -> dict[str, JsonValue] | None:
    """Fill every slot from the dataset's roles (as the app pre-fills them); ``None`` when a
    required slot has no role or no variable with it."""
    out: dict[str, JsonValue] = {}
    for name, slot in doc.slots.items():
        if slot.kind == "dim" or slot.role is None:
            if slot.required:
                return None
            continue
        names = [v for v, var in schema.vars.items() if var.role is slot.role]
        if not names:
            if slot.required:
                return None
            continue
        out[name] = list(names) if slot.kind == "var_list" else names[0]
    return out


def _check_prep(
    doc: PrepPresetDoc, catalog: Catalog, schema: DataSchema | None
) -> tuple[list[Issue], dict[str, JsonValue] | None]:
    filled = _slots_from_roles(doc, schema) if schema is not None else None
    notes: list[Issue] = []
    if filled is None:
        if schema is not None and doc.slots:
            notes.append(
                _issue(
                    "/slots",
                    "Choose a value for each slot to check the preset against the data.",
                    severity=Severity.INFO,
                )
            )
        filled, schema = _placeholder_slots(doc), None
    steps, issues = apply_prep_preset(doc, filled, schema=schema, catalog=catalog)
    compiled: dict[str, JsonValue] | None = None
    if not doc.slots:
        compiled = {
            "pipeline": [
                s.model_dump(mode="json", by_alias=True, exclude_defaults=True) for s in steps
            ]
        }
    return [*issues, *notes], compiled


def _check_chart(doc: ChartTemplateDoc, catalog: Catalog, example: PlotSpec | None) -> list[Issue]:
    if catalog.resolve_plot(doc.source_recipe) is None:
        return [
            _issue(
                "/source_recipe",
                f"The plot recipe {str(doc.source_recipe)!r} is not in the current catalog.",
                code=ErrorCode.EXTENSION_UNAVAILABLE,
            )
        ]
    if example is None:
        return [
            _issue(
                "/source_recipe",
                "The recipe has no example data. The template is checked when you apply it "
                "to a model.",
                severity=Severity.WARNING,
            )
        ]
    return check_chart_template(doc, example)


def check_with(
    doc: LibraryDoc,
    catalog: Catalog,
    *,
    nodes: Mapping[str, NodeImplementation],
    example: PlotSpec | None = None,
    refs: RefMap | None = None,
    schema: DataSchema | None = None,
) -> tuple[list[Issue], dict[str, JsonValue] | None]:
    """Check ``doc`` against a catalog (pure). ``nodes`` are the merged node contracts,
    ``example`` the chart template's source example, ``refs`` the model recipe's referenced
    items and ``schema`` the raw schema of a dataset (adds the data checks)."""
    if isinstance(doc, ModelRecipeDoc):
        compiled = compile_model_recipe(doc, catalog, schema=schema)
        return [*compiled.issues, *_ref_issues(doc, refs)], compiled.spec_fragment()
    if isinstance(doc, PrepPresetDoc):
        return _check_prep(doc, catalog, schema)
    if isinstance(doc, ChartTemplateDoc):
        return _check_chart(doc, catalog, example), None
    assert isinstance(doc, WorkflowTemplateDoc)
    return workflow_template_issues(doc, nodes=nodes, catalog=catalog), None


# ------------------------------------------------------------------------- request helpers


async def source_example(
    ctx: ServerContext, session: AsyncSession, catalog: Catalog, doc: ChartTemplateDoc
) -> PlotSpec | None:
    """The catalog example of a chart template's source recipe (``None``: no such recipe in
    the catalog, or no example)."""
    entry = catalog.resolve_plot(doc.source_recipe)
    if entry is None:
        return None
    return await ctx.catalogs.example(session, f"{entry.id}@{entry.version}")


async def dataset_schema(
    session: AsyncSession, scope: Scope, ref: DatasetRef | None
) -> tuple[DataSchema | None, list[Issue]]:
    """The raw schema of the dataset a check or a use names (an issue when it cannot be
    read)."""
    if ref is None:
        return None, []
    dv, why = await resolve_dataset(session, scope, ref)
    if dv is None or dv.data_schema is None:
        reason = why or "the dataset version has no stored schema"
        return None, [_issue("/dataset", reason, code=ErrorCode.REF_UNRESOLVED)]
    return DataSchema.model_validate(dv.data_schema), []


async def check_document(
    ctx: ServerContext,
    session: AsyncSession,
    scope: Scope,
    doc: LibraryDoc,
    *,
    schema: DataSchema | None = None,
    snapshot: CatalogSnapshot | None = None,
) -> Checked:
    """Check ``doc`` against the catalog of this image digest (``CATALOG_UNAVAILABLE`` when
    there is none yet), reading what it references as ``scope`` can."""
    # imported here: the store imports this module (the reference reads live there)
    from mapi_server.services.library.store import read_refs

    snap = snapshot if snapshot is not None else await ctx.catalogs.require(session)
    example = (
        await source_example(ctx, session, snap.catalog, doc)
        if isinstance(doc, ChartTemplateDoc)
        else None
    )
    refs = await read_refs(session, scope, doc) if isinstance(doc, ModelRecipeDoc) else None
    issues, compiled = check_with(
        doc,
        snap.catalog,
        nodes=contracts_for(snap),
        example=example,
        refs=refs,
        schema=schema,
    )
    return Checked(issues=issues, compiled=compiled, catalog_digest=snap.digest)


async def per_minute(ctx: ServerContext, name: str, limit: int, what: str) -> None:
    """``RATE_LIMITED`` (429, ``Retry-After``) after ``limit`` requests of the calling user
    in the current minute (a fixed window per user and tenant)."""
    auth = ctx.request
    who = (auth.user_id if auth is not None else None) or "anonymous"
    at = now()
    async with ctx.db.session() as system:
        before = await count_requests(system, f"{name}:{ctx.scope.tenant_id}:{who}"[:96], now=at)
    if before >= limit:
        raise ProblemWithHeaders(
            f"you can ask for at most {limit} {what} a minute; try again shortly",
            code=ErrorCode.RATE_LIMITED,
            headers={"Retry-After": str(max(1, 60 - at.second))},
        )


async def validate_doc(
    ctx: ServerContext,
    session: AsyncSession,
    document: Mapping[str, JsonValue] | None,
    text: str | None,
    dataset: DatasetRef | None,
) -> LibraryValidation:
    """``POST /v1/library:validate``: every problem of a document (nothing is saved).

    A document that does not parse is answered with its issues (``valid: false``), not an
    error, so the editor can show them next to the fields."""
    await per_minute(
        ctx, "libcheck", ctx.settings.library_validate_per_minute, "library document checks"
    )
    try:
        doc, _ = parse_body(document, text)
    except MapiError as exc:
        return LibraryValidation(valid=False, issues=exc.issues or [exc.to_issue()])
    scope = ctx.scope
    schema, issues = await dataset_schema(session, scope, dataset)
    checked = await check_document(ctx, session, scope, doc, schema=schema)
    issues = [*issues, *checked.issues]
    return LibraryValidation(valid=not has_errors(issues), issues=issues, compiled=checked.compiled)
