"""Marketing-mix model plugins on mmm-framework (the platform's only MMM engine, v0.5).

* :class:`~mapi_plugin_mmm.bayesian.MMMBayesian` - ``mmm.bayesian``, mmm-framework's
  ``BayesianMMM`` (national, or hierarchical over geos and products);
* :class:`~mapi_plugin_mmm.frequentist.MMMFrequentist` - ``mmm.frequentist``, its ridge
  estimator with bootstrap intervals.

Both are registered through the ``mapi.models`` entry point (``mapi_plugin_mmm.plugins``).
Attributes are imported lazily, so ``import mapi_plugin_mmm`` stays cheap (mmm-framework
is only imported when a model is built).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.9.0"

__all__ = ["PLUGINS", "MMMBayesian", "MMMFrequentist", "__version__"]

if TYPE_CHECKING:
    from mapi_plugin_mmm.bayesian import MMMBayesian
    from mapi_plugin_mmm.frequentist import MMMFrequentist
    from mapi_plugin_mmm.plugins import PLUGINS

_LAZY = {
    "MMMBayesian": "mapi_plugin_mmm.bayesian",
    "MMMFrequentist": "mapi_plugin_mmm.frequentist",
    "PLUGINS": "mapi_plugin_mmm.plugins",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module 'mapi_plugin_mmm' has no attribute {name!r}")
