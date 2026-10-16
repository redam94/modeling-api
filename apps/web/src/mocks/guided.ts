/**
 * MSW emulation of the Guided-mode API (`mapi_spec.api.M3_ENDPOINTS`: templates, role
 * detection, drafts, prior translations, "Run exact" compilation) plus the resources a Guided
 * session creates on the way: a dataset with its upload and sandboxed ingest, the spec and its
 * revisions, the fit run (with live SSE progress), the model version with its scorecard, plots,
 * response grid and score views, the endpoint it is promoted to and its embed link.
 *
 * Everything a session creates is labelled `guided` (datasets, specs, runs) or belongs to such a
 * resource, and these handlers answer only for those: every other request falls through to the
 * generic mock (`handlers.ts`). Time moves on its own: an ingest takes about a second, a fit
 * about six, so the smoke suite and the tutorial screenshots see every phase.
 */
import { http, HttpResponse, type HttpHandler } from "msw";
import { parse as parseYaml } from "yaml";

import type { ArtifactLink, Dataset, DatasetVersion, IngestMapping, Model, ModelVersion, PlotListItem, PlotSpec, Problem, Run, RunnerPhase, SpecDoc, SpecRevision } from "@/api/types";
import { env } from "@/env";
import type { Endpoint, GuidedAnswers, GuidedTemplate, PlotEmbedCreated, PriorTranslation, PriorTranslationRequest, SavedPlot } from "@/features/guided/types";
import { evaluateScenario, fsum, summarize, type ResponseGrid } from "@/features/guided/whatif";
import { crc32cBase64 } from "@/lib/crc32c";

import * as fx from "./fixtures";
import { buildDraft, DraftError, fitInputs, ingest, suggestRoles, synthGrid, synthScorecard, type FitInputs } from "./guided-engine";
import templatesPage from "./guided-templates.gen.json";
import type { IntegrationsState } from "./integrations";
import { mockPersonaId, PERSONAS } from "./tenancy";

const API = env.apiBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const STORE = "http://127.0.0.1:4443";
const enc = new TextEncoder();
const TEMPLATES = (templatesPage as unknown as { items: GuidedTemplate[] }).items;

/** Mock timings (ms). */
export const MOCK_TIMES = { ingest: 900, translate: 1200, fit: 6400 } as const;

// ----------------------------------------------------------------------------- state

interface GuidedSpec {
  doc: SpecDoc;
  revisions: SpecRevision[];
}

interface FitRecord {
  run: Run;
  started: number;
  specId: string;
  revision: number;
  cancelled: boolean;
}

interface ModelRecord {
  model: Model;
  versions: ModelVersion[];
}

interface Fitted {
  inputs: FitInputs;
  grid: ResponseGrid;
  /** Per draw and channel: contribution at the actual spend, for exact scoring. */
  at1: number[][];
  half: number[];
  template: GuidedTemplate;
}

export interface GuidedMockState {
  counter: number;
  datasets: Map<string, Dataset>;
  versions: Map<string, { version: DatasetVersion; created: number; mapping: IngestMapping; uploadId: string }[]>;
  uploads: Map<string, { datasetId: string; bytes: Uint8Array | null }>;
  specs: Map<string, GuidedSpec>;
  fits: Map<string, FitRecord>;
  runs: Map<string, Run>;
  translations: Map<string, { run: Run; started: number; request: PriorTranslationRequest }>;
  models: Map<string, ModelRecord>;
  fitted: Map<string, Fitted>;
  artifacts: Map<string, { name: string; bytes: Uint8Array; type: string }>;
  endpoints: Map<string, Endpoint>;
  plots: Map<string, SavedPlot>;
  embeds: Map<string, PlotEmbedCreated>;
}

export function createGuidedState(): GuidedMockState {
  return {
    counter: 0,
    datasets: new Map(),
    versions: new Map(),
    uploads: new Map(),
    specs: new Map(),
    fits: new Map(),
    runs: new Map(),
    translations: new Map(),
    models: new Map(),
    fitted: new Map(),
    artifacts: new Map(),
    endpoints: new Map(),
    plots: new Map(),
    embeds: new Map(),
  };
}

/** The parts of the generic mock state the Guided handlers read (lists merge both). */
export interface SharedMockState {
  revisions: SpecRevision[];
  /**
   * The Integrations hub's mock state (`mocks/integrations.ts`). When given, the endpoints,
   * saved plots and embed links a Guided session creates are written there too, so the hub
   * lists them, and the endpoint list is answered from that one map.
   */
  integrations?: IntegrationsState;
}

// ----------------------------------------------------------------------------- helpers

function problem(status: number, code: Problem["code"], title: string, detail?: string, errors: Problem["errors"] = []) {
  return HttpResponse.json({ type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors } satisfies Problem, {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });
const now = () => new Date().toISOString();

function nid(state: GuidedMockState, prefix: string): string {
  state.counter += 1;
  return `${prefix}_01jaf9${state.counter.toString(36).padStart(20, "0")}`;
}

function scopeOf(request: Request): { tenant: string; project: string; key: string } {
  const key = request.headers.get("X-Mapi-Project") ?? "default/default";
  const [tenant = "default", project = "default"] = key.split("/");
  return { tenant, project, key };
}

function roleIn(key: string): string | null {
  const id = mockPersonaId();
  const p = id ? PERSONAS[id] : undefined;
  if (!p) return "admin"; // single-tenant (v0.2) profile: the local user is admin
  const ranks = ["viewer", "editor", "runner", "admin"];
  const a = p.projects[key] ?? null;
  const b = p.tenants[key.split("/")[0] ?? ""] ?? null;
  return ranks.indexOf(a ?? "") >= ranks.indexOf(b ?? "") ? a : b;
}

function link(state: GuidedMockState, key: string): ArtifactLink | null {
  const a = state.artifacts.get(key);
  if (!a) return null;
  return {
    name: a.name,
    url: `${STORE}/mock-guided/${encodeURIComponent(key)}?X-Goog-Signature=mock`,
    method: "GET",
    expires_at: "2099-01-01T00:00:00Z",
    size: a.bytes.length,
    content_type: a.type,
    crc32c: crc32cBase64(a.bytes),
    generation: 1,
  };
}

function putJson(state: GuidedMockState, owner: string, name: string, value: unknown): void {
  state.artifacts.set(`${owner}:${name}`, { name, bytes: enc.encode(JSON.stringify(value)), type: "application/json" });
}

function template(id: string | null | undefined): GuidedTemplate | undefined {
  return TEMPLATES.find((t) => t.id === id);
}

function labelOf(doc: unknown): string | null {
  const labels = (doc as { metadata?: { labels?: Record<string, unknown> } } | null)?.metadata?.labels;
  return typeof labels?.guided === "string" ? labels.guided : null;
}

function baseRun(state: GuidedMockState, scope: { tenant: string; project: string }, patch: Partial<Run>): Run {
  const ts = now();
  return {
    id: nid(state, "run"),
    tenant_id: scope.tenant,
    project_id: scope.project,
    kind: "fit",
    state: "queued",
    attempt: 1,
    resource_class: "M",
    timeout: "45m",
    executor: "local-process",
    cost: { estimated: true, estimate_micros: 28_400, currency: "USD", price_version: fx.PRICE_VERSION },
    labels: {},
    created_at: ts,
    queued_at: ts,
    updated_at: ts,
    ...patch,
  };
}

// --- normal quantiles (Acklam's rational approximation; plenty for a mock)
function invNorm(p: number): number {
  const a = [-39.69683028665376, 220.9460984245205, -275.9285104469687, 138.357751867269, -30.66479806614716, 2.506628277459239];
  const b = [-54.47609879822406, 161.5858368580409, -155.6989798598866, 66.80131188771972, -13.28068155288572];
  const c = [-0.007784894002430293, -0.3223964580411365, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
  const d = [0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416];
  const q = p < 0.5 ? p : 1 - p;
  let x: number;
  if (q < 0.02425) {
    const r = Math.sqrt(-2 * Math.log(q));
    x = (((((c[0]! * r + c[1]!) * r + c[2]!) * r + c[3]!) * r + c[4]!) * r + c[5]!) / ((((d[0]! * r + d[1]!) * r + d[2]!) * r + d[3]!) * r + 1);
    x = -x;
  } else {
    const r = q - 0.5;
    const s = r * r;
    x = ((((((a[0]! * s + a[1]!) * s + a[2]!) * s + a[3]!) * s + a[4]!) * s + a[5]!) * r) / (((((b[0]! * s + b[1]!) * s + b[2]!) * s + b[3]!) * s + b[4]!) * s + 1);
  }
  return p < 0.5 ? x : -x;
}

const QS = [0.05, 0.25, 0.5, 0.75, 0.95];

function dist(values: number[]) {
  const s = summarize(values, QS);
  const mean = s.mean;
  const sd = Math.sqrt(fsum(values.map((v) => (v - mean) ** 2)) / Math.max(1, values.length - 1));
  return { mean, sd, quantiles: Object.fromEntries(QS.map((q, i) => [String(q), s.quantiles[i] as number])) };
}

function weeksAfter(last: string, n: number): string[] {
  const t = Date.parse(`${last.slice(0, 10)}T00:00:00Z`);
  return Array.from({ length: n }, (_, i) => new Date(t + (i + 1) * 7 * 86_400_000).toISOString().slice(0, 10));
}

// ----------------------------------------------------------------------------- lifecycle

function versionNow(rec: { version: DatasetVersion; created: number }): DatasetVersion {
  if (rec.version.status === "ingesting" && Date.now() - rec.created > MOCK_TIMES.ingest) {
    rec.version = { ...rec.version, status: "ready", ready_at: now() };
  }
  return rec.version;
}

const PHASES: [number, Run["state"], string | null, string][] = [
  [0.0, "queued", null, "waiting for a worker"],
  [0.06, "provisioning", "bootstrap", "starting the sandbox"],
  [0.14, "running", "load_inputs", "loading the dataset"],
  [0.22, "running", "preprocess", "scaling spend and adding seasonality"],
  [0.3, "running", "sample", "drawing samples"],
  [0.78, "running", "evaluate", "testing on the held-back weeks"],
  [0.87, "running", "plots", "drawing the report charts"],
  [0.94, "finalizing", "upload", "saving the model"],
];

function fitProgress(f: FitRecord): { state: Run["state"]; fraction: number; phase: string | null; message: string } {
  const t = Math.min(1, (Date.now() - f.started) / MOCK_TIMES.fit);
  let cur = PHASES[0]!;
  for (const p of PHASES) if (t >= p[0]) cur = p;
  return { state: t >= 1 ? "succeeded" : cur[1], fraction: t, phase: cur[2], message: cur[3] };
}

function progressPayload(f: FitRecord) {
  const p = fitProgress(f);
  const sampleFrac = Math.max(0, Math.min(1, (p.fraction - 0.3) / 0.48));
  const total = 1000;
  return {
    phase: (p.phase ?? "bootstrap") as RunnerPhase,
    fraction: Math.round(p.fraction * 1000) / 1000,
    message: p.message,
    eta_seconds: Math.max(0, Math.round(((1 - p.fraction) * MOCK_TIMES.fit) / 1000) * 30),
    chains:
      p.phase === "sample" || p.fraction >= 0.78
        ? [0, 1, 2, 3].map((chain) => {
            const done = Math.round(Math.min(1, sampleFrac * (1 - chain * 0.03)) * total);
            return { chain, draws_done: done, draws_total: total, tuning: done < 400, divergences: 0 };
          })
        : [],
  };
}

function finishFit(state: GuidedMockState, f: FitRecord): Run {
  if (f.run.state === "succeeded" || f.run.state === "cancelled") return f.run;
  if (f.cancelled) {
    f.run = { ...f.run, state: "cancelled", finished_at: now(), updated_at: now(), progress: null };
    return f.run;
  }
  const p = fitProgress(f);
  if (p.state !== "succeeded") {
    const payload = progressPayload(f);
    f.run = {
      ...f.run,
      state: p.state,
      started_at: f.run.started_at ?? (p.state === "queued" ? null : now()),
      updated_at: now(),
      progress: { seq: Math.round(p.fraction * 1000), ...payload, updated_at: now() },
    };
    return f.run;
  }
  const mv = createModelVersion(state, f);
  f.run = {
    ...f.run,
    state: "succeeded",
    finished_at: now(),
    updated_at: now(),
    progress: null,
    output_model_version_id: mv?.id ?? null,
    cost: { estimated: false, amount_micros: 26_910, currency: "USD", price_version: fx.PRICE_VERSION, vcpu_seconds: 1035, gib_seconds: 4140, usd: 0.02691, attempts_billed: 1 },
  };
  return f.run;
}

function statedRoi(doc: Record<string, unknown>): Record<string, number> {
  const ch = (doc.calibration as { business_priors?: { channels?: Record<string, { roi?: [number, number] }> } } | undefined)?.business_priors?.channels ?? {};
  return Object.fromEntries(Object.entries(ch).flatMap(([k, b]) => (b.roi ? [[k, Math.sqrt(b.roi[0] * b.roi[1])]] : [])));
}

function datasetFor(state: GuidedMockState, doc: Record<string, unknown>) {
  const ref = String((doc.data as { dataset?: string } | undefined)?.dataset ?? "");
  const name = /^dataset:\/\/[^/]+\/([^@/]+)/.exec(ref)?.[1];
  const ds = [...state.datasets.values()].find((d) => d.name === name);
  const recs = ds ? (state.versions.get(ds.id) ?? []) : [];
  const rec = recs[recs.length - 1];
  const upload = rec ? state.uploads.get(rec.uploadId) : undefined;
  return { ds, rec, bytes: upload?.bytes ?? null };
}

function createModelVersion(state: GuidedMockState, f: FitRecord): ModelVersion | null {
  const spec = state.specs.get(f.specId);
  const rev = spec?.revisions.find((r) => r.revision === f.revision);
  if (!spec || !rev) return null;
  const doc = rev.document as Record<string, unknown>;
  const tpl = template(labelOf(doc));
  const { rec, bytes } = datasetFor(state, doc);
  if (!tpl || !rec || !bytes) return null;
  const table = ingest(bytes, rec.mapping).table;
  const kpiVar = Object.entries(rec.mapping.vars ?? {}).find(([, v]) => v.role === "target")?.[0] ?? null;
  const kpiUnits = /eur/i.test(rec.mapping.vars?.[kpiVar ?? ""]?.column ?? "") ? "EUR" : null;
  const inputs = fitInputs(table, rec.mapping, kpiVar, statedRoi(doc), kpiUnits);
  const name = String((doc.metadata as { name?: string }).name);
  let mrec = [...state.models.values()].find((m) => m.model.name === name);
  if (!mrec) {
    const id = nid(state, "mdl");
    mrec = { model: { id, tenant_id: f.run.tenant_id, project_id: f.run.project_id, name, ref: `model://${f.run.project_id}/${name}`, latest_version: 0, created_at: now(), updated_at: now() }, versions: [] };
    state.models.set(id, mrec);
  }
  const version = mrec.versions.length + 1;
  const mvId = nid(state, "mv");
  const history = inputs.kpiSeries.length;
  const mv: ModelVersion = {
    ...fx.modelVersion,
    id: mvId,
    model_id: mrec.model.id,
    tenant_id: f.run.tenant_id,
    project_id: f.run.project_id,
    version,
    ref: `model://${f.run.project_id}/${name}@v${version}`,
    run_id: f.run.id,
    diagnostics: [],
    scorecard: synthScorecard(tpl, history),
    metrics: { mape: 12, coverage_90: 0.86 }, // mapi_sdk reports MAPE in percent
    lineage: { ...fx.modelVersion.lineage, run_id: f.run.id, plugin: `${tpl.plugin.split("@")[0]}@1.3.2` },
    capabilities: { inference: "mcmc", representation: "draws", score_modes: ["predict", "forecast", "counterfactual", "evaluate"], loo_valid: true },
    artifacts: [{ name: "bundle.zarr.zip", size: 2_097_152, content_type: "application/zip" }],
    created_at: now(),
  };
  mrec.versions.push(mv);
  mrec.model = { ...mrec.model, latest_version: version, updated_at: now() };
  // views and plots
  const plots: PlotListItem[] = [];
  const series = inputs.kpiSeries;
  const fit = series.map((p, i) => {
    const mean = p.y * (1 + 0.03 * Math.sin(i * 1.3));
    return { t: p.t, actual: p.y, mean, lo: mean * 0.92, hi: mean * 1.08 };
  });
  putJson(state, mvId, "ui/plotdata/fit.json", fit);
  plots.push({ id: "core.ppc_fan", title: "Model fit against your data", kind: "vega-lite", persona: "both", recipe: "core.ppc_fan@1.0.0", caption_plain: "Dots are your actual results; the band is where the model expected 90% of weeks.", href: `/v1/models/${mrec.model.id}/versions/${version}/plots/core.ppc_fan` });
  if (inputs.channels.length) {
    const grid = synthGrid(inputs);
    putJson(state, mvId, "ui/grids/response_curves.json", grid);
    const one = grid.multipliers.indexOf(1);
    const at1 = grid.contribution.map((perC) => perC.map((row) => row[one] as number));
    state.fitted.set(mvId, { inputs, grid, at1, half: inputs.channels.map((_, c) => 0.8 + 0.25 * (c % 3)), template: tpl });
    const curves = grid.channels.flatMap((ch, c) =>
      grid.multipliers.map((m, j) => {
        const s = summarize(grid.contribution.map((perC) => (perC[c] as number[])[j] as number), [0.05, 0.5, 0.95]);
        return { channel: ch, spend: (grid.spend[c] as number[])[j] as number, mean: s.mean, lo: s.quantiles[0] as number, hi: s.quantiles[2] as number, current: m === 1 };
      }),
    );
    putJson(state, mvId, "ui/plotdata/response.json", curves);
    plots.push({ id: "mmm.response_curves", title: "Response curves", kind: "vega-lite", persona: "both", recipe: "mmm.response_curves@1.0.0", caption_plain: "What each channel adds as its spend grows; the dot is what you actually spent.", href: `/v1/models/${mrec.model.id}/versions/${version}/plots/mmm.response_curves` });
  } else {
    state.fitted.set(mvId, { inputs, grid: synthGrid({ ...inputs, channels: [], spend: [] }), at1: [], half: [], template: tpl });
  }
  putJson(state, mvId, "plots.json", plots);
  return mv;
}

function plotSpecFor(id: string): PlotSpec | null {
  if (id === "core.ppc_fan") {
    return {
      id,
      title: "Model fit against your data",
      kind: "vega-lite",
      spec: {
        data: { name: "fit" },
        height: 240,
        layer: [
          { mark: { type: "area", opacity: 0.2 }, encoding: { x: { field: "t", type: "temporal", title: null }, y: { field: "lo", type: "quantitative", title: "Result", axis: { format: "~s" } }, y2: { field: "hi" } } },
          { mark: { type: "line", strokeWidth: 2 }, encoding: { x: { field: "t", type: "temporal" }, y: { field: "mean", type: "quantitative" } } },
          { mark: { type: "point", filled: true, size: 16, color: "#737373" }, encoding: { x: { field: "t", type: "temporal" }, y: { field: "actual", type: "quantitative" }, tooltip: [{ field: "t", type: "temporal", title: "Week" }, { field: "actual", title: "Actual", format: ",.0f" }, { field: "mean", title: "Model", format: ",.0f" }] } },
        ],
      },
      datasets: { fit: { kind: "data_ref", path: "ui/plotdata/fit.json", format: "json" } },
      schema_version: "v5",
      persona: "both",
      caption_plain: "Dots are your actual results; the band is where the model expected 90% of weeks.",
      provenance: { recipe: "core.ppc_fan@1.0.0", generated_at: fx.T0 },
    };
  }
  if (id === "mmm.response_curves") {
    return {
      id,
      title: "Response curves",
      kind: "vega-lite",
      spec: {
        data: { name: "curves" },
        height: 240,
        layer: [
          { mark: { type: "area", opacity: 0.15 }, encoding: { x: { field: "spend", type: "quantitative", title: "Spend", axis: { format: "~s" } }, y: { field: "lo", type: "quantitative", title: "Added result", axis: { format: "~s" } }, y2: { field: "hi" }, color: { field: "channel", type: "nominal", title: "Channel" } } },
          { mark: { type: "line", strokeWidth: 2 }, encoding: { x: { field: "spend", type: "quantitative" }, y: { field: "mean", type: "quantitative" }, color: { field: "channel", type: "nominal" } } },
          { transform: [{ filter: "datum.current" }], mark: { type: "point", filled: true, size: 60 }, encoding: { x: { field: "spend", type: "quantitative" }, y: { field: "mean", type: "quantitative" }, color: { field: "channel", type: "nominal" }, tooltip: [{ field: "channel", title: "Channel" }, { field: "spend", title: "Actual spend", format: ",.0f" }, { field: "mean", title: "Added result", format: ",.0f" }] } },
        ],
      },
      datasets: { curves: { kind: "data_ref", path: "ui/plotdata/response.json", format: "json" } },
      schema_version: "v5",
      persona: "both",
      caption_plain: "What each channel adds as its spend grows; the dot is what you actually spent.",
      provenance: { recipe: "mmm.response_curves@1.0.0", generated_at: fx.T0 },
    };
  }
  return null;
}

// ----------------------------------------------------------------------------- prior translation

function translationResult(state: GuidedMockState, req: PriorTranslationRequest) {
  const beliefs = req.business_priors.channels ?? {};
  const channels = Object.keys(beliefs);
  const roi: Record<string, ReturnType<typeof dist>> = {};
  const share: Record<string, ReturnType<typeof dist>> = {};
  const coverage: Record<string, number> = {};
  const mus: number[] = [];
  const sigmas: number[] = [];
  for (const [ch, b] of Object.entries(beliefs)) {
    const cred = b.credibility ?? 0.8;
    const z = invNorm(0.5 + cred / 2);
    const range = (b.roi ?? b.contribution_share) as [number, number];
    const isShare = !b.roi;
    const f = isShare ? (x: number) => Math.log(x / (1 - x)) : Math.log;
    const back = isShare ? (x: number) => 1 / (1 + Math.exp(-x)) : Math.exp;
    const mu = (f(range[0]) + f(range[1])) / 2;
    const sigma = (f(range[1]) - f(range[0])) / (2 * z);
    mus.push(mu);
    sigmas.push(sigma);
    const draws = Array.from({ length: 400 }, (_, i) => back(mu + sigma * invNorm((i + 0.5) / 400)));
    (isShare ? share : roi)[ch] = dist(draws);
    coverage[ch] = Math.max(0, Math.min(1, cred - 0.012 + 0.008 * Math.sin(ch.length)));
  }
  const spec = state.specs.get(req.spec_id ?? "");
  const doc = (spec?.revisions.find((r) => r.revision === req.revision)?.document ?? req.document ?? {}) as Record<string, unknown>;
  const { rec, bytes } = datasetFor(state, doc);
  let kpi_fan = null;
  if (rec && bytes) {
    const kpiVar = Object.entries(rec.mapping.vars ?? {}).find(([, v]) => v.role === "target")?.[0] ?? null;
    const inp = fitInputs(ingest(bytes, rec.mapping).table, rec.mapping, kpiVar, {}, null);
    // Like the server, preview every channel: the unticked ones at the model's wide default.
    for (const [i, ch] of inp.channels.filter((c) => !(c in beliefs)).entries()) {
      roi[ch] = dist(Array.from({ length: 400 }, (_, k) => Math.exp(Math.log(4 + 3 * i) + 1.1 * invNorm((k + 0.5) / 400))));
      share[ch] = dist(Array.from({ length: 400 }, (_, k) => 1 / (1 + Math.exp(-(-0.2 + 1.3 * invNorm((k + 0.5) / 400))))));
    }
    const coords = inp.kpiSeries.map((p) => p.t);
    const level = inp.kpiTotal / Math.max(1, coords.length);
    kpi_fan = {
      dim: "time",
      coords,
      quantiles: QS,
      values: QS.map((q) => coords.map((_, j) => Math.round(level * (1 + 0.04 * Math.sin(j / 3)) * (1 + 0.35 * invNorm(q))))),
      observed: null,
      units: null,
    };
  }
  return {
    format: "mapi.prior-translation/v1" as const,
    channels,
    priors: { beta_channel: { distribution: "LogNormal", mu: mus.map((m) => Math.round(m * 1000) / 1000), sigma: sigmas.map((s) => Math.round(s * 1000) / 1000), dims: ["channel"] } },
    preview: { roi, contribution_share: share, baseline_share: req.business_priors.baseline_share ? dist(Array.from({ length: 200 }, (_, i) => 0.55 + 0.25 * ((i + 0.5) / 200))) : null, kpi_fan },
    coverage,
    draws: req.draws ?? 500,
    seed: req.seed ?? 7,
    issues: [],
  };
}

function translationNow(state: GuidedMockState, id: string): PriorTranslation | null {
  const t = state.translations.get(id);
  if (!t) return null;
  const done = Date.now() - t.started > MOCK_TIMES.translate;
  if (done && t.run.state !== "succeeded") t.run = { ...t.run, state: "succeeded", finished_at: now(), updated_at: now() };
  else if (!done && t.run.state === "queued" && Date.now() - t.started > 200) t.run = { ...t.run, state: "running", started_at: now() };
  state.runs.set(id, t.run);
  return { run: t.run, done, result: done ? translationResult(state, t.request) : null, apply_at: "/model/settings/priors" };
}

// ----------------------------------------------------------------------------- scoring

type Intervention = { var?: string; sel?: Record<string, unknown>; multiply?: number; set?: number; add?: number };

function multipliersOf(interventions: Intervention[], channels: string[], dim = "channel"): Record<string, number> {
  const out: Record<string, number> = {};
  for (const iv of interventions) {
    const sel = iv.sel?.[dim];
    const name = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));
    const targets = sel === undefined ? channels : Array.isArray(sel) ? sel.map(name) : [name(sel)];
    for (const ch of targets) {
      if (iv.set === 0) out[ch] = 0;
      else if (typeof iv.multiply === "number") out[ch] = (out[ch] ?? 1) * iv.multiply;
    }
  }
  return out;
}

function exactDeltas(f: Fitted, mult: Record<string, number>): { scenario: number[]; base: number[] } {
  const scenario: number[] = [];
  const base: number[] = [];
  f.at1.forEach((perC, d) => {
    const b = (f.grid.baseline?.[d] ?? 0);
    let cur = b;
    let next = b;
    perC.forEach((v, c) => {
      const m = mult[f.grid.channels[c] as string] ?? 1;
      const h = f.half[c] as number;
      cur += v;
      next += (v * (m * (1 + h))) / (m + h);
    });
    scenario.push(next);
    base.push(cur);
  });
  return { scenario, base };
}

function scoreViews(f: Fitted, request: Record<string, unknown>): { name: string; view: unknown } {
  const ivs = (Array.isArray(request.interventions) ? request.interventions : []) as Intervention[];
  const mult = multipliersOf(ivs, f.grid.channels);
  if (request.mode === "forecast") {
    const horizon = (request.horizon as { periods?: number } | undefined)?.periods ?? 8;
    const hist = f.inputs.kpiSeries.slice(-12);
    const last = hist[hist.length - 1]?.t ?? "2025-10-05";
    const future = weeksAfter(last, horizon);
    const level = hist.reduce((a, p) => a + p.y, 0) / Math.max(1, hist.length);
    const media = f.at1.length ? f.at1[0]!.reduce((a, b) => a + b, 0) / Math.max(1, f.inputs.kpiSeries.length) : 0;
    const k = Object.values(mult)[0] ?? 1;
    const coords = [...hist.map((p) => p.t), ...future];
    const values = QS.map((q) =>
      coords.map((_, j) => {
        if (j < hist.length) return null;
        const step = j - hist.length + 1;
        const mid = level * (1 + 0.03 * Math.sin(j / 2)) + media * (k - 1);
        return Math.round(mid * (1 + 0.025 * Math.sqrt(step) * invNorm(q)));
      }),
    );
    const totals = Array.from({ length: 200 }, (_, i) => (level + media * (k - 1)) * horizon * (1 + 0.06 * invNorm((i + 0.5) / 200)));
    return {
      name: "ui/guided/forecast.json",
      view: {
        format: "mapi.forecast/v1",
        kpi: "y",
        units: f.inputs.kpiUnits,
        kind: "expected",
        dim: "time",
        history: hist.length,
        split_at: future[0],
        summed_over: f.inputs.geos ? ["geo"] : [],
        fan: { dim: "time", coords, quantiles: QS, values, observed: [...hist.map((p) => p.y), ...future.map(() => null)], units: f.inputs.kpiUnits },
        total: dist(totals),
      },
    };
  }
  const { scenario, base } = exactDeltas(f, mult);
  const delta = scenario.map((s, i) => s - (base[i] as number));
  return {
    name: "ui/guided/counterfactual.json",
    view: {
      format: "mapi.counterfactual/v1",
      kpi: "y",
      units: f.inputs.kpiUnits,
      kind: "expected",
      dim: "time",
      summed_over: f.inputs.geos ? ["geo"] : [],
      interventions: ivs,
      scenario_total: dist(scenario),
      baseline_total: dist(base),
      delta_total: dist(delta),
      delta_share: dist(delta.map((v, i) => v / (base[i] as number))),
    },
  };
}

// ----------------------------------------------------------------------------- handlers

export function createGuidedHandlers(shared: SharedMockState, state: GuidedMockState = createGuidedState()): HttpHandler[] {
  const genericSpecItem = (): SpecDoc => ({ ...fx.specDoc, latest_revision: shared.revisions.length, latest: shared.revisions.at(-1) ?? null });
  const specOfRun = (body: { spec_id?: string | null }) => (body.spec_id ? state.specs.get(body.spec_id) : undefined);
  // One endpoint map for Guided sessions and the Integrations hub (when both are mocked).
  const endpoints = shared.integrations?.endpoints ?? state.endpoints;

  return [
    // ------------------------------------------------------------------ templates, detection, drafts
    http.get(`${API}/v1/guided/templates`, () => HttpResponse.json(templatesPage)),
    http.get(`${API}/v1/guided/templates/:id`, ({ params }) => {
      const t = template(String(params.id));
      return t ? HttpResponse.json(t) : problem(404, "NOT_FOUND", "Not found", "no such guided template");
    }),
    http.post(`${API}/v1/guided/detect-roles`, async ({ request }) => {
      const body = (await request.json()) as { template_id: string; columns?: GuidedAnswers["columns"] };
      const t = template(body.template_id);
      if (!t) return problem(404, "NOT_FOUND", "Not found", "no such guided template");
      if (!body.columns?.length) return problem(422, "SCHEMA_INVALID", "Invalid request", "give exactly one of 'dataset' or 'columns'");
      return HttpResponse.json(suggestRoles(t, body.columns));
    }),
    http.post(`${API}/v1/guided/drafts`, async ({ request }) => {
      const body = (await request.json()) as GuidedAnswers;
      const t = template(body.template_id);
      if (!t) return problem(404, "NOT_FOUND", "Not found", "no such guided template");
      try {
        return HttpResponse.json(buildDraft(t, body, scopeOf(request).project));
      } catch (e) {
        if (e instanceof DraftError) {
          return problem(422, "GUIDED_ANSWERS_INVALID", "The answers do not make a spec", e.message, [{ code: "GUIDED_ANSWERS_INVALID", pointer: e.pointer, message: e.message, severity: "error" }]);
        }
        throw e;
      }
    }),
    http.post(new RegExp(`^${API_RE}/v1/guided/requests:compile$`), async ({ request }) => {
      const body = (await request.json()) as {
        kind: "whatif" | "forecast" | "counterfactual";
        model: string;
        spend_var?: string;
        channel_dim?: string;
        scenario?: { multipliers?: Record<string, number> } | string;
        periods?: number;
        dim?: string;
        preset?: { kind: "channel_off"; channel: string } | { kind: "budget_scale"; k: number; channels?: string[] | null };
      };
      const v = body.spend_var ?? "spend";
      const dim = body.channel_dim ?? "channel";
      const output = { kind: "expected", summary: ["mean", "hdi_90"] };
      const presetIvs = () =>
        !body.preset ? [] : body.preset.kind === "channel_off" ? [{ var: v, sel: { [dim]: body.preset.channel }, set: 0 }] : [{ var: v, sel: body.preset.channels?.length ? { [dim]: body.preset.channels } : {}, multiply: body.preset.k }];
      const base = { apiVersion: "mapi/v1", kind: "ScoreRequest", model: body.model, output };
      if (body.kind === "whatif") {
        const m = (typeof body.scenario === "object" ? body.scenario.multipliers : undefined) ?? {};
        const interventions = Object.entries(m)
          .filter(([, x]) => x !== 1)
          .sort(([a], [b]) => (a < b ? -1 : 1))
          .map(([ch, x]) => ({ var: v, sel: { [dim]: ch }, multiply: x }));
        if (!interventions.length) return problem(422, "SCHEMA_INVALID", "Invalid request", "the scenario changes nothing (every multiplier is 1)", [{ code: "SCHEMA_INVALID", pointer: "/multipliers", message: "the scenario changes nothing (every multiplier is 1)" }]);
        return HttpResponse.json({ ...base, mode: "counterfactual", interventions });
      }
      if (body.kind === "forecast") {
        return HttpResponse.json({ ...base, mode: "forecast", interventions: presetIvs(), horizon: { dim: body.dim ?? "time", periods: body.periods ?? 8 }, future_covariates: "carry_last" });
      }
      return HttpResponse.json({ ...base, mode: "counterfactual", interventions: presetIvs() });
    }),

    // ------------------------------------------------------------------ prior translations
    http.post(`${API}/v1/guided/prior-translations`, async ({ request }) => {
      const body = (await request.json()) as PriorTranslationRequest;
      if (!body.business_priors || (!Object.keys(body.business_priors.channels ?? {}).length && !body.business_priors.baseline_share)) {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "state at least one belief: a channel's or the baseline's");
      }
      const scope = scopeOf(request);
      const run = baseRun(state, scope, { kind: "translate_priors", spec_id: body.spec_id ?? null, spec_revision: body.revision ?? null, resource_class: "S", labels: { guided: "priors" } });
      state.translations.set(run.id, { run, started: Date.now(), request: body });
      state.runs.set(run.id, run);
      return HttpResponse.json({ run, done: false, result: null, apply_at: "/model/settings/priors" } satisfies PriorTranslation, { status: 202 });
    }),
    http.get(`${API}/v1/guided/prior-translations/:id`, ({ params }) => {
      const t = translationNow(state, String(params.id));
      return t ? HttpResponse.json(t) : problem(404, "NOT_FOUND", "Not found", "no such prior translation");
    }),

    // ------------------------------------------------------------------ datasets
    http.post(`${API}/v1/datasets`, async ({ request }) => {
      const body = (await request.clone().json()) as { name: string; description?: string; labels?: Record<string, string> };
      if (!body.labels?.guided) return undefined;
      if ([...state.datasets.values()].some((d) => d.name === body.name) || body.name === fx.dataset.name) return problem(409, "CONFLICT", "Conflict", "a dataset with this name exists");
      const scope = scopeOf(request);
      const ds: Dataset = { id: nid(state, "ds"), tenant_id: scope.tenant, project_id: scope.project, name: body.name, ref: `dataset://${scope.project}/${body.name}`, description: body.description ?? null, labels: body.labels, latest_version: null, created_at: now(), updated_at: now() };
      state.datasets.set(ds.id, ds);
      return HttpResponse.json(ds, { status: 201 });
    }),
    http.get(`${API}/v1/datasets`, () => (state.datasets.size ? page([...[...state.datasets.values()].reverse(), fx.dataset]) : undefined)),
    http.get(`${API}/v1/datasets/:id`, ({ params }) => {
      const ds = state.datasets.get(String(params.id));
      return ds ? HttpResponse.json(ds) : undefined;
    }),
    http.post(`${API}/v1/datasets/:id/uploads`, async ({ params, request }) => {
      const ds = state.datasets.get(String(params.id));
      if (!ds) return undefined;
      const body = (await request.json()) as { filename: string; content_type: string; size_bytes: number };
      const id = nid(state, "upl");
      state.uploads.set(id, { datasetId: ds.id, bytes: null });
      return HttpResponse.json(
        {
          id,
          tenant_id: ds.tenant_id,
          project_id: ds.project_id,
          dataset_id: ds.id,
          filename: body.filename,
          content_type: body.content_type,
          size_bytes: body.size_bytes,
          status: "pending",
          upload: { method: "PUT", url: `${STORE}/mock-guided-upload/${id}?X-Goog-Signature=mock`, name: `t/${ds.tenant_id}/uploads/${id}`, expires_at: "2099-01-01T00:00:00Z", headers: { "content-type": body.content_type, "x-goog-if-generation-match": "0" }, max_bytes: body.size_bytes },
          expires_at: "2099-01-01T00:00:00Z",
          created_at: now(),
        },
        { status: 201 },
      );
    }),
    http.put(`${STORE}/mock-guided-upload/:id`, async ({ params, request }) => {
      const u = state.uploads.get(String(params.id));
      if (!u) return new HttpResponse(null, { status: 404 });
      if (request.headers.get("x-goog-if-generation-match") !== "0") return new HttpResponse(null, { status: 412 });
      u.bytes = new Uint8Array(await request.arrayBuffer());
      return HttpResponse.json({ generation: "1" });
    }),
    http.post(`${API}/v1/datasets/:id/versions`, async ({ params, request }) => {
      const ds = state.datasets.get(String(params.id));
      if (!ds) return undefined;
      const body = (await request.json()) as { upload_id: string; mapping: IngestMapping; message?: string };
      const upload = state.uploads.get(body.upload_id);
      if (!upload?.bytes) return problem(409, "CONFLICT", "Conflict", "the upload is not finished");
      let outcome;
      try {
        outcome = ingest(upload.bytes, body.mapping);
      } catch {
        return problem(422, "SCHEMA_INVALID", "Invalid mapping", "the mapping does not fit the file");
      }
      const recs = state.versions.get(ds.id) ?? [];
      const version = recs.length + 1;
      const v: DatasetVersion = {
        dataset_id: ds.id,
        tenant_id: ds.tenant_id,
        project_id: ds.project_id,
        version,
        ref: `dataset://${ds.project_id}/${ds.name}@v${version}`,
        status: "ingesting",
        sha256: fx.SHA,
        size_bytes: upload.bytes.length,
        data_schema: outcome.schema,
        summary: outcome.summary,
        profile: null,
        preview: null,
        ingest_run_id: nid(state, "run"),
        issues: [],
        message: body.message ?? null,
        created_at: now(),
        ready_at: null,
      };
      recs.push({ version: v, created: Date.now(), mapping: body.mapping, uploadId: body.upload_id });
      state.versions.set(ds.id, recs);
      state.datasets.set(ds.id, { ...ds, latest_version: version, updated_at: now() });
      return HttpResponse.json(v, { status: 202 });
    }),
    http.get(`${API}/v1/datasets/:id/versions`, ({ params }) => {
      const recs = state.versions.get(String(params.id));
      if (!state.datasets.has(String(params.id))) return undefined;
      return page([...(recs ?? [])].reverse().map(versionNow));
    }),
    http.get(`${API}/v1/datasets/:id/versions/:v`, ({ params }) => {
      if (!state.datasets.has(String(params.id))) return undefined;
      const rec = state.versions.get(String(params.id))?.find((r) => r.version.version === Number(params.v));
      return rec ? HttpResponse.json(versionNow(rec)) : problem(404, "NOT_FOUND", "Not found");
    }),

    // ------------------------------------------------------------------ specs
    http.post(`${API}/v1/specs`, async ({ request }) => {
      const body = (await request.clone().json()) as { yaml: string; message?: string };
      let doc: Record<string, unknown>;
      try {
        doc = parseYaml(body.yaml) as Record<string, unknown>;
      } catch {
        return undefined;
      }
      if (!labelOf(doc)) return undefined;
      const scope = scopeOf(request);
      const name = String((doc.metadata as { name?: string }).name ?? "");
      if ([...state.specs.values()].some((s) => s.doc.name === name)) return problem(409, "CONFLICT", "Conflict", `a spec named ${name} exists`);
      const id = nid(state, "spc");
      const rev: SpecRevision = { spec_id: id, tenant_id: scope.tenant, project_id: scope.project, revision: 1, ref: `spec://${scope.project}/${name}@r1`, kind: "ModelSpec", hash: fx.SHA, yaml: body.yaml, document: doc, valid: true, issues: [], parent_revision: null, message: body.message ?? null, created_at: now() };
      const sd: SpecDoc = { id, tenant_id: scope.tenant, project_id: scope.project, name, kind: "ModelSpec", ref: `spec://${scope.project}/${name}`, latest_revision: 1, latest: rev, created_at: now(), updated_at: now() };
      state.specs.set(id, { doc: sd, revisions: [rev] });
      return HttpResponse.json(sd, { status: 201 });
    }),
    http.get(`${API}/v1/specs`, () => (state.specs.size ? page([...[...state.specs.values()].map((s) => s.doc).reverse(), genericSpecItem()]) : undefined)),
    http.get(`${API}/v1/specs/:id`, ({ params }) => {
      const s = state.specs.get(String(params.id));
      return s ? HttpResponse.json(s.doc) : undefined;
    }),
    http.get(`${API}/v1/specs/:id/revisions`, ({ params }) => {
      const s = state.specs.get(String(params.id));
      return s ? page([...s.revisions].reverse()) : undefined;
    }),
    http.get(`${API}/v1/specs/:id/revisions/:rev`, ({ params }) => {
      const s = state.specs.get(String(params.id));
      if (!s) return undefined;
      const r = s.revisions.find((x) => x.revision === Number(params.rev));
      return r ? HttpResponse.json(r) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.post(`${API}/v1/specs/:id/revisions`, async ({ params, request }) => {
      const s = state.specs.get(String(params.id));
      if (!s) return undefined;
      const body = (await request.json()) as { yaml: string; parent_revision: number; message?: string };
      if (body.parent_revision !== s.revisions.length) return problem(409, "CONFLICT", "Conflict", "parent_revision is stale");
      let doc: Record<string, unknown>;
      try {
        doc = parseYaml(body.yaml) as Record<string, unknown>;
      } catch {
        return problem(400, "SPEC_PARSE_ERROR", "The document could not be parsed", "invalid YAML");
      }
      const revision = s.revisions.length + 1;
      const rev: SpecRevision = { ...s.revisions[0]!, revision, ref: `spec://${s.doc.project_id}/${s.doc.name}@r${revision}`, yaml: body.yaml, document: doc, parent_revision: body.parent_revision, message: body.message ?? null, created_at: now() };
      s.revisions.push(rev);
      s.doc = { ...s.doc, latest_revision: revision, latest: rev, updated_at: now() };
      return HttpResponse.json(rev, { status: 201 });
    }),

    // ------------------------------------------------------------------ runs
    http.post(`${API}/v1/runs`, async ({ request }) => {
      const body = (await request.clone().json()) as { kind: Run["kind"]; spec_id?: string; revision?: number; labels?: Record<string, string> };
      const spec = specOfRun(body);
      if (!spec || body.kind !== "fit") return undefined;
      if (roleIn(scopeOf(request).key) === "editor") return problem(403, "FORBIDDEN", "Not allowed", "starting runs needs the runner role");
      const run = baseRun(state, scopeOf(request), { spec_id: spec.doc.id, spec_revision: body.revision ?? spec.doc.latest_revision, spec_hash: fx.SHA, labels: body.labels ?? {} });
      state.fits.set(run.id, { run, started: Date.now(), specId: spec.doc.id, revision: body.revision ?? spec.doc.latest_revision, cancelled: false });
      state.runs.set(run.id, run);
      return HttpResponse.json(run, { status: 201 });
    }),
    http.get(`${API}/v1/runs`, ({ request }) => {
      const url = new URL(request.url);
      const specId = url.searchParams.get("spec_id");
      if (!specId || !state.specs.has(specId)) return undefined;
      return page([...state.fits.values()].filter((f) => f.specId === specId).map((f) => finishFit(state, f)).reverse());
    }),
    http.get(`${API}/v1/runs/:id`, ({ params }) => {
      const id = String(params.id);
      const f = state.fits.get(id);
      if (f) return HttpResponse.json(finishFit(state, f));
      if (state.translations.has(id)) return HttpResponse.json(translationNow(state, id)!.run);
      const r = state.runs.get(id);
      return r ? HttpResponse.json(r) : undefined;
    }),
    http.get(`${API}/v1/runs/:id/events`, ({ params, request }) => {
      const id = String(params.id);
      const f = state.fits.get(id);
      const other = state.runs.get(id);
      if (!f && !other) return undefined;
      if (!(request.headers.get("accept") ?? "").includes("text/event-stream")) return page([]);
      const frame = (event: string, data: unknown, eid?: number) => enc.encode(`${eid !== undefined ? `id: ${eid}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
      const terminal = (r: Run) => ["succeeded", "failed", "cancelled", "partial", "rejected"].includes(r.state);
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          const current = () => (f ? finishFit(state, f) : (state.runs.get(id) as Run));
          controller.enqueue(enc.encode(`retry: 3000\n`));
          controller.enqueue(frame("snapshot", current()));
          let seq = 1;
          let lastPhase: string | null = null;
          const tick = () => {
            const r = current();
            if (terminal(r)) {
              controller.enqueue(frame("state", { id: seq, run_id: id, attempt: 1, seq: seq++, ts: now(), type: "state", payload: { state: r.state } }, seq));
              controller.enqueue(frame("end", r));
              clearInterval(timer);
              controller.close();
              return true;
            }
            if (f) {
              const payload = progressPayload(f);
              if (payload.phase !== lastPhase) {
                lastPhase = payload.phase;
                controller.enqueue(frame("state", { id: seq, run_id: id, attempt: 1, seq: seq++, ts: now(), type: "state", payload: { phase: payload.phase, state: r.state } }, seq));
              }
              controller.enqueue(frame("progress", { run_id: id, attempt: 1, seq: seq++, ts: now(), type: "progress", payload }));
            }
            return false;
          };
          const timer = setInterval(tick, 300);
          if (tick()) return;
          request.signal.addEventListener("abort", () => {
            clearInterval(timer);
            try {
              controller.close();
            } catch {
              // already closed
            }
          });
        },
      });
      return new Response(stream, { headers: { "content-type": "text/event-stream", "cache-control": "no-store" } });
    }),
    http.get(`${API}/v1/runs/:id/logs`, ({ params }) => (state.runs.has(String(params.id)) ? page([]) : undefined)),
    http.post(new RegExp(`^${API_RE}/v1/runs/[^/]+:cancel$`), ({ request }) => {
      const id = decodeURIComponent(new URL(request.url).pathname.split("/").at(-1)!.split(":")[0]!);
      const f = state.fits.get(id);
      if (!f) return undefined;
      if (f.run.state !== "succeeded") {
        f.cancelled = true;
        f.run = { ...f.run, state: "cancelling", updated_at: now() };
      }
      return HttpResponse.json(f.run, { status: 202 });
    }),

    // ------------------------------------------------------------------ models
    http.get(`${API}/v1/models`, () => (state.models.size ? page([...[...state.models.values()].map((m) => m.model).reverse(), fx.model]) : undefined)),
    http.get(`${API}/v1/models/:id`, ({ params }) => {
      const m = state.models.get(String(params.id));
      return m ? HttpResponse.json(m.model) : undefined;
    }),
    http.get(`${API}/v1/models/:id/versions`, ({ params }) => {
      const m = state.models.get(String(params.id));
      return m ? page([...m.versions].reverse()) : undefined;
    }),
    http.get(`${API}/v1/models/:id/versions/:v`, ({ params }) => {
      const m = state.models.get(String(params.id));
      if (!m) return undefined;
      const v = m.versions.find((x) => x.version === Number(params.v));
      return v ? HttpResponse.json(v) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.get(`${API}/v1/models/:id/versions/:v/plots`, ({ params }) => {
      const m = state.models.get(String(params.id));
      if (!m) return undefined;
      const v = m.versions.find((x) => x.version === Number(params.v));
      const a = v ? state.artifacts.get(`${v.id}:plots.json`) : undefined;
      return page(a ? (JSON.parse(new TextDecoder().decode(a.bytes)) as PlotListItem[]) : []);
    }),
    http.get(`${API}/v1/models/:id/versions/:v/plots/:plotId`, ({ params }) => {
      if (!state.models.has(String(params.id))) return undefined;
      const spec = plotSpecFor(String(params.plotId));
      return spec ? HttpResponse.json(spec) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.get(new RegExp(`^${API_RE}/v1/models/[^/]+/versions/\\d+/artifacts/(.+)$`), ({ request }) => {
      const parts = new URL(request.url).pathname.split("/");
      const m = state.models.get(parts[3] ?? "");
      if (!m) return undefined;
      const v = m.versions.find((x) => x.version === Number(parts[5]));
      const name = decodeURIComponent(new URL(request.url).pathname.split("/artifacts/")[1] ?? "");
      const l = v ? link(state, `${v.id}:${name}`) : null;
      return l ? HttpResponse.json(l) : problem(404, "NOT_FOUND", "Not found", `no artifact ${name}`);
    }),
    http.post(new RegExp(`^${API_RE}/v1/models/[^/]+/versions/\\d+:score$`), async ({ request }) => {
      const parts = new URL(request.url).pathname.split("/");
      const m = state.models.get(parts[3] ?? "");
      if (!m) return undefined;
      const v = m.versions.find((x) => x.version === Number((parts[5] ?? "").split(":")[0]));
      const f = v ? state.fitted.get(v.id) : undefined;
      if (!v || !f) return problem(404, "NOT_FOUND", "Not found");
      const body = (await request.json()) as { request: Record<string, unknown>; labels?: Record<string, string> };
      const { name, view } = scoreViews(f, body.request);
      const run = baseRun(state, scopeOf(request), { kind: "score", state: "succeeded", model_version_id: v.id, finished_at: now(), labels: body.labels ?? {} });
      state.runs.set(run.id, run);
      putJson(state, run.id, name, view);
      return HttpResponse.json({ done: true, run, outputs: [link(state, `${run.id}:${name}`)!] }, { headers: { "Preference-Applied": "wait=30" } });
    }),

    // ------------------------------------------------------------------ endpoints
    http.get(`${API}/v1/endpoints`, () => page([...endpoints.values()].sort((a, b) => a.alias.localeCompare(b.alias)))),
    http.get(`${API}/v1/endpoints/:alias`, ({ params }) => {
      const e = endpoints.get(String(params.alias));
      return e ? HttpResponse.json(e) : problem(404, "NOT_FOUND", "Not found", "no such endpoint");
    }),
    http.put(`${API}/v1/endpoints/:alias`, async ({ params, request }) => {
      const body = (await request.clone().json()) as { model_version_id?: string; override?: boolean; reason?: string; expected_revision?: number; description?: string };
      const rec = [...state.models.values()].find((m) => m.versions.some((v) => v.id === body.model_version_id));
      const mv = rec?.versions.find((v) => v.id === body.model_version_id);
      if (!rec || !mv) return undefined;
      const alias = String(params.alias);
      const prev = endpoints.get(alias);
      if (body.expected_revision !== undefined && body.expected_revision !== (prev?.revision ?? 0)) return problem(409, "CONFLICT", "Conflict", "the endpoint changed since you loaded it");
      const reasons = (mv.scorecard?.items ?? [])
        .filter((i) => i.status !== "pass")
        .map((i) => ({ code: i.status === "fail" ? "SCORECARD_FAIL" : "SCORECARD_WARN", outcome: i.status === "fail" ? "blocked" : "needs_override", message_plain: `${i.title}: ${i.message_plain}`, check: i.check ?? null }));
      const outcome = reasons.some((r) => r.outcome === "blocked") ? "blocked" : reasons.length ? "needs_override" : "allowed";
      const issues = reasons.map((r) => ({ code: outcome === "blocked" ? "PROMOTION_BLOCKED" : "OVERRIDE_REQUIRED", pointer: "", message: r.message_plain, severity: r.outcome === "blocked" ? "error" : "warning", context: { gate_code: r.code, check: r.check } })) as Problem["errors"];
      if (outcome === "blocked") return problem(409, "PROMOTION_BLOCKED", "Promotion blocked", "the promotion gate blocks this model version", issues);
      if (outcome === "needs_override") {
        if (!body.override) return problem(409, "OVERRIDE_REQUIRED", "An administrator override is needed", "this model version needs an administrator override (send override=true with a reason)", issues);
        if (roleIn(scopeOf(request).key) !== "admin") return problem(403, "FORBIDDEN", "Not allowed", "overriding the promotion gate needs a signed-in project admin");
        if (!body.reason?.trim()) return problem(422, "SCHEMA_INVALID", "Invalid request", "an override needs a reason");
      }
      const scope = scopeOf(request);
      const ep: Endpoint = {
        id: prev?.id ?? nid(state, "ep"),
        alias,
        tenant_id: scope.tenant,
        project_id: scope.project,
        ref: `endpoint://${scope.project}/${alias}`,
        description: body.description ?? prev?.description ?? null,
        model_id: rec.model.id,
        model_version_id: mv.id,
        model_ref: mv.ref,
        revision: (prev?.revision ?? 0) + 1,
        evaluation_policy: { fit_mode: "evaluate_then_refit", schema_change: "block" },
        schema_snapshot: { input_schema: { layout: "dense", dims: {}, vars: {} }, schema_hash: fx.SHA, score_modes: ["forecast", "counterfactual"] },
        gate: { outcome: outcome, scorecard_overall: mv.scorecard?.overall ?? null, reasons: reasons as Endpoint["gate"]["reasons"], failing_checks: reasons.map((r) => r.check ?? "") },
        overridden: outcome === "needs_override",
        promoted_by: PERSONAS[mockPersonaId() ?? ""]?.id ?? null,
        promoted_at: now(),
        created_at: prev?.created_at ?? now(),
        updated_at: now(),
      };
      endpoints.set(alias, ep);
      const history = shared.integrations?.history;
      if (history) {
        const rows = history.get(alias) ?? [];
        rows.unshift({ alias, revision: ep.revision, action: "promote", model_version_id: mv.id, model_ref: mv.ref, previous_model_version_id: prev?.model_version_id ?? null, gate: ep.gate, overridden: ep.overridden, reason: body.reason?.trim() || null, actor_id: ep.promoted_by ?? null, created_at: ep.promoted_at });
        history.set(alias, rows);
      }
      return HttpResponse.json(ep, { status: prev ? 200 : 201 });
    }),

    // ------------------------------------------------------------------ saved plots and embeds
    http.post(`${API}/v1/plots`, async ({ request }) => {
      const body = (await request.clone().json()) as { title: string; source?: { model_id: string; version: number; plot_id: string } };
      if (!body.source || !state.models.has(body.source.model_id)) return undefined;
      const spec = plotSpecFor(body.source.plot_id);
      if (!spec) return problem(404, "NOT_FOUND", "Not found", "no such plot");
      const scope = scopeOf(request);
      const p: SavedPlot = { id: nid(state, "plt"), tenant_id: scope.tenant, project_id: scope.project, title: body.title, kind: "vega-lite", source_kind: "model_plot", source: body.source, recipe: spec.provenance.recipe ?? null, provenance: "recipe", embeddable: true, created_by: PERSONAS[mockPersonaId() ?? ""]?.id ?? null, created_at: now(), updated_at: now() };
      state.plots.set(p.id, p);
      shared.integrations?.plots.push(p);
      return HttpResponse.json(p, { status: 201 });
    }),
    http.post(`${API}/v1/plots/:id/embeds`, async ({ params, request }) => {
      const p = state.plots.get(String(params.id));
      if (!p) return undefined;
      const body = (await request.json()) as { expires_in_days?: number; label?: string | null; allowed_origins?: string[] };
      const token = `mpe_${nid(state, "x").slice(2).replace(/_/g, "").padEnd(40, "k").slice(0, 40)}`;
      const created: PlotEmbedCreated = {
        embed: { id: nid(state, "emb"), plot_id: p.id, tenant_id: p.tenant_id, project_id: p.project_id, label: body.label ?? null, display: `${token.slice(0, 10)}…`, allowed_origins: body.allowed_origins ?? [], status: "active", views: 0, created_at: now(), expires_at: new Date(Date.now() + (body.expires_in_days ?? 30) * 86_400_000).toISOString() },
        url: `${env.plotsOrigin}/embed/${token}`,
        token,
      };
      state.embeds.set(created.embed.id, created);
      const hub = shared.integrations?.embeds;
      if (hub) hub.set(p.id, [created.embed, ...(hub.get(p.id) ?? [])]);
      return HttpResponse.json(created, { status: 201 });
    }),

    // ------------------------------------------------------------------ signed artifact URLs
    http.get(`${STORE}/mock-guided/:key`, ({ params }) => {
      const a = state.artifacts.get(decodeURIComponent(String(params.key)));
      return a ? new HttpResponse(a.bytes, { headers: { "content-type": a.type } }) : new HttpResponse(null, { status: 404 });
    }),
  ];
}

/** For tests: the what-if answer the mock's grid gives (the browser computes the same). */
export function mockWhatIf(state: GuidedMockState, mvId: string, multipliers: Record<string, number>) {
  const f = state.fitted.get(mvId);
  return f ? evaluateScenario(f.grid, { multipliers }) : null;
}
