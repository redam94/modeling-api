/**
 * MSW handlers for v0.14 run organisation and comparison: `GET /v1/runs` with the run-search
 * grammar (filters, order, tracked columns, keyset-like pages), experiments, saved run views,
 * `GET /v1/run-keys` and `GET /v1/run-compare`, over the runs, tracking and media of the mock.
 * The grammar is the app's TS port (`run-query.ts`): the mock answers 422 with its message, like
 * the server. Semantics follow I-3 closely enough for the pages; it is not a server.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import { MAX_COMPARE_KEYS, MAX_COMPARE_MEDIA_ITEMS, MAX_COMPARE_RUNS, MAX_COMPARE_SERIES, MAX_RUN_VIEWS } from "@/api/experiments";
import type { CompareMetric, CompareParam, CompareSummary, Experiment, Problem, Run, RunKeyCount, RunView, RunViewQuery } from "@/api/types";
import { env } from "@/env";
import { grammarError, labelsKey, parseRunFilter, parseRunOrder, parseRunRef, refText, RunQueryError, type RunFilter, type RunRef } from "@/features/runs/run-query";

import { experiments as experimentFixtures, runViews as viewFixtures } from "./fixtures";
import { compareMedia, type MediaState } from "./media";
import { summary, type TrackingState } from "./tracking";

const API = env.apiBase;

function problem(status: number, code: Problem["code"], title: string, detail?: string, pointer?: string) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: pointer ? [{ pointer, message: detail ?? title }] : [] } as Problem,
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

export interface ExperimentsState {
  experiments: Map<string, Experiment>;
  views: Map<string, RunView>;
}

export function createExperimentsState(): ExperimentsState {
  return {
    experiments: new Map(experimentFixtures.map((e) => [e.id, structuredClone(e)])),
    views: new Map(viewFixtures.map((v) => [v.id, structuredClone(v)])),
  };
}

interface Ctx {
  runs: Map<string, Run>;
  tracking: TrackingState;
  media: MediaState;
}

const nameKey = (name: string) => name.trim().toLowerCase();
let seq = 0;
const newId = (prefix: string) => `${prefix}_01jaf4${String(Date.now()).slice(-8)}${String(++seq).padStart(6, "0")}`;

// ------------------------------------------------------------------------- run search

/** The value of a ref for a run (`null`: the run has none). */
export function refValue(ctx: Ctx, run: Run, ref: RunRef): number | string | null {
  if (ref.kind === "name") return run.name ?? null;
  if (ref.kind === "created_at") return run.created_at;
  if (ref.kind === "tag") return run.tags?.[ref.key!] ?? null;
  const t = ctx.tracking.get(run.id);
  if (ref.kind === "param") {
    const p = t?.params.find((x) => x.key === ref.key);
    return p ? p.value : null;
  }
  const s = t?.series.find((x) => x.key === ref.key && labelsKey(x.labels) === labelsKey(ref.labels) && x.points.length);
  if (!s) return null;
  const m = summary(s);
  return ref.agg === "count" ? m.count : ref.agg === "min" ? m.min : ref.agg === "max" ? m.max : m.last;
}

function paramNumber(ctx: Ctx, run: Run, key: string): number | null {
  return ctx.tracking.get(run.id)?.params.find((x) => x.key === key)?.value_num ?? null;
}

function matchesFilter(ctx: Ctx, run: Run, f: RunFilter): boolean {
  const numeric = typeof f.value === "number";
  const v = numeric && f.ref.kind === "param" ? paramNumber(ctx, run, f.ref.key!) : refValue(ctx, run, f.ref);
  if (v === null) return f.op === "!=" && !numeric;
  if (numeric) {
    const n = Number(v);
    const x = f.value as number;
    return f.op === "<" ? n < x : f.op === "<=" ? n <= x : f.op === ">" ? n > x : f.op === ">=" ? n >= x : f.op === "=" ? n === x : n !== x;
  }
  const s = String(v);
  const x = String(f.value);
  return f.op === "~" ? s.toLowerCase().includes(x.toLowerCase()) : f.op === "=" ? s === x : s !== x;
}

/** `GET /v1/runs` of the mock: the filters, then the order (runs without the value last), then a page. */
export function listRuns(ctx: Ctx, url: URL): Response {
  const q = url.searchParams;
  let filters: RunFilter[];
  let order: ReturnType<typeof parseRunOrder> | null;
  let columns: RunRef[];
  try {
    filters = q.getAll("filter").map(parseRunFilter);
    order = q.get("order_by") ? parseRunOrder(q.get("order_by")!) : null;
    columns = q.getAll("column").map(parseRunRef);
  } catch (err) {
    if (err instanceof RunQueryError) return problem(422, "SCHEMA_INVALID", "Invalid request", err.message, "/query");
    throw err;
  }
  const state = q.get("state");
  const kind = q.get("kind");
  const experiment = q.get("experiment_id");
  const spec = q.get("spec_id");
  const parent = q.get("parent_run_id");
  const name = q.get("name")?.toLowerCase();
  const tags = q.getAll("tag").map((t) => t.split(":", 2) as [string, string | undefined]);
  let runs = [...ctx.runs.values()].filter(
    (r) =>
      (!state || r.state === state) &&
      (!kind || r.kind === kind) &&
      (!experiment || r.experiment_id === experiment) &&
      (!spec || r.spec_id === spec) &&
      (!parent || r.parent_run_id === parent) &&
      (!name || (r.name ?? "").toLowerCase().includes(name)) &&
      tags.every(([k, v]) => (v === undefined ? k in (r.tags ?? {}) : r.tags?.[k] === v)) &&
      filters.every((f) => matchesFilter(ctx, r, f)),
  );
  if (order) {
    const o = order;
    runs = runs
      .map((r) => ({ r, v: refValue(ctx, r, o.ref) }))
      .sort((a, b) => {
        if (a.v === null || b.v === null) return a.v === null ? (b.v === null ? 0 : 1) : -1;
        const c = typeof a.v === "number" && typeof b.v === "number" ? a.v - b.v : String(a.v).localeCompare(String(b.v));
        return o.descending ? -c : c;
      })
      .map((x) => x.r);
  } else {
    runs.sort((a, b) => b.created_at.localeCompare(a.created_at));
  }
  const limit = Math.min(200, Math.max(1, Number(q.get("limit") ?? 50) || 50));
  const offset = Number(/^o:(\d+)$/.exec(q.get("cursor") ?? "")?.[1] ?? 0);
  const page = runs.slice(offset, offset + limit).map((r) => (columns.length ? { ...r, columns: Object.fromEntries(columns.map((c) => [refText(c), refValue(ctx, r, c)])) } : r));
  return HttpResponse.json({ items: page, next_cursor: offset + limit < runs.length ? `o:${offset + limit}` : null });
}

// ------------------------------------------------------------------------- experiments, views, keys

function experimentView(ctx: Ctx, e: Experiment): Experiment {
  const runs = [...ctx.runs.values()].filter((r) => r.experiment_id === e.id);
  return { ...e, run_count: runs.length, last_run_at: runs.map((r) => r.created_at).sort().at(-1) ?? null };
}

function checkViewQuery(query: RunViewQuery | undefined): Response | null {
  if (!query || typeof query !== "object") return problem(422, "SCHEMA_INVALID", "Invalid request", "query is required", "/query");
  for (const [i, f] of (query.filters ?? []).entries()) {
    const err = grammarError(parseRunFilter, f);
    if (err) return problem(422, "SCHEMA_INVALID", "Invalid request", err, `/query/filters/${i}`);
  }
  for (const [i, c] of (query.columns ?? []).entries()) {
    const err = grammarError(parseRunRef, c);
    if (err) return problem(422, "SCHEMA_INVALID", "Invalid request", err, `/query/columns/${i}`);
  }
  const err = query.order_by ? grammarError(parseRunOrder, query.order_by) : null;
  return err ? problem(422, "SCHEMA_INVALID", "Invalid request", err, "/query/order_by") : null;
}

function runKeys(ctx: Ctx, experimentId: string | null, prefix: string) {
  const runs = [...ctx.runs.values()].filter((r) => !experimentId || r.experiment_id === experimentId);
  const metrics = new Map<string, { key: string; labels: Record<string, string>; runs: Set<string> }>();
  const params = new Map<string, Set<string>>();
  const tags = new Map<string, Set<string>>();
  for (const r of runs) {
    const t = ctx.tracking.get(r.id);
    for (const s of t?.series ?? []) {
      if (!s.points.length || !s.key.startsWith(prefix)) continue;
      const id = `${s.key}|${labelsKey(s.labels)}`;
      const e = metrics.get(id) ?? { key: s.key, labels: s.labels, runs: new Set<string>() };
      e.runs.add(r.id);
      metrics.set(id, e);
    }
    for (const p of t?.params ?? []) if (p.key.startsWith(prefix)) params.set(p.key, (params.get(p.key) ?? new Set()).add(r.id));
    for (const k of Object.keys(r.tags ?? {})) if (k.startsWith(prefix)) tags.set(k, (tags.get(k) ?? new Set()).add(r.id));
  }
  const byCount = (a: RunKeyCount, b: RunKeyCount) => b.runs - a.runs || a.key.localeCompare(b.key) || labelsKey(a.labels ?? {}).localeCompare(labelsKey(b.labels ?? {}));
  return {
    metrics: [...metrics.values()].map((m) => ({ key: m.key, labels: m.labels, runs: m.runs.size })).sort(byCount),
    params: [...params.entries()].map(([key, set]) => ({ key, labels: {}, runs: set.size })).sort(byCount),
    tags: [...tags.entries()].map(([key, set]) => ({ key, labels: {}, runs: set.size })).sort(byCount),
    truncated: false,
  };
}

/** Evenly spaced indices (first and last kept), the mock's stand-in for LTTB. */
function thin(n: number, max: number): number[] {
  if (n <= max) return Array.from({ length: n }, (_, i) => i);
  return Array.from({ length: max }, (_, i) => Math.round((i * (n - 1)) / (max - 1)));
}

function compare(ctx: Ctx, url: URL): Response {
  const q = url.searchParams;
  const ids = q.getAll("run");
  if (ids.length < 2 || ids.length > MAX_COMPARE_RUNS || new Set(ids).size !== ids.length) {
    return problem(422, "SCHEMA_INVALID", "Invalid request", `compare 2 to ${MAX_COMPARE_RUNS} different runs`, "/query/run");
  }
  const runs = ids.map((id) => ctx.runs.get(id));
  if (runs.some((r) => !r)) return problem(404, "NOT_FOUND", "Not found", "no such run");
  const maxPoints = Math.min(2000, Math.max(10, Number(q.get("max_points") ?? 300) || 300));
  const summaries: CompareSummary[] = ids.flatMap((id) => (ctx.tracking.get(id)?.series ?? []).filter((s) => s.points.length).map((s) => ({ ...summary(s), run_id: id })));
  let keys = q.getAll("key").slice(0, MAX_COMPARE_KEYS);
  if (!keys.length) {
    const count = new Map<string, Set<string>>();
    for (const s of summaries) count.set(s.key, (count.get(s.key) ?? new Set()).add(s.run_id));
    keys = [...count.entries()]
      .sort(([a, x], [b, y]) => y.size - x.size || a.localeCompare(b))
      .slice(0, MAX_COMPARE_KEYS)
      .map(([k]) => k);
  }
  const metrics: CompareMetric[] = keys.map((key) => ({
    key,
    series: ids.flatMap((id) =>
      (ctx.tracking.get(id)?.series ?? [])
        .filter((s) => s.key === key && s.points.length)
        .slice(0, MAX_COMPARE_SERIES)
        .map((s) => {
          const idx = thin(s.points.length, maxPoints);
          return { run_id: id, labels: s.labels, attempt: s.attempt, count: s.points.length, downsampled: idx.length < s.points.length, steps: idx.map((i) => s.points[i]!.step), values: idx.map((i) => s.points[i]!.value) };
        }),
    ),
  }));
  const paramKeys = [...new Set(ids.flatMap((id) => (ctx.tracking.get(id)?.params ?? []).map((p) => p.key)))].sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
  const params: CompareParam[] =
    q.get("params") === "false"
      ? []
      : paramKeys.map((key) => {
          const values = Object.fromEntries(ids.map((id) => [id, ctx.tracking.get(id)?.params.find((p) => p.key === key)?.value ?? null]));
          return { key, values, differs: new Set(Object.values(values)).size > 1 };
        });
  const mediaKeys = q.getAll("media_key").slice(0, 4);
  const media = mediaKeys.length ? ids.flatMap((id) => compareMedia(ctx.media, id, mediaKeys, MAX_COMPARE_MEDIA_ITEMS)) : [];
  return HttpResponse.json({
    runs,
    summaries,
    metrics,
    params,
    media: media.map(({ truncated: _t, ...m }) => m),
    truncated: { summaries: false, series: false, params: false, media: media.some((m) => m.truncated) },
  });
}

export function createExperimentHandlers(state: ExperimentsState, ctx: Ctx): HttpHandler[] {
  return [
    // experiments
    http.get(`${API}/v1/experiments`, ({ request }) => {
      const url = new URL(request.url);
      const name = url.searchParams.get("name")?.toLowerCase();
      const archived = url.searchParams.get("archived");
      const items = [...state.experiments.values()]
        .filter((e) => (!name || e.name.toLowerCase().includes(name)) && (archived === null || String(Boolean(e.archived)) === archived))
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .map((e) => experimentView(ctx, e));
      return HttpResponse.json({ items, next_cursor: null });
    }),
    http.post(`${API}/v1/experiments`, async ({ request }) => {
      const body = (await request.json()) as { name?: string; description?: string | null };
      const name = body.name?.trim();
      if (!name || name.length > 200) return problem(422, "SCHEMA_INVALID", "Invalid request", "name must have 1 to 200 characters", "/name");
      if ([...state.experiments.values()].some((e) => nameKey(e.name) === nameKey(name))) return problem(409, "CONFLICT", "Conflict", "an experiment with this name exists");
      const now = new Date().toISOString();
      const e: Experiment = { id: newId("exp"), name, description: body.description ?? null, archived: false, created_by: "usr_ana", created_at: now, updated_at: now };
      state.experiments.set(e.id, e);
      return HttpResponse.json(experimentView(ctx, e), { status: 201 });
    }),
    http.get(`${API}/v1/experiments/:id`, ({ params }) => {
      const e = state.experiments.get(String(params.id));
      return e ? HttpResponse.json(experimentView(ctx, e)) : problem(404, "NOT_FOUND", "Not found", "no such experiment");
    }),
    http.patch(`${API}/v1/experiments/:id`, async ({ params, request }) => {
      const e = state.experiments.get(String(params.id));
      if (!e) return problem(404, "NOT_FOUND", "Not found", "no such experiment");
      const body = (await request.json()) as { name?: string; description?: string | null; archived?: boolean };
      if (!["name", "description", "archived"].some((k) => k in body)) return problem(422, "SCHEMA_INVALID", "Invalid request", "set at least one field");
      const next: Experiment = { ...e, updated_at: new Date().toISOString() };
      if ("name" in body) {
        const name = body.name?.trim();
        if (!name) return problem(422, "SCHEMA_INVALID", "Invalid request", "name must have 1 to 200 characters", "/name");
        if ([...state.experiments.values()].some((x) => x.id !== e.id && nameKey(x.name) === nameKey(name))) return problem(409, "CONFLICT", "Conflict", "an experiment with this name exists");
        next.name = name;
      }
      if ("description" in body) next.description = body.description ?? null;
      if ("archived" in body) next.archived = Boolean(body.archived);
      state.experiments.set(e.id, next);
      return HttpResponse.json(experimentView(ctx, next));
    }),
    http.delete(`${API}/v1/experiments/:id`, ({ params }) => {
      const id = String(params.id);
      if (!state.experiments.delete(id)) return problem(404, "NOT_FOUND", "Not found", "no such experiment");
      for (const r of ctx.runs.values()) if (r.experiment_id === id) ctx.runs.set(r.id, { ...r, experiment_id: null });
      for (const v of state.views.values()) if (v.experiment_id === id) state.views.delete(v.id);
      return new HttpResponse(null, { status: 204 });
    }),
    http.post(`${API}/v1/experiments/:id/runs`, async ({ params, request }) => {
      const e = state.experiments.get(String(params.id));
      if (!e) return problem(404, "NOT_FOUND", "Not found", "no such experiment");
      const body = (await request.json()) as { add?: string[]; remove?: string[] };
      const add = body.add ?? [];
      const remove = body.remove ?? [];
      if (!add.length && !remove.length) return problem(422, "SCHEMA_INVALID", "Invalid request", "give at least one run id");
      if (add.length && e.archived) return problem(409, "CONFLICT", "Conflict", "the experiment is archived");
      const missing = [...add, ...remove].filter((id) => !ctx.runs.has(id));
      let added = 0;
      let removed = 0;
      for (const id of add) {
        const r = ctx.runs.get(id);
        if (r && r.experiment_id !== e.id) {
          ctx.runs.set(id, { ...r, experiment_id: e.id });
          added += 1;
        }
      }
      for (const id of remove) {
        const r = ctx.runs.get(id);
        if (r && r.experiment_id === e.id) {
          ctx.runs.set(id, { ...r, experiment_id: null });
          removed += 1;
        }
      }
      return HttpResponse.json({ added, removed, missing });
    }),

    // saved views
    http.get(`${API}/v1/run-views`, ({ request }) => {
      const exp = new URL(request.url).searchParams.get("experiment_id");
      const items = [...state.views.values()].filter((v) => !exp || v.experiment_id === exp).sort((a, b) => a.name.localeCompare(b.name));
      return HttpResponse.json({ items, next_cursor: null });
    }),
    http.post(`${API}/v1/run-views`, async ({ request }) => {
      const body = (await request.json()) as { name?: string; experiment_id?: string | null; query?: RunViewQuery };
      const name = body.name?.trim();
      if (!name) return problem(422, "SCHEMA_INVALID", "Invalid request", "name is required", "/name");
      const bad = checkViewQuery(body.query);
      if (bad) return bad;
      const exp = body.experiment_id ?? null;
      if ([...state.views.values()].some((v) => (v.experiment_id ?? null) === exp && nameKey(v.name) === nameKey(name))) return problem(409, "CONFLICT", "Conflict", "a view with this name exists");
      if (state.views.size >= MAX_RUN_VIEWS) return problem(429, "QUOTA_EXCEEDED", "Quota exceeded", `a project has ${MAX_RUN_VIEWS} views maximum`);
      const now = new Date().toISOString();
      const v: RunView = { id: newId("rv"), name, experiment_id: exp, query: body.query!, created_by: "usr_ana", created_at: now, updated_at: now };
      state.views.set(v.id, v);
      return HttpResponse.json(v, { status: 201 });
    }),
    http.patch(`${API}/v1/run-views/:id`, async ({ params, request }) => {
      const v = state.views.get(String(params.id));
      if (!v) return problem(404, "NOT_FOUND", "Not found", "no such view");
      const body = (await request.json()) as { name?: string; query?: RunViewQuery };
      if (!("name" in body) && !("query" in body)) return problem(422, "SCHEMA_INVALID", "Invalid request", "set name or query");
      const next: RunView = { ...v, updated_at: new Date().toISOString() };
      if (body.name !== undefined) {
        const name = body.name.trim();
        if ([...state.views.values()].some((x) => x.id !== v.id && (x.experiment_id ?? null) === (v.experiment_id ?? null) && nameKey(x.name) === nameKey(name))) {
          return problem(409, "CONFLICT", "Conflict", "a view with this name exists");
        }
        next.name = name;
      }
      if (body.query !== undefined) {
        const bad = checkViewQuery(body.query);
        if (bad) return bad;
        next.query = body.query;
      }
      state.views.set(v.id, next);
      return HttpResponse.json(next);
    }),
    http.delete(`${API}/v1/run-views/:id`, ({ params }) => (state.views.delete(String(params.id)) ? new HttpResponse(null, { status: 204 }) : problem(404, "NOT_FOUND", "Not found", "no such view"))),

    // tracked keys and comparison
    http.get(`${API}/v1/run-keys`, ({ request }) => {
      const url = new URL(request.url);
      return HttpResponse.json(runKeys(ctx, url.searchParams.get("experiment_id"), url.searchParams.get("prefix") ?? ""));
    }),
    http.get(`${API}/v1/run-compare`, ({ request }) => compare(ctx, new URL(request.url))),
  ];
}
