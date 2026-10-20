"""The plugins this package registers (entry point ``mapi.models: mmm``)."""

from __future__ import annotations

from mapi_plugin_mmm.bayesian import MMMBayesian
from mapi_plugin_mmm.frequentist import MMMFrequentist
from mapi_plugin_mmm.multivariate import MMMMultivariate
from mapi_plugin_mmm.nested import MMMNested

__all__ = ["PLUGINS"]

PLUGINS = (MMMBayesian, MMMFrequentist, MMMNested, MMMMultivariate)
