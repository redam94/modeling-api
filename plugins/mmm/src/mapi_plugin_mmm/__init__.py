"""Marketing-mix model plugins (hierarchical geo MMM, pymc-marketing adapter).

* :class:`~mapi_plugin_mmm.hierarchical_geo.HierarchicalGeoMMM` - ``mmm.hierarchical_geo``,
  the E33 hierarchical geo MMM (PyMC);
* :class:`~mapi_plugin_mmm.pymc_marketing.PyMCMarketingMMM` - ``mmm.pymc_marketing``, an
  adapter around ``pymc_marketing.mmm.MMM`` that owns scaling, carry-over and units.

Both are registered through the ``mapi.models`` entry point (``mapi_plugin_mmm.plugins``).
Attributes are imported lazily, so ``import mapi_plugin_mmm`` stays cheap.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

__all__ = ["PLUGINS", "HierarchicalGeoMMM", "PyMCMarketingMMM", "__version__"]

if TYPE_CHECKING:
    from mapi_plugin_mmm.hierarchical_geo import HierarchicalGeoMMM
    from mapi_plugin_mmm.plugins import PLUGINS
    from mapi_plugin_mmm.pymc_marketing import PyMCMarketingMMM

_LAZY = {
    "HierarchicalGeoMMM": "mapi_plugin_mmm.hierarchical_geo",
    "PyMCMarketingMMM": "mapi_plugin_mmm.pymc_marketing",
    "PLUGINS": "mapi_plugin_mmm.plugins",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module 'mapi_plugin_mmm' has no attribute {name!r}")
