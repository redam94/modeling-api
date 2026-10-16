"""PyMC model base classes for modeling-api (plan §5).

Plugin authors subclass :class:`PyMCModel` and implement only ``build(ds, settings, ctx)``.
The platform provides engines (nutpie, PyMC NUTS with segmented sampling, MAP, ADVI,
Pathfinder), opt-in ArviZ groups, convergence diagnostics, rebuild-and-predict scoring
with common random numbers for counterfactuals, new-level handling through
:meth:`BuildContext.hier_effect` and a compiled :class:`FrozenPredictor` for fast scoring.
"""

from importlib import metadata as _metadata

from mapi_pymc.checkpoint import (
    CheckpointSink,
    DirectoryCheckpointSink,
    MemoryCheckpointSink,
    SamplingCheckpoint,
)
from mapi_pymc.context import BuildContext, FrozenNoise, HierEffect
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_pymc.engines import (
    ENGINES,
    ChainProgress,
    EngineHooks,
    EnginePlan,
    EngineResult,
    engine_available,
    resolve_engine,
    run_engine,
)
from mapi_pymc.frozen import (
    FrozenPredictor,
    FrozenPredictorSpec,
    Reducer,
    create_frozen_predictor,
)
from mapi_pymc.model import DEFAULT_CAPABILITIES, PyMCModel

try:
    __version__ = _metadata.version("mapi-pymc")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.4.0"

__all__ = [
    "DEFAULT_CAPABILITIES",
    "ENGINES",
    "BuildContext",
    "ChainProgress",
    "CheckpointSink",
    "DirectoryCheckpointSink",
    "EngineHooks",
    "EnginePlan",
    "EngineResult",
    "FrozenNoise",
    "FrozenPredictor",
    "FrozenPredictorSpec",
    "HierEffect",
    "MemoryCheckpointSink",
    "PyMCModel",
    "Reducer",
    "SamplingCheckpoint",
    "__version__",
    "convergence_diagnostics",
    "create_frozen_predictor",
    "engine_available",
    "resolve_engine",
    "run_engine",
]
