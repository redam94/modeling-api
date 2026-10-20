"""Marketing-mix model plugins on mmm-framework (the platform's only MMM engine, v0.5).

* :class:`~mapi_plugin_mmm.bayesian.MMMBayesian` - ``mmm.bayesian``, mmm-framework's
  ``BayesianMMM`` (national, or hierarchical over geos and products);
* :class:`~mapi_plugin_mmm.frequentist.MMMFrequentist` - ``mmm.frequentist``, its ridge
  estimator with bootstrap intervals;
* :class:`~mapi_plugin_mmm.nested.MMMNested` - ``mmm.nested``, mmm-framework's
  ``NestedMMM`` (channels act on the KPI directly and through mediator series);
* :class:`~mapi_plugin_mmm.multivariate.MMMMultivariate` - ``mmm.multivariate``,
  mmm-framework's ``MultivariateMMM`` (several outcomes with correlated noise and
  cross-effects).

All are registered through the ``mapi.models`` entry point (``mapi_plugin_mmm.plugins``).
Attributes are imported lazily, so ``import mapi_plugin_mmm`` stays cheap (mmm-framework
is only imported when a model is built).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.15.1"

__all__ = [
    "PLUGINS",
    "MMMBayesian",
    "MMMFrequentist",
    "MMMMultivariate",
    "MMMNested",
    "__version__",
]

if TYPE_CHECKING:
    from mapi_plugin_mmm.bayesian import MMMBayesian
    from mapi_plugin_mmm.frequentist import MMMFrequentist
    from mapi_plugin_mmm.multivariate import MMMMultivariate
    from mapi_plugin_mmm.nested import MMMNested
    from mapi_plugin_mmm.plugins import PLUGINS

_LAZY = {
    "MMMBayesian": "mapi_plugin_mmm.bayesian",
    "MMMFrequentist": "mapi_plugin_mmm.frequentist",
    "MMMMultivariate": "mapi_plugin_mmm.multivariate",
    "MMMNested": "mapi_plugin_mmm.nested",
    "PLUGINS": "mapi_plugin_mmm.plugins",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module 'mapi_plugin_mmm' has no attribute {name!r}")
