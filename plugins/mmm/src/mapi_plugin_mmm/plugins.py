"""The plugins this package registers (entry point ``mapi.models: mmm``)."""

from __future__ import annotations

from mapi_plugin_mmm.hierarchical_geo import HierarchicalGeoMMM

__all__ = ["PLUGINS"]

PLUGINS = (HierarchicalGeoMMM,)
