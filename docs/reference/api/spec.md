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

See [HTTP API (v1)](../http-api.md) for the endpoint tables, conventions and the
[multi-tenant additions](../http-api.md#multi-tenant-api) of v0.3.

::: mapi_spec.api
    options:
      members: [Page, Dataset, DatasetCreate, UploadCreate, UploadSession, IngestMapping, DimMapping, VarMapping, DatasetVersionCreate, DatasetVersion, DatasetVersionStatus, SpecCreate, SpecDoc, SpecRevisionCreate, SpecRevision, ValidateRequest, ValidateResponse, StageSchema, RunCreate, RunRetry, Run, ProgressSnapshot, RunEventView, LogLine, SseEventName, Model, ModelVersion, Scorecard, ScorecardItem, Lineage, ScoreCall, ScoreCallResult, ArtifactLink, ObjectRef, PlotListItem, PlotValidateResponse, CatalogResponse, Health, HealthCheck, Me, MeTenant, MeProject, TenantCreate, TenantUpdate, TenantAdminUpdate, ProjectCreate, ProjectUpdate, MembershipCreate, MembershipUpdate, UsageReport, EndpointDecl, M1_ENDPOINTS, M2_ENDPOINTS, V03_ENDPOINTS, ENDPOINTS, RouteScope, RouteAccess, ROUTE_ACCESS, route_access, encode_sse, parse_last_event_id, parse_prefer_wait, check_idempotency_key, loc_to_pointer, new_id]
      heading_level: 3

## Tenancy and authorization

See [Tenancy and authentication](../../concepts/tenancy-and-auth.md).

::: mapi_spec.tenancy
    options:
      members: [Tenant, Project, TrustTier, TenantStatus, ResidencyRegion, region_satisfies, PlacementRequirement, TIER_PLACEMENT, placement_allows, ProjectRole, Action, ResourceKind, Level, Rule, PERMISSIONS, OPERATOR_PERMISSIONS, required_rule, can, Membership, effective_role, PrincipalKind, UserPrincipal, TokenPrincipal, ServicePrincipal, InternalService, Principal, AuthContext, PROJECT_HEADER, ProjectSelector, parse_project_selector, resolve_project_scope]
      heading_level: 3

## API tokens

::: mapi_spec.tokens
    options:
      members: [ParsedToken, generate_token, parse_token, token_checksum, looks_like_token, redact_token, bearer_token, hash_token, verify_token_hash, ScopeName, TokenScope, parse_scope, scope_allows, TokenCreate, ApiToken, TokenCreated, TokenRecord, TokenStatus, BudgetPeriod]
      heading_level: 3

## Sessions and Pub/Sub push

::: mapi_spec.auth
    options:
      members: [pkce_verifier, pkce_challenge, random_state, safe_return_to, LoginTransaction, build_authorize_url, CookieSpec, SessionInfo, PubSubMessage, PubSubPushEnvelope, PushAuthExpectation, check_push_claims]
      heading_level: 3

## Quotas and scheduling

See [Quotas and cost](../../concepts/quotas-and-cost.md).

::: mapi_spec.quotas
    options:
      members: [QuotaScope, QuotaLimits, QuotaPolicy, QuotaPolicyUpsert, QuotaUsage, QuotaDemand, QuotaDecision, LimitName, evaluate_quota, QuotaLimitStatus, quota_status, window_reset, DEFAULT_TIER_LIMITS, DEFAULT_GLOBAL_LIMITS, policy_for_token, PriorityClass, PRIORITY_WEIGHTS, default_priority, LeaseCandidate, plan_leases, backoff_seconds]
      heading_level: 3

## Cost

::: mapi_spec.cost
    options:
      members: [PriceTable, ClassPrice, DEFAULT_PRICE_TABLE, ResourceUsage, class_usage, price_usage, estimate_cost, CostEstimate, CostEntryKind, CostLedgerEntry, ledger_entry, CostTotals, summarize_ledger, RunCost, format_micros]
      heading_level: 3

## Audit

::: mapi_spec.audit
    options:
      members: [AuditAction, AuditOutcome, AuditEvent, AuditQuery, client_fingerprint, make_audit_event]
      heading_level: 3

## Operator controls

::: mapi_spec.admin
    options:
      members: [KillSwitches, KillSwitchUpdate, kill_switch_decision, ExecutorHealth, ExecutorStatus]
      heading_level: 3

## Executor conformance suite

::: mapi_spec.conformance_executor
    options:
      members: [EXECUTOR_CONFORMANCE, ConformanceCase, ConformanceCaseId, Fault, Requirement, Expectation, ConformanceTarget, applicable_cases, ObservedRun, check_expectation, ConformanceReport, ConformanceResult, CaseOutcome]
      heading_level: 3

## Guided mode <span class="mapi-badge">v0.4</span>

See [Guided mode](../../concepts/guided-mode.md).

::: mapi_spec.guided
    options:
      members: [GuidedTemplate, GuidedQuestion, TemplateLookup, GUIDED_TEMPLATES, get_template, template_placeholders, render_template, ColumnProfile, DetectRolesRequest, RoleCandidate, QuestionSuggestion, RoleSuggestions, suggest_roles, GuidedAnswers, GuidedDraft, build_draft, safe_name, channel_label, BayesInsightImport, draft_from_bayesinsight]
      heading_level: 3

## Ingest presets <span class="mapi-badge">v0.4</span>

::: mapi_spec.ingest
    options:
      members: [IngestMapping, DimMapping, VarMapping, WideVarMapping, IngestPreset, MFF_COLUMNS, MFF_KEY_COLUMNS, MffOptions, MeridianColumns, BayesInsightMetadata, BayesInsightVariable, BayesInsightModelDef, BayesInsightVarRef, bayesinsight_var_ref]
      heading_level: 3

## Business priors and calibration <span class="mapi-badge">v0.4</span>

::: mapi_spec.calibration
    options:
      members: [BusinessPriors, ChannelBelief, ShareBelief, check_business_priors, lognormal_from_range, logitnormal_from_range, LiftTest, Calibration, PriorTranslationJob, PriorTranslationResult, PriorPreview, DistributionSummary, FanChart, apply_prior_translation]
      heading_level: 3

## Scorecard and promotion gate <span class="mapi-badge">v0.4</span>

::: mapi_spec.scorecard
    options:
      members: [Scorecard, ScorecardItem, ScorecardCheck, CheckDirection, CheckThresholds, SCORECARD_CHECKS, evaluate_check, rollup, GateOutcome, GateReason, PromotionGateDecision, promotion_gate, authorize_promotion]
      heading_level: 3

## What-if scenarios <span class="mapi-badge">v0.4</span>

::: mapi_spec.views
    options:
      members: [ResponseGridView]
      heading_level: 3

::: mapi_spec.whatif
    options:
      members: [WhatIfScenario, WhatIfResult, ChannelOutcome, SummaryStats, evaluate_scenario, scenario_score_request, ChannelOff, BudgetScale, preset_interventions, counterfactual_request, forecast_request, GuidedCompile, compile_guided_request]
      heading_level: 3

## Endpoints and snippets <span class="mapi-badge">v0.4</span>

See [Integrations](../../concepts/integrations.md).

::: mapi_spec.endpoints
    options:
      members: [Endpoint, EvaluationPolicy, EndpointSchemaSnapshot, EndpointPromote, EndpointRevision, EndpointRollback, EndpointScoreCall, EndpointSchema, EndpointValidation, schema_breaks, example_score_call]
      heading_level: 3

::: mapi_spec.xrjson
    options:
      members: [XrJsonDataset, XrJsonVariable, check_xrjson_size]
      heading_level: 3

::: mapi_spec.snippets
    options:
      members: [SnippetLanguage, Snippet, SnippetSet, SnippetContext, render_snippet, render_snippets]
      heading_level: 3

## Webhooks <span class="mapi-badge">v0.4</span>

::: mapi_spec.webhooks
    options:
      members: [WebhookEventType, WebhookCreate, WebhookUpdate, Webhook, WebhookCreated, WebhookEnvelope, WebhookDelivery, DeliveryStatus, sign_payload, signature_header, parse_signature_header, check_signature, verify_signature, next_retry_delay, SsrfPolicy, SSRF_POLICY, ip_is_blocked, check_webhook_url, check_resolved_addresses]
      heading_level: 3

## Plot sharing <span class="mapi-badge">v0.4</span>

::: mapi_spec.sharing
    options:
      members: [SavedPlotCreate, SavedPlot, ModelPlotRef, PlotRenderRequest, PlotEmbedCreate, PlotEmbed, PlotEmbedCreated, EmbedView, embeddable, is_platform_recipe]
      heading_level: 3

## Trust lifecycle <span class="mapi-badge">v0.4</span>

::: mapi_spec.trust
    options:
      members: [SignupRequest, SignupAccepted, check_signup, DisposableEmailPolicy, BlocklistEmailPolicy, email_domain, EmailVerify, VerificationResult, VerificationStatus, VerificationMethod, VerificationUpdate, uploaded_code_allowed, DeletionState, next_deletion_state, TenantDeletionRequest, TenantDeletion, StoreProgress, GdprExportRequest, GdprExport]
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
