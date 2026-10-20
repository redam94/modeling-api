/**
 * v0.14 run organisation and comparison: experiments (named groups of runs), saved run views,
 * the tracked keys of the project (`GET /v1/run-keys`) and the comparison of 2..10 runs
 * (`GET /v1/run-compare`). Limits mirror `mapi_spec.tracking` (I-3).
 */
import { api, unwrap, unwrapVoid } from "./client";
import type { ExperimentCreate, ExperimentPatch, ExperimentRunsWrite, RunViewCreate, RunViewPatch } from "./types";

export const MAX_COMPARE_RUNS = 10;
export const MIN_COMPARE_RUNS = 2;
export const MAX_COMPARE_KEYS = 12;
export const MAX_COMPARE_MEDIA_KEYS = 4;
export const MAX_COMPARE_MEDIA_ITEMS = 50;
export const MAX_COMPARE_SERIES = 16;
export const MAX_COMPARE_PARAMS = 2000;
export const MAX_RUN_VIEWS = 200;
export const MAX_EXPERIMENT_RUNS_WRITE = 200;
export const MAX_EXPERIMENT_NAME_CHARS = 200;
export const MAX_EXPERIMENT_DESCRIPTION_CHARS = 20_000;
/** Points per series the compare page asks for (the server allows 10..2000). */
export const COMPARE_POINTS = 300;

interface Ctx {
  headers: Record<string, string>;
  signal?: AbortSignal | undefined;
}

// ------------------------------------------------------------------------- experiments

export interface ExperimentFilters {
  name?: string | undefined;
  archived?: boolean | undefined;
}

/** Experiments of the project, newest first (up to 200; the page lists them all). */
export function fetchExperiments(f: ExperimentFilters, ctx: Ctx) {
  return unwrap(
    api.GET("/v1/experiments", {
      params: { query: { limit: 200, ...(f.name ? { name: f.name } : {}), ...(f.archived !== undefined ? { archived: f.archived } : {}) } },
      headers: ctx.headers,
      signal: ctx.signal,
    }),
  );
}

export function fetchExperiment(id: string, ctx: Ctx) {
  return unwrap(api.GET("/v1/experiments/{experiment_id}", { params: { path: { experiment_id: id } }, headers: ctx.headers, signal: ctx.signal }));
}

export function createExperiment(body: ExperimentCreate, ctx: Ctx) {
  return unwrap(api.POST("/v1/experiments", { body, headers: ctx.headers }));
}

export function updateExperiment(id: string, body: ExperimentPatch, ctx: Ctx) {
  return unwrap(api.PATCH("/v1/experiments/{experiment_id}", { params: { path: { experiment_id: id } }, body, headers: ctx.headers }));
}

export function deleteExperiment(id: string, ctx: Ctx) {
  return unwrapVoid(api.DELETE("/v1/experiments/{experiment_id}", { params: { path: { experiment_id: id } }, headers: ctx.headers }));
}

export function updateExperimentRuns(id: string, body: ExperimentRunsWrite, ctx: Ctx) {
  return unwrap(api.POST("/v1/experiments/{experiment_id}/runs", { params: { path: { experiment_id: id } }, body, headers: ctx.headers }));
}

// ------------------------------------------------------------------------- saved views

/** Saved views of the project (`experimentId`: only that experiment's), by name. */
export function fetchRunViews(experimentId: string | null, ctx: Ctx) {
  return unwrap(
    api.GET("/v1/run-views", {
      params: { query: { limit: 200, ...(experimentId ? { experiment_id: experimentId } : {}) } },
      headers: ctx.headers,
      signal: ctx.signal,
    }),
  );
}

export function createRunView(body: RunViewCreate, ctx: Ctx) {
  return unwrap(api.POST("/v1/run-views", { body, headers: ctx.headers }));
}

export function updateRunView(id: string, body: RunViewPatch, ctx: Ctx) {
  return unwrap(api.PATCH("/v1/run-views/{view_id}", { params: { path: { view_id: id } }, body, headers: ctx.headers }));
}

export function deleteRunView(id: string, ctx: Ctx) {
  return unwrapVoid(api.DELETE("/v1/run-views/{view_id}", { params: { path: { view_id: id } }, headers: ctx.headers }));
}

// ------------------------------------------------------------------------- keys and compare

export function fetchRunKeys(q: { experimentId?: string | null | undefined; prefix?: string | undefined }, ctx: Ctx) {
  return unwrap(
    api.GET("/v1/run-keys", {
      params: { query: { ...(q.experimentId ? { experiment_id: q.experimentId } : {}), ...(q.prefix ? { prefix: q.prefix } : {}) } },
      headers: ctx.headers,
      signal: ctx.signal,
    }),
  );
}

export interface CompareQuery {
  runs: string[];
  keys?: string[] | undefined;
  mediaKeys?: string[] | undefined;
  maxPoints?: number | undefined;
  params?: boolean | undefined;
}

export function fetchRunCompare(q: CompareQuery, ctx: Ctx) {
  const keys = q.keys?.slice(0, MAX_COMPARE_KEYS) ?? [];
  const media = q.mediaKeys?.slice(0, MAX_COMPARE_MEDIA_KEYS) ?? [];
  return unwrap(
    api.GET("/v1/run-compare", {
      params: {
        query: {
          run: q.runs,
          ...(keys.length ? { key: keys } : {}),
          ...(media.length ? { media_key: media } : {}),
          max_points: q.maxPoints ?? COMPARE_POINTS,
          ...(q.params === false ? { params: false } : {}),
        },
      },
      headers: ctx.headers,
      signal: ctx.signal,
    }),
  );
}
