"""Time-series and state-space model plugins (``ts.*``).

* :class:`~mapi_plugin_ts.trend.HSGPTrendModel` (``ts.hsgp_trend``) - an HSGP trend with
  Fourier seasonality whose domain is fixed from the anticipated coordinates (C08);
* :class:`~mapi_plugin_ts.hmm.GaussianHMMModel` (``ts.gaussian_hmm``) - a K-state Gaussian
  HMM over padded sequences with a terminal state, forecasting and filtering (E24).

Both register through the ``mapi.models`` entry point (``ts = mapi_plugin_ts:PLUGINS``).
"""

from importlib import metadata as _metadata

from mapi_plugin_ts.hmm import GaussianHMMModel, GaussianHMMSettings
from mapi_plugin_ts.trend import HSGPTrendModel, HSGPTrendSettings

try:
    __version__ = _metadata.version("mapi-plugin-ts")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.8.0"

PLUGINS = (HSGPTrendModel, GaussianHMMModel)
"""Every ``ts.*`` model plugin (loaded by the ``mapi.models`` entry point)."""

__all__ = [
    "PLUGINS",
    "GaussianHMMModel",
    "GaussianHMMSettings",
    "HSGPTrendModel",
    "HSGPTrendSettings",
    "__version__",
]
