"""The plugins this package registers (entry point ``mapi.models: mmm``)."""

from __future__ import annotations

from mapi_plugin_mmm.hierarchical_geo import HierarchicalGeoMMM
from mapi_plugin_mmm.pymc_marketing import PyMCMarketingMMM

__all__ = ["PLUGINS"]

PLUGINS = (HierarchicalGeoMMM, PyMCMarketingMMM)
