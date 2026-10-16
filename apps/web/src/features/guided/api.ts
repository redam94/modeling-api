/**
 * TanStack Query hooks for Guided mode (`/v1/guided/*`, endpoints, saved plots and embeds) and
 * the lookups that rebuild a session from server state. Project-scoped keys start with
 * `["p", "<tenant>/<project>", "guided", …]` like every other project query.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, newIdempotencyKey, unwrap, unwrapWithStatus } from "@/api/client";
import { ik } from "@/api/integrations";
import { modelArtifactLink, qk, useProjectScope } from "@/api/queries";
import type { ArtifactLink, DatasetVersion, ModelVersion, Run, SpecDoc } from "@/api/types";
import { fetchArtifactJson } from "@/lib/artifacts";

import { guidedTemplateOf } from "./session";
import type {
  ColumnProfile,
  EndpointPromote,
  GuidedAnswers,
  GuidedCompileBody,
  PlotEmbedCreate,
  PriorTranslationRequest,
  SavedPlotCreate,
} from "./types";
import { COUNTERFACTUAL_VIEW, FORECAST_VIEW, parseCounterfactualView, parseForecastView } from "./views";
import { parseResponseGrid, RESPONSE_GRID_VIEW, type ResponseGrid } from "./whatif";

export const gk = {
  templates: ["guided", "templates"] as const,
  template: (id: string) => ["guided", "templates", id] as const,
  detect: (p: string, templateId: string, digest: string) => ["p", p, "guided", "detect", templateId, digest] as const,
  draft: (p: string, digest: string) => ["p", p, "guided", "draft", digest] as const,
  translation: (p: string, runId: string) => ["p", p, "guided", "translations", runId] as const,
  grid: (p: string, modelId: string, version: number) => ["p", p, "guided", "grid", modelId, version] as const,
};

// ----------------------------------------------------------------------------- templates

export function useGuidedTemplates(opts: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: gk.templates,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/guided/templates", { params: { query: { limit: 50 } }, signal })),
    staleTime: 10 * 60_000,
    enabled: opts.enabled ?? true,
  });
}

export function useGuidedTemplate(id: string | null | undefined) {
  return useQuery({
    queryKey: gk.template(id ?? ""),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/guided/templates/{template_id}", { params: { path: { template_id: id ?? "" } }, signal })),
    enabled: Boolean(id),
    staleTime: 10 * 60_000,
  });
}

// ----------------------------------------------------------------------------- questions and drafts

function digest(value: unknown): string {
  // A short, stable key for a JSON value (FNV-1a over its text); collisions only cost a refetch.
  const text = JSON.stringify(value) ?? "";
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return `${(h >>> 0).toString(36)}-${text.length}`;
}

/** Suggested columns per question from the browser's profiles of the upload. */
export function useDetectRoles(templateId: string, columns: ColumnProfile[] | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: gk.detect(s.key, templateId, digest(columns)),
    queryFn: ({ signal }) =>
      unwrap(api.POST("/v1/guided/detect-roles", { body: { template_id: templateId, columns: columns ?? [] }, headers: s.headers, signal })),
    enabled: Boolean(columns?.length),
    staleTime: Infinity,
    retry: false,
  });
}

/** The draft (ingest mapping + spec + YAML + issues) of the current answers; stores nothing. */
export function useGuidedDraft(body: GuidedAnswers | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: gk.draft(s.key, digest(body)),
    queryFn: ({ signal }) => unwrap(api.POST("/v1/guided/drafts", { body: body as GuidedAnswers, headers: s.headers, signal })),
    enabled: body !== null,
    placeholderData: keepPreviousData,
    staleTime: Infinity,
    retry: false,
  });
}

// ----------------------------------------------------------------------------- business priors

export function useStartPriorTranslation() {
  const s = useProjectScope();
  return useMutation({
    mutationFn: ({ body, key }: { body: PriorTranslationRequest; key: string }) =>
      unwrap(
        api.POST("/v1/guided/prior-translations", {
          params: { header: { "Idempotency-Key": key } },
          body,
          headers: s.headers,
        }),
      ),
  });
}

/** A `translate_priors` run and, once it succeeded, the translated priors and their preview. */
export function usePriorTranslation(runId: string | null | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: gk.translation(s.key, runId ?? ""),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/guided/prior-translations/{run_id}", { params: { path: { run_id: runId ?? "" } }, headers: s.headers, signal })),
    enabled: Boolean(runId),
    refetchInterval: (q) => (q.state.data && !q.state.data.done ? 1500 : false),
  });
}

// ----------------------------------------------------------------------------- run exact / forecasts

export function compileGuided(body: GuidedCompileBody, headers: Record<string, string>, signal?: AbortSignal) {
  return unwrap(api.POST("/v1/guided/requests:compile", { body, headers, ...(signal ? { signal } : {}) }));
}

// ----------------------------------------------------------------------------- session lookups

/** Specs drafted in Guided mode (`metadata.labels.guided`), newest first. */
export function guidedSessions(specs: SpecDoc[] | undefined): SpecDoc[] {
  return (specs ?? [])
    .filter((s) => guidedTemplateOf(s.latest?.document) !== null)
    .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
}

/** The dataset a spec names (by name; the API looks datasets up by id). */
export function useDatasetByName(name: string | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: [...qk.datasets(s.key), "by-name", name ?? ""],
    queryFn: async ({ signal }) => {
      const page = await unwrap(api.GET("/v1/datasets", { params: { query: { limit: 200 } }, headers: s.headers, signal }));
      return page.items.find((d) => d.name === name) ?? null;
    },
    enabled: Boolean(name),
  });
}

/** The newest version of a dataset, polled while it is being ingested. */
export function useLatestDatasetVersion(datasetId: string | null | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: [...qk.datasetVersions(s.key, datasetId ?? ""), "latest"],
    queryFn: async ({ signal }): Promise<DatasetVersion | null> => {
      const page = await unwrap(
        api.GET("/v1/datasets/{dataset_id}/versions", {
          params: { path: { dataset_id: datasetId ?? "" }, query: { limit: 20 } },
          headers: s.headers,
          signal,
        }),
      );
      return [...page.items].sort((a, b) => b.version - a.version)[0] ?? null;
    },
    enabled: Boolean(datasetId),
    refetchInterval: (q) => (q.state.data?.status === "ingesting" ? 1500 : false),
  });
}

/** The fit runs of a spec, newest first (polled while one is active). */
export function useFitRuns(specId: string | null | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.runs(s.key, { kind: "fit", spec_id: specId ?? "", limit: 20 }),
    queryFn: async ({ signal }): Promise<Run[]> => {
      const page = await unwrap(
        api.GET("/v1/runs", { params: { query: { kind: "fit", spec_id: specId ?? "", limit: 20 } }, headers: s.headers, signal }),
      );
      return page.items.filter((r) => r.spec_id === specId).sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    },
    enabled: Boolean(specId),
  });
}

/** The model version a fit produced (looked up through the model named like the spec). */
export function useModelVersionForRun(modelName: string | null, run: Run | null | undefined) {
  const s = useProjectScope();
  const mvId = run?.output_model_version_id ?? null;
  return useQuery({
    queryKey: [...qk.models(s.key), "guided", modelName ?? "", mvId ?? ""],
    queryFn: async ({ signal }): Promise<ModelVersion | null> => {
      const models = await unwrap(api.GET("/v1/models", { params: { query: { limit: 200 } }, headers: s.headers, signal }));
      const model = models.items.find((m) => m.name === modelName);
      if (!model) return null;
      const versions = await unwrap(
        api.GET("/v1/models/{model_id}/versions", { params: { path: { model_id: model.id }, query: { limit: 200 } }, headers: s.headers, signal }),
      );
      return versions.items.find((v) => v.id === mvId) ?? null;
    },
    enabled: Boolean(modelName && mvId),
    staleTime: Infinity,
  });
}

/** The what-if grid of a fitted model version (`ui/grids/response_curves.json`), or `null`. */
export function useResponseGrid(mv: ModelVersion | null | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: gk.grid(s.key, mv?.model_id ?? "", mv?.version ?? 0),
    queryFn: async ({ signal }): Promise<ResponseGrid | null> => {
      if (!mv) return null;
      let link: ArtifactLink;
      try {
        link = await modelArtifactLink(mv.model_id, mv.version, RESPONSE_GRID_VIEW);
      } catch {
        return null; // the model wrote no grid (not an MMM, or an older runner)
      }
      return parseResponseGrid(await fetchArtifactJson(link, { signal }));
    },
    enabled: Boolean(mv),
    staleTime: Infinity,
  });
}

// ----------------------------------------------------------------------------- score results

export type ScoreViewKind = "forecast" | "counterfactual";

/** Fetch a finished score run's Guided view from its output links. */
export async function fetchScoreView(outputs: ArtifactLink[], kind: ScoreViewKind, signal?: AbortSignal) {
  const name = kind === "forecast" ? FORECAST_VIEW : COUNTERFACTUAL_VIEW;
  const link = outputs.find((o) => o.name === name || o.name.endsWith(`/${name}`));
  if (!link) throw new Error("The run finished but did not write the result view this screen reads.");
  const raw = await fetchArtifactJson(link, signal ? { signal } : {});
  return kind === "forecast" ? { kind, view: parseForecastView(raw) } : { kind, view: parseCounterfactualView(raw) };
}

// ----------------------------------------------------------------------------- endpoints & sharing

/**
 * The project's endpoints: the Integrations hub's query (`ik.endpoints`), so a promotion here
 * shows in the hub at once and the other way round.
 */
export { useEndpoints } from "@/api/integrations";

export function usePromoteEndpoint() {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ alias, body }: { alias: string; body: EndpointPromote }) =>
      unwrap(api.PUT("/v1/endpoints/{alias}", { params: { path: { alias } }, body, headers: s.headers })),
    // The list, the endpoint, its history, schema and snippets (all under ik.endpoints).
    onSuccess: () => qc.invalidateQueries({ queryKey: ik.endpoints(s.key) }),
  });
}

/** Save a model version's plot and create a tokenised embed link for it (shown once). */
export function useCreateEmbed() {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ plot, embed }: { plot: SavedPlotCreate; embed: PlotEmbedCreate }) => {
      const saved = await unwrap(api.POST("/v1/plots", { body: plot, headers: s.headers }));
      const created = await unwrap(api.POST("/v1/plots/{plot_id}/embeds", { params: { path: { plot_id: saved.id } }, body: embed, headers: s.headers }));
      return { saved, created };
    },
    // The hub's "Plots and links" tab lists the saved plot and its link.
    onSuccess: () => qc.invalidateQueries({ queryKey: ik.savedPlots(s.key) }),
  });
}

/** Submit a score call (`Prefer: wait`); replaying with the same key collects the result. */
export async function submitScore(
  mv: Pick<ModelVersion, "model_id" | "version">,
  request: Record<string, unknown>,
  opts: { key?: string; wait?: number; headers: Record<string, string>; labels?: Record<string, string> },
) {
  const key = opts.key ?? newIdempotencyKey();
  const { data, status } = await unwrapWithStatus(
    api.POST("/v1/models/{model_id}/versions/{version}:score", {
      params: { path: { model_id: mv.model_id, version: mv.version }, header: { "Idempotency-Key": key, Prefer: `wait=${opts.wait ?? 30}` } },
      body: { request: request as never, labels: opts.labels ?? {} },
      headers: opts.headers,
    }),
  );
  return { result: data, status, key };
}
