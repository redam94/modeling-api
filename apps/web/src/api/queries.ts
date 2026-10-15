/**
 * TanStack Query hooks over the typed client. Server state lives here (never in Zustand).
 */
import {
  keepPreviousData,
  queryOptions,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import { isTerminal } from "@/lib/run-state";

import { api, newIdempotencyKey, unwrap, unwrapWithStatus } from "./client";
import { advanceLogTail, EMPTY_LOG_TAIL, type LogTail } from "./logs";
import type {
  ArtifactLink,
  DatasetCreate,
  DatasetVersionCreate,
  Model,
  ModelVersion,
  Page,
  PlotSpec,
  Run,
  RunCreate,
  RunKind,
  RunRetry,
  RunState,
  ScoreCall,
  ScoreCallResult,
} from "./types";

export const PAGE_LIMIT = 50;

export const qk = {
  health: ["health"] as const,
  ready: ["ready"] as const,
  catalog: ["catalog"] as const,
  schema: (name: string) => ["schema", name] as const,
  datasets: ["datasets"] as const,
  dataset: (id: string) => ["datasets", id] as const,
  datasetVersions: (id: string) => ["datasets", id, "versions"] as const,
  datasetVersion: (id: string, v: number) => ["datasets", id, "versions", v] as const,
  datasetArtifact: (id: string, v: number, name: string) => ["datasets", id, "versions", v, "artifact", name] as const,
  specs: ["specs"] as const,
  spec: (id: string) => ["specs", id] as const,
  specRevisions: (id: string) => ["specs", id, "revisions"] as const,
  specRevision: (id: string, r: number) => ["specs", id, "revisions", r] as const,
  runs: (filters: RunFilters) => ["runs", "list", filters] as const,
  run: (id: string) => ["runs", id] as const,
  runEvents: (id: string) => ["runs", id, "events"] as const,
  runLogs: (id: string, stream: string | null) => ["runs", id, "logs", stream] as const,
  models: ["models"] as const,
  model: (id: string) => ["models", id] as const,
  modelVersions: (id: string) => ["models", id, "versions"] as const,
  modelVersion: (id: string, v: number) => ["models", id, "versions", v] as const,
  modelArtifact: (id: string, v: number, name: string) => ["models", id, "versions", v, "artifact", name] as const,
  plots: (id: string, v: number) => ["models", id, "versions", v, "plots"] as const,
  plot: (id: string, v: number, plotId: string) => ["models", id, "versions", v, "plots", plotId] as const,
  modelVersionById: (mvId: string) => ["model-version-by-id", mvId] as const,
};

export interface RunFilters {
  state?: RunState;
  kind?: RunKind;
  spec_id?: string;
  limit?: number;
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
  return useQuery({
    queryKey: qk.datasets,
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/datasets", { params: { query: { limit: 200 } }, signal })),
  });
}

export function useDataset(id: string) {
  return useQuery({
    queryKey: qk.dataset(id),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/datasets/{dataset_id}", { params: { path: { dataset_id: id } }, signal })),
  });
}

export function useDatasetVersions(id: string | undefined) {
  return useQuery({
    queryKey: qk.datasetVersions(id ?? ""),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/datasets/{dataset_id}/versions", {
          params: { path: { dataset_id: id ?? "" }, query: { limit: 200 } },
          signal,
        }),
      ),
    enabled: Boolean(id),
    refetchInterval: (q) =>
      q.state.data?.items.some((v) => v.status === "ingesting") ? 3000 : false,
  });
}

export function useDatasetVersion(id: string, version: number | undefined) {
  return useQuery({
    queryKey: qk.datasetVersion(id, version ?? 0),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/datasets/{dataset_id}/versions/{version}", {
          params: { path: { dataset_id: id, version: version ?? 0 } },
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
  return useMutation({
    mutationFn: (body: DatasetCreate) => unwrap(api.POST("/v1/datasets", { body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.datasets }),
  });
}

export function createUpload(datasetId: string, body: { filename: string; content_type: string; size_bytes: number }) {
  return unwrap(
    api.POST("/v1/datasets/{dataset_id}/uploads", { params: { path: { dataset_id: datasetId } }, body }),
  );
}

export function useCreateDatasetVersion(datasetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ body, key }: { body: DatasetVersionCreate; key: string }) =>
      unwrap(
        api.POST("/v1/datasets/{dataset_id}/versions", {
          params: { path: { dataset_id: datasetId }, header: { "Idempotency-Key": key } },
          body,
        }),
      ),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: qk.dataset(datasetId) });
    },
  });
}

// ------------------------------------------------------------------------- specs

export function useSpecs() {
  return useQuery({
    queryKey: qk.specs,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/specs", { params: { query: { limit: 200 } }, signal })),
  });
}

export function useSpec(id: string | undefined) {
  return useQuery({
    queryKey: qk.spec(id ?? ""),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/specs/{spec_id}", { params: { path: { spec_id: id ?? "" } }, signal })),
    enabled: Boolean(id),
  });
}

export function useSpecRevisions(id: string | undefined) {
  return useQuery({
    queryKey: qk.specRevisions(id ?? ""),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/specs/{spec_id}/revisions", {
          params: { path: { spec_id: id ?? "" }, query: { limit: 200 } },
          signal,
        }),
      ),
    enabled: Boolean(id),
  });
}

export function useSpecRevision(id: string | undefined, revision: number | undefined) {
  return useQuery({
    queryKey: qk.specRevision(id ?? "", revision ?? 0),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/specs/{spec_id}/revisions/{revision}", {
          params: { path: { spec_id: id ?? "", revision: revision ?? 0 } },
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
  return useMutation({
    mutationFn: (body: { yaml: string; message?: string | null }) => unwrap(api.POST("/v1/specs", { body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.specs }),
  });
}

export function useCreateRevision(specId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { yaml: string; parent_revision: number; message?: string | null }) =>
      unwrap(api.POST("/v1/specs/{spec_id}/revisions", { params: { path: { spec_id: specId } }, body })),
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: qk.spec(specId) }),
        qc.invalidateQueries({ queryKey: qk.specs }),
      ]);
    },
  });
}

export function useDryRun() {
  return useMutation({
    mutationFn: ({ specId, revision }: { specId: string; revision: number }) =>
      unwrap(
        api.POST("/v1/specs/{spec_id}/revisions/{revision}:dry-run", {
          params: { path: { spec_id: specId, revision }, header: { "Idempotency-Key": newIdempotencyKey() } },
        }),
      ),
  });
}

// ------------------------------------------------------------------------- runs

export function useRuns(filters: RunFilters, opts: { live?: boolean } = {}) {
  return useQuery({
    queryKey: qk.runs(filters),
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
          signal,
        }),
      ),
    placeholderData: keepPreviousData,
    refetchInterval: opts.live === false ? false : 5000,
  });
}

export const runQuery = (id: string) =>
  queryOptions({
    queryKey: qk.run(id),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/runs/{run_id}", { params: { path: { run_id: id } }, signal })),
  });

export function useRun(id: string | undefined, opts: { poll?: boolean } = {}) {
  return useQuery({
    ...runQuery(id ?? ""),
    enabled: Boolean(id),
    // SSE drives live updates; polling is a slow safety net while the run is active.
    refetchInterval: (q) => (opts.poll !== false && q.state.data && !isTerminal(q.state.data.state) ? 15_000 : false),
  });
}

export function useRunEventsPage(id: string) {
  return useQuery({
    queryKey: qk.runEvents(id),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/runs/{run_id}/events", {
          params: { path: { run_id: id }, query: { limit: 200 } },
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
  return useQuery({
    queryKey: qk.runLogs(id, stream),
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
              signal,
            }),
          ),
        { maxPages: MAX_LOG_PAGES, maxLines: MAX_LOG_LINES },
      ),
    refetchInterval: (q) => (live ? (q.state.data?.behind ? 500 : 4000) : false),
  });
}

function setRun(qc: QueryClient, run: Run) {
  qc.setQueryData(qk.run(run.id), run);
  void qc.invalidateQueries({ queryKey: ["runs", "list"] });
}

export function useCreateRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ body, key }: { body: RunCreate; key: string }) =>
      unwrap(api.POST("/v1/runs", { params: { header: { "Idempotency-Key": key } }, body })),
    onSuccess: (run) => setRun(qc, run),
  });
}

export function useCancelRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => unwrap(api.POST("/v1/runs/{run_id}:cancel", { params: { path: { run_id: id } } })),
    onSuccess: (run) => setRun(qc, run),
  });
}

export function useResumeRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => unwrap(api.POST("/v1/runs/{run_id}:resume", { params: { path: { run_id: id } } })),
    onSuccess: (run) => setRun(qc, run),
  });
}

export function useRetryRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body, key }: { id: string; body: RunRetry; key: string }) =>
      unwrap(
        api.POST("/v1/runs/{run_id}:retry", {
          params: { path: { run_id: id }, header: { "Idempotency-Key": key } },
          body,
        }),
      ),
    onSuccess: (run) => setRun(qc, run),
  });
}

// ------------------------------------------------------------------------- models

export function useModels() {
  return useQuery({
    queryKey: qk.models,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/models", { params: { query: { limit: 200 } }, signal })),
  });
}

export function useModel(id: string | undefined) {
  return useQuery({
    queryKey: qk.model(id ?? ""),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/models/{model_id}", { params: { path: { model_id: id ?? "" } }, signal })),
    enabled: Boolean(id),
  });
}

export function useModelVersions(id: string | undefined) {
  return useQuery({
    queryKey: qk.modelVersions(id ?? ""),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/models/{model_id}/versions", {
          params: { path: { model_id: id ?? "" }, query: { limit: 200 } },
          signal,
        }),
      ),
    enabled: Boolean(id),
  });
}

export function useModelVersion(id: string | undefined, version: number | undefined) {
  return useQuery({
    queryKey: qk.modelVersion(id ?? "", version ?? 0),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/models/{model_id}/versions/{version}", {
          params: { path: { model_id: id ?? "", version: version ?? 0 } },
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
 * Find a model version by its id (`Run.output_model_version_id`). The M1 API has no
 * direct lookup, so this scans models and their versions (bounded; single-tenant).
 */
export function useModelVersionById(mvId: string | null | undefined) {
  return useQuery({
    queryKey: qk.modelVersionById(mvId ?? ""),
    enabled: Boolean(mvId),
    staleTime: Infinity,
    queryFn: async ({ signal }): Promise<ModelVersion | null> => {
      const models: Page<Model> = await unwrap(
        api.GET("/v1/models", { params: { query: { limit: 200 } }, signal }),
      );
      for (const m of models.items) {
        const versions = await unwrap(
          api.GET("/v1/models/{model_id}/versions", {
            params: { path: { model_id: m.id }, query: { limit: 200 } },
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
  return useQuery({
    queryKey: qk.plots(modelId, version),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/models/{model_id}/versions/{version}/plots", {
          params: { path: { model_id: modelId, version }, query: { limit: 200 } },
          signal,
        }),
      ),
  });
}

/** Sanitized plots a succeeded run rendered (score fans, counterfactual bands). */
export function useRunPlots(runId: string | undefined) {
  return useQuery({
    queryKey: ["runs", runId ?? "", "plots"],
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/runs/{run_id}/plots", {
          params: { path: { run_id: runId ?? "" }, query: { limit: 200 } },
          signal,
        }),
      ),
    enabled: Boolean(runId),
    staleTime: Infinity,
  });
}

export function usePlot(modelId: string, version: number, plotId: string | null) {
  return useQuery({
    queryKey: qk.plot(modelId, version, plotId ?? ""),
    queryFn: ({ signal }): Promise<PlotSpec> =>
      unwrap(
        api.GET("/v1/models/{model_id}/versions/{version}/plots/{plot_id}", {
          params: { path: { model_id: modelId, version, plot_id: plotId ?? "" } },
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
