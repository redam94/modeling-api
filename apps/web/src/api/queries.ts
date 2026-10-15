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

import { api, newIdempotencyKey, PROJECT_HEADER, unwrap, unwrapVoid, unwrapWithStatus } from "./client";
import { advanceLogTail, EMPTY_LOG_TAIL, type LogTail } from "./logs";
import { fetchSession } from "./session";
import type {
  ArtifactLink,
  AuditAction,
  AuditOutcome,
  DatasetCreate,
  DatasetVersionCreate,
  KillSwitchUpdate,
  Me,
  MembershipCreate,
  ProjectCreate,
  ProjectRole,
  Model,
  ModelVersion,
  Page,
  PlotSpec,
  QuotaPolicyUpsert,
  QuotaScope,
  Run,
  RunCreate,
  RunKind,
  RunRetry,
  RunState,
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
  runLogs: (p: string, id: string, stream: string | null) => ["p", p, "runs", id, "logs", stream] as const,
  runPlots: (p: string, id: string) => ["p", p, "runs", id, "plots"] as const,
  models: (p: string) => ["p", p, "models"] as const,
  model: (p: string, id: string) => ["p", p, "models", id] as const,
  modelVersions: (p: string, id: string) => ["p", p, "models", id, "versions"] as const,
  modelVersion: (p: string, id: string, v: number) => ["p", p, "models", id, "versions", v] as const,
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
  executors: ["admin", "executors"] as const,
  adminAudit: (f: AuditFilters) => ["admin", "audit", f] as const,
};

export interface RunFilters {
  state?: RunState;
  kind?: RunKind;
  spec_id?: string;
  limit?: number;
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

export function useRuns(filters: RunFilters, opts: { live?: boolean } = {}) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runs(s.key, filters),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/runs", {
          params: {
            query: {
              limit: filters.limit ?? PAGE_LIMIT,
              ...(filters.state ? { state: filters.state } : {}),
              ...(filters.kind ? { kind: filters.kind } : {}),
              ...(filters.spec_id ? { spec_id: filters.spec_id } : {}),
            },
          },
          headers: s.headers,
          signal,
        }),
      ),
    placeholderData: keepPreviousData,
    refetchInterval: opts.live === false ? false : 5000,
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

/** Pages of 200 lines fetched per poll (or per "Load more"). */
export const MAX_LOG_PAGES = 10;
/** Lines kept in memory per (run, stream); the oldest are dropped beyond this. */
export const MAX_LOG_LINES = 10_000;

/**
 * Captured log lines, followed as a tail: each fetch continues after what is already loaded
 * (see {@link advanceLogTail}), so a live run's newest lines keep arriving however long it
 * logs, and no page is downloaded twice except the still-open last one. `behind` means newer
 * pages exist that are not loaded yet; a live run catches up quickly, otherwise `refetch()`
 * loads the next batch.
 */
export function useRunLogs(id: string, stream: "stdout" | "stderr" | "system" | null, live: boolean) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runLogs(s.key, id, stream),
    queryFn: ({ queryKey, signal }): Promise<LogTail> =>
      advanceLogTail(
        qc.getQueryData<LogTail>(queryKey) ?? EMPTY_LOG_TAIL,
        (cursor) =>
          unwrap(
            api.GET("/v1/runs/{run_id}/logs", {
              params: {
                path: { run_id: id },
                query: { limit: 200, ...(stream ? { stream } : {}), ...(cursor ? { cursor } : {}) },
              },
              headers: s.headers,
              signal,
            }),
          ),
        { maxPages: MAX_LOG_PAGES, maxLines: MAX_LOG_LINES },
      ),
    refetchInterval: (q) => (live ? (q.state.data?.behind ? 500 : 4000) : false),
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

export function usePlots(modelId: string, version: number) {
  const s = useProjectScope();
  return useQuery({
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

/** Sanitized plots a succeeded run rendered (score fans, counterfactual bands). */
export function useRunPlots(runId: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runPlots(s.key, runId ?? ""),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/runs/{run_id}/plots", {
          params: { path: { run_id: runId ?? "" }, query: { limit: 200 } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(runId),
    staleTime: Infinity,
  });
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
    mutationFn: ({ principalId, role }: { principalId: string; role: ProjectRole }) =>
      projectId
        ? unwrap(
            api.PATCH("/v1/tenants/{tenant_id}/projects/{project_id}/members/{principal_id}", {
              params: { path: { tenant_id: tenantId, project_id: projectId, principal_id: principalId } },
              body: { role },
            }),
          )
        : unwrap(
            api.PATCH("/v1/tenants/{tenant_id}/members/{principal_id}", {
              params: { path: { tenant_id: tenantId, principal_id: principalId } },
              body: { role },
            }),
          ),
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

export function useUsage(query: UsageQuery) {
  const s = useProjectScope();
  return useQuery({
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
    onSuccess: (next) => qc.setQueryData(qk.killSwitches, next),
  });
}

export function useExecutors() {
  return useQuery({
    queryKey: qk.executors,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/admin/executors", { params: { query: { limit: 200 } }, signal })),
    refetchInterval: 15_000,
  });
}
