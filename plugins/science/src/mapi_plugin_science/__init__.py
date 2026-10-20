"""Scientific model plugins (``sci.*``): fields, simulators and image inverse problems.

Ported from the pymc-challenges case studies:

* ``sci.lgcp_grid`` - a log-Gaussian Cox process on a space-time count grid with a 2-D
  HSGP field per era (E07, Oklahoma earthquakes);
* ``sci.fisher_kpp`` - a Fisher-KPP reaction-diffusion PDE solved inside the model, fitted
  to cell counts along one space dim over time (E50, scratch assay);
* ``sci.traction_force`` - a Gaussian inverse problem in Fourier space that recovers a 2-D
  traction field from a displacement field, with exact posterior draws (E60);
* ``sci.strong_lens`` - a pixel forward model of a galaxy-scale strong lens, with a
  pixelated source and critical curves per draw (E80);
* ``sci.heat_plate`` - the 2-D steady heat equation of a heated plate, a finite-volume
  linear system solved inside the model, calibrated on thermocouple readings (E51);
* ``sci.turing_pattern`` - Schnakenberg reaction-diffusion kinetics from early images of a
  growing Turing pattern, with the exact Fourier-mode likelihood of the linearised dynamics,
  the dispersion relation, P(Turing) and simulated patterns (E61 part A);
* ``sci.storm_localisation`` - single-molecule localisation in raw EMCCD STORM frames: spot
  detection and camera calibration in NumPy, then a batched pixel-integrated Gaussian PSF
  fit with Poisson counts, and a super-resolved render (E58);
* ``sci.ibp_features`` - latent binary features of small images, a truncated Indian buffet
  process with every image's pattern summed out (E31, handwritten digits);
* ``sci.colour_naming`` - how the speakers of one language name colour chips: a softmax
  over quadratic CIELAB logits, pooled or with speaker effects, mapped on the chip palette
  (E77, World Color Survey);
* ``sci.cmb_field`` - field-level inference of a masked CMB map: sky draws behind the mask
  and bandpowers from a Wandelt-Eriksen Gibbs sampler (E79);
* ``sci.areal_bym2`` - disease counts in areas on a graph of neighbours, with iid, ICAR,
  proper CAR or BYM2 area effects; the polygons go into the bundle for the maps (E18);
* ``sci.network_latent_space`` - an undirected graph as Bernoulli ties between pairs of
  nodes: pair covariates, sociality and Procrustes-aligned latent positions (E21);
* ``sci.graphical_model`` - a Gaussian graphical model: partial correlations and edge
  probabilities from a dense LKJ model or horseshoe neighbourhood regressions (E19, Sachs
  protein signalling);
* ``sci.phylo_regression`` - a regression across species whose residuals are correlated by
  a phylogeny (i.i.d., Brownian motion, Pagel's lambda, Ornstein-Uhlenbeck), with traits
  predicted from relatives (E23, primate brains);
* ``sci.circular_regression`` - directions and times of day on the circle: a von Mises
  regression with a canonical link on harmonics of the hour and the day of year (or a
  projected normal, or a mixture of two regimes), and log-Fourier activity densities with
  overlaps (E81, Santa Barbara winds and Barro Colorado camera traps).

Every plugin registers through the ``mapi.models`` entry point
(``science = mapi_plugin_science:PLUGINS``). Outputs that are 2-D fields declare a
``grid`` so the Model Explorer draws them in its field viewer.
"""

from importlib import metadata as _metadata

from mapi_plugin_science.areal import ArealBYM2Model, ArealBYM2Settings
from mapi_plugin_science.circular import CircularRegressionModel, CircularRegressionSettings
from mapi_plugin_science.cmb import CMBFieldModel, CMBFieldSettings
from mapi_plugin_science.colour_naming import ColourNamingModel, ColourNamingSettings
from mapi_plugin_science.ggm import GraphicalModel, GraphicalModelSettings
from mapi_plugin_science.heat_plate import HeatPlateModel, HeatPlateSettings
from mapi_plugin_science.ibp import IBPFeaturesModel, IBPFeaturesSettings
from mapi_plugin_science.kpp import FisherKPPModel, FisherKPPSettings
from mapi_plugin_science.lens import StrongLensModel, StrongLensSettings
from mapi_plugin_science.lgcp import LGCPGridModel, LGCPGridSettings
from mapi_plugin_science.network import NetworkLatentSpaceModel, NetworkLatentSpaceSettings
from mapi_plugin_science.phylo import PhyloRegressionModel, PhyloRegressionSettings
from mapi_plugin_science.storm import StormLocalisationModel, StormLocalisationSettings
from mapi_plugin_science.traction import TractionForceModel, TractionForceSettings
from mapi_plugin_science.turing import TuringPatternModel, TuringPatternSettings

try:
    __version__ = _metadata.version("mapi-plugin-science")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.13.0"

PLUGINS = (
    LGCPGridModel,
    FisherKPPModel,
    TractionForceModel,
    StrongLensModel,
    HeatPlateModel,
    TuringPatternModel,
    StormLocalisationModel,
    IBPFeaturesModel,
    ColourNamingModel,
    CMBFieldModel,
    ArealBYM2Model,
    NetworkLatentSpaceModel,
    GraphicalModel,
    PhyloRegressionModel,
    CircularRegressionModel,
)
"""Every ``sci.*`` model plugin (loaded by the ``mapi.models`` entry point)."""

__all__ = [
    "PLUGINS",
    "ArealBYM2Model",
    "ArealBYM2Settings",
    "CMBFieldModel",
    "CMBFieldSettings",
    "CircularRegressionModel",
    "CircularRegressionSettings",
    "ColourNamingModel",
    "ColourNamingSettings",
    "FisherKPPModel",
    "FisherKPPSettings",
    "GraphicalModel",
    "GraphicalModelSettings",
    "HeatPlateModel",
    "HeatPlateSettings",
    "IBPFeaturesModel",
    "IBPFeaturesSettings",
    "LGCPGridModel",
    "LGCPGridSettings",
    "NetworkLatentSpaceModel",
    "NetworkLatentSpaceSettings",
    "PhyloRegressionModel",
    "PhyloRegressionSettings",
    "StormLocalisationModel",
    "StormLocalisationSettings",
    "StrongLensModel",
    "StrongLensSettings",
    "TractionForceModel",
    "TractionForceSettings",
    "TuringPatternModel",
    "TuringPatternSettings",
    "__version__",
]
