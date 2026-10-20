/**
 * MSW handlers for v0.11 run tracking (`PATCH /v1/runs/{id}`, metrics, params) over the
 * fixtures of `fixtures.ts`, plus the live `tracking` frames of the running fit's SSE stream.
 * Semantics follow the I-3 contract closely enough for the run page; it is not a server.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type {
  MetricHistory,
  MetricHistorySeries,
  MetricPointIn,
  MetricSeries,
  RunPatch,
  TrackingFrame,
} from "@/api/tracking";
import { MAX_RUN_NAME_CHARS, MAX_TAGS_PER_RUN, METRIC_KEY_RE, RESERVED_TAG_PREFIX, TAG_KEY_RE } from "@/api/tracking";
import type { Experiment, Problem, Run } from "@/api/types";
import { env } from "@/env";

import { mockDivergences, mockStepSize, trackingFixtures, type MockSeries, type MockTracking } from "./fixtures";

const API = env.apiBase;

export type TrackingState = Map<string, MockTracking>;

export function createTrackingState(): TrackingState {
  return trackingFixtures();
}

function problem(status: number, code: Problem["code"], title: string, detail?: string) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: [] } satisfies Problem,
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

const labelsKey = (labels: Record<string, string>) =>
  Object.entries(labels)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${v}`)
    .join(",");

export function summary(s: MockSeries): MetricSeries {
  const values = s.points.map((p) => p.value);
  const last = s.points.reduce((a, b) => (b.ts > a.ts || (b.ts === a.ts && (b.step ?? -1) >= (a.step ?? -1)) ? b : a));
  return {
    key: s.key,
    labels: s.labels,
    attempt: s.attempt,
    count: s.points.length,
    last: last.value,
    last_step: last.step,
    min: Math.min(...values),
    max: Math.max(...values),
    first_ts: s.points.reduce((a, p) => (p.ts < a ? p.ts : a), s.points[0]!.ts),
    last_ts: last.ts,
  };
}

/** Evenly spaced indices (first and last kept): the mock's stand-in for the server's LTTB. */
function thin(n: number, max: number): number[] {
  if (n <= max) return Array.from({ length: n }, (_, i) => i);
  return Array.from({ length: max }, (_, i) => Math.round((i * (n - 1)) / (max - 1)));
}

function history(runId: string, key: string, series: MockSeries[], url: URL): MetricHistory {
  const maxPoints = Math.min(10_000, Math.max(10, Number(url.searchParams.get("max_points") ?? 1000) || 1000));
  const want = url.searchParams.getAll("label").map((l) => l.split(":", 2) as [string, string]);
  const matching = series.filter((s) => s.key === key && want.every(([k, v]) => s.labels[k] === v)).sort((a, b) => labelsKey(a.labels).localeCompare(labelsKey(b.labels)));
  return {
    run_id: runId,
    key,
    truncated: matching.length > 64,
    series: matching.slice(0, 64).map((s): MetricHistorySeries => {
      const idx = thin(s.points.length, maxPoints);
      return {
        labels: s.labels,
        attempt: s.attempt,
        count: s.points.length,
        downsampled: idx.length < s.points.length,
        steps: idx.map((i) => s.points[i]!.step),
        values: idx.map((i) => s.points[i]!.value),
        ts: idx.map((i) => s.points[i]!.ts),
      };
    }),
  };
}

function addPoint(t: MockTracking, p: MetricPointIn, ts: string): boolean {
  const labels = p.labels ?? {};
  let s = t.series.find((x) => x.key === p.key && labelsKey(x.labels) === labelsKey(labels));
  if (!s) {
    s = { key: p.key, labels, attempt: 1, points: [] };
    t.series.push(s);
  }
  const step = p.step ?? null;
  const same = s.points.find((x) => x.step === step);
  if (same && step !== null) return false; // stepped points: the first one wins
  if (same) Object.assign(same, { value: p.value, ts }); // scalars: the newest wins
  else s.points.push({ step, value: p.value, ts });
  s.points.sort((a, b) => (a.step ?? -1) - (b.step ?? -1));
  return true;
}

/**
 * The next live `tracking` frame of a running fit: 25 more draws for every chain's
 * `step_size` and `divergences` series. `null` when the run has no tracking data.
 */
export function advanceTracking(state: TrackingState, runId: string): TrackingFrame | null {
  const t = state.get(runId);
  if (!t) return null;
  const changed: MockSeries[] = [];
  const ts = new Date().toISOString();
  for (const s of t.series) {
    if (s.key !== "step_size" && s.key !== "divergences") continue;
    const chain = Number(s.labels.chain ?? 0);
    const step = (s.points.at(-1)?.step ?? 0) + 25;
    if (step > 1300) continue; // 300 tuning draws + 1000 draws
    s.points.push({ step, value: s.key === "step_size" ? mockStepSize(chain, step) : mockDivergences(chain, step), ts });
    changed.push(s);
  }
  return changed.length ? { run_id: runId, series: changed.map(summary), params: false, tags: false, media: false } : null;
}

export function createTrackingHandlers(state: TrackingState, ctx: { runs: Map<string, Run>; experiments?: Map<string, Experiment> }): HttpHandler[] {
  const tracking = (id: string): MockTracking => {
    let t = state.get(id);
    if (!t) {
      t = { series: [], params: [] };
      state.set(id, t);
    }
    return t;
  };
  return [
    http.patch(`${API}/v1/runs/:id`, async ({ params, request }) => {
      const run = ctx.runs.get(String(params.id));
      if (!run) return problem(404, "NOT_FOUND", "Not found", "no such run");
      const body = (await request.json()) as RunPatch;
      if (!body || typeof body !== "object" || !["name", "note", "tags", "experiment_id"].some((k) => k in body)) {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "set at least one of name, note, tags and experiment_id");
      }
      const next: Run = { ...run, tags: { ...(run.tags ?? {}) }, updated_at: new Date().toISOString() };
      if ("name" in body) {
        const name = body.name?.trim() || null;
        if (name && name.length > MAX_RUN_NAME_CHARS) return problem(422, "SCHEMA_INVALID", "Invalid request", "name is too long");
        next.name = name;
      }
      if ("note" in body) next.note = body.note || null;
      // v0.14: move the run to an experiment (null: out of its experiment)
      if ("experiment_id" in body) {
        const exp = body.experiment_id ? ctx.experiments?.get(body.experiment_id) : null;
        if (body.experiment_id && !exp) return problem(422, "SCHEMA_INVALID", "Invalid request", "no such experiment");
        if (exp?.archived) return problem(409, "CONFLICT", "Conflict", "the experiment is archived");
        next.experiment_id = body.experiment_id ?? null;
      }
      for (const [k, v] of Object.entries(body.tags ?? {})) {
        if (!TAG_KEY_RE.test(k) || k.startsWith(RESERVED_TAG_PREFIX)) return problem(422, "SCHEMA_INVALID", "Invalid request", `tag key '${k}' is not allowed`);
        if (v === null) delete next.tags![k];
        else next.tags![k] = v;
      }
      if (Object.keys(next.tags!).length > MAX_TAGS_PER_RUN) return problem(422, "SCHEMA_INVALID", "Invalid request", "too many tags");
      ctx.runs.set(next.id, next);
      return HttpResponse.json(next);
    }),
    http.get(`${API}/v1/runs/:id/metrics`, ({ params, request }) => {
      const id = String(params.id);
      if (!ctx.runs.has(id)) return problem(404, "NOT_FOUND", "Not found", "no such run");
      const key = new URL(request.url).searchParams.get("key");
      const items = (state.get(id)?.series ?? [])
        .filter((s) => s.points.length && (!key || s.key === key))
        .sort((a, b) => a.key.localeCompare(b.key) || labelsKey(a.labels).localeCompare(labelsKey(b.labels)))
        .map(summary);
      return HttpResponse.json({ items, next_cursor: null });
    }),
    http.get(`${API}/v1/runs/:id/metrics/:key`, ({ params, request }) => {
      const id = String(params.id);
      const key = String(params.key);
      if (!ctx.runs.has(id)) return problem(404, "NOT_FOUND", "Not found", "no such run");
      if (!METRIC_KEY_RE.test(key)) return problem(422, "SCHEMA_INVALID", "Invalid request", "bad metric key");
      return HttpResponse.json(history(id, key, state.get(id)?.series ?? [], new URL(request.url)));
    }),
    http.post(`${API}/v1/runs/:id/metrics`, async ({ params, request }) => {
      const id = String(params.id);
      if (!ctx.runs.has(id)) return problem(404, "NOT_FOUND", "Not found", "no such run");
      const body = (await request.json()) as { metrics?: MetricPointIn[] };
      const points = body.metrics ?? [];
      if (!points.length || points.some((p) => !METRIC_KEY_RE.test(p.key) || !Number.isFinite(p.value))) {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "bad metric points");
      }
      const t = tracking(id);
      const now = new Date().toISOString();
      const accepted = points.filter((p) => addPoint(t, p, p.ts ?? now)).length;
      return HttpResponse.json({ accepted, duplicates: points.length - accepted });
    }),
    http.get(`${API}/v1/runs/:id/params`, ({ params, request }) => {
      const id = String(params.id);
      if (!ctx.runs.has(id)) return problem(404, "NOT_FOUND", "Not found", "no such run");
      const prefix = new URL(request.url).searchParams.get("prefix") ?? "";
      const items = (state.get(id)?.params ?? []).filter((p) => p.key.startsWith(prefix)).sort((a, b) => a.key.localeCompare(b.key, "en", { numeric: true }));
      return HttpResponse.json({ items, next_cursor: null });
    }),
    http.post(`${API}/v1/runs/:id/params`, async ({ params, request }) => {
      const id = String(params.id);
      if (!ctx.runs.has(id)) return problem(404, "NOT_FOUND", "Not found", "no such run");
      const body = (await request.json()) as { params?: Record<string, string | number | boolean> };
      const entries = Object.entries(body.params ?? {});
      if (!entries.length) return problem(422, "SCHEMA_INVALID", "Invalid request", "no params");
      const t = tracking(id);
      const text = (v: string | number | boolean) => String(v);
      const conflicts = entries.filter(([k, v]) => t.params.some((p) => p.key === k && p.value !== text(v))).map(([k]) => k);
      if (conflicts.length) return problem(409, "CONFLICT", "Conflict", `params already logged with other values: ${conflicts.slice(0, 10).join(", ")}`);
      let accepted = 0;
      for (const [k, v] of entries) {
        if (t.params.some((p) => p.key === k)) continue;
        t.params.push({ key: k, value: text(v), value_num: typeof v === "number" ? v : null, source: "api", logged_at: new Date().toISOString() });
        accepted += 1;
      }
      return HttpResponse.json({ accepted, unchanged: entries.length - accepted });
    }),
  ];
}
