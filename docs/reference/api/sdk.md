# `mapi_sdk`

The xarray runtime. Runs in notebooks and runner images, never in the control plane.

## Fit and score

::: mapi_sdk.harness
    options:
      members: [fit_spec, score_bundle, FitHooks]
      heading_level: 3

## Steps

::: mapi_sdk.steps
    options:
      members: [Step, StepSettings, StepState, StepContext, CancelToken]
      heading_level: 3

## Models

::: mapi_sdk.models
    options:
      members: [CustomModel, ModelPlugin, ModelSettings, FitContext, FitOutput, Fitted, ScoreContext, new_level_mask, build_manifest, PREDICTIONS]
      heading_level: 3

## Pipelines

::: mapi_sdk.pipeline
    options:
      members: [Pipeline, PipelineStep]
      heading_level: 3

## Bundles

::: mapi_sdk.bundle
    options:
      members: [Bundle, BundleManifest, MetricsReport, FileDigest]
      heading_level: 3

## Coordinates

::: mapi_sdk.coords
    options:
      members: [CoordPlan, canonicalise_coords, CanonicalPlan, horizon_coords]
      heading_level: 3

## Binding

::: mapi_sdk.binding
    options:
      members: [BindingMap]
      heading_level: 3

## Data checks

::: mapi_sdk.schema
    options:
      members: [infer_schema, validate_dataset, check_dataset, SchemaIssue, data_summary]
      heading_level: 3

## Summaries

::: mapi_sdk.summarise
    options:
      members: [aggregate, hdi, thin_index]
      heading_level: 3

## Registry

::: mapi_sdk.registry
    options:
      members: [Registry, default_registry]
      heading_level: 3

## In-process executor

::: mapi_sdk.executors.inproc
    options:
      members: [InprocExecutor, mapping_resolver]
      heading_level: 3

## Wire formats

::: mapi_sdk.wire
    options:
      members: [to_xrjson, from_xrjson, write_zarr_zip, read_zarr_zip, write_datatree_zip, read_datatree_zip, stack_rows, unstack_rows, to_arrow_table]
      heading_level: 3

## Contract kit

::: mapi_sdk.testing.checks
    options:
      members: [check_step, check_model, ModelFixture, ContractReport, ContractViolation]
      heading_level: 3
