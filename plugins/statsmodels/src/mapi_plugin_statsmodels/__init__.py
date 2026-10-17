"""statsmodels time-series model plugins (``ts.*``, mapi-plugin-statsmodels).

==============================  ================================================================
plugin                          model
==============================  ================================================================
``ts.sarimax``                  seasonal ARIMA with drivers (ARIMAX/SARIMAX), state space
``ts.arima``                    ARIMA without drivers, state space
``ts.unobserved_components``    level/trend + season + cycle + noise (structural model)
``ts.ets``                      exponential smoothing (error, trend, season)
``ts.theta``                    the theta method (a strong simple baseline)
``ts.ardl``                     autoregressive distributed lags
``ts.ecm``                      single-equation error correction (UECM + bounds test)
``ts.var``                      vector autoregression (IRF, FEVD, Granger)
``ts.vecm``                     vector error correction (Johansen rank selection)
``ts.varmax``                   VARMA/VARX with drivers, state space
``ts.dynamic_factor``           dynamic factor model, state space
``ts.markov_switching``         Markov regime-switching regression/autoregression
==============================  ================================================================

Every plugin fits one model per series of a panel (``settings.series_dims``), returns
prediction-interval quantiles and is rebuilt from stored parameters - never pickled (see
:mod:`mapi_plugin_statsmodels.base`). They register through the ``mapi.models`` entry
point (``statsmodels = mapi_plugin_statsmodels:PLUGINS``).
"""

from importlib import metadata as _metadata
from typing import Any

from mapi_plugin_statsmodels.ardl import ArdlModel, ArdlSettings, EcmModel, EcmSettings
from mapi_plugin_statsmodels.base import StatsmodelsModel
from mapi_plugin_statsmodels.markov import MarkovSwitchingModel, MarkovSwitchingSettings
from mapi_plugin_statsmodels.multivariate import (
    DynamicFactorModel,
    DynamicFactorSettings,
    VarmaxModel,
    VarmaxSettings,
    VarModel,
    VarSettings,
    VecmModel,
    VecmSettings,
)
from mapi_plugin_statsmodels.smoothing import EtsModel, EtsSettings, ThetaModel, ThetaSettings
from mapi_plugin_statsmodels.statespace import (
    ArimaModel,
    ArimaSettings,
    SarimaxModel,
    SarimaxSettings,
    UnobservedComponentsModel,
    UnobservedComponentsSettings,
)

try:
    __version__ = _metadata.version("mapi-plugin-statsmodels")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.6.0"

PLUGINS: tuple[type[StatsmodelsModel[Any]], ...] = (
    SarimaxModel,
    ArimaModel,
    UnobservedComponentsModel,
    EtsModel,
    ThetaModel,
    ArdlModel,
    EcmModel,
    VarModel,
    VecmModel,
    VarmaxModel,
    DynamicFactorModel,
    MarkovSwitchingModel,
)
"""Every statsmodels plugin (loaded by the ``mapi.models`` entry point)."""

PLUGIN_IDS: tuple[str, ...] = tuple(p.id for p in PLUGINS)

__all__ = [
    "PLUGINS",
    "PLUGIN_IDS",
    "ArdlModel",
    "ArdlSettings",
    "ArimaModel",
    "ArimaSettings",
    "DynamicFactorModel",
    "DynamicFactorSettings",
    "EcmModel",
    "EcmSettings",
    "EtsModel",
    "EtsSettings",
    "MarkovSwitchingModel",
    "MarkovSwitchingSettings",
    "SarimaxModel",
    "SarimaxSettings",
    "StatsmodelsModel",
    "ThetaModel",
    "ThetaSettings",
    "UnobservedComponentsModel",
    "UnobservedComponentsSettings",
    "VarModel",
    "VarSettings",
    "VarmaxModel",
    "VarmaxSettings",
    "VecmModel",
    "VecmSettings",
    "__version__",
]
