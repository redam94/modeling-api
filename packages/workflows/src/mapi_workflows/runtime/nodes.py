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
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from mapi_spec.about import about_issues
from mapi_spec.catalog import CatalogDiagnostic, DiagnosticCode, TemplateCatalogEntry
from mapi_spec.errors import ErrorCode, Issue, Severity
from mapi_spec.namespaces import check_namespace, is_builtin_distribution
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
    "node_registry",
    "plugin_node_contracts",
    "sandbox_node_ids",
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


def _dist_of(ep: importlib.metadata.EntryPoint) -> str | None:
    return getattr(getattr(ep, "dist", None), "name", None)


@dataclass(slots=True)
class _Diagnostics:
    """Collects diagnostics, at most :data:`MAX_DIAGNOSTICS_PER_DISTRIBUTION` per distribution."""

    group: str
    rows: list[CatalogDiagnostic] = field(default_factory=list)
    per_dist: dict[str | None, int] = field(default_factory=dict)

    def add(
        self,
        code: DiagnosticCode,
        ep: importlib.metadata.EntryPoint,
        *,
        id_: str | None = None,
        untrusted: str | None = None,
    ) -> None:
        dist = _dist_of(ep)
        n = self.per_dist.get(dist, 0)
        self.per_dist[dist] = n + 1
        if n >= MAX_DIAGNOSTICS_PER_DISTRIBUTION:
            return
        self.rows.append(
            CatalogDiagnostic(
                code=code,
                group=self.group,
                entry_point=_clip(ep.name, 128),
                distribution=_clip(dist, 128),
                id=_clip(id_, 128),
                message=_MESSAGES[code],
                untrusted_text=_clip(untrusted, 500),
            )
        )


def _exc_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _issues_text(issues: Sequence[Issue]) -> str:
    return "; ".join(i.message for i in issues)


def _plugin_entry_points(group: str) -> list[importlib.metadata.EntryPoint]:
    return [
        ep
        for ep in importlib.metadata.entry_points(group=group)
        if not ep.value.startswith(_BUILTIN_PREFIX)
    ]


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


def _load_nodes() -> _NodeLoad:
    out = _NodeLoad()
    diags = _Diagnostics(ENTRY_POINT_GROUP)
    builtin = builtin_nodes()
    for ep in _plugin_entry_points(ENTRY_POINT_GROUP):
        dist = _dist_of(ep)
        try:
            obj = _load(ep)
        except Exception as exc:  # report and keep going: one bad plugin
            diags.add("load_error", ep, untrusted=_exc_text(exc))
            continue
        if isinstance(obj, Mapping):
            _legacy(obj, ep, builtin, out, diags)
            continue
        items = _items(obj)
        if items is None:
            diags.add(
                "node_contract_invalid",
                ep,
                untrusted=f"exported {type(obj).__name__}, not NodeExport objects",
            )
            continue
        for item in items:
            if not isinstance(item, NodeExport) or not isinstance(
                item.contract, NodeImplementation
            ):
                diags.add(
                    "node_contract_invalid",
                    ep,
                    untrusted=f"exported {type(item).__name__}, not a NodeExport with a "
                    "NodeImplementation contract",
                )
                continue
            c = item.contract
            if not callable(item.fn):
                diags.add("node_contract_invalid", ep, id_=c.id, untrusted="fn is not callable")
                continue
            issues = plugin_contract_issues(c, distribution=dist)
            if issues:
                diags.add(_contract_code(c, issues), ep, id_=c.id, untrusted=_issues_text(issues))
                continue
            if c.id in out.exports:
                diags.add("id_collision", ep, id_=c.id)
                continue
            out.exports[c.id] = item
    out.diagnostics = diags.rows
    return out


def _legacy(
    table: Mapping[Any, Any],
    ep: importlib.metadata.EntryPoint,
    builtin: Mapping[str, NodeFn],
    out: _NodeLoad,
    diags: _Diagnostics,
) -> None:
    """A pre-v0.18 ``{node id: fn}`` export: fills built-in contract ids only."""
    diags.add("deprecated", ep)
    for k, v in table.items():
        if not isinstance(k, str) or not callable(v):
            diags.add(
                "node_contract_invalid", ep, untrusted=f"{k!r} is not a node id with a function"
            )
        elif k not in NODE_IMPLEMENTATIONS:
            diags.add(
                "node_contract_invalid",
                ep,
                id_=k,
                untrusted="no contract declares this id; export a NodeExport with its contract",
            )
        elif k in builtin or k in out.legacy:
            diags.add("id_collision", ep, id_=k)
        else:
            out.legacy[k] = v


def load_node_exports() -> tuple[dict[str, NodeExport], list[CatalogDiagnostic]]:
    """The valid plugin node exports by id and a diagnostic for everything refused (load
    errors, invalid contracts, reserved namespaces, collisions, legacy mapping exports)."""
    loaded = _load_nodes()
    return loaded.exports, loaded.diagnostics


def plugin_node_contracts() -> list[NodeImplementation]:
    """The contracts of the valid plugin nodes (what the runner exports into
    ``Catalog.workflow_nodes``), by id."""
    exports, _ = load_node_exports()
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


def node_registry(extra: Mapping[str, NodeFn] | None = None) -> dict[str, NodeFn]:
    """Built-ins, valid plugin exports and legacy fills (built-ins win), then ``extra``
    (which wins: tests and drivers can override). What is refused is logged here and
    reported as catalog diagnostics by the runner's ``describe``."""
    loaded = _load_nodes()
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


def _template(
    item: Any,
    ep: importlib.metadata.EntryPoint,
    nodes: Mapping[str, NodeImplementation],
    taken: set[str],
    diags: _Diagnostics,
) -> TemplateCatalogEntry | None:
    dist = _dist_of(ep)
    if isinstance(item, Mapping):
        try:
            item = TemplateCatalogEntry.model_validate(dict(item))
        except ValidationError as exc:
            diags.add("template_invalid", ep, untrusted=str(exc))
            return None
    if not isinstance(item, TemplateCatalogEntry):
        diags.add(
            "template_invalid",
            ep,
            untrusted=f"exported {type(item).__name__}, not a TemplateCatalogEntry",
        )
        return None
    reason = check_namespace(item.id, dist, plugin=_template_namespace(item.id))
    if reason is not None:
        diags.add("namespace_reserved", ep, id_=item.id, untrusted=reason)
        return None
    if item.id in taken:
        diags.add("id_collision", ep, id_=item.id)
        return None
    problems = about_issues(item.about)
    if problems:
        diags.add("about_incomplete", ep, id_=item.id, untrusted="; ".join(problems))
        return None
    result = validate_workflow(item.document, nodes=nodes)
    if not result.valid:
        errors = [i for i in result.issues if i.severity is Severity.ERROR] or result.issues
        diags.add(
            "template_invalid",
            ep,
            id_=item.id,
            untrusted="; ".join(f"{i.pointer or '/'}: {i.message}" for i in errors),
        )
        return None
    provider = None if dist is None or is_builtin_distribution(dist) else _clip(dist, 128)
    return item.model_copy(update={"provider": provider}) if provider else item


def load_plugin_templates(
    nodes: Mapping[str, NodeImplementation],
) -> tuple[list[TemplateCatalogEntry], list[CatalogDiagnostic]]:
    """The valid plugin workflow templates (``provider`` set for non-built-in
    distributions) and a diagnostic for each refused one.

    Each document is validated against ``nodes`` (the built-in contracts merged with the
    plugin ones: :func:`all_node_contracts`). An id a built-in template or an earlier
    export already has is refused (``id_collision``)."""
    diags = _Diagnostics(TEMPLATE_ENTRY_POINT_GROUP)
    out: list[TemplateCatalogEntry] = []
    taken = set(WORKFLOW_TEMPLATES)
    for ep in _plugin_entry_points(TEMPLATE_ENTRY_POINT_GROUP):
        try:
            obj = _load(ep)
        except Exception as exc:  # report and keep going: one bad plugin
            diags.add("load_error", ep, untrusted=_exc_text(exc))
            continue
        items = [obj] if isinstance(obj, Mapping) else _items(obj)
        if items is None:
            diags.add(
                "template_invalid",
                ep,
                untrusted=f"exported {type(obj).__name__}, not TemplateCatalogEntry objects",
            )
            continue
        for item in items:
            entry = _template(item, ep, nodes, taken, diags)
            if entry is not None:
                taken.add(entry.id)
                out.append(entry)
    return out, diags.rows
