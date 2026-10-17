/**
 * About cards, "Help me choose" and dataset About (M3.5): `GET /v1/catalog/abouts`,
 * `GET /v1/catalog/chooser`, `GET /v1/catalog/recommendations` and
 * `GET`/`PATCH /v1/datasets/{dataset_id}/about`.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { api, unwrap } from "@/api/client";
import { qk, useProjectScope } from "@/api/queries";

import { aboutKey, bareId } from "./labels";
import type { About, AboutKind, CatalogAbout, ChooserAnswers, ChooserKind, ChooserQuestionnaire, ChooserResult, DatasetAbout, DatasetAboutUpdate } from "./types";

export const aboutKeys = {
  abouts: ["catalog", "abouts"] as const,
  chooser: ["catalog", "chooser"] as const,
  recommendations: (answers: ChooserAnswers, kind: ChooserKind | null, limit: number) => ["catalog", "recommendations", answers, kind, limit] as const,
  datasetAbout: (p: string, id: string) => ["p", p, "datasets", id, "about"] as const,
};

/** Every page of the cards (at most 200 per page; the catalog holds about 150). */
export async function fetchAllAbouts(signal?: AbortSignal): Promise<CatalogAbout[]> {
  const out: CatalogAbout[] = [];
  let cursor: string | null | undefined = undefined;
  for (let i = 0; i < 20; i++) {
    const page: { items: CatalogAbout[]; next_cursor?: string | null } = await unwrap(api.GET("/v1/catalog/abouts", { params: { query: { limit: 200, ...(cursor ? { cursor } : {}) } }, ...(signal ? { signal } : {}) }));
    out.push(...page.items);
    cursor = page.next_cursor;
    if (!cursor) break;
  }
  return out;
}

export interface AboutIndex {
  items: CatalogAbout[];
  /** By `kind:id`. */
  byKey: Map<string, CatalogAbout>;
  /** By id alone (several kinds can share one; the first by catalog order wins). */
  byId: Map<string, CatalogAbout>;
}

export function indexAbouts(items: CatalogAbout[]): AboutIndex {
  const byKey = new Map<string, CatalogAbout>();
  const byId = new Map<string, CatalogAbout>();
  for (const a of items) {
    byKey.set(aboutKey(a.kind, a.id), a);
    if (!byId.has(a.id)) byId.set(a.id, a);
  }
  return { items, byKey, byId };
}

/** The installed catalog's About cards, indexed (cached for the session: they change only on deploys). */
export function useAboutIndex() {
  return useQuery({
    queryKey: aboutKeys.abouts,
    queryFn: ({ signal }) => fetchAllAbouts(signal),
    staleTime: 30 * 60_000,
    select: indexAbouts,
  });
}

/**
 * The card of one item, looked up by kind and id (`mmm.bayesian@^1` works too). `undefined`
 * while loading or when the item has no card in this catalog.
 */
export function useAbout(kind: AboutKind | null, ref: string | null | undefined): { about: About | undefined; entry: CatalogAbout | undefined; isPending: boolean } {
  const index = useAboutIndex();
  return useMemo(() => {
    if (!ref || !index.data) return { about: undefined, entry: undefined, isPending: index.isPending };
    const id = bareId(ref);
    const entry = kind ? index.data.byKey.get(aboutKey(kind, id)) : index.data.byId.get(id);
    return { about: entry?.about, entry, isPending: false };
  }, [index.data, index.isPending, kind, ref]);
}

export function useChooser() {
  return useQuery({
    queryKey: aboutKeys.chooser,
    queryFn: ({ signal }): Promise<ChooserQuestionnaire> => unwrap(api.GET("/v1/catalog/chooser", { signal })),
    staleTime: 30 * 60_000,
  });
}


/** The chooser's question order (`CHOOSER_QUESTIONS`); the server lists reasons in query order. */
const QUESTION_ORDER = ["goal", "data_shape", "frequency", "history", "drivers", "uncertainty", "experience", "speed"];

function questionRank(id: string): number {
  const i = QUESTION_ORDER.indexOf(id);
  return i < 0 ? QUESTION_ORDER.length : i;
}

/** Answers without empty questions, in the questionnaire's order (query keys and URLs). */
export function normaliseAnswers(answers: ChooserAnswers): ChooserAnswers {
  const out: ChooserAnswers = {};
  for (const key of Object.keys(answers).sort((a, b) => questionRank(a) - questionRank(b) || a.localeCompare(b))) {
    const values = [...new Set(answers[key] ?? [])].filter(Boolean);
    if (values.length) out[key] = values;
  }
  return out;
}

export function fetchRecommendations(answers: ChooserAnswers, kind: ChooserKind | null, limit: number, signal?: AbortSignal): Promise<ChooserResult> {
  // One query parameter per question; several answers are separated by commas.
  const query: Record<string, string | number> = { limit };
  if (kind) query.kind = kind;
  for (const [q, values] of Object.entries(answers)) query[q] = values.join(",");
  return unwrap(api.GET("/v1/catalog/recommendations", { params: { query: query }, ...(signal ? { signal } : {}) }));
}

/** Ranked recommendations for the answers so far (the previous list stays while the next loads). */
export function useRecommendations(answers: ChooserAnswers, opts: { kind?: ChooserKind | null; limit?: number; enabled?: boolean } = {}) {
  const normal = normaliseAnswers(answers);
  const kind = opts.kind ?? null;
  const limit = opts.limit ?? 6;
  return useQuery({
    queryKey: aboutKeys.recommendations(normal, kind, limit),
    queryFn: ({ signal }) => fetchRecommendations(normal, kind, limit, signal),
    enabled: opts.enabled ?? true,
    placeholderData: keepPreviousData,
    staleTime: 10 * 60_000,
  });
}

// ------------------------------------------------------------------------------ datasets

export function useDatasetAbout(datasetId: string) {
  const s = useProjectScope();
  return useQuery({
    queryKey: aboutKeys.datasetAbout(s.key, datasetId),
    queryFn: ({ signal }): Promise<DatasetAbout> =>
      unwrap(api.GET("/v1/datasets/{dataset_id}/about", { params: { path: { dataset_id: datasetId } }, headers: s.headers, signal })),
  });
}

export function useUpdateDatasetAbout(datasetId: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: DatasetAboutUpdate) =>
      unwrap(api.PATCH("/v1/datasets/{dataset_id}/about", { params: { path: { dataset_id: datasetId } }, headers: s.headers, body })),
    onSuccess: async (about) => {
      qc.setQueryData(aboutKeys.datasetAbout(s.key, datasetId), about);
      // The dataset's own description is the same text (lists and the page header).
      await Promise.all([qc.invalidateQueries({ queryKey: qk.dataset(s.key, datasetId) }), qc.invalidateQueries({ queryKey: qk.datasets(s.key) })]);
    },
  });
}
