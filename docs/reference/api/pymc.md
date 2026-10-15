# `mapi_pymc`

PyMC models: write `build()`, get engines, diagnostics, new levels and counterfactuals.

## The base class

::: mapi_pymc.model
    options:
      members: [PyMCModel]
      heading_level: 3

## Build context

::: mapi_pymc.context
    options:
      members: [BuildContext, HierEffect, FrozenNoise]
      heading_level: 3

## Engines

::: mapi_pymc.engines
    options:
      members: [EnginePlan, EngineResult, EngineHooks, ChainProgress, run_engine, resolve_engine, engine_available]
      heading_level: 3

## Fast scoring

::: mapi_pymc.frozen
    options:
      members: [FrozenPredictorSpec, FrozenPredictor, create_frozen_predictor, Reducer]
      heading_level: 3

## Checkpoints

::: mapi_pymc.checkpoint
    options:
      members: [CheckpointSink, MemoryCheckpointSink, DirectoryCheckpointSink, SamplingCheckpoint]
      heading_level: 3

## Diagnostics

::: mapi_pymc.diagnostics
    options:
      members: [convergence_diagnostics]
      heading_level: 3
