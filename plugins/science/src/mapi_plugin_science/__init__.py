"""Scientific model plugins (``sci.*``): fields, simulators and image inverse problems.

Ported from the pymc-challenges case studies:

* ``sci.lgcp_grid`` - a log-Gaussian Cox process on a space-time count grid with a 2-D
  HSGP field per era (E07, Oklahoma earthquakes);
* ``sci.fisher_kpp`` - a Fisher-KPP reaction-diffusion PDE solved inside the model, fitted
  to cell counts along one space dim over time (E50, scratch assay);
* ``sci.traction_force`` - a Gaussian inverse problem in Fourier space that recovers a 2-D
  traction field from a displacement field, with exact posterior draws (E60);
* ``sci.strong_lens`` - a pixel forward model of a galaxy-scale strong lens, with a
  pixelated source and critical curves per draw (E80).

Every plugin registers through the ``mapi.models`` entry point
(``science = mapi_plugin_science:PLUGINS``). Outputs that are 2-D fields declare a
``grid`` so the Model Explorer draws them in its field viewer.
"""

from importlib import metadata as _metadata

from mapi_plugin_science.kpp import FisherKPPModel, FisherKPPSettings
from mapi_plugin_science.lens import StrongLensModel, StrongLensSettings
from mapi_plugin_science.lgcp import LGCPGridModel, LGCPGridSettings
from mapi_plugin_science.traction import TractionForceModel, TractionForceSettings

try:
    __version__ = _metadata.version("mapi-plugin-science")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.6.0"

PLUGINS = (LGCPGridModel, FisherKPPModel, TractionForceModel, StrongLensModel)
"""Every ``sci.*`` model plugin (loaded by the ``mapi.models`` entry point)."""

__all__ = [
    "PLUGINS",
    "FisherKPPModel",
    "FisherKPPSettings",
    "LGCPGridModel",
    "LGCPGridSettings",
    "StrongLensModel",
    "StrongLensSettings",
    "TractionForceModel",
    "TractionForceSettings",
    "__version__",
]
