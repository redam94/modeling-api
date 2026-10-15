"""Tabular model plugins (``tabular.*``).

* :class:`~mapi_plugin_tabular.glm.HierarchicalGLM` (``tabular.hierarchical_glm``) - a
  partially pooled GLM in long layout with hyperprior new levels (E02 radon style);
* :class:`~mapi_plugin_tabular.regressor.TabularRegressor` (``tabular.regressor``) - a
  quantile regressor over stacked rows with the registry's quantile-capable estimators
  (plan §6.2 store demand).

Both register through the ``mapi.models`` entry point (``tabular = mapi_plugin_tabular:PLUGINS``).
"""

from importlib import metadata as _metadata

from mapi_plugin_tabular.glm import HierarchicalGLM, HierarchicalGLMSettings
from mapi_plugin_tabular.regressor import TabularRegressor, TabularRegressorSettings

try:
    __version__ = _metadata.version("mapi-plugin-tabular")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.2.0"

PLUGINS = (HierarchicalGLM, TabularRegressor)
"""Every ``tabular.*`` model plugin (loaded by the ``mapi.models`` entry point)."""

__all__ = [
    "PLUGINS",
    "HierarchicalGLM",
    "HierarchicalGLMSettings",
    "TabularRegressor",
    "TabularRegressorSettings",
    "__version__",
]
