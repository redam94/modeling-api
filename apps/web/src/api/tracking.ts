/**
 * v0.11 run tracking: metrics, params, tags, notes and structured (user) log lines.
 *
 * The DTOs are the generated schema aliases of `./types` (re-exported here); the SSE
 * `tracking` frame is not an OpenAPI component, so its type is written here from
 * `mapi_spec.api.TrackingFrame`. The fetchers page through the list routes.
 */
import { api, unwrap } from "./client";
import type { LogLine, MetricHistory, MetricSeries, Page, Run, RunParam, RunPatch } from "./types";

export type {
  MetricHistory,
  MetricHistorySeries,
  MetricPointIn,
  MetricSeries,
  MetricsWrite,
  MetricsWriteResult,
  ParamsWrite,
  ParamsWriteResult,
  RunParam,
  RunPatch,
} from "./types";

// ------------------------------------------------------------------------- contract (I-1, I-3)

export type LogLevel = NonNullable<LogLine["level"]>;
/** Ascending severity (`mapi_spec.tracking.LOG_LEVELS`). */
export const LOG_LEVELS: readonly LogLevel[] = ["debug", "info", "warning", "error"];
export type LogStream = LogLine["stream"];
export type LogFieldValue = string | number | boolean | null;

export const METRIC_KEY_RE = /^[a-z][a-z0-9_]{0,63}$/;
export const TAG_KEY_RE = /^[A-Za-z0-9_][A-Za-z0-9_.\-/]{0,127}$/;
/** Platform-only tag keys: the API refuses writes to them. */
export const RESERVED_TAG_PREFIX = "mapi.";
export const MAX_RUN_NAME_CHARS = 200;
export const MAX_RUN_NOTE_CHARS = 20_000;
export const MAX_TAG_VALUE_CHARS = 1000;
export const MAX_TAGS_PER_RUN = 100;

/** Data of the SSE `tracking` frame: summaries that changed, and whether params/tags did. */
export interface TrackingFrame {
  run_id: string;
  series: MetricSeries[];
  params: boolean;
  tags: boolean;
}

/** The run's name, `null` when it has none (or only spaces). */
export function runName(run: Run): string | null {
  return run.name?.trim() ? run.name : null;
}

export function runNote(run: Run): string | null {
  return run.note || null;
}

export function runTags(run: Run): Record<string, string> {
  return run.tags ?? {};
}

// ------------------------------------------------------------------------- fetchers

/** Pages of `GET /v1/runs/{id}/metrics` read per load (200 series each). */
export const MAX_METRIC_PAGES = 10;
/** Pages of `GET /v1/runs/{id}/params` read per load (200 params each). */
export const MAX_PARAM_PAGES = 25;

async function allPages<T>(fetchPage: (cursor: string | null) => Promise<Page<T>>, maxPages: number): Promise<{ items: T[]; more: boolean }> {
  const items: T[] = [];
  let cursor: string | null = null;
  for (let i = 0; i < maxPages; i++) {
    const page: Page<T> = await fetchPage(cursor);
    items.push(...page.items);
    cursor = page.next_cursor ?? null;
    if (!cursor) return { items, more: false };
  }
  return { items, more: true };
}

interface Ctx {
  headers: Record<string, string>;
  signal?: AbortSignal | undefined;
}

export function fetchRunMetrics(runId: string, ctx: Ctx) {
  return allPages<MetricSeries>(
    (cursor) =>
      unwrap(
        api.GET("/v1/runs/{run_id}/metrics", {
          params: { path: { run_id: runId }, query: { limit: 200, ...(cursor ? { cursor } : {}) } },
          headers: ctx.headers,
          signal: ctx.signal,
        }),
      ),
    MAX_METRIC_PAGES,
  );
}

export interface MetricHistoryQuery {
  maxPoints?: number | undefined;
  attempt?: number | undefined;
}

export function fetchMetricHistory(runId: string, key: string, q: MetricHistoryQuery, ctx: Ctx) {
  return unwrap<MetricHistory>(
    api.GET("/v1/runs/{run_id}/metrics/{key}", {
      params: {
        path: { run_id: runId, key },
        query: { ...(q.maxPoints ? { max_points: q.maxPoints } : {}), ...(q.attempt ? { attempt: q.attempt } : {}) },
      },
      headers: ctx.headers,
      signal: ctx.signal,
    }),
  );
}

export function fetchRunParams(runId: string, ctx: Ctx) {
  return allPages<RunParam>(
    (cursor) =>
      unwrap(
        api.GET("/v1/runs/{run_id}/params", {
          params: { path: { run_id: runId }, query: { limit: 200, ...(cursor ? { cursor } : {}) } },
          headers: ctx.headers,
          signal: ctx.signal,
        }),
      ),
    MAX_PARAM_PAGES,
  );
}

export function patchRun(runId: string, body: RunPatch, ctx: Ctx) {
  return unwrap(
    api.PATCH("/v1/runs/{run_id}", { params: { path: { run_id: runId } }, body, headers: ctx.headers, signal: ctx.signal }),
  );
}

/** Validate the JSON of an SSE `tracking` frame (unknown fields are ignored). */
export function parseTrackingFrame(v: unknown): TrackingFrame | null {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return null;
  const f = v as Record<string, unknown>;
  if (typeof f.run_id !== "string") return null;
  const series = Array.isArray(f.series)
    ? f.series.filter(
        (s): s is MetricSeries =>
          typeof s === "object" && s !== null && typeof (s as MetricSeries).key === "string" && typeof (s as MetricSeries).last === "number",
      )
    : [];
  return { run_id: f.run_id, series, params: f.params === true, tags: f.tags === true };
}
