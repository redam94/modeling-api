/**
 * Named aliases for the generated OpenAPI component schemas. Every other module imports
 * DTO types from here, so regenerating `schema.d.ts` from the real server only needs this
 * file adjusted if a component is renamed.
 */
import type { components } from "./schema";

/**
 * openapi-fetch hands back `Readable<T>` data, which turns tuples into arrays. Applying the
 * same mapping here keeps these aliases assignable to and from what the client returns.
 */
type Arrayify<T> = T extends (infer E)[] ? Arrayify<E>[] : T extends object ? { [K in keyof T]: Arrayify<T[K]> } : T;

type S = { [K in keyof components["schemas"]]: Arrayify<components["schemas"][K]> };

export type ArtifactLink = S["ArtifactLink"];
export type AssemblySource = S["AssemblySource"];
// v0.15 image collections (I-1, I-4)
export type BlobMissing = S["BlobMissing"];
export type BlobQuery = S["BlobQuery"];
export type ClassCount = S["ClassCount"];
export type CollectionInfo = S["CollectionInfo"];
export type CollectionOptions = S["CollectionOptions"];
export type CollectionSource = S["CollectionSource"];
export type PackInfo = S["PackInfo"];
export type BundleManifest = S["BundleManifest"];
export type Capabilities = S["Capabilities"];
export type CatalogResponse = S["CatalogResponse"];
export type ChainProgress = S["ChainProgress"];
export type ContextNeed = S["ContextNeed"];
export type CoordSummary = S["CoordSummary"];
export type DataSchema = S["DataSchema"];
export type DataSummary = S["DataSummary"];
export type Dataset = S["Dataset"];
export type DatasetCreate = S["DatasetCreate"];
export type DatasetVersion = S["DatasetVersion"];
export type DatasetVersionCreate = S["DatasetVersionCreate"];
export type Diagnostic = S["Diagnostic"];
export type DimMapping = S["DimMapping"];
export type DimSpec = S["DimSpec"];
export type ErrorCode = S["ErrorCode"];
export type Hdf5Coord = S["Hdf5Coord"];
export type Hdf5Mapping = S["Hdf5Mapping"];
export type Hdf5Var = S["Hdf5Var"];
export type Health = S["Health"];
export type IngestAssembly = S["IngestAssembly"];
export type IngestMapping = S["IngestMapping"];
export type InspectedArray = S["InspectedArray"];
export type Intervention = S["Intervention"];
export type Issue = S["Issue"];
export type Lineage = S["Lineage"];
export type LogLine = S["LogLine"];
export type Model = S["Model"];
export type ModelCatalogEntry = S["ModelCatalogEntry"];
export type ModelVersion = S["ModelVersion"];
export type ObjectRef = S["ObjectRef"];
export type OutputSelector = S["OutputSelector"];
export type PlotCatalogEntry = S["PlotCatalogEntry"];
export type PlotListItem = S["PlotListItem"];
export type PlotSpec = S["PlotSpec"];
export type Problem = S["Problem"];
export type ProgressSnapshot = S["ProgressSnapshot"];
export type Project = S["Project"];
export type ResolvedSpec = S["ResolvedSpec"];
export type ResolvedStep = S["ResolvedStep"];
export type ResourceClass = S["ResourceClass"];
export type Role = S["Role"];
export type Run = S["Run"];
export type RunCreate = S["RunCreate"];
export type RunEventType = S["RunEventType"];
export type RunEventView = S["RunEventView"];
export type RunKind = S["RunKind"];
export type RunRetry = S["RunRetry"];
export type RunState = S["RunState"];
export type RunnerPhase = S["RunnerPhase"];
export type ScoreCall = S["ScoreCall"];
export type ScoreInline = S["ScoreInline"];
export type ModelInputSchema = S["ModelInputSchema"];
export type ScoreCallResult = S["ScoreCallResult"];
export type ScoreRequest = S["ScoreRequest"];
export type Scorecard = S["Scorecard"];
export type ScorecardItem = S["ScorecardItem"];
export type SpecDoc = S["SpecDoc"];
export type SpecRevision = S["SpecRevision"];
export type StageSchema = S["StageSchema"];
export type StepCatalogEntry = S["StepCatalogEntry"];
export type TreeSchema = S["TreeSchema"];
export type UploadInspection = S["UploadInspection"];
export type UploadSession = S["UploadSession"];
export type ValidateResponse = S["ValidateResponse"];
export type VarMapping = S["VarMapping"];
export type VarSpec = S["VarSpec"];

// v0.3: identity, tenancy, tokens, usage and operators.
export type ApiToken = S["ApiToken"];
export type AuditAction = S["AuditAction"];
export type AuditEvent = S["AuditEvent"];
export type AdminUsageReport = S["AdminUsageReport"];
export type AuditOutcome = S["AuditOutcome"];
export type BudgetPeriod = S["BudgetPeriod"];
export type ConformanceReport = S["ConformanceReport"];
export type CostTotals = S["CostTotals"];
export type ExecutorCaps = S["ExecutorCaps"];
export type ExecutorHealth = S["ExecutorHealth"];
export type ExecutorStatus = S["ExecutorStatus"];
export type ExecutorUpdate = S["ExecutorUpdate"];
export type GlobalSpend = S["GlobalSpend"];
export type KillSwitchUpdate = S["KillSwitchUpdate"];
export type KillSwitches = S["KillSwitches"];
export type LimitName = S["LimitName"];
export type Me = S["Me"];
export type MeProject = S["MeProject"];
export type MeTenant = S["MeTenant"];
export type Membership = S["Membership"];
export type MembershipCreate = S["MembershipCreate"];
export type MembershipUpdate = S["MembershipUpdate"];
export type ProjectCreate = S["ProjectCreate"];
export type ProjectRole = S["ProjectRole"];
export type ProjectUpdate = S["ProjectUpdate"];
export type QuotaLimitStatus = S["QuotaLimitStatus"];
export type QuotaLimits = S["QuotaLimits"];
export type QuotaPolicy = S["QuotaPolicy"];
export type QuotaPolicyUpsert = S["QuotaPolicyUpsert"];
export type QuotaScope = S["QuotaScope"];
export type ResourceKind = S["ResourceKind"];
export type RunCost = S["RunCost"];
export type SessionInfo = S["SessionInfo"];
export type Tenant = S["Tenant"];
export type TenantAdminUpdate = S["TenantAdminUpdate"];
export type TenantCost = S["TenantCost"];
export type TenantCreate = S["TenantCreate"];
export type TenantStatus = S["TenantStatus"];
export type TokenCreate = S["TokenCreate"];
export type TokenCreated = S["TokenCreated"];
export type TokenStatus = S["TokenStatus"];
export type TrustTier = S["TrustTier"];
export type UsageReport = S["UsageReport"];
export type UserPrincipal = S["UserPrincipal"];

// v0.4: endpoints, webhooks, saved plots and embeds, and the trust lifecycle.
export type DeliveryStatus = S["DeliveryStatus"];
export type DeletionState = S["DeletionState"];
export type Endpoint = S["Endpoint"];
export type EndpointPromote = S["EndpointPromote"];
export type EndpointRevision = S["EndpointRevision"];
export type EndpointRollback = S["EndpointRollback"];
export type EndpointSchema = S["EndpointSchema"];
export type EndpointSchemaSnapshot = S["EndpointSchemaSnapshot"];
export type EndpointScoreCall = S["EndpointScoreCall"];
export type EndpointValidation = S["EndpointValidation"];
export type EvaluationPolicy = S["EvaluationPolicy"];
export type ForecastConditioning = S["ForecastConditioning"];
export type ScoreSummary = S["ScoreSummary"];
export type InputTable = S["InputTable"];
export type InputTableColumn = S["InputTableColumn"];
export type GateOutcome = S["GateOutcome"];
export type GateReason = S["GateReason"];
export type GdprExport = S["GdprExport"];
export type GdprExportRequest = S["GdprExportRequest"];
export type ModelPlotRef = S["ModelPlotRef"];
export type PlotEmbed = S["PlotEmbed"];
export type PlotEmbedCreate = S["PlotEmbedCreate"];
export type PlotEmbedCreated = S["PlotEmbedCreated"];
export type PlotRender = S["PlotRender"];
export type PlotRenderRequest = S["PlotRenderRequest"];
export type PromotionGateDecision = S["PromotionGateDecision"];
export type SavedPlot = S["SavedPlot"];
export type SavedPlotCreate = S["SavedPlotCreate"];
export type SignupAccepted = S["SignupAccepted"];
export type SignupRequest = S["SignupRequest"];
export type SignupReview = S["SignupReview"];
export type SignupReviewDecision = S["SignupReviewDecision"];
export type SignupReviewState = S["SignupReviewState"];
export type Snippet = S["Snippet"];
export type SnippetLanguage = S["SnippetLanguage"];
export type SnippetSet = S["SnippetSet"];
export type StoreProgress = S["StoreProgress"];
export type TenantDeletion = S["TenantDeletion"];
export type VerificationMethod = S["VerificationMethod"];
export type VerificationResult = S["VerificationResult"];
export type VerificationStatus = S["VerificationStatus"];
export type VerificationUpdate = S["VerificationUpdate"];
export type Webhook = S["Webhook"];
export type WebhookCreate = S["WebhookCreate"];
export type WebhookCreated = S["WebhookCreated"];
export type WebhookDelivery = S["WebhookDelivery"];
export type WebhookEventType = S["WebhookEventType"];
export type WebhookUpdate = S["WebhookUpdate"];

// v0.11: run tracking (metrics, params, name, note and tags).
export type MetricHistory = S["MetricHistory"];
export type MetricHistorySeries = S["MetricHistorySeries"];
export type MetricPointIn = S["MetricPointIn"];
export type MetricSeries = S["MetricSeries"];
export type MetricsWrite = S["MetricsWrite"];
export type MetricsWriteResult = S["MetricsWriteResult"];
export type ParamsWrite = S["ParamsWrite"];
export type ParamsWriteResult = S["ParamsWriteResult"];
export type RunParam = S["RunParam"];
export type RunPatch = S["RunPatch"];

// Run-event payloads (inside RunEventView.payload and SSE frames).
export type StatePayload = S["StatePayload"];
export type ProgressPayload = S["ProgressPayload"];
export type LogPayload = S["LogPayload"];
export type WarningPayload = S["WarningPayload"];
export type CheckpointPayload = S["CheckpointPayload"];
export type MetricPayload = S["MetricPayload"];
export type CompletePayload = S["CompletePayload"];
export type ErrorPayload = S["ErrorPayload"];

export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
}

// v0.14: media, experiments, saved views, run keys and run comparison.
export type MediaKind = RunMedia["kind"];
export type MediaContentType = RunMedia["content_type"];
export type RunMedia = S["RunMedia"];
export type MediaKeySummary = S["MediaKeySummary"];
export type MediaWrite = S["MediaWrite"];
export type Experiment = S["Experiment"];
export type ExperimentCreate = S["ExperimentCreate"];
export type ExperimentPatch = S["ExperimentPatch"];
export type ExperimentRunsWrite = S["ExperimentRunsWrite"];
export type ExperimentRunsResult = S["ExperimentRunsResult"];
export type RunViewQuery = S["RunViewQuery"];
export type RunView = S["RunView"];
export type RunViewCreate = S["RunViewCreate"];
export type RunViewPatch = S["RunViewPatch"];
export type RunKeyCount = S["RunKeyCount"];
export type RunKeys = S["RunKeys"];
export type CompareSummary = S["CompareSummary"];
export type CompareSeries = S["CompareSeries"];
export type CompareMetric = S["CompareMetric"];
export type CompareParam = S["CompareParam"];
export type CompareMedia = S["CompareMedia"];
export type RunCompare = S["RunCompare"];
