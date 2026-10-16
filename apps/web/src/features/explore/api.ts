/**
 * Explorer data (M3.5): the views an ingest or a fit wrote (`…/artifacts/ui/explore/…`),
 * explore runs (`POST …/explore`, `POST /v1/models:compare`, `GET /v1/explorations/{run_id}`)
 * and the signed view links they hand out, merged into one list of sections.
 */
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import type { JsonObject } from "@mapi/plot-sandbox/protocol";

import { ApiError, api, newIdempotencyKey, unwrap, unwrapWithStatus } from "@/api/client";
import type { components } from "@/api/schema";
import { datasetArtifactLink, modelArtifactLink, qk, useProjectScope, type ProjectScope } from "@/api/queries";
import type { ArtifactLink, PlotSpec } from "@/api/types";
import { fetchArtifactJson } from "@/lib/artifacts";

import { EXPLORE_INDEX, parseIndex, parseView, plotsName, viewName, type ExplorerIndexView, type ExplorerSection, type SectionViews } from "./views";

type Arrayify<T> = T extends (infer E)[] ? Arrayify<E>[] : T extends object ? { [K in keyof T]: Arrayify<T[K]> } : T;
type S = { [K in keyof components["schemas"]]: Arrayify<components["schemas"][K]> };

export type Exploration = S["Exploration"];
export type DataExploreRequest = S["DataExploreRequest"];
export type ModelExploreRequest = S["ModelExploreRequest"];
export type ModelCompareRequest = S["ModelCompareRequest"];
export type ModelValidation = S["ModelValidation"];
export type CheckResult = S["CheckResult"];
export type EvidenceItem = S["EvidenceItem"];
export type ValidationStatus = S["ValidationStatus"];
export type SplitPlan = NonNullable<ModelExploreRequest["backtest"]>;

/** What an explorer looks at. */
export type ExplorerTarget = { kind: "dataset"; id: string; version: number } | { kind: "model"; id: string; version: number };

export function targetKey(t: ExplorerTarget): string {
  return `${t.kind}:${t.id}@${t.version}`;
}

export const exploreKeys = {
  index: (p: string, t: ExplorerTarget) => ["p", p, "explore", targetKey(t), "index"] as const,
  view: (p: string, t: ExplorerTarget, origin: string, section: string) => ["p", p, "explore", targetKey(t), origin, section] as const,
  plots: (p: string, t: ExplorerTarget, origin: string, section: string) => ["p", p, "explore", targetKey(t), origin, section, "plots"] as const,
  exploration: (p: string, runId: string) => ["p", p, "explorations", runId] as const,
};

function artifactLink(t: ExplorerTarget, name: string): Promise<ArtifactLink> {
  return t.kind === "dataset" ? datasetArtifactLink(t.id, t.version, name) : modelArtifactLink(t.id, t.version, name);
}

/**
 * The index the ingest (datasets) or the fit (models) wrote; `null` when there is none (data
 * or models from before v0.5, or an ingest whose explorer step failed).
 */
export function useBaseIndex(t: ExplorerTarget | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: t ? exploreKeys.index(s.key, t) : ["p", s.key, "explore", "none"],
    enabled: t !== null,
    staleTime: 5 * 60_000,
    queryFn: async ({ signal }): Promise<ExplorerIndexView | null> => {
      if (!t) return null;
      try {
        const link = await artifactLink(t, EXPLORE_INDEX);
        return parseIndex(await fetchArtifactJson(link, { signal }));
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }
    },
  });
}

function explorationQuery(s: ProjectScope, runId: string) {
  return {
    queryKey: exploreKeys.exploration(s.key, runId),
    queryFn: ({ signal }: { signal?: AbortSignal }) =>
      unwrap(api.GET("/v1/explorations/{run_id}", { params: { path: { run_id: runId } }, headers: s.headers, ...(signal ? { signal } : {}) })) as Promise<Exploration>,
  };
}

/** An explore run, polled until it is done (its view links are signed for 15 minutes). */
export function useExploration(runId: string | null | undefined) {
  const s = useProjectScope();
  return useQuery({
    ...explorationQuery(s, runId ?? ""),
    enabled: Boolean(runId),
    staleTime: 60_000,
    refetchInterval: (q) => (q.state.data && !q.state.data.done ? 1500 : false),
  });
}

/** Fresh view links of a finished exploration (refetched when they are about to expire). */
async function explorationLinks(qc: QueryClient, s: ProjectScope, runId: string): Promise<ArtifactLink[]> {
  const ex = await qc.fetchQuery({ ...explorationQuery(s, runId), staleTime: 60_000 });
  return ex.views ?? [];
}

export interface StartResult {
  exploration: Exploration;
  /** `true` when the server answered from an earlier run with the same request (200). */
  cached: boolean;
}

export function useStartExploration(t: ExplorerTarget) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: DataExploreRequest | ModelExploreRequest): Promise<StartResult> => {
      const key = newIdempotencyKey();
      const res =
        t.kind === "dataset"
          ? await unwrapWithStatus(
              api.POST("/v1/datasets/{dataset_id}/versions/{version}/explore", {
                params: { path: { dataset_id: t.id, version: t.version }, header: { "Idempotency-Key": key } },
                headers: s.headers,
                body: body as DataExploreRequest,
              }),
            )
          : await unwrapWithStatus(
              api.POST("/v1/models/{model_id}/versions/{version}/explore", {
                params: { path: { model_id: t.id, version: t.version }, header: { "Idempotency-Key": key } },
                headers: s.headers,
                body: body as ModelExploreRequest,
              }),
            );
      return { exploration: res.data, cached: res.status === 200 };
    },
    onSuccess: (r) => {
      qc.setQueryData(exploreKeys.exploration(s.key, r.exploration.run.id), r.exploration);
      void qc.invalidateQueries({ queryKey: qk.runLists(s.key) });
    },
  });
}

/** `POST /v1/models:compare`. */
export function useCompareModels() {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: ModelCompareRequest): Promise<StartResult> => {
      const res = await unwrapWithStatus(
        api.POST("/v1/models:compare", { params: { header: { "Idempotency-Key": newIdempotencyKey() } }, headers: s.headers, body }),
      );
      return { exploration: res.data, cached: res.status === 200 };
    },
    onSuccess: (r) => qc.setQueryData(exploreKeys.exploration(s.key, r.exploration.run.id), r.exploration),
  });
}

// ------------------------------------------------------------------------------ sections

export type SectionStatus = "ok" | "skipped" | "error" | "missing";

export interface SectionState<T extends ExplorerSection = ExplorerSection> {
  section: T;
  status: SectionStatus;
  /** Why a section is skipped or failed, in plain words. */
  message: string | null;
  /** Where the view lives: written by the ingest/fit, or by an explore run. */
  origin: "base" | "run";
  hasPlots: boolean;
}

/**
 * One state per section, in `order`: an explore run's result wins over the base index
 * (same section asked again, perhaps with other options); sections neither computed are
 * `missing` (they need an explore run).
 */
export function mergeSections<T extends ExplorerSection>(order: readonly T[], base: ExplorerIndexView | null | undefined, run: Exploration | null | undefined): SectionState<T>[] {
  const fromRun = new Map((run?.done ? (run.index?.sections ?? []) : []).map((s) => [s.section, s]));
  const fromBase = new Map((base?.sections ?? []).map((s) => [s.section, s]));
  return order.map((section): SectionState<T> => {
    const r = fromRun.get(section);
    const b = fromBase.get(section);
    // A skipped or failed run result does not hide a good view from the ingest or fit.
    const ref = r && (r.status ?? "ok") === "ok" ? r : b && (b.status ?? "ok") === "ok" ? b : (r ?? b);
    if (!ref) return { section, status: "missing", message: null, origin: "base", hasPlots: false };
    const status = ref.status ?? "ok";
    return {
      section,
      status,
      message: ref.message_plain ?? null,
      origin: ref === r ? "run" : "base",
      hasPlots: typeof (ref as Record<string, unknown>).plots === "string",
    };
  });
}

async function resolveLink(qc: QueryClient, s: ProjectScope, t: ExplorerTarget, origin: "base" | "run", runId: string | null, name: string): Promise<ArtifactLink> {
  if (origin === "run" && runId) {
    const link = (await explorationLinks(qc, s, runId)).find((l) => l.name === name);
    if (link) return link;
  }
  return artifactLink(t, name);
}

/** A section's view (fetched through its signed link, checked against its format). */
export function useSectionView<K extends ExplorerSection>(t: ExplorerTarget, state: SectionState | undefined, runId: string | null, section: K) {
  const s = useProjectScope();
  const qc = useQueryClient();
  const origin = state?.origin ?? "base";
  return useQuery({
    queryKey: exploreKeys.view(s.key, t, origin === "run" ? `run:${runId ?? ""}` : "base", section),
    enabled: state?.status === "ok",
    staleTime: 10 * 60_000,
    queryFn: async ({ signal }): Promise<SectionViews[K]> => {
      const link = await resolveLink(qc, s, t, origin, runId, viewName(section));
      return parseView(section, await fetchArtifactJson(link, { signal }));
    },
  });
}

/** The section's platform plots (`ui/explore/plots/<section>.json`, mapi_plots.explorer recipes). */
export function useSectionPlots(t: ExplorerTarget, state: SectionState | undefined, runId: string | null, enabled: boolean) {
  const s = useProjectScope();
  const qc = useQueryClient();
  const origin = state?.origin ?? "base";
  const section = state?.section ?? "overview";
  return useQuery({
    queryKey: exploreKeys.plots(s.key, t, origin === "run" ? `run:${runId ?? ""}` : "base", section),
    enabled: enabled && state?.status === "ok" && state.hasPlots,
    staleTime: 10 * 60_000,
    queryFn: async ({ signal }): Promise<PlotSpec[]> => {
      const link = await resolveLink(qc, s, t, origin, runId, plotsName(section));
      const raw = await fetchArtifactJson(link, { signal });
      if (!Array.isArray(raw)) return [];
      return raw.filter((p): p is PlotSpec => typeof p === "object" && p !== null && typeof (p as { spec?: unknown }).spec === "object" && (p as { kind?: unknown }).kind === "vega-lite");
    },
  });
}

/** The body of a recipe PlotSpec for the sandbox (its data is inline in `spec.datasets`). */
export function recipeBody(plot: PlotSpec): JsonObject {
  return plot.spec as JsonObject;
}
