"""Causal-inference model plugins (``causal.*``).

* :class:`~mapi_plugin_causal.impact.CausalImpactModel` (``causal.impact``) - CausalImpact /
  interrupted time series: a Bayesian structural time series (the ``ts.bsts`` core) fitted
  before the intervention predicts the series without it;
* :class:`~mapi_plugin_causal.synthetic.SyntheticControlModel` (``causal.synthetic_control``)
  - Bayesian synthetic control (geo-lift): treated units as a weighted mix of donor units;
* :class:`~mapi_plugin_causal.did.DiDModel` (``causal.did``) - Bayesian difference-in-
  differences / event study with unit and time effects.

Every model stores the effect as the ``impact`` bundle group (see
:mod:`mapi_plugin_causal._common`). They register through the ``mapi.models`` entry point
(``causal = mapi_plugin_causal:PLUGINS``).
"""

from importlib import metadata as _metadata

from mapi_plugin_causal.did import DiDModel
from mapi_plugin_causal.impact import CausalImpactModel, ImpactSettings
from mapi_plugin_causal.synthetic import SyntheticControlModel

try:
    __version__ = _metadata.version("mapi-plugin-causal")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.13.0"

PLUGINS: tuple = (CausalImpactModel, SyntheticControlModel, DiDModel)
"""Every ``causal.*`` model plugin (loaded by the ``mapi.models`` entry point)."""

__all__ = [
    "PLUGINS",
    "CausalImpactModel",
    "DiDModel",
    "ImpactSettings",
    "SyntheticControlModel",
    "__version__",
]
