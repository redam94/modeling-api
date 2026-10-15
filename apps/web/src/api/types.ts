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
export type Health = S["Health"];
export type IngestMapping = S["IngestMapping"];
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
export type ScoreCallResult = S["ScoreCallResult"];
export type ScoreRequest = S["ScoreRequest"];
export type Scorecard = S["Scorecard"];
export type ScorecardItem = S["ScorecardItem"];
export type SpecDoc = S["SpecDoc"];
export type SpecRevision = S["SpecRevision"];
export type StageSchema = S["StageSchema"];
export type StepCatalogEntry = S["StepCatalogEntry"];
export type UploadSession = S["UploadSession"];
export type ValidateResponse = S["ValidateResponse"];
export type VarMapping = S["VarMapping"];
export type VarSpec = S["VarSpec"];

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
