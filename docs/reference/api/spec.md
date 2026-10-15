# `mapi_spec`

Pure-Pydantic contracts. No numpy, xarray or pandas: the control plane depends on this package.

## Data schema

::: mapi_spec.schema
    options:
      members: [DimPolicy, DimSpec, Role, VarSpec, RaggedSpec, DataSchema, DataSchemaTemplate]
      heading_level: 3

## Documents

::: mapi_spec.spec
    options:
      members: [ModelSpec, DataBlock, Binding, SideTable, StepCall, ModelBlock, FitConfig, ScoreRequest, Horizon, Intervention]
      heading_level: 3

## Loading and hashing

::: mapi_spec.loader
    options:
      members: [load_document, load_model_spec, load_score_request, parse_document, canonical_json, spec_hash, Limits]
      heading_level: 3

## Capabilities, manifests and outputs

::: mapi_spec.capabilities
    options:
      members: [Representation, Capabilities, ModelManifest, OutputDecl, DerivedDecl, OutputSelector, Aggregation, Diagnostic, can_aggregate, summary_allowed, default_summary, parse_summary]
      heading_level: 3

## Schema effects and stages

::: mapi_spec.effects
    options:
      members: [StepKind, SchemaEffect, ContextNeed, Stage, propagate, resolve_effect, apply_effect]
      heading_level: 3

## Static validation

::: mapi_spec.validate
    options:
      members: [validate_spec, SpecValidation, ResolvedSpec, validate_score_request, ScoreMetadata]
      heading_level: 3

## Catalogs

::: mapi_spec.catalog
    options:
      members: [Catalog, StepCatalogEntry, ModelCatalogEntry, PlotCatalogEntry]
      heading_level: 3

## Errors

::: mapi_spec.errors
    options:
      members: [ErrorCode, MapiError, Issue, Problem, Severity, ValidationFailed, http_status, json_pointer]
      heading_level: 3

## Plot envelope

::: mapi_spec.plots
    options:
      members: [PlotSpec, InlineArrow, DataRef, PlotBindings, PlotProvenance]
      heading_level: 3

## Executor protocol

::: mapi_spec.executor
    options:
      members: [Executor, RunSubmit, RunKind, Handle, ExecState, ExecStatus, ExecutorCaps, Isolation, RunEvent, RunEventType]
      heading_level: 3

## JSON Schema export

::: mapi_spec.jsonschema_export
    options:
      members: [export_schemas]
      heading_level: 3
