"""The workflow node contracts and gallery templates in effect for the current catalog (v0.18).

The built-in nodes and templates are static in :mod:`mapi_spec`; installed plugins add node
contracts (``Catalog.workflow_nodes``) and templates (``Catalog.workflow_templates``) through
the runner's catalog export. Everything that validates, starts or lists workflows reads the
merged maps from here, so a plugin node is listed, validated, planned by the engine and
dispatched like a built-in one. The control plane never imports plugin code: a plugin node is
only a JSON contract here and always runs in a sandbox.

Both maps are computed once per catalog digest (catalogs are immutable) and are the built-ins
alone while no catalog is cached for the image digest yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from mapi_spec.library_workflows import all_templates
from mapi_spec.library_workflows import node_contracts as merged_node_contracts
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NodeImplementation
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES, WorkflowTemplate
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.services.catalog import CatalogSnapshot

__all__ = [
    "NODE_PLUGIN_PARAM",
    "contracts_for",
    "node_contracts",
    "templates",
    "templates_for",
]

NODE_PLUGIN_PARAM: Final = "_plugin"
"""The internal job parameter that carries a plugin node's namespace (``contract.plugin``)
on its ``workflow_node`` run, so the dispatcher routes the run to the image that ships the
plugin (:data:`~mapi_spec.executor.PLUGIN_LABEL`). The sandbox drops ``_``-prefixed
parameters before the node sees them."""

_CACHE_SIZE: Final = 4
_BUILTIN_NODES: Final[Mapping[str, NodeImplementation]] = MappingProxyType(NODE_IMPLEMENTATIONS)
_merged: dict[str, tuple[Mapping[str, NodeImplementation], dict[str, WorkflowTemplate]]] = {}


def _entry(
    snapshot: CatalogSnapshot,
) -> tuple[Mapping[str, NodeImplementation], dict[str, WorkflowTemplate]]:
    hit = _merged.get(snapshot.digest)
    if hit is None:
        hit = (merged_node_contracts(snapshot.catalog), all_templates(snapshot.catalog))
        while len(_merged) >= _CACHE_SIZE:
            _merged.pop(next(iter(_merged)))
        _merged[snapshot.digest] = hit
    return hit


def contracts_for(snapshot: CatalogSnapshot | None) -> Mapping[str, NodeImplementation]:
    """Every node contract by id (read-only): the built-in nodes plus the snapshot's plugin
    nodes (a built-in wins an id collision)."""
    return _BUILTIN_NODES if snapshot is None else _entry(snapshot)[0]


def templates_for(snapshot: CatalogSnapshot | None) -> dict[str, WorkflowTemplate]:
    """Every gallery template by id (a new dict): the built-in templates plus the snapshot's
    plugin templates that validate against :func:`contracts_for` (``source="plugin"``)."""
    return dict(WORKFLOW_TEMPLATES if snapshot is None else _entry(snapshot)[1])


async def node_contracts(
    ctx: ServerContext, session: AsyncSession
) -> Mapping[str, NodeImplementation]:
    """The node contracts of the catalog cached for this image digest."""
    return contracts_for(await ctx.catalogs.get(session))


async def templates(ctx: ServerContext, session: AsyncSession) -> dict[str, WorkflowTemplate]:
    """The gallery templates of the catalog cached for this image digest."""
    return templates_for(await ctx.catalogs.get(session))
