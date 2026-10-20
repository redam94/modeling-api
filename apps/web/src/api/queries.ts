/**
 * TanStack Query hooks over the typed client. Server state lives here (never in Zustand).
 *
 * Every project-scoped key starts with `["p", "<tenant>/<project>"]` (see {@link qk}) and its
 * request names the same project in `X-Mapi-Project` (taken from the same render), so caches
 * of two projects never mix and a switch never shows the previous project's data.
 */
import {
  keepPreviousData,
  queryOptions,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { useMemo } from "react";

import { isTerminal } from "@/lib/run-state";
import { useAuth } from "@/stores/auth";
import { scopeKey, useScope } from "@/stores/scope";

import { ApiError, api, newIdempotencyKey, PROJECT_HEADER, unwrap, unwrapVoid, unwrapWithStatus } from "./client";
import {
  createExperiment,
  createRunView,
  deleteExperiment,
  deleteRunView,
  fetchExperiment,
  fetchExperiments,
  fetchRunCompare,
  fetchRunKeys,
  fetchRunViews,
  updateExperiment,
  updateExperimentRuns,
  updateRunView,
  type CompareQuery,
  type ExperimentFilters,
} from "./experiments";
import { advanceLogTail, EMPTY_LOG_TAIL, logPollDelay, type LogTail } from "./logs";
import { fetchRunMedia, fetchRunMediaKeys, type MediaListQuery } from "./media";
import { fetchSession } from "./session";
import {
  fetchMetricHistory,
  fetchRunMetrics,
  fetchRunParams,
  patchRun,
  type LogLevel,
  type LogStream,
  type MetricHistoryQuery,
} from "./tracking";
import type {
  ArtifactLink,
  AuditAction,
  AuditOutcome,
  DatasetCreate,
  DatasetVersionCreate,
  ExperimentCreate,
  ExperimentPatch,
  ExperimentRunsWrite,
  ExecutorUpdate,
  KillSwitchUpdate,
  Me,
  MembershipCreate,
  MembershipUpdate,
  ProjectCreate,
  ProjectRole,
  ProjectUpdate,
  Model,
  ModelVersion,
  Page,
  PlotSpec,
  QuotaPolicyUpsert,
  QuotaScope,
  Run,
  RunCreate,
  RunPatch,
  RunKind,
  RunRetry,
  RunState,
  RunViewCreate,
  RunViewPatch,
  ScoreCall,
  ScoreCallResult,
  TenantAdminUpdate,
  TenantCreate,
  TenantStatus,
  TokenCreate,
  TokenStatus,
  TrustTier,
  UsageReport,
} from "./types";

export const PAGE_LIMIT = 50;

/** The selected project: query-key prefix and request headers. */
export interface ProjectScope {
  /** `<tenant>/<project>` (`"-"` before the workspace gate resolved one). */
  key: string;
  tenantId: string;
  projectId: string;
  headers: Record<string, string>;
}

/** The active project, for keys and headers of project-scoped queries. */
export function useProjectScope(): ProjectScope {
  const active = useScope((s) => s.active);
  const mode = useAuth((s) => s.mode);
  return useMemo((): ProjectScope => {
    const key = active ? scopeKey(active) : "-";
    const headers: Record<string, string> = mode === "session" && active ? { [PROJECT_HEADER]: key } : {};
    return {
      key,
      tenantId: active?.tenantId ?? "",
      projectId: active?.projectId ?? "",
      headers,
    };
  }, [active, mode]);
}

export const qk = {
  // global
  session: ["auth", "session"] as const,
  me: ["me"] as const,
  health: ["health"] as const,
  ready: ["ready"] as const,
  catalog: ["catalog"] as const,
  schema: (name: string) => ["schema", name] as const,
  // project-scoped (prefix with the `tenant/project` key)
  project: (p: string) => ["p", p] as const,
  datasets: (p: string) => ["p", p, "datasets"] as const,
  dataset: (p: string, id: string) => ["p", p, "datasets", id] as const,
  datasetVersions: (p: string, id: string) => ["p", p, "datasets", id, "versions"] as const,
  datasetVersion: (p: string, id: string, v: number) => ["p", p, "datasets", id, "versions", v] as const,
  specs: (p: string) => ["p", p, "specs"] as const,
  spec: (p: string, id: string) => ["p", p, "specs", id] as const,
  specRevisions: (p: string, id: string) => ["p", p, "specs", id, "revisions"] as const,
  specRevision: (p: string, id: string, r: number) => ["p", p, "specs", id, "revisions", r] as const,
  runLists: (p: string) => ["p", p, "runs", "list"] as const,
  runs: (p: string, filters: RunFilters) => ["p", p, "runs", "list", filters] as const,
  run: (p: string, id: string) => ["p", p, "runs", id] as const,
  runEvents: (p: string, id: string) => ["p", p, "runs", id, "events"] as const,
  runLogs: (p: string, id: string, stream: string | null, level: string | null = null) => ["p", p, "runs", id, "logs", stream, level] as const,
  runPlots: (p: string, id: string) => ["p", p, "runs", id, "plots"] as const,
  // v0.11 tracking. `qk.run` is a prefix of these: invalidate it with `exact: true` for the run only.
  /** Prefix of the summaries and every history of a run. */
  runMetricData: (p: string, id: string) => ["p", p, "runs", id, "metrics"] as const,
  runMetrics: (p: string, id: string) => ["p", p, "runs", id, "metrics", "summary"] as const,
  /** Without `q`: the prefix of every history query of `key` (for invalidation). */
  metricHistory: (p: string, id: string, key: string, q?: MetricHistoryQuery) =>
    q ? (["p", p, "runs", id, "metrics", "history", key, q] as const) : (["p", p, "runs", id, "metrics", "history", key] as const),
  runParams: (p: string, id: string) => ["p", p, "runs", id, "params"] as const,
  // v0.14: media, experiments, saved views, tracked keys and run comparison.
  /** Prefix of the media keys and every media list of a run. */
  runMediaData: (p: string, id: string) => ["p", p, "runs", id, "media"] as const,
  runMediaKeys: (p: string, id: string) => ["p", p, "runs", id, "media", "keys"] as const,
  runMedia: (p: string, id: string, q: MediaListQuery) => ["p", p, "runs", id, "media", "list", q] as const,
  runKeys: (p: string, experimentId: string | null) => ["p", p, "run-keys", experimentId] as const,
  runCompare: (p: string, q: CompareQuery) => ["p", p, "run-compare", q] as const,
  experiments: (p: string) => ["p", p, "experiments"] as const,
  experimentList: (p: string, f: ExperimentFilters) => ["p", p, "experiments", "list", f] as const,
  experiment: (p: string, id: string) => ["p", p, "experiments", id] as const,
  runViews: (p: string) => ["p", p, "run-views"] as const,
  runViewList: (p: string, experimentId: string | null) => ["p", p, "run-views", experimentId] as const,
  models: (p: string) => ["p", p, "models"] as const,
  model: (p: string, id: string) => ["p", p, "models", id] as const,
  modelVersions: (p: string, id: string) => ["p", p, "models", id, "versions"] as const,
  modelVersion: (p: string, id: string, v: number) => ["p", p, "models", id, "versions", v] as const,
  modelInputs: (p: string, id: string, v: number) => ["p", p, "models", id, "versions", v, "inputs"] as const,
  plots: (p: string, id: string, v: number) => ["p", p, "models", id, "versions", v, "plots"] as const,
  plot: (p: string, id: string, v: number, plotId: string) => ["p", p, "models", id, "versions", v, "plots", plotId] as const,
  modelVersionById: (p: string, mvId: string) => ["p", p, "model-version-by-id", mvId] as const,
  tokens: (p: string) => ["p", p, "tokens"] as const,
  tokenList: (p: string, f: TokenFilters) => ["p", p, "tokens", f] as const,
  usage: (p: string, q: UsageQuery) => ["p", p, "usage", q] as const,
  // path-scoped (tenant routes)
  tenantProjects: (t: string) => ["tenants", t, "projects"] as const,
  members: (t: string, p: string | null) => (p ? (["tenants", t, "projects", p, "members"] as const) : (["tenants", t, "members"] as const)),
  tenantUsage: (t: string, q: UsageQuery) => ["tenants", t, "usage", q] as const,
  tenantAudit: (t: string, f: AuditFilters) => ["tenants", t, "audit", f] as const,
  // operators
  admin: ["admin"] as const,
  adminTenants: (f: AdminTenantFilters) => ["admin", "tenants", f] as const,
  adminQuotas: (f: QuotaFilters) => ["admin", "quotas", f] as const,
  killSwitches: ["admin", "kill-switches"] as const,
  adminUsage: (q: AdminUsageQuery) => ["admin", "usage", q] as const,
  executors: ["admin", "executors"] as const,
  adminAudit: (f: AuditFilters) => ["admin", "audit", f] as const,
};

export interface RunFilters {
  state?: RunState;
  kind?: RunKind;
  spec_id?: string;
  limit?: number;
  // v0.11 run search (v0.14 UI) and the v0.14 experiment filter
  experiment_id?: string;
  parent_run_id?: string;
  /** Case-insensitive substring of the run name. */
  name?: string;
  /** `key` or `key:value`. */
  tag?: string[];
  /** Grammar strings (`metric.rmse < 3`); at most 8. */
  filter?: string[];
  order_by?: string;
  /** Refs whose values `Run.columns` returns; at most 16. */
  column?: string[];
  cursor?: string;
}

export interface TokenFilters {
  all?: boolean;
  status?: TokenStatus;
}

export interface UsageQuery {
  period: "day" | "month";
  start?: string;
  group_by?: NonNullable<UsageReport["group_by"]>;
}

export interface AuditFilters {
  tenant_id?: string;
  project_id?: string;
  actor_id?: string;
  action?: AuditAction;
  outcome?: AuditOutcome;
  since?: string;
  until?: string;
}

export interface AdminTenantFilters {
  status?: TenantStatus;
  trust_tier?: TrustTier;
}

export interface QuotaFilters {
  scope?: QuotaScope;
  subject_id?: string;
}

function defined<T extends object>(o: T): Partial<T> {
  return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== "")) as Partial<T>;
}

// ------------------------------------------------------------------------- session / identity

export function useSession() {
  return useQuery({
    queryKey: qk.session,
    queryFn: ({ signal }) => fetchSession(signal),
    staleTime: 60_000,
    refetchInterval: 5 * 60_000,
    // A network failure is retried (the API may be starting); a 4xx/5xx answer is final.
    retry: (count, err) => err instanceof TypeError && count < 2,
  });
}

/** `GET /v1/me` (v0.3 servers only; the gate synthesises the single-tenant workspace). */
export function useMe(enabled: boolean) {
  return useQuery({
    queryKey: qk.me,
    queryFn: ({ signal }): Promise<Me> => unwrap(api.GET("/v1/me", { signal })),
    enabled,
    staleTime: 60_000,
  });
}

// ------------------------------------------------------------------------- health / catalog

export function useReady() {
  return useQuery({
    queryKey: qk.ready,
    queryFn: async ({ signal }) => {
      const res = await api.GET("/readyz", { signal });
      if (res.data) return res.data; // 200 and 503 share the Health body
      throw new Error(`HTTP ${res.response.status}`);
    },
    refetchInterval: 30_000,
    retry: false,
  });
}

export const catalogQuery = queryOptions({
  queryKey: qk.catalog,
  queryFn: ({ signal }) => unwrap(api.GET("/v1/catalog", { signal })),
  staleTime: 5 * 60_000,
});

export function useCatalog() {
  return useQuery(catalogQuery);
}

export function useJsonSchema(name: string, enabled = true) {
  return useQuery({
    queryKey: qk.schema(name),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/schemas/{name}", { params: { path: { name } }, signal })),
    staleTime: Infinity,
    enabled,
  });
}

// ------------------------------------------------------------------------- datasets

export function useDatasets() {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.datasets(s.key),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/datasets", { params: { query: { limit: 200 } }, headers: s.headers, signal })),
  });
}

export function useDataset(id: string) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.dataset(s.key, id),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/datasets/{dataset_id}", { params: { path: { dataset_id: id } }, headers: s.headers, signal })),
  });
}

export function useDatasetVersions(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.datasetVersions(s.key, id ?? ""),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/datasets/{dataset_id}/versions", {
          params: { path: { dataset_id: id ?? "" }, query: { limit: 200 } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(id),
    refetchInterval: (q) =>
      q.state.data?.items.some((v) => v.status === "ingesting") ? 3000 : false,
  });
}

export function useDatasetVersion(id: string, version: number | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.datasetVersion(s.key, id, version ?? 0),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/datasets/{dataset_id}/versions/{version}", {
          params: { path: { dataset_id: id, version: version ?? 0 } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: version !== undefined,
    refetchInterval: (q) => (q.state.data?.status === "ingesting" ? 3000 : false),
  });
}

export async function datasetArtifactLink(id: string, version: number, name: string): Promise<ArtifactLink> {
  return unwrap(
    api.GET("/v1/datasets/{dataset_id}/versions/{version}/artifacts/{name}", {
      params: { path: { dataset_id: id, version, name } },
    }),
  );
}

export function useCreateDataset() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: DatasetCreate) => unwrap(api.POST("/v1/datasets", { body, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.datasets(s.key) }),
  });
}

export function createUpload(datasetId: string, body: { filename: string; content_type: string; size_bytes: number }) {
  return unwrap(
    api.POST("/v1/datasets/{dataset_id}/uploads", { params: { path: { dataset_id: datasetId } }, body }),
  );
}

export function useCreateDatasetVersion(datasetId: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: ({ body, key }: { body: DatasetVersionCreate; key: string }) =>
      unwrap(
        api.POST("/v1/datasets/{dataset_id}/versions", {
          params: { path: { dataset_id: datasetId }, header: { "Idempotency-Key": key } },
          headers: s.headers,
          body,
        }),
      ),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: qk.dataset(s.key, datasetId) });
    },
  });
}

// ------------------------------------------------------------------------- specs

export function useSpecs() {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.specs(s.key),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/specs", { params: { query: { limit: 200 } }, headers: s.headers, signal })),
  });
}

export function useSpec(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.spec(s.key, id ?? ""),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/specs/{spec_id}", { params: { path: { spec_id: id ?? "" } }, headers: s.headers, signal })),
    enabled: Boolean(id),
  });
}

export function useSpecRevisions(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.specRevisions(s.key, id ?? ""),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/specs/{spec_id}/revisions", {
          params: { path: { spec_id: id ?? "" }, query: { limit: 200 } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(id),
  });
}

export function useSpecRevision(id: string | undefined, revision: number | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.specRevision(s.key, id ?? "", revision ?? 0),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/specs/{spec_id}/revisions/{revision}", {
          params: { path: { spec_id: id ?? "", revision: revision ?? 0 } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(id) && revision !== undefined,
    staleTime: Infinity, // revisions are immutable
  });
}

export function validateSpec(yaml: string, signal?: AbortSignal) {
  return unwrap(api.POST("/v1/specs:validate", { body: { yaml }, signal }));
}

export function useCreateSpec() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: { yaml: string; message?: string | null }) => unwrap(api.POST("/v1/specs", { body, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.specs(s.key) }),
  });
}

export function useCreateRevision(specId: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: { yaml: string; parent_revision: number; message?: string | null }) =>
      unwrap(api.POST("/v1/specs/{spec_id}/revisions", { params: { path: { spec_id: specId } }, headers: s.headers, body })),
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: qk.spec(s.key, specId) }),
        qc.invalidateQueries({ queryKey: qk.specs(s.key) }),
      ]);
    },
  });
}

export function useDryRun() {
  const s = useProjectScope();
  return useMutation({
    mutationFn: ({ specId, revision }: { specId: string; revision: number }) =>
      unwrap(
        api.POST("/v1/specs/{spec_id}/revisions/{revision}:dry-run", {
          params: { path: { spec_id: specId, revision }, header: { "Idempotency-Key": newIdempotencyKey() } },
          headers: s.headers,
        }),
      ),
  });
}

// ------------------------------------------------------------------------- runs

/** The `GET /v1/runs` query of `filters` (empty values left out). */
export function runListQuery(filters: RunFilters) {
  return {
    limit: filters.limit ?? PAGE_LIMIT,
    ...(filters.state ? { state: filters.state } : {}),
    ...(filters.kind ? { kind: filters.kind } : {}),
    ...(filters.spec_id ? { spec_id: filters.spec_id } : {}),
    ...(filters.experiment_id ? { experiment_id: filters.experiment_id } : {}),
    ...(filters.parent_run_id ? { parent_run_id: filters.parent_run_id } : {}),
    ...(filters.name ? { name: filters.name } : {}),
    ...(filters.tag?.length ? { tag: filters.tag } : {}),
    ...(filters.filter?.length ? { filter: filters.filter } : {}),
    ...(filters.order_by ? { order_by: filters.order_by } : {}),
    ...(filters.column?.length ? { column: filters.column } : {}),
    ...(filters.cursor ? { cursor: filters.cursor } : {}),
  };
}

function fetchRunPage(filters: RunFilters, headers: Record<string, string>, signal: AbortSignal) {
  return unwrap(api.GET("/v1/runs", { params: { query: runListQuery(filters) }, headers, signal }));
}

export function useRuns(filters: RunFilters, opts: { live?: boolean } = {}) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runs(s.key, filters),
    queryFn: ({ signal }) => fetchRunPage(filters, s.headers, signal),
    placeholderData: keepPreviousData,
    refetchInterval: opts.live === false ? false : 5000,
  });
}

/**
 * Pages of runs for the runs table: the server's order and keyset cursor ("Load more"). A
 * live refresh refetches the pages that are loaded.
 */
export function useRunPages(filters: Omit<RunFilters, "cursor">, opts: { live?: boolean } = {}) {
  const s = useProjectScope();
  return useInfiniteQuery({
    queryKey: [...qk.runs(s.key, filters), "pages"] as const,
    queryFn: ({ signal, pageParam }) => fetchRunPage({ ...filters, ...(pageParam ? { cursor: pageParam } : {}) }, s.headers, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? null,
    placeholderData: keepPreviousData,
    refetchInterval: opts.live === false ? false : 10_000,
    // A refused filter (422) does not get better on retry.
    retry: (n, err) => !(err instanceof ApiError && err.status === 422) && n < 2,
  });
}

export const runQuery = (s: ProjectScope, id: string) =>
  queryOptions({
    queryKey: qk.run(s.key, id),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/runs/{run_id}", { params: { path: { run_id: id } }, headers: s.headers, signal })),
  });

export function useRun(id: string | undefined, opts: { poll?: boolean } = {}) {
  const s = useProjectScope();
  return useQuery({
    ...runQuery(s, id ?? ""),
    enabled: Boolean(id),
    // SSE drives live updates; polling is a slow safety net while the run is active.
    refetchInterval: (q) => (opts.poll !== false && q.state.data && !isTerminal(q.state.data.state) ? 15_000 : false),
  });
}

export function useRunEventsPage(id: string) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runEvents(s.key, id),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/runs/{run_id}/events", {
          params: { path: { run_id: id }, query: { limit: 200 } },
          headers: s.headers,
          signal,
        }),
      ),
  });
}

/** Pages of 200 lines fetched per "Load more" (or the first load) of a finished run. */
export const MAX_LOG_PAGES = 5;
/** Pages fetched per poll of a live run: catching up is paced by {@link logPollDelay}. */
export const LIVE_LOG_PAGES = 1;
/** Lines kept in memory per (run, stream); the oldest are dropped beyond this. */
export const MAX_LOG_LINES = 10_000;

/**
 * Captured log lines, followed as a tail: each fetch continues after what is already loaded
 * (see {@link advanceLogTail}), so a live run's newest lines keep arriving however long it
 * logs, and no page is downloaded twice except the still-open last one. `behind` means newer
 * pages exist that are not loaded yet; a live run catches up one page per poll (paced to fit
 * the tenant's request rate, backing off on 429), otherwise `refetch()` loads the next batch.
 */
export function useRunLogs(id: string, stream: LogStream | null, live: boolean, level: LogLevel | null = null) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runLogs(s.key, id, stream, level),
    queryFn: ({ queryKey, signal }): Promise<LogTail> =>
      advanceLogTail(
        qc.getQueryData<LogTail>(queryKey) ?? EMPTY_LOG_TAIL,
        (cursor) =>
          unwrap(
            api.GET("/v1/runs/{run_id}/logs", {
              params: {
                path: { run_id: id },
                // without a stream filter, ask for the `user` lines too (the server leaves
                // them out for clients older than v0.11)
                query: { limit: 200, ...(stream ? { stream } : { include_user: true }), ...(level ? { level } : {}), ...(cursor ? { cursor } : {}) },
              },
              headers: s.headers,
              signal,
            }),
          ),
        { maxPages: live ? LIVE_LOG_PAGES : MAX_LOG_PAGES, maxLines: MAX_LOG_LINES },
      ),
    refetchInterval: (q) =>
      logPollDelay({
        live,
        behind: q.state.data?.behind ?? false,
        error: q.state.status === "error" ? q.state.error : null,
      }),
  });
}

function setRun(qc: QueryClient, scope: string, run: Run) {
  qc.setQueryData(qk.run(scope, run.id), run);
  void qc.invalidateQueries({ queryKey: qk.runLists(scope) });
}

export function useCreateRun() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: ({ body, key }: { body: RunCreate; key: string }) =>
      unwrap(api.POST("/v1/runs", { params: { header: { "Idempotency-Key": key } }, headers: s.headers, body })),
    onSuccess: (run) => setRun(qc, s.key, run),
  });
}

export function useCancelRun() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (id: string) =>
      unwrap(api.POST("/v1/runs/{run_id}:cancel", { params: { path: { run_id: id } }, headers: s.headers })),
    onSuccess: (run) => setRun(qc, s.key, run),
  });
}

export function useResumeRun() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (id: string) =>
      unwrap(api.POST("/v1/runs/{run_id}:resume", { params: { path: { run_id: id } }, headers: s.headers })),
    onSuccess: (run) => setRun(qc, s.key, run),
  });
}

export function useRetryRun() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: ({ id, body, key }: { id: string; body: RunRetry; key: string }) =>
      unwrap(
        api.POST("/v1/runs/{run_id}:retry", {
          params: { path: { run_id: id }, header: { "Idempotency-Key": key } },
          headers: s.headers,
          body,
        }),
      ),
    onSuccess: (run) => setRun(qc, s.key, run),
  });
}

// ------------------------------------------------------------------------- run tracking (v0.11)

/** Metric series of a run (summaries). The run page refetches them on SSE `tracking` frames. */
export function useRunMetrics(runId: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runMetrics(s.key, runId ?? ""),
    queryFn: ({ signal }) => fetchRunMetrics(runId ?? "", { headers: s.headers, signal }),
    enabled: Boolean(runId),
  });
}

/** The (downsampled) history of one metric key, every series of it. */
export function useMetricHistory(runId: string | undefined, key: string, q: MetricHistoryQuery = {}) {
  const s = useProjectScope();
  const query: MetricHistoryQuery = { ...(q.maxPoints ? { maxPoints: q.maxPoints } : {}), ...(q.attempt ? { attempt: q.attempt } : {}) };
  return useQuery({
    queryKey: qk.metricHistory(s.key, runId ?? "", key, query),
    queryFn: ({ signal }) => fetchMetricHistory(runId ?? "", key, query, { headers: s.headers, signal }),
    enabled: Boolean(runId && key),
    placeholderData: keepPreviousData,
  });
}

export function useRunParams(runId: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runParams(s.key, runId ?? ""),
    queryFn: ({ signal }) => fetchRunParams(runId ?? "", { headers: s.headers, signal }),
    enabled: Boolean(runId),
  });
}

/** The cached run with a patch applied (what the server answers, applied ahead of it). */
export function applyRunPatch(run: Run, patch: RunPatch): Run {
  const next: Run = { ...run };
  if ("name" in patch) next.name = patch.name ?? null;
  if ("note" in patch) next.note = patch.note ?? null;
  if ("experiment_id" in patch) next.experiment_id = patch.experiment_id ?? null;
  if (patch.tags) {
    const tags = { ...(run.tags ?? {}) };
    for (const [k, v] of Object.entries(patch.tags)) {
      if (v === null) delete tags[k];
      else tags[k] = v;
    }
    next.tags = tags;
  }
  return next;
}

/** `PATCH /v1/runs/{id}` (name, note, tags), applied to the cached run at once and undone on failure. */
export function useUpdateRun() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: RunPatch }) => patchRun(id, patch, { headers: s.headers }),
    onMutate: async ({ id, patch }) => {
      const key = qk.run(s.key, id);
      await qc.cancelQueries({ queryKey: key, exact: true });
      const previous = qc.getQueryData<Run>(key);
      if (previous) qc.setQueryData(key, applyRunPatch(previous, patch));
      return { previous };
    },
    onError: (_err, { id }, ctx) => {
      if (ctx?.previous) qc.setQueryData(qk.run(s.key, id), ctx.previous);
    },
    onSuccess: (run, { patch }) => {
      setRun(qc, s.key, run);
      // the run counts of the experiments changed
      if ("experiment_id" in patch) void qc.invalidateQueries({ queryKey: qk.experiments(s.key) });
    },
  });
}

// ------------------------------------------------------------------------- media (v0.14)

export const runMediaKeysQuery = (s: ProjectScope, runId: string) =>
  queryOptions({
    queryKey: qk.runMediaKeys(s.key, runId),
    queryFn: ({ signal }) => fetchRunMediaKeys(runId, { headers: s.headers, signal }),
    enabled: Boolean(runId),
  });

/** The media series (key + labels) of a run. A live run refetches them on `tracking` frames. */
export function useRunMediaKeys(runId: string | undefined) {
  const s = useProjectScope();
  return useQuery(runMediaKeysQuery(s, runId ?? ""));
}

/** The media items of one key of a run (every label set), with signed links. */
export function useRunMedia(runId: string | undefined, q: MediaListQuery) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runMedia(s.key, runId ?? "", q),
    queryFn: ({ signal }) => fetchRunMedia(runId ?? "", q, { headers: s.headers, signal }),
    enabled: Boolean(runId && q.key),
    placeholderData: keepPreviousData,
    // the links are signed for a limited time: get new ones before they expire
    staleTime: 5 * 60_000,
  });
}

// ------------------------------------------------------------------------- run keys and compare (v0.14)

/** Metric series, param keys and tag keys of the project's runs (or of one experiment). */
export function useRunKeys(experimentId: string | null, enabled = true) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runKeys(s.key, experimentId),
    queryFn: ({ signal }) => fetchRunKeys({ experimentId }, { headers: s.headers, signal }),
    enabled,
    staleTime: 60_000,
  });
}

/** The comparison of 2..10 runs: summaries, histories of the chosen keys, params and media. */
export function useRunCompare(q: CompareQuery, enabled = true) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runCompare(s.key, q),
    queryFn: ({ signal }) => fetchRunCompare(q, { headers: s.headers, signal }),
    enabled: enabled && q.runs.length >= 2,
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
    retry: (n, err) => !(err instanceof ApiError && (err.status === 404 || err.status === 422)) && n < 2,
  });
}

// ------------------------------------------------------------------------- experiments (v0.14)

export function useExperiments(f: ExperimentFilters = {}) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.experimentList(s.key, f),
    queryFn: ({ signal }) => fetchExperiments(f, { headers: s.headers, signal }),
  });
}

export function useExperiment(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.experiment(s.key, id ?? ""),
    queryFn: ({ signal }) => fetchExperiment(id ?? "", { headers: s.headers, signal }),
    enabled: Boolean(id),
  });
}

export function useCreateExperiment() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: ExperimentCreate) => createExperiment(body, { headers: s.headers }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.experiments(s.key) }),
  });
}

export function useUpdateExperiment() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: ExperimentPatch }) => updateExperiment(id, patch, { headers: s.headers }),
    onSuccess: (exp) => {
      qc.setQueryData(qk.experiment(s.key, exp.id), exp);
      void qc.invalidateQueries({ queryKey: [...qk.experiments(s.key), "list"] });
    },
  });
}

export function useDeleteExperiment() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (id: string) => deleteExperiment(id, { headers: s.headers }),
    onSuccess: async (_res, id) => {
      // dropped, not refetched: the open experiment page would turn into a 404 error and
      // unmount the dialog before it navigates away
      await qc.cancelQueries({ queryKey: qk.experiment(s.key, id), exact: true });
      qc.removeQueries({ queryKey: qk.experiment(s.key, id), exact: true });
      await Promise.all([
        qc.invalidateQueries({ queryKey: qk.experiments(s.key) }),
        qc.invalidateQueries({ queryKey: qk.runViews(s.key) }),
        qc.invalidateQueries({ queryKey: qk.runLists(s.key) }),
      ]);
    },
  });
}

/** Add runs to an experiment, or take them out (at most 200 ids per call). */
export function useUpdateExperimentRuns() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ExperimentRunsWrite }) => updateExperimentRuns(id, body, { headers: s.headers }),
    onSuccess: async (_res, { body }) => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: qk.experiments(s.key) }),
        qc.invalidateQueries({ queryKey: qk.runLists(s.key) }),
        ...[...(body.add ?? []), ...(body.remove ?? [])].map((id) => qc.invalidateQueries({ queryKey: qk.run(s.key, id), exact: true })),
      ]);
    },
  });
}

// ------------------------------------------------------------------------- saved run views (v0.14)

/** Saved views: the project's (`experimentId` null) or one experiment's. */
export function useRunViews(experimentId: string | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runViewList(s.key, experimentId),
    queryFn: ({ signal }) => fetchRunViews(experimentId, { headers: s.headers, signal }),
  });
}

export function useCreateRunView() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: RunViewCreate) => createRunView(body, { headers: s.headers }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.runViews(s.key) }),
  });
}

export function useUpdateRunView() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: RunViewPatch }) => updateRunView(id, patch, { headers: s.headers }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.runViews(s.key) }),
  });
}

export function useDeleteRunView() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (id: string) => deleteRunView(id, { headers: s.headers }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.runViews(s.key) }),
  });
}

// ------------------------------------------------------------------------- models

export function useModels() {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.models(s.key),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/models", { params: { query: { limit: 200 } }, headers: s.headers, signal })),
  });
}

export function useModel(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.model(s.key, id ?? ""),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/models/{model_id}", { params: { path: { model_id: id ?? "" } }, headers: s.headers, signal })),
    enabled: Boolean(id),
  });
}

export function useModelVersions(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.modelVersions(s.key, id ?? ""),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/models/{model_id}/versions", {
          params: { path: { model_id: id ?? "" }, query: { limit: 200 } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(id),
  });
}

/** What a score call of the version can send: the input contract and the input table. */
export function useModelInputs(id: string | undefined, version: number | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.modelInputs(s.key, id ?? "", version ?? 0),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/models/{model_id}/versions/{version}/inputs", {
          params: { path: { model_id: id ?? "", version: version ?? 0 } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(id) && version !== undefined,
    staleTime: 5 * 60_000,
    retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2,
  });
}

export function useModelVersion(id: string | undefined, version: number | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.modelVersion(s.key, id ?? "", version ?? 0),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/models/{model_id}/versions/{version}", {
          params: { path: { model_id: id ?? "", version: version ?? 0 } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(id) && version !== undefined,
  });
}

export async function modelArtifactLink(modelId: string, version: number, name: string): Promise<ArtifactLink> {
  return unwrap(
    api.GET("/v1/models/{model_id}/versions/{version}/artifacts/{name}", {
      params: { path: { model_id: modelId, version, name } },
    }),
  );
}

/**
 * Find a model version by its id (`Run.output_model_version_id`). The API has no direct
 * lookup, so this scans the project's models and their versions (bounded).
 */
export function useModelVersionById(mvId: string | null | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.modelVersionById(s.key, mvId ?? ""),
    enabled: Boolean(mvId),
    staleTime: Infinity,
    queryFn: async ({ signal }): Promise<ModelVersion | null> => {
      const models: Page<Model> = await unwrap(
        api.GET("/v1/models", { params: { query: { limit: 200 } }, headers: s.headers, signal }),
      );
      for (const m of models.items) {
        const versions = await unwrap(
          api.GET("/v1/models/{model_id}/versions", {
            params: { path: { model_id: m.id }, query: { limit: 200 } },
            headers: s.headers,
            signal,
          }),
        );
        const hit = versions.items.find((v) => v.id === mvId);
        if (hit) return hit;
      }
      return null;
    },
  });
}

export function usePlots(modelId: string, version: number, enabled = true) {
  const s = useProjectScope();
  return useQuery({
    enabled: enabled && Boolean(modelId) && version > 0,
    queryKey: qk.plots(s.key, modelId, version),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/models/{model_id}/versions/{version}/plots", {
          params: { path: { model_id: modelId, version }, query: { limit: 200 } },
          headers: s.headers,
          signal,
        }),
      ),
  });
}

/**
 * Sanitized plots a run rendered (score fans, counterfactual bands, area maps, classifier
 * curves): v0.10 serves them for any run state whose newest attempt with plots wrote them
 * (succeeded, partial, and failed or cancelled after a partial attempt); empty otherwise.
 */
export const runPlotsQuery = (s: ProjectScope, runId: string) =>
  queryOptions({
    queryKey: qk.runPlots(s.key, runId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/runs/{run_id}/plots", {
          params: { path: { run_id: runId }, query: { limit: 200 } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(runId),
    staleTime: Infinity,
  });

export function useRunPlots(runId: string | undefined) {
  const s = useProjectScope();
  return useQuery(runPlotsQuery(s, runId ?? ""));
}

export function usePlot(modelId: string, version: number, plotId: string | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.plot(s.key, modelId, version, plotId ?? ""),
    queryFn: ({ signal }): Promise<PlotSpec> =>
      unwrap(
        api.GET("/v1/models/{model_id}/versions/{version}/plots/{plot_id}", {
          params: { path: { model_id: modelId, version, plot_id: plotId ?? "" } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(plotId),
    staleTime: Infinity,
  });
}

export async function scoreModel(
  modelId: string,
  version: number,
  body: ScoreCall,
  opts: { key: string; waitSeconds: number; signal?: AbortSignal },
): Promise<{ result: ScoreCallResult; status: number }> {
  const { data, status } = await unwrapWithStatus(
    api.POST("/v1/models/{model_id}/versions/{version}:score", {
      params: {
        path: { model_id: modelId, version },
        header: { "Idempotency-Key": opts.key, Prefer: `wait=${Math.max(0, Math.min(60, opts.waitSeconds))}` },
      },
      body,
      ...(opts.signal ? { signal: opts.signal } : {}),
    }),
  );
  return { result: data, status };
}

export function validatePlot(spec: PlotSpec) {
  return unwrap(api.POST("/v1/plots:validate", { body: spec }));
}

// ------------------------------------------------------------------------- tenants & projects

export function useTenantProjects(tenantId: string | undefined) {
  return useQuery({
    queryKey: qk.tenantProjects(tenantId ?? ""),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/tenants/{tenant_id}/projects", {
          params: { path: { tenant_id: tenantId ?? "" }, query: { limit: 200 } },
          signal,
        }),
      ),
    enabled: Boolean(tenantId),
  });
}

export function useCreateProject(tenantId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ProjectCreate) =>
      unwrap(api.POST("/v1/tenants/{tenant_id}/projects", { params: { path: { tenant_id: tenantId } }, body })),
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: qk.me }),
        qc.invalidateQueries({ queryKey: qk.tenantProjects(tenantId) }),
      ]);
    },
  });
}

/** `PATCH` a project (project admins): its name, description or Guided lock. */
export function useUpdateProject(tenantId: string, projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ProjectUpdate) =>
      unwrap(api.PATCH("/v1/tenants/{tenant_id}/projects/{project_id}", { params: { path: { tenant_id: tenantId, project_id: projectId } }, body })),
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: qk.me }),
        qc.invalidateQueries({ queryKey: qk.tenantProjects(tenantId) }),
      ]);
    },
  });
}

// ------------------------------------------------------------------------- members

/** Members of a project (tenant-wide grants included) or, with `projectId` null, tenant-wide members. */
export function useMembers(tenantId: string, projectId: string | null) {
  return useQuery({
    queryKey: qk.members(tenantId, projectId),
    queryFn: ({ signal }) =>
      projectId
        ? unwrap(
            api.GET("/v1/tenants/{tenant_id}/projects/{project_id}/members", {
              params: { path: { tenant_id: tenantId, project_id: projectId }, query: { limit: 200 } },
              signal,
            }),
          )
        : unwrap(
            api.GET("/v1/tenants/{tenant_id}/members", {
              params: { path: { tenant_id: tenantId }, query: { limit: 200 } },
              signal,
            }),
          ),
    enabled: Boolean(tenantId),
  });
}

function useInvalidateMembers(tenantId: string) {
  const qc = useQueryClient();
  return () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: ["tenants", tenantId] }),
      qc.invalidateQueries({ queryKey: qk.me }),
    ]);
}

export function useAddMember(tenantId: string, projectId: string | null) {
  const invalidate = useInvalidateMembers(tenantId);
  return useMutation({
    mutationFn: (body: MembershipCreate) =>
      projectId
        ? unwrap(
            api.POST("/v1/tenants/{tenant_id}/projects/{project_id}/members", {
              params: { path: { tenant_id: tenantId, project_id: projectId } },
              body,
            }),
          )
        : unwrap(api.POST("/v1/tenants/{tenant_id}/members", { params: { path: { tenant_id: tenantId } }, body })),
    onSuccess: invalidate,
  });
}

export function useUpdateMember(tenantId: string, projectId: string | null) {
  const invalidate = useInvalidateMembers(tenantId);
  return useMutation({
    // M3.5: `capabilities` grants on top of the role (`reviewer` approves validations).
    mutationFn: ({ principalId, role, capabilities }: { principalId: string; role?: ProjectRole; capabilities?: MembershipUpdate["capabilities"] }) => {
      const body: MembershipUpdate = { ...(role ? { role } : {}), ...(capabilities ? { capabilities } : {}) };
      return projectId
        ? unwrap(
            api.PATCH("/v1/tenants/{tenant_id}/projects/{project_id}/members/{principal_id}", {
              params: { path: { tenant_id: tenantId, project_id: projectId, principal_id: principalId } },
              body,
            }),
          )
        : unwrap(
            api.PATCH("/v1/tenants/{tenant_id}/members/{principal_id}", {
              params: { path: { tenant_id: tenantId, principal_id: principalId } },
              body,
            }),
          );
    },
    onSuccess: invalidate,
  });
}

export function useRemoveMember(tenantId: string, projectId: string | null) {
  const invalidate = useInvalidateMembers(tenantId);
  return useMutation({
    mutationFn: (principalId: string) =>
      projectId
        ? unwrapVoid(
            api.DELETE("/v1/tenants/{tenant_id}/projects/{project_id}/members/{principal_id}", {
              params: { path: { tenant_id: tenantId, project_id: projectId, principal_id: principalId } },
            }),
          )
        : unwrapVoid(
            api.DELETE("/v1/tenants/{tenant_id}/members/{principal_id}", {
              params: { path: { tenant_id: tenantId, principal_id: principalId } },
            }),
          ),
    onSuccess: invalidate,
  });
}

// ------------------------------------------------------------------------- tokens

export function useTokens(filters: TokenFilters = {}) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.tokenList(s.key, filters),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/tokens", {
          params: { query: { limit: 200, ...defined({ all: filters.all ? true : undefined, status: filters.status }) } },
          headers: s.headers,
          signal,
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

export function useCreateToken() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: TokenCreate) => unwrap(api.POST("/v1/tokens", { body, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.tokens(s.key) }),
  });
}

export function useRevokeToken() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (tokenId: string) =>
      unwrapVoid(api.DELETE("/v1/tokens/{token_id}", { params: { path: { token_id: tokenId } }, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.tokens(s.key) }),
  });
}

// ------------------------------------------------------------------------- usage

export function useUsage(query: UsageQuery, enabled = true) {
  const s = useProjectScope();
  return useQuery({
    enabled,
    queryKey: qk.usage(s.key, query),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/usage", {
          params: { query: { period: query.period, ...defined({ start: query.start, group_by: query.group_by }) } },
          headers: s.headers,
          signal,
        }),
      ),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  });
}

export function useTenantUsage(tenantId: string, query: UsageQuery, enabled = true) {
  return useQuery({
    queryKey: qk.tenantUsage(tenantId, query),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/tenants/{tenant_id}/usage", {
          params: {
            path: { tenant_id: tenantId },
            query: { period: query.period, ...defined({ start: query.start, group_by: query.group_by }) },
          },
          signal,
        }),
      ),
    enabled: enabled && Boolean(tenantId),
    placeholderData: keepPreviousData,
  });
}

// ------------------------------------------------------------------------- audit

const AUDIT_PAGE = 100;

export function useAudit(filters: AuditFilters, tenantId: string | null) {
  return useInfiniteQuery({
    queryKey: tenantId ? qk.tenantAudit(tenantId, filters) : qk.adminAudit(filters),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => {
      const query = { limit: AUDIT_PAGE, ...defined(filters), ...(pageParam ? { cursor: pageParam } : {}) };
      return tenantId
        ? unwrap(api.GET("/v1/tenants/{tenant_id}/audit", { params: { path: { tenant_id: tenantId }, query }, signal }))
        : unwrap(api.GET("/v1/admin/audit", { params: { query }, signal }));
    },
    getNextPageParam: (last) => last.next_cursor ?? null,
  });
}

// ------------------------------------------------------------------------- operators

export function useAdminTenants(filters: AdminTenantFilters = {}) {
  return useQuery({
    queryKey: qk.adminTenants(filters),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/admin/tenants", { params: { query: { limit: 200, ...defined(filters) } }, signal })),
    placeholderData: keepPreviousData,
  });
}

export function useAdminCreateTenant() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: TenantCreate) => unwrap(api.POST("/v1/admin/tenants", { body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "tenants"] }),
  });
}

export function useAdminUpdateTenant() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ tenantId, body }: { tenantId: string; body: TenantAdminUpdate }) =>
      unwrap(api.PATCH("/v1/admin/tenants/{tenant_id}", { params: { path: { tenant_id: tenantId } }, body })),
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["admin", "tenants"] }),
        qc.invalidateQueries({ queryKey: qk.me }),
      ]);
    },
  });
}

export function useQuotaPolicies(filters: QuotaFilters = {}) {
  return useQuery({
    queryKey: qk.adminQuotas(filters),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/admin/quotas", { params: { query: { limit: 200, ...defined(filters) } }, signal })),
  });
}

export function usePutQuota() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: QuotaPolicyUpsert) => unwrap(api.PUT("/v1/admin/quotas", { body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "quotas"] }),
  });
}

export function useDeleteQuota() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ scope, subjectId }: { scope: QuotaScope; subjectId: string }) =>
      unwrapVoid(
        api.DELETE("/v1/admin/quotas/{scope}/{subject_id}", { params: { path: { scope, subject_id: subjectId } } }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "quotas"] }),
  });
}

export interface AdminUsageQuery {
  period: "day" | "month";
  /** An instant inside the period (default: now). */
  start?: string | undefined;
}

/** `GET /v1/admin/usage`: cost by tenant for one UTC period and spend against the global cap. */
export function useAdminUsage(query: AdminUsageQuery, enabled = true) {
  return useQuery({
    queryKey: qk.adminUsage(query),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/admin/usage", { params: { query: { period: query.period, ...defined({ start: query.start }) } }, signal })),
    enabled,
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  });
}

export function useKillSwitches() {
  return useQuery({
    queryKey: qk.killSwitches,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/admin/kill-switches", { signal })),
    refetchInterval: 30_000,
  });
}

export function usePutKillSwitches() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: KillSwitchUpdate) => unwrap(api.PUT("/v1/admin/kill-switches", { body })),
    onSuccess: (next) => {
      qc.setQueryData(qk.killSwitches, next);
      void qc.invalidateQueries({ queryKey: ["admin", "usage"] }); // the cap shown next to spend
    },
  });
}

export function useExecutors() {
  return useQuery({
    queryKey: qk.executors,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/admin/executors", { params: { query: { limit: 200 } }, signal })),
    refetchInterval: 15_000,
  });
}

/** Enable or disable an executor (operators; audited). A disabled one gets no new runs. */
export function usePutExecutor() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: ExecutorUpdate }) =>
      unwrap(api.PUT("/v1/admin/executors/{name}", { params: { path: { name } }, body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.executors }),
  });
}
