"""The catalog ids a document names, and the overlay pins they need (v0.19, D9).

Every admission of a run that may execute plugin code collects the catalog ids its document
names (:func:`spec_catalog_ids`, :func:`workflow_catalog_ids`, :func:`document_ids`), keeps
those in an uploaded namespace (:func:`uploaded_ids`) and asks the caller's tenant catalog
view for their pins (:meth:`mapi_server.services.plugins.view.CatalogView.pins_for`). Runs
derived from a model version also inherit the pins of its lineage
(:func:`mapi_spec.plugins.lineage_pins`); :func:`merge_pins` joins both.

Pure functions over JSON and typed documents: nothing here reads the database, and nothing
here ever opens an upload.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Final

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.namespaces import uploaded_namespace_of
from mapi_spec.plugins import OVERLAYS_MAX, OverlayPin, lineage_pins
from mapi_spec.spec import ModelSpec
from mapi_spec.workflow import WorkflowSpec
from mapi_spec.workflow_nodes import NodeImplementation

__all__ = [
    "TEMPLATE_LABEL",
    "document_ids",
    "merge_pins",
    "model_version_pins",
    "spec_catalog_ids",
    "uploaded_ids",
    "workflow_catalog_ids",
]

TEMPLATE_LABEL: Final = "workflow-template"
"""The ``metadata.labels`` key that records the gallery template a workflow came from."""

_MAX_STRINGS: Final = 20_000
"""Most strings :func:`document_ids` looks at (documents are size-capped before this)."""


def spec_catalog_ids(spec: ModelSpec) -> set[str]:
    """The catalog ids a model spec names: every pipeline step, the model and every plot
    recipe in ``outputs.plots`` (bare ids, without version ranges)."""
    ids = {step.use.id for step in spec.pipeline}
    ids.add(spec.model.use.id)
    ids.update(ref.id for ref in spec.outputs.plots)
    return ids


def _strings(value: Any, budget: list[int]) -> Iterator[str]:
    """Every string in a JSON value (keys too), depth first, at most ``budget[0]``."""
    stack: list[Any] = [value]
    while stack and budget[0] > 0:
        item = stack.pop()
        if isinstance(item, str):
            budget[0] -= 1
            yield item
        elif isinstance(item, Mapping):
            for k, v in item.items():
                if isinstance(k, str):
                    budget[0] -= 1
                    yield k
                stack.append(v)
        elif isinstance(item, list | tuple):
            stack.extend(item)


def document_ids(value: Any) -> set[str]:
    """The uploaded catalog ids that occur anywhere in a JSON document (a node's ``with``
    block, a child spec, a stored payload), as bare ids.

    A string counts when it has one of the five uploaded id forms
    (:func:`mapi_spec.namespaces.uploaded_namespace_of`); a version range is dropped. This
    over-approximates on purpose: a string that only looks like an uploaded id is pinned (or
    refused when no visible version has its namespace), never ignored.
    """
    out: set[str] = set()
    for s in _strings(value, [_MAX_STRINGS]):
        if len(s) <= 256 and uploaded_namespace_of(s) is not None:
            out.add(s.partition("@")[0].strip())
    return out


def workflow_catalog_ids(
    doc: Mapping[str, Any] | WorkflowSpec, nodes: Mapping[str, NodeImplementation]
) -> set[str]:
    """The catalog ids a workflow document names: every node implementation, the uploaded
    ids in each node's parameters (a ``model`` or ``recipe``, child spec documents), and
    the gallery template the workflow came from (``metadata.labels.workflow-template``).

    ``nodes`` are the node contracts in effect: an implementation id that resolves to an
    uploaded contract is pinned through the contract's own id.
    """
    raw: Mapping[str, Any] = (
        doc.model_dump(mode="json", by_alias=True) if isinstance(doc, WorkflowSpec) else doc
    )
    ids: set[str] = set()
    for node in raw.get("nodes") or []:
        if not isinstance(node, Mapping):
            continue
        use = node.get("use")
        if isinstance(use, str) and use:
            impl = use.partition("@")[0]
            ids.add(impl)
            contract = nodes.get(impl)
            if contract is not None and uploaded_namespace_of(contract.id) is not None:
                ids.add(contract.id)
        ids |= document_ids(node.get("with") or {})
    meta = raw.get("metadata")
    labels = meta.get("labels") if isinstance(meta, Mapping) else None
    template = labels.get(TEMPLATE_LABEL) if isinstance(labels, Mapping) else None
    if isinstance(template, str) and template:
        ids.add(template)
    return ids


def uploaded_ids(ids: Iterable[str]) -> set[str]:
    """The ids of ``ids`` that are in an uploaded namespace (bare, without ranges)."""
    return {i.partition("@")[0] for i in ids if i and uploaded_namespace_of(i) is not None}


def model_version_pins(lineage: Mapping[str, Any] | None) -> list[OverlayPin]:
    """The pins a run derived from a model version inherits (its lineage's ``overlays``).

    A lineage that records uploaded code only in the legacy ``overlay_hash`` key cannot be
    mounted again (it names no version): ``PLUGIN_UNAVAILABLE``. It never occurs in
    practice (that key was always empty); refusing keeps the rule fail closed.
    """
    pins = lineage_pins(lineage)
    if not pins and lineage and lineage.get("overlay_hash"):
        raise MapiError(
            "This model version used uploaded code that is not recorded by version. Fit "
            "the model again to use it.",
            code=ErrorCode.PLUGIN_UNAVAILABLE,
        )
    return pins


def merge_pins(*groups: Sequence[OverlayPin]) -> list[OverlayPin]:
    """One list of the pins of every group, sorted by namespace, without duplicates.

    Two different versions of one uploaded plugin cannot be mounted by one run (they share
    a namespace): ``PLUGIN_UNAVAILABLE`` naming the plugin. More than
    :data:`~mapi_spec.plugins.OVERLAYS_MAX` plugins is refused the same way.
    """
    by_ns: dict[str, OverlayPin] = {}
    for group in groups:
        for pin in group:
            seen = by_ns.get(pin.namespace)
            if seen is None:
                by_ns[pin.namespace] = pin
            elif seen != pin:
                raise MapiError(
                    f"This run would use two versions of the uploaded plugin {pin.namespace} "
                    "at once. Use one version: fit the model again with the version you "
                    "want.",
                    code=ErrorCode.PLUGIN_UNAVAILABLE,
                )
    if len(by_ns) > OVERLAYS_MAX:
        raise MapiError(
            f"A run can use at most {OVERLAYS_MAX} uploaded plugins; this one uses {len(by_ns)}.",
            code=ErrorCode.PLUGIN_UNAVAILABLE,
        )
    return [by_ns[ns] for ns in sorted(by_ns)]
