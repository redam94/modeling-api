/**
 * Mock API of the M3.5 explorers, About cards, Help me choose, governance evidence and budget
 * optimisation, serving `explorer.gen.json`: views, plots, cards, rankings, check results and
 * a budget plan computed by the real platform code (`scripts/gen_explorer_fixtures.py`).
 *
 * - the dataset `eu_media_daily` (every version) explores a synthetic weekly geo panel;
 * - `eu-geo-mmm` (and any model this file does not know, e.g. Guided sessions' models) is the
 *   mmm-framework MMM; `sales-arimax` (v1, v2), `sales-ets` and `macro-vecm` are added to the
 *   model list for forecasts, backtests, comparisons and impulse responses; `demo-fields`
 *   serves the `fields` view of `fields.gen.json` (synthetic posterior draws on grids);
 * - explore and optimize runs finish {@link MOCK_RUN_MS} after they start.
 */
import { delay, http, HttpResponse, type HttpHandler } from "msw";

import type { Model, ModelVersion, Problem, Run } from "@/api/types";
import { env } from "@/env";
import type { CatalogAbout, ChooserQuestionnaire } from "@/features/about/types";
import type { Exploration, ModelValidation } from "@/features/explore/api";
import type { Optimization, OptimizeRequest, OptimizeResult } from "@/features/optimize/api";
import { crc32cBase64 } from "@/lib/crc32c";

import { answersFromQuery, ChooserAnswerError, recommend } from "./chooser";
import gen from "./explorer.gen.json";
import fieldsGen from "./fields.gen.json";
import * as fx from "./fixtures";

const API = env.apiBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const STORE = "http://127.0.0.1:4443";
const enc = new TextEncoder();

/** How long a mock explore or optimize run takes. */
export const MOCK_RUN_MS = 1200;

// ------------------------------------------------------------------------------ fixture

type Json = Record<string, unknown>;
interface SectionRef {
  section: string;
  view: string;
  status?: "ok" | "skipped" | "error";
  message_plain?: string;
  plots?: string;
}
interface Part {
  index: { format: string; target: "dataset" | "model"; ref: string; sections: SectionRef[] };
  views: Record<string, Json>;
  plots: Record<string, Json[]>;
  request?: Json;
}
interface ModelFixture {
  ref: string;
  fit: Part;
  explore: Part;
  governance?: Json[];
}
interface Fixture {
  abouts: CatalogAbout[];
  chooser: ChooserQuestionnaire;
  chooser_cases: { kind: string | null; limit: number; query: Record<string, string[]>; result: Json }[];
  dataset: { ref: string; about: Json; ingest: Part; explore: Part };
  models: Record<string, ModelFixture>;
  optimize: { model: string; request: Json; result: OptimizeResult };
}

export const FIXTURE = gen as unknown as Fixture;
/** The demo model of the field viewer (only the `fields` section). */
export const FIELDS_FIXTURE = fieldsGen as unknown as ModelFixture;

const MMM = "model://default/eu-geo-mmm@v1";
const ARIMAX = "model://default/sales-arimax";
const ETS = "model://default/sales-ets@v1";
const VECM = "model://default/macro-vecm@v1";

export const EXTRA_MODEL_IDS = {
  arimax: "mdl_01jaf3m35arimax000000001",
  ets: "mdl_01jaf3m35ets00000000001",
  vecm: "mdl_01jaf3m35vecm0000000001",
  /** The ARIMAX the canonical workflow run fitted (`mocks/workflows.ts`), explored like sales-arimax v1. */
  workflowFit: "mdl_01jaf3m35wffit000000001",
  /** A model whose outputs lie on grids (`fields.gen.json`): the field viewer. */
  fields: "mdl_01jaf3m35fields00000001",
} as const;

interface ExtraModel {
  model: Model;
  versions: ModelVersion[];
}

function extraModel(id: string, name: string, versions: { v: number; plugin: string; inference: NonNullable<ModelVersion["capabilities"]>["inference"]; metrics: Record<string, number> }[]): ExtraModel {
  const model: Model = { ...fx.model, id, name, ref: `model://default/${name}`, latest_version: versions.length, created_at: fx.T0, updated_at: fx.T0 };
  return {
    model,
    versions: versions.map(({ v, plugin, inference, metrics }) => ({
      ...fx.modelVersion,
      id: `mv_${id.slice(4, 20)}${v}`,
      model_id: id,
      version: v,
      ref: `model://default/${name}@v${v}`,
      metrics,
      diagnostics: [],
      scorecard: null,
      capabilities: { inference, representation: "quantiles", score_modes: ["predict", "forecast", "evaluate"], decision: false },
      lineage: { ...fx.modelVersion.lineage, plugin, base_image: "base-forecast@2026.10", engine: null },
    })),
  };
}

export const EXTRA_MODELS: ExtraModel[] = [
  extraModel(EXTRA_MODEL_IDS.arimax, "sales-arimax", [
    { v: 1, plugin: "ts.sarimax@1.0.0", inference: "kalman", metrics: { mape: 10.37, rmse: 2.62 } },
    { v: 2, plugin: "ts.sarimax@1.0.0", inference: "kalman", metrics: { mape: 10.11, rmse: 2.5 } },
  ]),
  extraModel(EXTRA_MODEL_IDS.ets, "sales-ets", [{ v: 1, plugin: "ts.ets@1.0.0", inference: "kalman", metrics: { mape: 22.75, rmse: 7.75 } }]),
  extraModel(EXTRA_MODEL_IDS.vecm, "macro-vecm", [{ v: 1, plugin: "ts.vecm@1.0.0", inference: "point", metrics: {} }]),
  extraModel(EXTRA_MODEL_IDS.workflowFit, "wf-weekly-readiness-fit", [{ v: 1, plugin: "ts.sarimax@1.0.0", inference: "kalman", metrics: { mape: 10.37, rmse: 2.62 } }]),
  extraModel(EXTRA_MODEL_IDS.fields, "demo-fields", [{ v: 1, plugin: "sci.lgcp_grid@1.0.0", inference: "mcmc", metrics: {} }]),
];

/** The fixture of a model version (unknown models are the MMM). */
function modelFixture(modelId: string, version: number): ModelFixture {
  if (modelId === EXTRA_MODEL_IDS.arimax) return FIXTURE.models[`${ARIMAX}@v${version === 2 ? 2 : 1}`]!;
  if (modelId === EXTRA_MODEL_IDS.workflowFit) return FIXTURE.models[`${ARIMAX}@v1`]!;
  if (modelId === EXTRA_MODEL_IDS.ets) return FIXTURE.models[ETS]!;
  if (modelId === EXTRA_MODEL_IDS.vecm) return FIXTURE.models[VECM]!;
  if (modelId === EXTRA_MODEL_IDS.fields) return FIELDS_FIXTURE;
  return FIXTURE.models[MMM]!;
}

/**
 * The validations the explorer fixtures computed (the real governance checks of the fitted
 * MMM and ARIMAX versions), seeded into the governance mock (`mocks/governance.ts`), which
 * serves every validation route.
 */
export function explorerValidations(): ModelValidation[] {
  return [
    [fx.MODEL_ID, 1],
    [EXTRA_MODEL_IDS.arimax, 1],
  ].flatMap(([mid, ver]) => {
    const validation = computedValidation(String(mid), Number(ver));
    return validation ? [validation] : [];
  });
}

function computedValidation(id: string, v: number): ModelValidation | null {
  const m = modelFixture(id, v);
  if (!m.governance || (id !== fx.MODEL_ID && !(id === EXTRA_MODEL_IDS.arimax && v === 1))) return null;
  const failing = (m.governance as { check: string; status: string }[]).filter((r) => r.status === "fail" || r.status === "error").map((r) => r.check);
  const status = failing.length ? "checks_failed" : "validated";
  // The MMM's computed validation is its first one: the governance mock's later validations
  // of that version (failed, then awaiting approval) are the story the governance screens tell.
  const submitted = id === fx.MODEL_ID ? "2026-10-12T09:00:00Z" : fx.T0;
  const done = id === fx.MODEL_ID ? "2026-10-12T09:10:00Z" : "2026-10-15T09:40:00Z";
  const validation: ModelValidation = {
    id: `val_${id.slice(4, 16)}${v}`,
    tenant_id: "default",
    project_id: "default",
    model_id: id,
    version: v,
    status,
    policies: [id === fx.MODEL_ID ? "pol_default_mmm@1" : "pol_default_forecast@1"],
    required_checks: (m.governance as { check: string }[]).map((r) => ({ check: r.check, severity: r.check === "residuals.whiteness" ? "warning" : "blocking" })),
    results: m.governance as ModelValidation["results"],
    failing_checks: failing,
    require_approval: false,
    approvals_required: 1,
    approvals: [],
    submitted_by: "usr_ana",
    submitted_at: submitted,
    updated_at: done,
    history: [
      { at: submitted, event: "submit", from_status: "draft", to_status: "checks_running", actor_id: "usr_ana" },
      { at: done, event: failing.length ? "checks_failed" : "checks_passed", from_status: "checks_running", to_status: failing.length ? "checks_failed" : "checks_passed", actor_id: null },
      ...(failing.length ? [] : [{ at: done, event: "validate", from_status: "checks_passed", to_status: "validated", actor_id: null }]),
    ] as ModelValidation["history"],
  };
  return validation;
}

// ------------------------------------------------------------------------------ state

export interface ExploreMockShared {
  runs: Map<string, Run>;
  artifacts: Map<string, { bytes: Uint8Array; type: string }>;
}

interface RunRec {
  kind: "explore" | "optimize";
  started: number;
  /** View name -> artifact key, once done. */
  views: Map<string, string>;
  index: Part["index"] | null;
  target: "dataset" | "model";
  ref: string;
  optimize?: { request: OptimizeRequest; result: OptimizeResult | null; problem: Problem | null };
}

export interface ExploreMockState {
  runs: Map<string, RunRec>;
  cache: Map<string, string>;
  descriptions: Map<string, string | null>;
  seq: number;
}

export function createExploreState(): ExploreMockState {
  return { runs: new Map(), cache: new Map(), descriptions: new Map(), seq: 0 };
}

function problem(status: number, code: string, title: string, detail?: string, pointer?: string) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: pointer ? [{ code, message: detail ?? title, pointer, severity: "error" }] : [] },
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

const page = <T,>(items: T[], next: string | null = null) => HttpResponse.json({ items, next_cursor: next });

function putJson(shared: ExploreMockShared, key: string, value: unknown): void {
  shared.artifacts.set(key, { bytes: enc.encode(JSON.stringify(value)), type: "application/json" });
}

/** A signed-link look-alike for an artifact stored under `key` (served as `name`). */
function linkFor(shared: ExploreMockShared, key: string, name: string) {
  const a = shared.artifacts.get(key);
  if (!a) return null;
  return {
    name,
    url: `${STORE}/mock-artifacts/${encodeURIComponent(key)}?X-Goog-Signature=mock`,
    method: "GET" as const,
    expires_at: "2099-01-01T00:00:00Z",
    size: a.bytes.length,
    content_type: a.type,
    crc32c: crc32cBase64(a.bytes),
    generation: 7,
  };
}

/** Register a part's index, views and plots under `prefix` (once). */
function registerPart(shared: ExploreMockShared, prefix: string, part: Part, fallback?: Part): Map<string, string> {
  const names = new Map<string, string>();
  const put = (name: string, value: unknown) => {
    const key = `${prefix}:${name}`;
    if (!shared.artifacts.has(key)) putJson(shared, key, value);
    names.set(name, key);
  };
  put("ui/explore/index.json", part.index);
  for (const ref of part.index.sections) {
    if ((ref.status ?? "ok") !== "ok") continue;
    const view = part.views[ref.section] ?? fallback?.views[ref.section];
    if (view) put(ref.view, view);
    const plots = part.plots[ref.section] ?? fallback?.plots[ref.section];
    if (ref.plots && plots) put(ref.plots, plots);
  }
  return names;
}

function runOf(id: string, rec: RunRec, done: boolean, failed: Problem | null): Run {
  return {
    ...fx.runDone,
    id,
    kind: rec.kind,
    state: done ? (failed ? "failed" : "succeeded") : "running",
    spec_id: null,
    spec_revision: null,
    output_model_version_id: null,
    problem: failed,
    progress: done ? null : { seq: 3, phase: "fit", fraction: Math.min(0.9, (Date.now() - rec.started) / MOCK_RUN_MS), message: rec.kind === "optimize" ? "optimising the budget over the posterior" : "computing explorer sections", updated_at: new Date().toISOString() },
    created_at: new Date(rec.started).toISOString(),
    started_at: new Date(rec.started).toISOString(),
    finished_at: done ? new Date(rec.started + MOCK_RUN_MS).toISOString() : null,
    updated_at: new Date().toISOString(),
  };
}

// ------------------------------------------------------------------------------ handlers

export function createExploreHandlers(shared: ExploreMockShared, state: ExploreMockState = createExploreState()): { early: HttpHandler[]; late: HttpHandler[] } {
  const ds = FIXTURE.dataset;
  const nextRun = (kind: string) => `run_01jaf3m35${kind.slice(0, 3)}${String(++state.seq).padStart(12, "0")}`;
  const isDone = (rec: RunRec) => Date.now() - rec.started >= MOCK_RUN_MS;
  const syncRun = (id: string, rec: RunRec) => {
    const run = runOf(id, rec, isDone(rec), rec.optimize?.problem ?? null);
    shared.runs.set(id, run);
    return run;
  };
  const exploration = (id: string, rec: RunRec): Exploration => {
    const run = syncRun(id, rec);
    const done = isDone(rec);
    return {
      run,
      done,
      target: rec.target,
      ref: rec.ref,
      index: done ? rec.index : null,
      views: done ? [...rec.views].map(([name, key]) => linkFor(shared, key, name)).filter((l) => l !== null) : [],
    } as Exploration;
  };
  const start = (key: string, make: () => RunRec): { id: string; rec: RunRec; cached: boolean } => {
    const prior = state.cache.get(key);
    if (prior && state.runs.has(prior)) return { id: prior, rec: state.runs.get(prior)!, cached: true };
    const rec = make();
    const id = nextRun(rec.kind);
    state.runs.set(id, rec);
    state.cache.set(key, id);
    return { id, rec, cached: false };
  };
  /** An explore run of `sections` from a fixture part (`fallback` serves sections it lacks). */
  const exploreRec = (target: "dataset" | "model", ref: string, part: Part, sections: string[], prefix: string, fallback?: Part, extra?: Part): RunRec => {
    const refs: SectionRef[] = sections.map((s) => {
      const fromPart = part.index.sections.find((x) => x.section === s);
      const fromExtra = extra?.index.sections.find((x) => x.section === s);
      return (
        (fromPart && (fromPart.status ?? "ok") === "ok" ? fromPart : undefined) ??
        fromExtra ??
        fromPart ?? { section: s, view: `ui/explore/${s}.json`, status: "skipped", message_plain: "Not available: this mock has no such view for this model." }
      );
    });
    const index = { ...part.index, sections: refs };
    const merged: Part = { index, views: { ...(extra?.views ?? {}), ...(fallback?.views ?? {}), ...part.views }, plots: { ...(extra?.plots ?? {}), ...(fallback?.plots ?? {}), ...part.plots } };
    const views = registerPart(shared, prefix, merged);
    return { kind: "explore", started: Date.now(), views, index, target, ref };
  };

  const early: HttpHandler[] = [
    // About cards and Help me choose
    http.get(`${API}/v1/catalog/abouts`, ({ request }) => {
      const url = new URL(request.url);
      const kind = url.searchParams.get("kind");
      const limit = Math.min(200, Number(url.searchParams.get("limit") ?? 50) || 50);
      const offset = Number(url.searchParams.get("cursor") ?? 0) || 0;
      const items = FIXTURE.abouts.filter((a) => !kind || a.kind === kind);
      const chunk = items.slice(offset, offset + limit);
      return page(chunk, offset + limit < items.length ? String(offset + limit) : null);
    }),
    http.get(`${API}/v1/catalog/chooser`, () => HttpResponse.json(FIXTURE.chooser)),
    http.get(`${API}/v1/catalog/recommendations`, ({ request }) => {
      const url = new URL(request.url);
      const kind = url.searchParams.get("kind");
      const limit = Math.max(1, Math.min(50, Number(url.searchParams.get("limit") ?? 10) || 10));
      try {
        const answers = answersFromQuery(url.searchParams, FIXTURE.chooser.questions);
        const starting = new Set(["model", "template", "workflow_template"]);
        const blocks = new Set(["step", "node", "check"]);
        const candidates = FIXTURE.abouts.filter((a) => (kind ? a.kind === kind : starting.has(a.kind)));
        const fallback = kind ? undefined : FIXTURE.abouts.filter((a) => blocks.has(a.kind));
        return HttpResponse.json(recommend(FIXTURE.chooser.questions, answers, candidates, { limit, includeAbout: true, fallback }));
      } catch (e) {
        if (e instanceof ChooserAnswerError) return problem(422, "CHOOSER_ANSWER_INVALID", "Invalid chooser answer", e.message, e.pointer);
        throw e;
      }
    }),

    // Dataset About
    http.get(`${API}/v1/datasets/:id/about`, ({ params }) => {
      const id = String(params.id);
      const description = state.descriptions.has(id) ? state.descriptions.get(id) : (ds.about.description_user as string | null);
      return HttpResponse.json({ ...ds.about, description_user: description ?? null, generated_at: fx.T0 });
    }),
    http.patch(`${API}/v1/datasets/:id/about`, async ({ params, request }) => {
      const body = (await request.json()) as { description_user?: string | null };
      const text = body.description_user ?? null;
      if (text !== null && text.length > 4000) return problem(422, "SCHEMA_INVALID", "Invalid request", "description_user is at most 4000 characters", "/description_user");
      state.descriptions.set(String(params.id), text);
      return HttpResponse.json({ ...ds.about, description_user: text, generated_at: fx.T0 });
    }),

    // Ingest- and fit-time explorer views through the artifact routes
    http.get(new RegExp(`^${API_RE}/v1/datasets/[^/]+/versions/\\d+/artifacts/(ui%2Fexplore|ui/explore)(.*)$`), ({ request }) => {
      const name = decodeURIComponent(new URL(request.url).pathname.split("/artifacts/")[1] ?? "");
      const keys = registerPart(shared, "explore-ds-ingest", ds.ingest);
      const key = keys.get(name);
      const l = key ? linkFor(shared, key, name) : null;
      return l ? HttpResponse.json(l) : problem(404, "NOT_FOUND", "Not found", `no artifact ${name}`);
    }),
    http.get(new RegExp(`^${API_RE}/v1/models/[^/]+/versions/\\d+/artifacts/(ui%2Fexplore|ui/explore)(.*)$`), ({ request }) => {
      const parts = new URL(request.url).pathname.split("/");
      const m = modelFixture(parts[3] ?? "", Number(parts[5]));
      const name = decodeURIComponent(new URL(request.url).pathname.split("/artifacts/")[1] ?? "");
      const keys = registerPart(shared, `explore-fit:${m.ref}`, m.fit);
      const key = keys.get(name);
      const l = key ? linkFor(shared, key, name) : null;
      return l ? HttpResponse.json(l) : problem(404, "NOT_FOUND", "Not found", `no artifact ${name}`);
    }),

    // Explore runs
    http.post(`${API}/v1/datasets/:id/versions/:v/explore`, async ({ params, request }) => {
      const body = (await request.json()) as { sections?: string[] };
      const sections = body.sections?.length ? body.sections : ds.explore.index.sections.map((s) => s.section);
      const ref = `dataset://default/eu_media_daily@v${String(params.v)}`;
      const { id, rec, cached } = start(`ds:${String(params.id)}:${String(params.v)}:${JSON.stringify(body)}`, () =>
        exploreRec("dataset", ref, ds.explore, sections, `explore-ds-run:${state.seq + 1}`, ds.ingest),
      );
      await delay(80);
      return HttpResponse.json(exploration(id, rec), { status: cached ? 200 : 202 });
    }),
    http.post(new RegExp(`^${API_RE}/v1/models/[^/]+/versions/\\d+/explore$`), async ({ request }) => {
      const parts = new URL(request.url).pathname.split("/");
      const m = modelFixture(parts[3] ?? "", Number(parts[5]));
      const body = (await request.json()) as { sections?: string[] };
      const sections = body.sections?.length ? body.sections : [...m.fit.index.sections.map((s) => s.section), "forecast", "backtest"];
      const { id, rec, cached } = start(`model:${parts[3]}:${parts[5]}:${JSON.stringify(body)}`, () =>
        exploreRec("model", m.ref, m.explore, sections, `explore-model-run:${state.seq + 1}`, undefined, m.fit),
      );
      await delay(80);
      return HttpResponse.json(exploration(id, rec), { status: cached ? 200 : 202 });
    }),
    http.post(new RegExp(`^${API_RE}/v1/models:compare$`), async ({ request }) => {
      const body = (await request.json()) as { models?: string[]; sections?: string[] };
      const refs = body.models ?? [];
      if (refs.length < 2) return problem(422, "SCHEMA_INVALID", "Invalid request", "compare at least two model versions", "/models");
      const source = FIXTURE.models[`${ARIMAX}@v2`]!;
      const generated = (source.explore.views.comparison as { models: string[] }).models;
      // Relabel the computed comparison with the versions asked for (up to three).
      let text = JSON.stringify(source.explore.views.comparison);
      generated.forEach((g, i) => {
        text = text.replaceAll(g, refs[i] ?? `${g}#unused`);
      });
      const comparison = JSON.parse(text) as Json & { models: string[]; table: Record<string, unknown> };
      comparison.models = comparison.models.filter((r) => !r.endsWith("#unused"));
      comparison.table = Object.fromEntries(Object.entries(comparison.table).filter(([k]) => !k.endsWith("#unused")));
      const part: Part = {
        index: { format: "mapi.explorer-index/v1", target: "model", ref: refs[0]!, sections: [{ section: "comparison", view: "ui/explore/comparison.json", status: "ok" }] },
        views: { comparison },
        plots: {},
      };
      const { id, rec, cached } = start(`compare:${JSON.stringify(body)}`, () => exploreRec("model", refs[0]!, part, ["comparison"], `compare-run:${state.seq + 1}`));
      await delay(80);
      return HttpResponse.json(exploration(id, rec), { status: cached ? 200 : 202 });
    }),
    http.get(`${API}/v1/explorations/:id`, ({ params }) => {
      const rec = state.runs.get(String(params.id));
      if (!rec || rec.kind !== "explore") return problem(404, "NOT_FOUND", "Not found", "no such exploration");
      return HttpResponse.json(exploration(String(params.id), rec));
    }),


    // Budget optimisation
    http.post(new RegExp(`^${API_RE}/v1/models/[^/]+/versions/\\d+:optimize$`), async ({ request }) => {
      const parts = new URL(request.url).pathname.split("/");
      const modelId = parts[3] ?? "";
      if (Object.values(EXTRA_MODEL_IDS).includes(modelId as never)) {
        return problem(422, "CAPABILITY_UNSUPPORTED", "Capability not supported", "this model does not optimise budgets (it declares no decision capability)");
      }
      const body = (await request.json()) as OptimizeRequest;
      const { id, rec } = start(`optimize:${modelId}:${parts[5]}:${JSON.stringify(body)}:${state.seq}`, () => ({
        kind: "optimize",
        started: Date.now(),
        views: new Map(),
        index: null,
        target: "model",
        ref: MMM,
        optimize: planFor(body),
      }));
      const wait = /wait=(\d+)/.exec(request.headers.get("Prefer") ?? "");
      if (wait) {
        await delay(MOCK_RUN_MS);
        rec.started = Date.now() - MOCK_RUN_MS;
      }
      const o = optimization(id, rec);
      return HttpResponse.json(o, { status: o.done ? 200 : 202, headers: wait ? { "Preference-Applied": `wait=${wait[1]}` } : {} });
    }),
    http.get(`${API}/v1/optimizations/:id`, ({ params }) => {
      const rec = state.runs.get(String(params.id));
      if (!rec || rec.kind !== "optimize") return problem(404, "NOT_FOUND", "Not found", "no such optimization");
      return HttpResponse.json(optimization(String(params.id), rec));
    }),
  ];

  function optimization(id: string, rec: RunRec): Optimization {
    const run = syncRun(id, rec);
    const done = isDone(rec);
    return { run, done, request: rec.optimize!.request, result: done && !rec.optimize!.problem ? rec.optimize!.result : null };
  }

  const late: HttpHandler[] = [
    // The extra models (after the Guided handlers, which list their own sessions' models).
    http.get(`${API}/v1/models`, () => page([fx.model, ...EXTRA_MODELS.map((m) => m.model)])),
    http.get(`${API}/v1/models/:id`, ({ params }) => {
      const m = EXTRA_MODELS.find((x) => x.model.id === params.id);
      return m ? HttpResponse.json(m.model) : undefined;
    }),
    http.get(`${API}/v1/models/:id/versions`, ({ params }) => {
      const m = EXTRA_MODELS.find((x) => x.model.id === params.id);
      return m ? page([...m.versions].reverse()) : undefined;
    }),
    http.get(`${API}/v1/models/:id/versions/:v`, ({ params }) => {
      const m = EXTRA_MODELS.find((x) => x.model.id === params.id);
      if (!m) return undefined;
      const v = m.versions.find((x) => x.version === Number(params.v));
      return v ? HttpResponse.json(v) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.get(`${API}/v1/models/:id/versions/:v/plots`, ({ params }) => (EXTRA_MODELS.some((x) => x.model.id === params.id) ? page([]) : undefined)),
  ];

  return { early, late };
}

// ------------------------------------------------------------------------------ optimisation

/** The fixture plan, scaled to the budget asked for (and split by market on request). */
export function planFor(req: OptimizeRequest): { request: OptimizeRequest; result: OptimizeResult | null; problem: Problem | null } {
  const base = FIXTURE.optimize.result;
  const current = base.current_total;
  const total = req.mode === "free" ? current * 1.15 : (req.total_budget ?? (req.budget_change_pct != null ? current * (1 + req.budget_change_pct / 100) : current));
  // Static feasibility, as the server's check_feasible.
  let lo = 0;
  let hi = 0;
  for (const a of base.allocation) {
    const b = req.channels?.[a.channel] ?? req.default_bounds ?? { min_multiplier: 0, max_multiplier: 2 };
    if (b.locked) {
      lo += a.current_spend;
      hi += a.current_spend;
      continue;
    }
    lo += b.min_spend ?? (b.min_multiplier ?? 0) * a.current_spend;
    hi += b.max_spend ?? (b.max_multiplier ?? 2) * a.current_spend;
  }
  if (req.mode !== "free" && (lo > total * (1 + 1e-9) || hi < total * (1 - 1e-9))) {
    const detail = lo > total ? `the channel minimums add up to ${Math.round(lo).toLocaleString("en")}, more than the budget ${Math.round(total).toLocaleString("en")}` : `the channel maximums add up to ${Math.round(hi).toLocaleString("en")}, less than the budget ${Math.round(total).toLocaleString("en")}`;
    return { request: req, result: null, problem: { type: "urn:mapi:error:OPTIMIZE_INFEASIBLE", title: "The budget plan is infeasible", status: 422, code: "OPTIMIZE_INFEASIBLE", detail, errors: [] } as unknown as Problem };
  }
  const k = total / Math.max(1e-9, base.allocation.reduce((s, a) => s + a.optimal_spend, 0));
  let allocation = base.allocation.map((a) => ({ ...a, optimal_spend: a.optimal_spend * k, optimal_share: a.optimal_share, change_pct: ((a.optimal_spend * k) / a.current_spend - 1) * 100 }));
  if (req.by_geo) {
    const geos = [
      ["east", 0.42],
      ["north", 0.33],
      ["south", 0.25],
    ] as const;
    allocation = allocation.flatMap((a) =>
      geos.map(([geo, w], i) => {
        const tilt = 1 + (i - 1) * 0.12 * ((a.change_pct ?? 0) > 0 ? 1 : -1);
        return { ...a, geo, current_spend: a.current_spend * w, optimal_spend: a.optimal_spend * w * tilt, current_share: a.current_share * w, optimal_share: a.optimal_share * w * tilt };
      }),
    );
    const sum = allocation.reduce((s, a) => s + a.optimal_spend, 0);
    allocation = allocation.map((a) => ({ ...a, optimal_spend: (a.optimal_spend * total) / sum, optimal_share: a.optimal_spend / sum, change_pct: ((a.optimal_spend * total) / sum / a.current_spend - 1) * 100 }));
  }
  const objective = req.objective ?? "mean";
  const label = objective === "mean" ? "expected KPI (posterior mean)" : objective.startsWith("cvar") ? `average of the worst ${objective.slice(4)}% of outcomes` : `cautious plan (${objective.slice(1)}th percentile of the KPI)`;
  return {
    request: req,
    problem: null,
    result: {
      ...base,
      mode: req.mode ?? "fixed",
      objective,
      objective_label: label,
      total_budget: total,
      horizon: req.horizon ?? base.horizon ?? null,
      allocation,
      frontier: req.frontier ? base.frontier : [],
    },
  };
}
