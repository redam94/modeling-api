"""The registry of node implementations that work on data (the sandbox side).

:func:`builtin_nodes` maps every contract node id whose ``runs_in`` is ``sandbox`` or
``child_run`` (except ``subworkflow.run``, which drivers handle) to its implementation;
orchestrator nodes (branches, comparisons, selections, reports) are computed by the engine.

Other distributions add nodes and templates through two entry-point groups (v0.18):

* ``mapi.workflow_nodes`` (:data:`ENTRY_POINT_GROUP`) exports a :class:`NodeExport` (or a
  sequence of them, or a callable returning either): a JSON contract
  (:func:`mapi_spec.node_authoring.node`) with its implementation. A contract must pass
  :func:`mapi_spec.workflow_nodes.plugin_contract_issues` for the exporting distribution
  (a :data:`~mapi_spec.workflow_nodes.PLUGIN_NODE_KINDS` kind, the sandbox, an id
  ``<kind>.<namespace>_<name>`` in a namespace the distribution may use, identifier ports,
  a complete About card, a safe parameter schema). The runner exports the contracts into the
  catalog (:func:`plugin_node_contracts`); the control plane validates workflows against
  them and never imports plugin code. The legacy export, a mapping ``{node id: fn}``, still
  fills ids the built-in contracts declare (diagnostic ``deprecated``).
* ``mapi.workflow_templates`` (:data:`TEMPLATE_ENTRY_POINT_GROUP`) exports a
  :class:`~mapi_spec.catalog.TemplateCatalogEntry` (or a sequence, or a callable): a gallery
  template whose document is validated against the node contracts (:func:`load_plugin_templates`).

Nothing an entry point exports is dropped silently: every refusal is a
:class:`~mapi_spec.catalog.CatalogDiagnostic` (at most :data:`MAX_DIAGNOSTICS_PER_DISTRIBUTION`
per distribution and loader). Built-ins always win an id collision.

Uploaded code (v0.19) follows the SDK registry's path rule
(:func:`mapi_sdk.registry.plugin_entry_points`): an entry point whose distribution lives under
a mounted overlay directory is an upload whatever its name. Its nodes must name the overlay's
namespace in ``plugin`` (``<kind>.<namespace>_<name>``), its templates must be
``<namespace>_<name>`` and carry the provider ``upload:<pvid>``; a legacy mapping export from
an upload is refused (it would fill built-in contract ids). The overlays default to the ones
the worker recorded (:func:`mapi_sdk.registry.process_overlays`).
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from mapi_sdk.registry import EntryPointOrigin, UploadOverlay, plugin_entry_points
from mapi_spec.about import about_issues
from mapi_spec.catalog import CatalogDiagnostic, DiagnosticCode, TemplateCatalogEntry
from mapi_spec.errors import ErrorCode, Issue, Severity
from mapi_spec.namespaces import check_namespace, is_builtin_distribution, uploaded_namespace_of
from mapi_spec.workflow import validate_workflow
from mapi_spec.workflow_nodes import (
    NODE_IMPLEMENTATIONS,
    SUBWORKFLOW_NODE,
    NodeImplementation,
    plugin_contract_issues,
)
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES
from mapi_workflows.runtime.causal import CAUSAL_NODES
from mapi_workflows.runtime.evaluate import EVALUATE_NODES
from mapi_workflows.runtime.governance import GOVERNANCE_NODES
from mapi_workflows.runtime.models import MODEL_NODES
from mapi_workflows.runtime.plots import PLOT_NODES
from mapi_workflows.runtime.posterior import POSTERIOR_NODES
from mapi_workflows.runtime.scoring import SCORE_NODES
from mapi_workflows.runtime.search import SEARCH_NODES
from mapi_workflows.runtime.splits import SPLIT_NODES
from mapi_workflows.runtime.stattests import TEST_NODES
from mapi_workflows.runtime.transforms import TRANSFORM_NODES
from mapi_workflows.runtime.values import NodeFn
from pydantic import ValidationError

__all__ = [
    "ENTRY_POINT_GROUP",
    "MAX_DIAGNOSTICS_PER_DISTRIBUTION",
    "TEMPLATE_ENTRY_POINT_GROUP",
    "NodeExport",
    "all_node_contracts",
    "builtin_nodes",
    "load_node_exports",
    "load_plugin_templates",
    "node_origins",
    "node_registry",
    "plugin_node_contracts",
    "sandbox_node_ids",
    "template_namespace_problem",
]

_log = logging.getLogger(__name__)

ENTRY_POINT_GROUP: Final = "mapi.workflow_nodes"
TEMPLATE_ENTRY_POINT_GROUP: Final = "mapi.workflow_templates"
MAX_DIAGNOSTICS_PER_DISTRIBUTION: Final = 20
"""One broken distribution cannot flood the catalog's diagnostics (D12)."""

_BUILTIN_PREFIX: Final = "mapi_workflows."
"""This distribution's own entry point (the built-in table) is not a plugin export."""

_MESSAGES: Final[dict[DiagnosticCode, str]] = {
    "load_error": "The entry point could not be loaded. Its nodes or templates are not available.",
    "namespace_reserved": "The node or template uses a namespace that this distribution may "
    "not use. It is not available.",
    "id_collision": "Another node or template already has this id. The first one is kept.",
    "schema_unsafe": "The parameter schema of the node is not safe. The node is not available.",
    "about_incomplete": "The About card is not complete. It is not available.",
    "node_contract_invalid": "The node contract is not valid. The node is not available.",
    "template_invalid": "The workflow template is not valid. It is not available.",
    "deprecated": "The entry point exports a mapping of node ids to functions. Export "
    "NodeExport objects instead.",
}
_UPLOAD_NAMESPACE_MESSAGE: Final = (
    "An upload may export nodes and templates only in its own namespace. It is not available."
)
_UPLOAD_MAPPING_MESSAGE: Final = (
    "Uploaded code cannot export a mapping of node ids to functions. Export NodeExport "
    "objects instead. The entry point is ignored."
)
"""Trusted, platform-authored text of each diagnostic; plugin text goes in ``untrusted_text``."""


@dataclass(frozen=True, slots=True)
class NodeExport:
    """A plugin workflow node: its JSON contract and its implementation.

    Export it (or a list of them) from the ``mapi.workflow_nodes`` entry-point group::

        [project.entry-points."mapi.workflow_nodes"]
        acme = "acme_nodes:NODES"
    """

    contract: NodeImplementation
    fn: NodeFn


def builtin_nodes() -> dict[str, NodeFn]:
    """Every built-in data node implementation by node id."""
    out: dict[str, NodeFn] = {}
    for table in (
        TEST_NODES,
        TRANSFORM_NODES,
        SPLIT_NODES,
        SEARCH_NODES,
        MODEL_NODES,
        EVALUATE_NODES,
        GOVERNANCE_NODES,
        POSTERIOR_NODES,
        CAUSAL_NODES,
        SCORE_NODES,
        PLOT_NODES,
    ):
        out.update(table)
    return out


def sandbox_node_ids() -> list[str]:
    """Contract node ids that need data (everything but orchestrator nodes and
    sub-workflows)."""
    return sorted(
        i
        for i, impl in NODE_IMPLEMENTATIONS.items()
        if impl.runs_in != "orchestrator" and i != SUBWORKFLOW_NODE
    )


# =========================================================================== diagnostics


def _clip(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


@dataclass(slots=True)
class _Diagnostics:
    """Collects diagnostics, at most :data:`MAX_DIAGNOSTICS_PER_DISTRIBUTION` per origin (a
    distribution, or one upload)."""

    group: str
    rows: list[CatalogDiagnostic] = field(default_factory=list)
    per_dist: dict[str | None, int] = field(default_factory=dict)

    def add(
        self,
        code: DiagnosticCode,
        found: EntryPointOrigin,
        *,
        id_: str | None = None,
        untrusted: str | None = None,
        message: str | None = None,
    ) -> None:
        key = found.origin
        n = self.per_dist.get(key, 0)
        self.per_dist[key] = n + 1
        if n >= MAX_DIAGNOSTICS_PER_DISTRIBUTION:
            return
        self.rows.append(
            CatalogDiagnostic(
                code=code,
                group=self.group,
                entry_point=_clip(found.entry_point.name, 128),
                distribution=_clip(found.distribution, 128),
                id=_clip(id_, 128),
                message=message or _MESSAGES[code],
                untrusted_text=_clip(untrusted, 500),
            )
        )


def _exc_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _issues_text(issues: Sequence[Issue]) -> str:
    return "; ".join(i.message for i in issues)


def _plugin_entry_points(
    group: str, overlays: Sequence[UploadOverlay] | None, only: str | None
) -> list[EntryPointOrigin]:
    """The plugin entry points of ``group`` (installed ones, then each overlay's), without
    this distribution's own table; ``only`` keeps those of one origin (a PEP 503
    distribution name or ``upload:<pvid>``)."""
    return [
        found
        for found in plugin_entry_points(group, overlays)
        if (found.overlay is not None or not found.entry_point.value.startswith(_BUILTIN_PREFIX))
        and (only is None or found.origin == only)
    ]


def _upload_contract_issues(c: NodeImplementation, found: EntryPointOrigin) -> list[Issue]:
    """:func:`plugin_contract_issues` for a node from an overlay: the generic namespace rule
    (which refuses every ``t_`` namespace) is replaced by the upload's own: ``plugin`` is the
    overlay's namespace and the id is ``<kind>.<namespace>_<name>``."""
    ov = found.overlay
    assert ov is not None
    out = [
        i
        for i in plugin_contract_issues(c, distribution=found.distribution)
        if i.pointer != "/plugin"
    ]
    reason = check_namespace(c.id, found.distribution, plugin=c.plugin, claimed=ov.namespace)
    if reason is None and uploaded_namespace_of(c.id) != ov.namespace:
        reason = f"{c.id!r}: the node id must be '<kind>.{ov.namespace}_<name>'."
    if reason is not None:
        out.insert(0, Issue(code=ErrorCode.WORKFLOW_INVALID, pointer="/plugin", message=reason))
    return out


def _load(ep: importlib.metadata.EntryPoint) -> Any:
    """What an entry point exports (a factory is called; a mapping or export is kept)."""
    obj = ep.load()
    if callable(obj) and not isinstance(obj, Mapping | NodeExport | type):
        obj = obj()
    return obj


def _items(obj: Any) -> list[Any] | None:
    """The exports of a single object or a sequence (``None``: neither)."""
    if isinstance(obj, NodeExport | TemplateCatalogEntry):
        return [obj]
    if isinstance(obj, Iterable) and not isinstance(obj, str | bytes | Mapping):
        return list(obj)
    return None


# ================================================================================= nodes


def _contract_code(c: NodeImplementation, issues: Sequence[Issue]) -> DiagnosticCode:
    """The diagnostic code of a refused contract: the most specific reason first."""
    pointers = {i.pointer for i in issues}
    codes = {i.code for i in issues}
    if "/plugin" in pointers and c.plugin is not None:
        return "namespace_reserved"
    if "/id" in pointers:
        return "id_collision"
    if ErrorCode.SCHEMA_UNSAFE in codes:
        return "schema_unsafe"
    if codes == {ErrorCode.ABOUT_INCOMPLETE}:
        return "about_incomplete"
    return "node_contract_invalid"


@dataclass(slots=True)
class _NodeLoad:
    exports: dict[str, NodeExport] = field(default_factory=dict)
    legacy: dict[str, NodeFn] = field(default_factory=dict)
    diagnostics: list[CatalogDiagnostic] = field(default_factory=list)
    origins: dict[str, str] = field(default_factory=dict)
    """v0.19: node id -> origin (PEP 503 distribution name or ``upload:<pvid>``)."""


def _load_nodes(
    overlays: Sequence[UploadOverlay] | None = None, only: str | None = None
) -> _NodeLoad:
    out = _NodeLoad()
    diags = _Diagnostics(ENTRY_POINT_GROUP)
    builtin = builtin_nodes()
    for found in _plugin_entry_points(ENTRY_POINT_GROUP, overlays, only):
        ep, uploaded = found.entry_point, found.overlay is not None
        try:
            obj = _load(ep)
        except Exception as exc:  # report and keep going: one bad plugin
            diags.add("load_error", found, untrusted=_exc_text(exc))
            continue
        if isinstance(obj, Mapping):
            if uploaded:  # a mapping fills built-in contract ids: never from an upload
                diags.add("node_contract_invalid", found, message=_UPLOAD_MAPPING_MESSAGE)
            else:
                _legacy(obj, found, builtin, out, diags)
            continue
        items = _items(obj)
        if items is None:
            diags.add(
                "node_contract_invalid",
                found,
                untrusted=f"exported {type(obj).__name__}, not NodeExport objects",
            )
            continue
        for item in items:
            if not isinstance(item, NodeExport) or not isinstance(
                item.contract, NodeImplementation
            ):
                diags.add(
                    "node_contract_invalid",
                    found,
                    untrusted=f"exported {type(item).__name__}, not a NodeExport with a "
                    "NodeImplementation contract",
                )
                continue
            c = item.contract
            if not callable(item.fn):
                diags.add("node_contract_invalid", found, id_=c.id, untrusted="fn is not callable")
                continue
            issues = (
                _upload_contract_issues(c, found)
                if uploaded
                else plugin_contract_issues(c, distribution=found.distribution)
            )
            if issues:
                code = _contract_code(c, issues)
                diags.add(
                    code,
                    found,
                    id_=c.id,
                    untrusted=_issues_text(issues),
                    message=_UPLOAD_NAMESPACE_MESSAGE
                    if uploaded and code == "namespace_reserved"
                    else None,
                )
                continue
            if c.id in out.exports:
                diags.add("id_collision", found, id_=c.id)
                continue
            out.exports[c.id] = item
            if found.origin is not None:
                out.origins[c.id] = found.origin
    out.diagnostics = diags.rows
    return out


def _legacy(
    table: Mapping[Any, Any],
    found: EntryPointOrigin,
    builtin: Mapping[str, NodeFn],
    out: _NodeLoad,
    diags: _Diagnostics,
) -> None:
    """A pre-v0.18 ``{node id: fn}`` export: fills built-in contract ids only."""
    diags.add("deprecated", found)
    for k, v in table.items():
        if not isinstance(k, str) or not callable(v):
            diags.add(
                "node_contract_invalid", found, untrusted=f"{k!r} is not a node id with a function"
            )
        elif k not in NODE_IMPLEMENTATIONS:
            diags.add(
                "node_contract_invalid",
                found,
                id_=k,
                untrusted="no contract declares this id; export a NodeExport with its contract",
            )
        elif k in builtin or k in out.legacy:
            diags.add("id_collision", found, id_=k)
        else:
            out.legacy[k] = v


def load_node_exports(
    *, overlays: Sequence[UploadOverlay] | None = None, only: str | None = None
) -> tuple[dict[str, NodeExport], list[CatalogDiagnostic]]:
    """The valid plugin node exports by id and a diagnostic for everything refused (load
    errors, invalid contracts, reserved namespaces, collisions, legacy mapping exports).

    ``overlays`` (v0.19, default: the process's) are the mounted uploads; ``only`` keeps the
    entry points of one origin (a PEP 503 distribution name or ``upload:<pvid>``)."""
    loaded = _load_nodes(overlays, only)
    return loaded.exports, loaded.diagnostics


def node_origins(*, overlays: Sequence[UploadOverlay] | None = None) -> dict[str, str]:
    """``{node id: origin}`` of every valid plugin node export (v0.19)."""
    return dict(_load_nodes(overlays).origins)


def plugin_node_contracts(
    *, overlays: Sequence[UploadOverlay] | None = None, only: str | None = None
) -> list[NodeImplementation]:
    """The contracts of the valid plugin nodes (what the runner exports into
    ``Catalog.workflow_nodes``), by id."""
    exports, _ = load_node_exports(overlays=overlays, only=only)
    return [exports[k].contract for k in sorted(exports)]


def all_node_contracts(
    plugins: Iterable[NodeImplementation] | None = None,
) -> dict[str, NodeImplementation]:
    """Built-in contracts merged with ``plugins`` (default: the installed
    :func:`plugin_node_contracts`); built-ins win. Pass it as ``nodes=`` to
    :func:`~mapi_spec.workflow.validate_workflow` and as ``registry=`` to the engine."""
    merged = dict(NODE_IMPLEMENTATIONS)
    for c in plugin_node_contracts() if plugins is None else plugins:
        merged.setdefault(c.id, c)
    return merged


def node_registry(
    extra: Mapping[str, NodeFn] | None = None,
    *,
    overlays: Sequence[UploadOverlay] | None = None,
) -> dict[str, NodeFn]:
    """Built-ins, valid plugin exports and legacy fills (built-ins win), then ``extra``
    (which wins: tests and drivers can override). What is refused is logged here and
    reported as catalog diagnostics by the runner's ``describe``."""
    loaded = _load_nodes(overlays)
    for d in loaded.diagnostics:
        _log.warning(
            "workflow node entry point %s (%s) refused: %s %s",
            d.entry_point,
            d.distribution,
            d.code,
            d.untrusted_text or "",
        )
    out = builtin_nodes()
    for k, fn in loaded.legacy.items():
        out.setdefault(k, fn)
    for k, export in loaded.exports.items():
        out.setdefault(k, export.fn)
    out.update(extra or {})
    return out


# ============================================================================= templates


def _template_namespace(id_: str) -> str:
    return id_.partition("_")[0]


def template_namespace_problem(id_: str, found: EntryPointOrigin) -> str | None:
    """Why ``found`` may not export a template with ``id_`` (``None``: it may). An upload's
    template id is ``<its namespace>_<name>``; anyone else's follows the v0.18 rules."""
    if found.overlay is None:
        return check_namespace(id_, found.distribution, plugin=_template_namespace(id_))
    ns = found.overlay.namespace
    plugin = uploaded_namespace_of(id_) or _template_namespace(id_)
    reason = check_namespace(id_, found.distribution, plugin=plugin, claimed=ns)
    if reason is None and not id_.startswith(f"{ns}_"):
        reason = f"{id_!r}: a template id of this upload must start with '{ns}_'."
    return reason


def _template(
    item: Any,
    found: EntryPointOrigin,
    nodes: Mapping[str, NodeImplementation],
    taken: set[str],
    diags: _Diagnostics,
) -> TemplateCatalogEntry | None:
    dist = found.distribution
    if isinstance(item, Mapping):
        try:
            item = TemplateCatalogEntry.model_validate(dict(item))
        except ValidationError as exc:
            diags.add("template_invalid", found, untrusted=str(exc))
            return None
    if not isinstance(item, TemplateCatalogEntry):
        diags.add(
            "template_invalid",
            found,
            untrusted=f"exported {type(item).__name__}, not a TemplateCatalogEntry",
        )
        return None
    reason = template_namespace_problem(item.id, found)
    if reason is not None:
        diags.add(
            "namespace_reserved",
            found,
            id_=item.id,
            untrusted=reason,
            message=_UPLOAD_NAMESPACE_MESSAGE if found.overlay is not None else None,
        )
        return None
    if item.id in taken:
        diags.add("id_collision", found, id_=item.id)
        return None
    problems = about_issues(item.about, uploaded=found.overlay is not None)
    if problems:
        diags.add("about_incomplete", found, id_=item.id, untrusted="; ".join(problems))
        return None
    result = validate_workflow(item.document, nodes=nodes)
    if not result.valid:
        errors = [i for i in result.issues if i.severity is Severity.ERROR] or result.issues
        diags.add(
            "template_invalid",
            found,
            id_=item.id,
            untrusted="; ".join(f"{i.pointer or '/'}: {i.message}" for i in errors),
        )
        return None
    if found.overlay is not None:  # the path decided: uploaded, whatever the name
        return item.model_copy(update={"provider": found.overlay.provider})
    provider = None if dist is None or is_builtin_distribution(dist) else _clip(dist, 128)
    return item.model_copy(update={"provider": provider}) if provider else item


def load_plugin_templates(
    nodes: Mapping[str, NodeImplementation],
    *,
    overlays: Sequence[UploadOverlay] | None = None,
    only: str | None = None,
) -> tuple[list[TemplateCatalogEntry], list[CatalogDiagnostic]]:
    """The valid plugin workflow templates (``provider`` set for non-built-in
    distributions, ``upload:<pvid>`` for an upload) and a diagnostic for each refused one.

    Each document is validated against ``nodes`` (the built-in contracts merged with the
    plugin ones: :func:`all_node_contracts`). An id a built-in template or an earlier
    export already has is refused (``id_collision``). ``overlays`` and ``only`` as in
    :func:`load_node_exports`."""
    diags = _Diagnostics(TEMPLATE_ENTRY_POINT_GROUP)
    out: list[TemplateCatalogEntry] = []
    taken = set(WORKFLOW_TEMPLATES)
    for found in _plugin_entry_points(TEMPLATE_ENTRY_POINT_GROUP, overlays, only):
        try:
            obj = _load(found.entry_point)
        except Exception as exc:  # report and keep going: one bad plugin
            diags.add("load_error", found, untrusted=_exc_text(exc))
            continue
        items = [obj] if isinstance(obj, Mapping) else _items(obj)
        if items is None:
            diags.add(
                "template_invalid",
                found,
                untrusted=f"exported {type(obj).__name__}, not TemplateCatalogEntry objects",
            )
            continue
        for item in items:
            entry = _template(item, found, nodes, taken, diags)
            if entry is not None:
                taken.add(entry.id)
                out.append(entry)
    return out, diags.rows
