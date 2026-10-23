"""The catalog an image exports (``describe`` runs and the image build's ``warm.py``).

:func:`full_catalog` is :meth:`mapi_sdk.registry.Registry.catalog` (steps, models and plot
recipes, with ``provider`` on entries from non-built-in distributions) plus what the
workflow entry points of the image add (v0.18, D6, D7, D12):

* ``workflow_nodes``: the contracts of the valid plugin nodes (``mapi.workflow_nodes``);
* ``workflow_templates``: the valid plugin workflow templates (``mapi.workflow_templates``),
  each validated against the built-in contracts merged with the plugin ones;
* ``diagnostics``: everything an entry point exported that is not in the catalog (the
  registry's refusals, refused node contracts and templates, governance check runners that
  could not load), at most :data:`~mapi_spec.catalog.DIAGNOSTICS_PER_DISTRIBUTION_MAX` per
  distribution and :data:`~mapi_spec.catalog.CATALOG_DIAGNOSTICS_MAX` in all.

All three are empty for an image of built-ins only, so its catalog JSON and digest are the
ones :meth:`~mapi_sdk.registry.Registry.catalog` gives. This module imports plugin code (it
loads entry points): only the worker and the image build use it, never the supervisor.

v0.19: ``full_catalog(registry, only_distribution=...)`` keeps what one origin exports (a
PEP 503 distribution name, or ``upload:<pvid>`` for a mounted upload): the ``plugin_describe``
run exports only its upload's entries.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterable
from typing import Any

from mapi_sdk.registry import Registry
from mapi_spec.catalog import (
    CATALOG_DIAGNOSTICS_MAX,
    DIAGNOSTICS_PER_DISTRIBUTION_MAX,
    Catalog,
    CatalogDiagnostic,
    ModelCatalogEntry,
)
from mapi_workflows.runtime.governance import load_check_runners
from mapi_workflows.runtime.nodes import (
    all_node_contracts,
    load_node_exports,
    load_plugin_templates,
)

__all__ = ["cap_diagnostics", "extension_versions", "full_catalog"]


def _norm(origin: str) -> str:
    if origin.startswith("upload:"):
        return origin
    return origin.strip().lower().replace("_", "-").replace(".", "-")


def full_catalog(registry: Registry, *, only_distribution: str | None = None) -> Catalog:
    """The image's catalog: ``registry.catalog()`` with the plugin workflow nodes and
    templates and every load diagnostic (capped; see :func:`cap_diagnostics`).

    ``only_distribution`` (v0.19) keeps only what the entry points of one origin export and
    the diagnostics they caused: a distribution name (compared in its PEP 503 form), or
    ``upload:<plugin version id>`` for a mounted upload (an upload never matches a plain
    distribution name, even when its distribution is called like an installed one). Built-in
    nodes and templates are never part of it; the node contracts the templates are validated
    against still include the built-in and every other valid plugin contract."""
    if only_distribution is None:
        return _catalog(registry, keep=None)
    return _catalog(registry, keep=_norm(only_distribution))


def _catalog(registry: Registry, *, keep: str | None) -> Catalog:
    base = registry.catalog()
    exports, node_diags = load_node_exports(overlays=registry.overlays)
    nodes = [exports[k].contract for k in sorted(exports)]
    templates, template_diags = load_plugin_templates(
        all_node_contracts(nodes), overlays=registry.overlays, only=keep
    )
    check_diags: list[CatalogDiagnostic] = []
    load_check_runners(diagnostics=check_diags, overlays=registry.overlays, only=keep)
    if keep is None:
        return Catalog(
            steps=base.steps,
            models=base.models,
            plots=base.plots,
            workflow_nodes=nodes,
            workflow_templates=sorted(templates, key=lambda t: t.id),
            diagnostics=cap_diagnostics(
                [*registry.diagnostics, *node_diags, *template_diags, *check_diags]
            ),
        )
    own_nodes, own_node_diags = load_node_exports(overlays=registry.overlays, only=keep)

    def mine(kind: Any, id_: str, version: str) -> bool:
        return registry.origin_of(kind, id_, version) == keep

    models: list[ModelCatalogEntry] = [
        m for m in base.models if mine("model", m.manifest.id, m.manifest.version)
    ]
    return Catalog(
        steps=[s for s in base.steps if mine("step", s.id, s.version)],
        models=models,
        plots=[p for p in base.plots if mine("plot", p.id, p.version)],
        workflow_nodes=[own_nodes[k].contract for k in sorted(own_nodes)],
        workflow_templates=sorted(templates, key=lambda t: t.id),
        diagnostics=cap_diagnostics(
            [*registry.diagnostics_of(keep), *own_node_diags, *template_diags, *check_diags]
        ),
    )


def _more(first: CatalogDiagnostic, count: int, *, distribution: str | None) -> CatalogDiagnostic:
    noun = "diagnostic is" if count == 1 else "diagnostics are"
    whose = "of this distribution " if distribution is not None else ""
    return CatalogDiagnostic(
        code=first.code,
        group=first.group,
        distribution=distribution,
        message=f"{count} more {noun} {whose}not listed. Run `mapi plugin doctor` to see all.",
    )


def cap_diagnostics(rows: Iterable[CatalogDiagnostic]) -> list[CatalogDiagnostic]:
    """``rows`` in order, keeping the first
    :data:`~mapi_spec.catalog.DIAGNOSTICS_PER_DISTRIBUTION_MAX` of each distribution and
    adding one row per distribution that says how many more there were; then at most
    :data:`~mapi_spec.catalog.CATALOG_DIAGNOSTICS_MAX` rows in all (the last one says how
    many were left out). One broken distribution cannot flood the catalog."""
    kept: list[CatalogDiagnostic] = []
    seen: dict[str | None, int] = {}
    dropped: dict[str | None, tuple[CatalogDiagnostic, int]] = {}
    for row in rows:
        n = seen.get(row.distribution, 0)
        seen[row.distribution] = n + 1
        if n < DIAGNOSTICS_PER_DISTRIBUTION_MAX:
            kept.append(row)
        else:
            first, count = dropped.get(row.distribution, (row, 0))
            dropped[row.distribution] = (first, count + 1)
    kept += [_more(first, count, distribution=d) for d, (first, count) in dropped.items()]
    if len(kept) <= CATALOG_DIAGNOSTICS_MAX:
        return kept
    head = kept[: CATALOG_DIAGNOSTICS_MAX - 1]
    return [*head, _more(kept[len(head)], len(kept) - len(head), distribution=None)]


def extension_versions(catalog: Catalog) -> dict[str, str]:
    """``{distribution: version}`` of every non-built-in distribution the catalog names as
    a ``provider`` (``"unknown"`` when its metadata is not readable)."""
    providers: set[str] = set()
    entries: list[Any] = [*catalog.steps, *catalog.models, *catalog.plots]
    entries += catalog.workflow_templates
    for e in entries:
        if e.provider:
            providers.add(e.provider)
    out: dict[str, str] = {}
    for dist in sorted(providers):
        try:
            out[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            out[dist] = "unknown"
    return out
