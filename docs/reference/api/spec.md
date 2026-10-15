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

The executor shapes are re-exported unchanged by `mapi_spec.run`.

::: mapi_spec.executor
    options:
      members: [Executor, RunSubmit, RunKind, Handle, ExecState, ExecStatus, ExecutorCaps, Isolation, RunEvent, RunEventType]
      heading_level: 3

## Run protocol

See [Run protocol](../run-protocol.md) for the narrative specification.

::: mapi_spec.run
    options:
      members: [RunState, FailureClass, RunTrigger, Actor, TransitionRule, transition, can_transition, allowed_triggers, IllegalTransition, RunSpec, RunLimits, RunnerEnv, RunnerExit, failure_class_for_exit, worker_environ, RunnerPhase, StatePayload, ProgressPayload, ChainProgress, LogPayload, WarningPayload, CheckpointPayload, MetricPayload, CompletePayload, ErrorPayload, parse_payload, make_event, canonical_bytes, sign_event, verify_event, compute_event_hmac, encode_event_line, decode_event_line, event_from_log_payload, InvalidEvent, OutputManifest, ManifestObject, InputDigest, verify_output_objects, VerificationResult, sign_output_manifest, verify_output_manifest, BundleManifest, BundleObject, ManifestSignature, sign_bundle_manifest, verify_bundle_manifest, catalog_digest, new_nonce, new_hmac_key, decode_hmac_key]
      heading_level: 3

## Job payloads

See [Job payloads and object names](../run-protocol.md#job-payloads-and-object-names).

::: mapi_spec.jobs
    options:
      members: [IngestJob, IngestSource, MffOptions, DescribeJob, ScoreJob, EvaluateJob, DeriveJob, RenderJob, MetricsSidecar, parse_job, parse_model_job_params, side_table_input, SCORE_INPUTS, DATASET_SCHEMA_ATTR, DATASET_OUTPUTS, FIT_SIDECARS, BUNDLE_MANIFEST_OUTPUT]
      heading_level: 3

## UI views

::: mapi_spec.views
    options:
      members: [PreviewView, PreviewColumn, ProfileView, VarProfile, MissingnessGrid, TreeIndexView, TreeGroup, TreeCoord, TreeVar]
      heading_level: 3

## Object storage

::: mapi_spec.storage
    options:
      members: [ObjectStore, ObjectMeta, ObjectPage, SignedGet, SignedPut, PostPolicy, StorageError, NotFound, PreconditionFailed, AccessDenied, BucketPurpose, Buckets, bucket_name, quarantine_key, bootstrap_key, run_prefix, run_checkpoint_prefix, bundle_prefix, dataset_prefix, check_object_name, check_segment, check_ttl, crc32c, crc32c_to_base64, crc32c_from_base64, md5_base64]
      heading_level: 3

## REST DTOs

See [HTTP API (v1)](../http-api.md) for the endpoint table and conventions.

::: mapi_spec.api
    options:
      members: [Page, Project, Dataset, DatasetCreate, UploadCreate, UploadSession, IngestMapping, DimMapping, VarMapping, DatasetVersionCreate, DatasetVersion, DatasetVersionStatus, SpecCreate, SpecDoc, SpecRevisionCreate, SpecRevision, ValidateRequest, ValidateResponse, StageSchema, RunCreate, RunRetry, Run, RunCost, ProgressSnapshot, RunEventView, LogLine, SseEventName, Model, ModelVersion, Scorecard, ScorecardItem, Lineage, ScoreCall, ScoreCallResult, ArtifactLink, ObjectRef, PlotListItem, PlotValidateResponse, CatalogResponse, Health, HealthCheck, EndpointDecl, M1_ENDPOINTS, encode_sse, parse_last_event_id, parse_prefer_wait, check_idempotency_key, loc_to_pointer, new_id]
      heading_level: 3

## Local development

::: mapi_spec.localdev
    options:
      members: [LocalDev]
      heading_level: 3

## JSON Schema export

::: mapi_spec.jsonschema_export
    options:
      members: [export_schemas]
      heading_level: 3
