"""The plugins this package registers (entry point ``mapi.models: mmm``)."""

from __future__ import annotations

from mapi_plugin_mmm.bayesian import MMMBayesian
from mapi_plugin_mmm.frequentist import MMMFrequentist

__all__ = ["PLUGINS"]

PLUGINS = (MMMBayesian, MMMFrequentist)
