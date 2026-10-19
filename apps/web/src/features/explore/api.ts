/**
 * Explorer data (M3.5): the views an ingest or a fit wrote (`…/artifacts/ui/explore/…`),
 * explore runs (`POST …/explore`, `POST /v1/models:compare`, `GET /v1/explorations/{run_id}`)
 * and the signed view links they hand out, merged into one list of sections.
 */
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import type { JsonObject, PlotDatasets } from "@mapi/plot-sandbox/protocol";

import { ApiError, api, newIdempotencyKey, unwrap, unwrapWithStatus } from "@/api/client";
import type { components } from "@/api/schema";
import { datasetArtifactLink, modelArtifactLink, qk, useProjectScope, type ProjectScope } from "@/api/queries";
import type { ArtifactLink, PlotSpec } from "@/api/types";
import { fetchArtifactJson } from "@/lib/artifacts";

import { resolvePlotDatasets } from "@/features/plots/plot-data";

import { EXPLORE_INDEX, parseIndex, parseView, plotsName, skipReason, viewName, type ExplorerIndexView, type ExplorerSection, type SectionViews, type SkipReason, type StructureHints } from "./views";

type Arrayify<T> = T extends (infer E)[] ? Arrayify<E>[] : T extends object ? { [K in keyof T]: Arrayify<T[K]> } : T;
type S = { [K in keyof components["schemas"]]: Arrayify<components["schemas"][K]> };

export type Exploration = S["Exploration"];
/**
 * `POST …/explore` of a dataset. v0.10: every one of the 21 sections, and `hints` that override
 * the structure detection (the OpenAPI types still list the 13 sections of v0.5).
 */
export type DataExploreRequest = Omit<S["DataExploreRequest"], "sections"> & { sections?: string[]; hints?: StructureHints | null };
export type ModelExploreRequest = S["ModelExploreRequest"];
export type ModelCompareRequest = S["ModelCompareRequest"];
export type ModelValidation = S["ModelValidation"];
export type CheckResult = S["CheckResult"];
export type EvidenceItem = S["EvidenceItem"];
export type ValidationStatus = S["ValidationStatus"];
export type SplitPlan = NonNullable<ModelExploreRequest["backtest"]>;

/**
 * What an explorer looks at. M4: `node` is the node of a tree dataset (its ingest views are
 * under `ui/explore/nodes/<slug>/`, listed in `ui/explore/nodes/index.json`).
 */
export type ExplorerTarget = { kind: "dataset"; id: string; version: number; node?: string | null } | { kind: "model"; id: string; version: number };

export function targetKey(t: ExplorerTarget): string {
  const node = t.kind === "dataset" && t.node ? `#${t.node}` : "";
  return `${t.kind}:${t.id}@${t.version}${node}`;
}

/** M4: the node → explorer index map a tree ingest writes. */
export const EXPLORE_NODES_INDEX = "ui/explore/nodes/index.json";

/** The per-node index of `node` from `ui/explore/nodes/index.json` (`null`: not listed). */
export function nodeIndexName(raw: unknown, node: string): string | null {
  if (typeof raw !== "object" || raw === null) return null;
  const nodes = (raw as { nodes?: unknown }).nodes;
  if (typeof nodes !== "object" || nodes === null) return null;
  const entry = (nodes as Record<string, unknown>)[node];
  const index = typeof entry === "object" && entry !== null ? (entry as { index?: unknown }).index : null;
  // Only an index under ui/explore/nodes/ (the server serves nothing else there either).
  return typeof index === "string" && index.startsWith("ui/explore/nodes/") && index.endsWith(".json") && !index.includes("..") ? index : null;
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
        if (t.kind === "dataset" && t.node) {
          const nodes = await fetchArtifactJson(await artifactLink(t, EXPLORE_NODES_INDEX), { signal });
          const name = nodeIndexName(nodes, t.node);
          if (!name) return null;
          return parseIndex(await fetchArtifactJson(await artifactLink(t, name), { signal }));
        }
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
                // the OpenAPI types still list the 13 sections of v0.5 and no hints
                body: body as unknown as S["DataExploreRequest"],
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
  /** v0.10: why a section is skipped (`null`: unknown, e.g. an index from before v0.10). */
  reason?: SkipReason | null;
  /** Where the view lives: written by the ingest/fit, or by an explore run. */
  origin: "base" | "run";
  hasPlots: boolean;
  /** The view's object name (M4: under `ui/explore/nodes/<slug>/` for a node of a tree). */
  view?: string | undefined;
  /** The plots' object name, when `hasPlots`. */
  plots?: string | undefined;
}

const VIEW_NAME_RE = /^ui\/explore\/[A-Za-z0-9_.~\-/]{1,240}\.json$/;
const safeView = (v: unknown): string | undefined => (typeof v === "string" && VIEW_NAME_RE.test(v) && !v.includes("..") ? v : undefined);

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
    const plots = safeView((ref as Record<string, unknown>).plots);
    return {
      section,
      status,
      message: ref.message_plain ?? null,
      reason: status === "skipped" ? skipReason(ref) : null,
      origin: ref === r ? "run" : "base",
      hasPlots: typeof (ref as Record<string, unknown>).plots === "string",
      view: safeView(ref.view),
      plots,
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
      const link = await resolveLink(qc, s, t, origin, runId, state?.view ?? viewName(section));
      return parseView(section, await fetchArtifactJson(link, { signal }));
    },
  });
}

/** A recipe plot ready for the sandbox: its kind, body and datasets (decoded in the app). */
export interface RecipePlot {
  plot: PlotSpec;
  kind: "vega-lite" | "plotly";
  datasets: PlotDatasets;
}

const refuseLinkedData = (): Promise<never> => Promise.reject(new Error("an explorer plot must carry its data inline"));

/**
 * The recipe plots of a plots document: Vega-Lite (data inline in `spec.datasets`) and, since
 * v0.10, Plotly (the runner's `sci.*`/`net.*` recipes; inline Arrow datasets only, a dataset at
 * a path is refused). Other kinds and plots whose data does not decode are left out.
 */
export async function recipePlots(raw: unknown): Promise<RecipePlot[]> {
  if (!Array.isArray(raw)) return [];
  const out: RecipePlot[] = [];
  for (const p of raw) {
    if (typeof p !== "object" || p === null || typeof (p as { spec?: unknown }).spec !== "object") continue;
    const plot = p as PlotSpec;
    const kind = (p as { kind?: unknown }).kind;
    if (kind === "vega-lite") out.push({ plot, kind, datasets: {} });
    else if (kind === "plotly") {
      try {
        out.push({ plot, kind, datasets: await resolvePlotDatasets(plot, refuseLinkedData) });
      } catch {
        // a plot whose data the app cannot decode is left out; the others still show
      }
    }
  }
  return out;
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
    queryFn: async ({ signal }): Promise<RecipePlot[]> => {
      const link = await resolveLink(qc, s, t, origin, runId, state?.plots ?? plotsName(section));
      return recipePlots(await fetchArtifactJson(link, { signal }));
    },
  });
}

/** The body of a recipe PlotSpec for the sandbox (its data is inline in `spec.datasets`). */
export function recipeBody(plot: PlotSpec): JsonObject {
  return plot.spec as JsonObject;
}
