/**
 * MSW emulation of the v0.18 library and extension routes (I-5): `/v1/library` (items,
 * revisions with `If-Match`, archive, promote, `:validate`, `:use`, `charts:preview`), the
 * live recipe previews (`/v1/catalog/plots/{id}/{version}:preview` and its result), the
 * catalog diagnostics of one plugin distribution and the operator's describe action.
 *
 * It is not the server: validation checks the shapes the UI relies on and points issues
 * into the document like `mapi_spec.library` does (`/declarative/terms/0/prior/sigma`), and
 * "compiling" builds the spec fragment, steps, chart or workflow the server would. A chart
 * template is drawn on its source recipe's catalog example (inline data) both for the
 * preview and when used on a model version, so it renders in the mock without data refs.
 *
 * Fixtures: the project `acme/growth` holds a declarative model recipe (two revisions), a
 * preset over `ts.sarimax`, a prep preset, a chart template, an archived chart and a prep
 * preset that names a step this catalog lacks (`valid: false`); the tenant holds a workflow
 * template. The sample project gets the three seed items when it is ready (`seedLibrary`).
 * User-authored strings are stored and returned verbatim (one summary holds markup on
 * purpose: pages must render it as text).
 *
 * Switches (localStorage): `mapi.mock.previewMs` (how long a recipe preview run takes,
 * default 1500), `mapi.mock.previewOff=1` (previews turned off: 409 `PREVIEW_DISABLED`),
 * `mapi.mock.extensions=1` (the catalog lists one step installed by the `acme-mapi-extras`
 * distribution, `provider` set).
 */
import { delay, http, HttpResponse, type HttpHandler } from "msw";
import { parse as parseYaml } from "yaml";

import type { CatalogResponse, Issue, PlotSpec, Problem, StepCatalogEntry } from "@/api/types";
import { env } from "@/env";
import type { CatalogDiagnostic } from "@/features/extend/api";
import {
  datasetNamesOf,
  DECLARATIVE_MODEL_ID,
  GLM_FAMILIES,
  isLibraryItemId,
  KIND_DOC,
  LABEL_MODEL,
  LABEL_PREP,
  LABEL_TEMPLATE,
  libraryKindOf,
  libraryLabel,
  PRIOR_DISTS,
  SIGNS,
  slotName,
  splitUseRef,
  TRANSFORM_USES,
  type ChartTemplateDoc,
  type DeclarativeModel,
  type Json,
  type JsonObject,
  type LibraryDoc,
  type LibraryItem,
  type LibraryKind,
  type LibraryRevision,
  type LibraryUseResult,
  type ModelRecipeDoc,
  type PrepPresetDoc,
  type PriorSpec,
  type WorkflowTemplateDoc,
} from "@/features/library/types";
import { applyInstantSettings, serverSettingsKey, splitSettings, type RecipePreviewResult } from "@/features/plots/preview-api";

import editorGen from "./editor.gen.json";
import * as fx from "./fixtures";
import { mockPersonaId, PERSONAS } from "./tenancy";
import { TEMPLATES } from "./workflows";

const API = env.apiBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const ID = "([^/:?]+)";

export const MOCK_PREVIEW_MS_KEY = "mapi.mock.previewMs";
export const MOCK_PREVIEW_OFF_KEY = "mapi.mock.previewOff";
export const MOCK_EXTENSIONS_KEY = "mapi.mock.extensions";

/** The plugin distribution of the fixtures (diagnostics, `provider`). */
export const MOCK_PLUGIN_DIST = "acme-mapi-extras";

const CATALOG = editorGen.catalog as unknown as CatalogResponse;
const MAX_ITEMS = 500;
const MAX_REVISIONS = 200;
const DOC_MAX_BYTES = 262_144;
const VALIDATE_MAX_BYTES = 65_536;
const PREVIEW_PER_MINUTE = 20;

type Persona = NonNullable<(typeof PERSONAS)[string]>;

function problem(status: number, code: Problem["code"], title: string, detail?: string, opts: { errors?: Issue[]; headers?: Record<string, string> } = {}) {
  return HttpResponse.json({ type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: opts.errors ?? [] } satisfies Problem, {
    status,
    headers: { "content-type": "application/problem+json", ...(opts.headers ?? {}) },
  });
}

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function persona(): Persona | null {
  const id = mockPersonaId();
  return id ? (PERSONAS[id] ?? null) : null;
}

const bytes = (v: unknown) => new TextEncoder().encode(JSON.stringify(v) ?? "").length;
const clone = <T,>(v: T): T => structuredClone(v);
const now = () => new Date().toISOString();

// ----------------------------------------------------------------------------- catalog lookups

const MODELS = new Map((CATALOG.models ?? []).map((m) => [m.manifest.id, m]));
const STEPS = new Map((CATALOG.steps ?? []).map((s) => [s.id, s]));
const PLOTS = new Map((CATALOG.plots ?? []).map((p) => [p.id, p]));

interface Examples {
  plots: Record<string, PlotSpec>;
}

let examples: Promise<Examples> | null = null;
/** The catalog examples (loaded on first use: the file is large). */
function loadExamples(): Promise<Examples> {
  examples ??= import("./plot-examples.gen.json").then((m) => m.default as unknown as Examples);
  return examples;
}

async function exampleOf(recipeRef: string): Promise<PlotSpec | null> {
  const [id] = splitUseRef(recipeRef);
  const entry = PLOTS.get(id);
  if (!entry) return null;
  const ex = (await loadExamples()).plots[`${id}@${entry.version ?? "1.0.0"}`];
  return ex ? clone(ex) : null;
}

/** `acme.winsorize`, the step the `acme-mapi-extras` distribution installs (with `mapi.mock.extensions=1`). */
function pluginStep(): StepCatalogEntry {
  const clip = STEPS.get("core.clip");
  return {
    ...(clip ? clone(clip) : { kind: "value", stateful: false }),
    id: "acme.winsorize",
    version: "0.3.0",
    provider: MOCK_PLUGIN_DIST,
    summary: "Caps each column at its lower and upper percentiles.",
    summary_plain: "Caps each column at its lower and upper percentiles.",
    about: {
      ...(clip?.about ?? {}),
      title: "Winsorize at percentiles",
      summary_plain: "Changes values below the 1st and above the 99th percentile to those percentiles. Installed by your team.",
      related: ["core.clip"],
    },
  } as StepCatalogEntry;
}

/** The catalog the mock serves: the real one, plus the team's plugin step when opted in. */
export function mockCatalog(): CatalogResponse {
  if (read(MOCK_EXTENSIONS_KEY) !== "1") return CATALOG;
  return { ...CATALOG, steps: [...(CATALOG.steps ?? []), pluginStep()] };
}

export const MOCK_DIAGNOSTICS: CatalogDiagnostic[] = [
  {
    code: "schema_unsafe",
    group: "mapi.steps",
    entry_point: "winsorize",
    distribution: MOCK_PLUGIN_DIST,
    id: "acme.winsorize",
    message: "A pattern in the settings schema could make the server slow. The pattern was removed; the step still works.",
    untrusted_text: "properties.column.pattern = (a+)+$",
  },
  {
    code: "load_error",
    group: "mapi.plots",
    entry_point: "acme_bands",
    distribution: MOCK_PLUGIN_DIST,
    id: null,
    message: "The entry point did not load. Its plot recipes are not in the catalog.",
    untrusted_text: "ModuleNotFoundError: No module named 'acme_extras.bands' <script>alert(1)</script>",
  },
  {
    code: "namespace_reserved",
    group: "mapi.models",
    entry_point: "quick_map",
    distribution: "lab-tools",
    id: "sci.quick_map",
    message: "The namespace sci is reserved for built-in plugins. The model was not loaded.",
    untrusted_text: null,
  },
];

// ----------------------------------------------------------------------------- validation

interface Ctx {
  tenant: string;
  project: string;
}

const issue = (pointer: string, message: string, code: Issue["code"] = "LIBRARY_INVALID", severity: Issue["severity"] = "error"): Issue => ({ code, message, pointer, severity });

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

const SLUG_RE = /^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/;
const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

function metadataIssues(doc: Record<string, unknown>): Issue[] {
  const out: Issue[] = [];
  const md = doc.metadata;
  if (!isObj(md)) return [issue("/metadata", "Give the item a name and a title.")];
  if (typeof md.name !== "string" || !SLUG_RE.test(md.name)) out.push(issue("/metadata/name", "Use lower-case letters, digits, '-' or '_' (at most 64 characters)."));
  if (typeof md.title !== "string" || !md.title.trim() || md.title.length > 120) out.push(issue("/metadata/title", "Give a title of 1 to 120 characters."));
  if (typeof md.summary === "string" && md.summary.length > 300) out.push(issue("/metadata/summary", "Use at most 300 characters."));
  if (Array.isArray(md.tags) && md.tags.length > 8) out.push(issue("/metadata/tags", "Use at most 8 tags."));
  return out;
}

function priorIssues(p: unknown, ptr: string): Issue[] {
  if (!isObj(p)) return [issue(ptr, "Choose a prior.")];
  const out: Issue[] = [];
  if (p.dist !== undefined && !(PRIOR_DISTS as readonly unknown[]).includes(p.dist)) out.push(issue(`${ptr}/dist`, `Choose one of: ${PRIOR_DISTS.join(", ")}.`));
  if (p.sigma !== undefined && !(typeof p.sigma === "number" && p.sigma > 0 && p.sigma <= 1e6)) out.push(issue(`${ptr}/sigma`, "The scale must be more than 0 and at most 1000000."));
  if (p.mu !== undefined && !(typeof p.mu === "number" && Number.isFinite(p.mu))) out.push(issue(`${ptr}/mu`, "The location must be a finite number."));
  if (p.nu != null && !(typeof p.nu === "number" && p.nu > 0 && p.nu <= 1000)) out.push(issue(`${ptr}/nu`, "The degrees of freedom must be more than 0 and at most 1000."));
  return out;
}

/** The variables and dims of a dataset the mock knows (the fixture dataset), else `null` (no data checks). */
function datasetNames(dataset: string | null | undefined): Set<string> | null {
  if (!dataset || !dataset.includes(fx.dataset.name)) return null;
  const schema = fx.datasetVersion.data_schema as { vars?: Record<string, unknown>; dims?: Record<string, unknown> } | null | undefined;
  return new Set([...Object.keys(schema?.vars ?? {}), ...Object.keys(schema?.dims ?? {})]);
}

function declarativeIssues(m: unknown, names: Set<string> | null): Issue[] {
  const base = "/declarative";
  if (!isObj(m)) return [issue(base, "A declarative model needs an outcome and at least one driver.")];
  const out: Issue[] = [];
  const known = (v: unknown, ptr: string) => {
    if (typeof v !== "string" || !NAME_RE.test(v)) out.push(issue(ptr, "Pick a column."));
    else if (names && !names.has(v)) out.push(issue(ptr, `The dataset has no column "${v}".`, "LIBRARY_DATA_SHAPE"));
  };
  const outcome = m.outcome;
  if (!isObj(outcome)) out.push(issue(`${base}/outcome`, "Pick what the model explains."));
  else {
    known(outcome.var, `${base}/outcome/var`);
    if (outcome.family !== undefined && !(GLM_FAMILIES as readonly unknown[]).includes(outcome.family)) out.push(issue(`${base}/outcome/family`, "Pick a kind of outcome from the list."));
    if (outcome.family === "ordinal" && typeof outcome.n_categories !== "number") out.push(issue(`${base}/outcome/n_categories`, "A rating needs the number of categories (2 to 50)."));
  }
  const groups = Array.isArray(m.groups) ? m.groups : [];
  if (groups.length > 3) out.push(issue(`${base}/groups`, "Use at most 3 groups."));
  const groupDims = new Set<string>();
  groups.forEach((g, i) => {
    if (!isObj(g)) return;
    known(g.dim, `${base}/groups/${i}/dim`);
    if (typeof g.dim === "string") groupDims.add(g.dim);
    if (g.sigma_prior !== undefined) out.push(...priorIssues(g.sigma_prior, `${base}/groups/${i}/sigma_prior`));
  });
  const terms = Array.isArray(m.terms) ? m.terms : [];
  if (!terms.length) out.push(issue(`${base}/terms`, "Add at least one driver."));
  if (terms.length > 64) out.push(issue(`${base}/terms`, "Use at most 64 drivers."));
  const termVars = new Set<string>();
  terms.forEach((t, i) => {
    const ptr = `${base}/terms/${i}`;
    if (!isObj(t)) return void out.push(issue(ptr, "A driver needs a column and a prior."));
    known(t.var, `${ptr}/var`);
    if (typeof t.var === "string") {
      if (termVars.has(t.var)) out.push(issue(`${ptr}/var`, `"${t.var}" is already a driver.`));
      termVars.add(t.var);
      if (isObj(outcome) && outcome.var === t.var) out.push(issue(`${ptr}/var`, "A driver cannot be the outcome."));
    }
    out.push(...priorIssues(t.prior, `${ptr}/prior`));
    if (t.sign !== undefined && !(SIGNS as readonly unknown[]).includes(t.sign)) out.push(issue(`${ptr}/sign`, "Pick any, positive or negative."));
    const tf = Array.isArray(t.transform) ? t.transform : [];
    if (tf.length > 3) out.push(issue(`${ptr}/transform`, "Use at most 3 transforms."));
    tf.forEach((c, j) => {
      if (!isObj(c) || !(TRANSFORM_USES as readonly unknown[]).includes(c.use)) out.push(issue(`${ptr}/transform/${j}/use`, "Pick a transform from the list."));
    });
    if (t.varies_by != null && (typeof t.varies_by !== "string" || !groupDims.has(t.varies_by))) {
      out.push(issue(`${ptr}/varies_by`, "Add this group under Groups first."));
    }
  });
  const inter = Array.isArray(m.interactions) ? m.interactions : [];
  if (inter.length > 16) out.push(issue(`${base}/interactions`, "Use at most 16 interactions."));
  inter.forEach((x, i) => {
    if (!isObj(x)) return;
    for (const side of ["a", "b"] as const) {
      if (typeof x[side] !== "string" || !termVars.has(x[side])) out.push(issue(`${base}/interactions/${i}/${side}`, "Pick one of the drivers."));
    }
  });
  if (m.intercept !== undefined) out.push(...priorIssues(m.intercept, `${base}/intercept`));
  if (m.noise !== undefined) out.push(...priorIssues(m.noise, `${base}/noise`));
  return out;
}

function modelRecipeIssues(doc: Record<string, unknown>, names: Set<string> | null): Issue[] {
  const base = doc.base ?? "declarative";
  if (base === "declarative") return declarativeIssues(doc.declarative, names);
  if (typeof base !== "string") return [issue("/base", "Pick a model.")];
  const [id] = splitUseRef(base);
  const model = MODELS.get(id);
  if (!model) return [issue("/base", `The model ${id} is not in this catalog.`, "EXTENSION_UNAVAILABLE")];
  const props = Object.keys((model.manifest.settings_schema as { properties?: Record<string, unknown> } | undefined)?.properties ?? {});
  const out: Issue[] = [];
  const exposed = Array.isArray(doc.exposed) ? doc.exposed : [];
  if (exposed.length > 12) out.push(issue("/exposed", "Show at most 12 settings."));
  exposed.forEach((k, i) => !props.includes(String(k)) && out.push(issue(`/exposed/${i}`, `${id} has no setting "${String(k)}".`)));
  for (const k of Object.keys(isObj(doc.settings) ? doc.settings : {})) {
    if (!props.includes(k)) out.push(issue(`/settings/${k}`, `${id} has no setting "${k}".`));
  }
  if (doc.declarative != null) out.push(issue("/declarative", "A preset has no declarative model. Remove it or set base to declarative."));
  return out;
}

function prepIssues(doc: Record<string, unknown>, names: Set<string> | null): Issue[] {
  const out: Issue[] = [];
  const slots = isObj(doc.slots) ? doc.slots : {};
  if (Object.keys(slots).length > 16) out.push(issue("/slots", "Use at most 16 slots."));
  const steps = Array.isArray(doc.steps) ? doc.steps : [];
  if (!steps.length) out.push(issue("/steps", "Add at least one step."));
  const ids = new Set<string>();
  steps.forEach((s, i) => {
    const ptr = `/steps/${i}`;
    if (!isObj(s)) return void out.push(issue(ptr, "A step needs an id and a step type."));
    if (typeof s.id !== "string" || !NAME_RE.test(s.id)) out.push(issue(`${ptr}/id`, "Give the step an id (letters, digits, '_')."));
    else if (ids.has(s.id)) out.push(issue(`${ptr}/id`, `Another step has the id "${s.id}".`));
    else ids.add(s.id);
    const [use] = splitUseRef(typeof s.use === "string" ? s.use : "");
    if (!STEPS.has(use) && !(use === "acme.winsorize" && read(MOCK_EXTENSIONS_KEY) === "1")) {
      out.push(issue(`${ptr}/use`, `The step ${use || "(none)"} is not in this catalog.`, "EXTENSION_UNAVAILABLE"));
    }
    const walk = (v: unknown, p: string) => {
      const slot = slotName(v);
      if (slot !== null) {
        if (!(slot in slots)) out.push(issue(p, `The slot "${slot}" is not declared under slots.`));
      } else if (Array.isArray(v)) v.forEach((x, j) => walk(x, `${p}/${j}`));
      else if (typeof v === "string" && names && /\/with\/vars\/\d+$/.test(p) && !names.has(v)) {
        out.push(issue(p, `The dataset has no column "${v}".`, "LIBRARY_DATA_SHAPE"));
      }
    };
    if (isObj(s.with)) for (const [k, v] of Object.entries(s.with)) walk(v, `${ptr}/with/${k}`);
  });
  return out;
}

const FORBIDDEN_PLOT_KEYS = new Set(["usermeta", "href", "loader", "url"]);

async function chartIssues(doc: Record<string, unknown>): Promise<Issue[]> {
  const out: Issue[] = [];
  const ref = typeof doc.source_recipe === "string" ? doc.source_recipe : "";
  const [id] = splitUseRef(ref);
  if (!PLOTS.has(id)) out.push(issue("/source_recipe", `The chart type ${id || "(none)"} is not in this catalog.`, "EXTENSION_UNAVAILABLE"));
  if (!isObj(doc.plot)) return [...out, issue("/plot", "Give the Vega-Lite spec of the chart.")];
  if (bytes(doc.plot) > 65_536) out.push(issue("/plot", "The Vega-Lite spec is larger than 64 KiB."));
  const walk = (v: unknown, p: string, depth: number) => {
    if (depth > 64) return;
    if (Array.isArray(v)) return v.forEach((x, i) => walk(x, `${p}/${i}`, depth + 1));
    if (!isObj(v)) return;
    for (const [k, x] of Object.entries(v)) {
      if (FORBIDDEN_PLOT_KEYS.has(k)) out.push(issue(`${p}/${k}`, `A chart cannot use "${k}": it could load content from outside. Remove it.`, "LIBRARY_INVALID"));
      else walk(x, `${p}/${k}`, depth + 1);
    }
  };
  walk(doc.plot, "/plot", 0);
  if (typeof doc.caption_plain === "string" && doc.caption_plain.length > 300) out.push(issue("/caption_plain", "Use at most 300 characters."));
  const ex = id && PLOTS.has(id) ? await exampleOf(ref) : null;
  if (ex) {
    const have = new Set(Object.keys(ex.datasets ?? {}));
    for (const name of datasetNamesOf(doc.plot)) {
      if (!have.has(name)) out.push(issue("/plot", `The chart reads the dataset "${name}", but ${id} has only: ${[...have].join(", ") || "none"}.`));
    }
  }
  if (doc.bindings != null) out.push(issue("/bindings", "Role bindings apply on a real model, not on the example.", "LIBRARY_INVALID", "info"));
  return out;
}

function workflowIssues(doc: Record<string, unknown>): Issue[] {
  const d = doc.document;
  if (!isObj(d)) return [issue("/document", "Give the workflow document.")];
  const out: Issue[] = [];
  if (d.kind !== "Workflow") out.push(issue("/document/kind", 'The document must be "kind: Workflow".'));
  if (!isObj(d.metadata)) out.push(issue("/document/metadata", "The workflow needs metadata with a name."));
  return out;
}

/** The issues of a document (pointers into it), as `POST /v1/library:validate` gives them. */
export async function validateDoc(doc: unknown, dataset?: string | null): Promise<{ kind: LibraryKind | null; issues: Issue[] }> {
  if (!isObj(doc)) return { kind: null, issues: [issue("", "The document must be a JSON object.")] };
  const kind = (({ ModelRecipe: "model_recipe", PrepPreset: "prep_preset", ChartTemplate: "chart_template", WorkflowTemplate: "workflow_template" }) as Record<string, LibraryKind>)[String(doc.kind)] ?? null;
  if (!kind) return { kind: null, issues: [issue("/kind", "Use ModelRecipe, PrepPreset, ChartTemplate or WorkflowTemplate.")] };
  const issues: Issue[] = [];
  if (doc.apiVersion !== "mapi/v1") issues.push(issue("/apiVersion", 'Use "apiVersion: mapi/v1".'));
  issues.push(...metadataIssues(doc));
  if (bytes(doc) > DOC_MAX_BYTES) issues.push(issue("", "The document is larger than 256 KiB."));
  const names = datasetNames(dataset);
  if (kind === "model_recipe") issues.push(...modelRecipeIssues(doc, names));
  else if (kind === "prep_preset") issues.push(...prepIssues(doc, names));
  else if (kind === "chart_template") issues.push(...(await chartIssues(doc)));
  else issues.push(...workflowIssues(doc));
  return { kind, issues };
}

const hasErrors = (issues: Issue[]) => issues.some((i) => (i.severity ?? "error") === "error");

// ----------------------------------------------------------------------------- compiling

function prior(p: PriorSpec | undefined, defaults: PriorSpec): JsonObject {
  const q = { ...defaults, ...(p ?? {}) };
  const out: JsonObject = { dist: q.dist ?? "normal", mu: q.mu ?? 0, sigma: q.sigma ?? 1 };
  if (q.nu != null) out.nu = q.nu;
  return out;
}

/** `mapi_spec.model_recipes.declarative_settings`. */
export function declarativeSettings(m: DeclarativeModel): JsonObject {
  const out: JsonObject = { family: m.outcome.family ?? "normal" };
  if (m.outcome.n_categories != null) out.n_categories = m.outcome.n_categories;
  const terms: JsonObject = {};
  for (const t of m.terms) terms[t.var] = { ...prior(t.prior, {}), sign: t.sign ?? "any", ...(t.varies_by ? { varies_by: t.varies_by } : {}) };
  out.covariates = m.terms.map((t) => t.var);
  out.groups = (m.groups ?? []).map((g) => g.dim);
  out.terms = terms;
  out.interactions = (m.interactions ?? []).map((x) => ({ a: x.a, b: x.b, prior: prior(x.prior, { sigma: 0.5 }) }));
  out.intercept = prior(m.intercept, { sigma: 2 });
  out.group_sigma = Object.fromEntries((m.groups ?? []).map((g) => [g.dim, prior(g.sigma_prior, { dist: "half_normal" })]));
  out.noise = prior(m.noise, { dist: "half_normal" });
  return out;
}

/** The transform steps of a declarative model: `t<n>_<use>`, identical transforms merged. */
function transformSteps(m: DeclarativeModel): JsonObject[] {
  const steps: { id: string; use: string; with: JsonObject; key: string }[] = [];
  let n = 0;
  for (const t of m.terms) {
    for (const call of t.transform ?? []) {
      const key = `${call.use}|${JSON.stringify(call.with ?? {})}`;
      const same = steps.find((s) => s.key === key);
      if (same) {
        (same.with.vars as Json[]).push(t.var);
        continue;
      }
      steps.push({ id: `t${n++}_${call.use}`, use: `core.${call.use}@^1`, with: { ...(call.with ?? {}), vars: [t.var] }, key });
    }
  }
  return steps.map(({ id, use, with: w }) => ({ id, use, with: w }));
}

function compileModelRecipe(doc: ModelRecipeDoc, label: string, exposed: JsonObject = {}): JsonObject {
  const base = doc.base ?? "declarative";
  const labels = { [LABEL_MODEL]: label };
  let out: JsonObject;
  if (base === "declarative" && doc.declarative) {
    out = { pipeline: transformSteps(doc.declarative), model: { use: `${DECLARATIVE_MODEL_ID}@^1`, settings: declarativeSettings(doc.declarative) } };
  } else {
    const settings: JsonObject = { ...(doc.settings ?? {}) };
    for (const k of doc.exposed ?? []) if (k in exposed) settings[k] = exposed[k] ?? null;
    out = { pipeline: [], model: { use: base, settings } };
  }
  if (doc.plots?.length) out.outputs = { plots: doc.plots };
  out.metadata = { labels };
  return out;
}

/** Fill a prep preset's slots (`"$slot:x"` whole values; a list slot is spliced into a list). */
function applyPrep(doc: PrepPresetDoc, slots: JsonObject): { pipeline: JsonObject[]; issues: Issue[] } {
  const issues: Issue[] = [];
  const fill = (v: Json, ptr: string): Json => {
    const name = slotName(v);
    if (name !== null) {
      if (!(name in slots)) {
        if (doc.slots?.[name]?.required !== false) issues.push(issue(ptr, `Fill the slot "${doc.slots?.[name]?.label ?? name}".`));
        return null;
      }
      return slots[name] ?? null;
    }
    if (Array.isArray(v)) {
      const out: Json[] = [];
      v.forEach((x, i) => {
        const filled = fill(x, `${ptr}/${i}`);
        if (slotName(x) !== null && Array.isArray(filled)) out.push(...filled);
        else out.push(filled);
      });
      return out;
    }
    return v;
  };
  const pipeline = doc.steps.map((s, i) => {
    const w: JsonObject = {};
    for (const [k, v] of Object.entries(s.with ?? {})) w[k] = fill(v, `/steps/${i}/with/${k}`);
    return { ...s, id: `p_${s.id}`, with: w };
  });
  return { pipeline, issues };
}

async function drawChart(doc: ChartTemplateDoc, author: string): Promise<{ plot: PlotSpec | null; issues: Issue[] }> {
  const issues = await chartIssues(doc as unknown as Record<string, unknown>);
  if (hasErrors(issues)) return { plot: null, issues };
  const ex = await exampleOf(doc.source_recipe);
  if (!ex) return { plot: null, issues: [...issues, issue("/source_recipe", "This chart type has no example to draw on.", "MODE_NOT_SUPPORTED")] };
  const schema = typeof (ex.spec as Record<string, unknown>).$schema === "string" ? { $schema: (ex.spec as Record<string, unknown>).$schema as string } : {};
  const plot: PlotSpec = {
    ...ex,
    id: `lib.${doc.metadata.name}`,
    title: doc.metadata.title,
    caption_plain: doc.caption_plain || ex.caption_plain || null,
    spec: { ...schema, ...clone(doc.plot) },
    provenance: { author, generated_at: now() },
  };
  return { plot, issues };
}

// ----------------------------------------------------------------------------- state

interface ItemRecord {
  item: LibraryItem;
  tenant: string;
  /** `null` for a tenant item. */
  project: string | null;
  revisions: LibraryRevision[];
}

interface PreviewRun {
  key: string;
  readyAt: number;
  result: RecipePreviewResult;
}

export interface LibraryMockState {
  items: Map<string, ItemRecord>;
  seq: number;
  /** Recipe previews: cache by key, runs by id, request times per persona (rate limit). */
  previews: Map<string, RecipePreviewResult>;
  previewRuns: Map<string, PreviewRun>;
  previewCalls: Map<string, number[]>;
}

const ID_CHARS = "0123456789abcdefghjkmnpqrstvwxyz";

function newId(state: LibraryMockState, prefix: string): string {
  state.seq += 1;
  let n = state.seq;
  let tail = "";
  for (let i = 0; i < 8; i++) {
    tail = (ID_CHARS[n % 32] ?? "0") + tail;
    n = Math.floor(n / 32);
  }
  return `${prefix}01jaf3l1b2r3y4m5n6${tail}`;
}

function makeRevision(rec: Pick<ItemRecord, "item">, doc: LibraryDoc, revision: number, issues: Issue[], compiled: JsonObject | null, by: string, message: string | null, at: string): LibraryRevision {
  return {
    item_id: rec.item.id,
    revision,
    document: clone(doc) as unknown as LibraryRevision["document"],
    doc_hash: `sha256:${(revision * 7919 + rec.item.id.length).toString(16).padStart(64, "0")}`,
    valid: !hasErrors(issues),
    issues,
    compiled: compiled,
    catalog_digest: CATALOG.digest,
    created_by: by,
    created_at: at,
    message,
  };
}

async function compiledOf(doc: LibraryDoc, label: string): Promise<JsonObject | null> {
  const kind = libraryKindOf(doc);
  if (kind === "model_recipe") return compileModelRecipe(doc as ModelRecipeDoc, label);
  if (kind === "prep_preset") return { pipeline: applyPrep(doc as PrepPresetDoc, {}).pipeline };
  if (kind === "workflow_template") return { document: clone((doc as WorkflowTemplateDoc).document) };
  if (kind === "chart_template") {
    const { plot } = await drawChart(doc as ChartTemplateDoc, label);
    return plot ? ({ plot: { id: plot.id, title: plot.title } }) : null;
  }
  return null;
}

interface AddInput {
  tenant: string;
  project: string | null;
  doc: LibraryDoc;
  by: string;
  message?: string | null;
  issues?: Issue[];
  archived?: boolean;
  at?: string;
}

function addItemSync(state: LibraryMockState, input: AddInput, compiled: JsonObject | null): ItemRecord {
  const kind = libraryKindOf(input.doc) ?? "model_recipe";
  const at = input.at ?? now();
  const item: LibraryItem = {
    id: newId(state, "lib_"),
    kind,
    scope: input.project === null ? "tenant" : "project",
    project_id: input.project,
    name: input.doc.metadata.name,
    title: input.doc.metadata.title,
    summary: input.doc.metadata.summary ?? "",
    tags: input.doc.metadata.tags ?? [],
    latest_revision: 1,
    archived: input.archived ?? false,
    created_by: input.by,
    created_at: at,
    updated_at: at,
    etag: '"r1"',
  };
  const rec: ItemRecord = { item, tenant: input.tenant, project: input.project, revisions: [] };
  rec.revisions.push(makeRevision(rec, input.doc, 1, input.issues ?? [], compiled, input.by, input.message ?? null, at));
  state.items.set(item.id, rec);
  return rec;
}

function reviseSync(rec: ItemRecord, doc: LibraryDoc, issues: Issue[], compiled: JsonObject | null, by: string, message: string | null, at: string = now()): LibraryRevision {
  const revision = rec.item.latest_revision + 1;
  const rev = makeRevision(rec, doc, revision, issues, compiled, by, message, at);
  rec.revisions.push(rev);
  rec.item = { ...rec.item, latest_revision: revision, name: doc.metadata.name, title: doc.metadata.title, summary: doc.metadata.summary ?? "", tags: doc.metadata.tags ?? [], updated_at: at, etag: `"r${revision}"` };
  return rev;
}

// ----------------------------------------------------------------------------- fixtures

const META = (name: string, title: string, summary: string, tags: string[] = []) => ({ name, title, summary, tags });

export const FIXTURE_DOCS: {
  model: ModelRecipeDoc;
  preset: ModelRecipeDoc;
  prep: PrepPresetDoc;
  chart: ChartTemplateDoc;
  oldChart: ChartTemplateDoc;
  brokenPrep: PrepPresetDoc;
} = {
  model: {
    apiVersion: "mapi/v1",
    kind: "ModelRecipe",
    metadata: { ...META("revenue-by-market", "Revenue by market", "How revenue moves with media spend, market by market. Spend can only add revenue.", ["mmm", "starter"]), about: { title: "Revenue by market", summary_plain: "A simple model you can read: spend lifts revenue, and each market has its own level and its own lift.", use_when: ["You want a first, readable model of weekly revenue"], avoid_when: ["Spend has long carry-over effects (use the MMM)"] } },
    base: "declarative",
    declarative: {
      outcome: { var: "revenue_local", family: "normal" },
      terms: [{ var: "spend_local", label: "Media spend", transform: [{ use: "log1p" }, { use: "standardize" }], prior: { dist: "half_normal", sigma: 0.5 }, sign: "positive", varies_by: "market" }],
      groups: [{ dim: "market" }],
    },
    plots: ["core.ppc_fan@^1", "core.posterior_forest@^1"],
  },
  preset: {
    apiVersion: "mapi/v1",
    kind: "ModelRecipe",
    metadata: META("sarimax-weekly", "Weekly SARIMAX with a yearly season", "Our usual time-series setup. You can change the order and the trend.", ["forecast"]),
    base: "ts.sarimax@^1",
    settings: { order: [1, 1, 1], seasonal_order: [1, 0, 0, 52], trend: "auto" },
    exposed: ["order", "trend"],
  },
  prep: {
    apiVersion: "mapi/v1",
    kind: "PrepPreset",
    metadata: META("weekly-totals-log", "Weekly totals, then log", "Add up each week, then take log(1 + x) of the columns you pick. <b>Markup here is text.</b>", ["prep"]),
    slots: { time: { kind: "dim", label: "Date or week" }, skewed: { kind: "var_list", label: "Columns to log" } },
    steps: [
      { id: "weekly", use: "core.resample@^1", with: { dim: "$slot:time", freq: "W-SUN", how: "sum", full_periods_only: true } },
      { id: "log", use: "core.log1p@^1", with: { vars: ["$slot:skewed"] } },
    ],
  },
  chart: {
    apiVersion: "mapi/v1",
    kind: "ChartTemplate",
    metadata: META("fit-with-band", "Fit with a 90% band", "The model's line, its 90% band and the actual values, one panel per group.", ["fit"]),
    source_recipe: "core.ppc_fan@^1",
    plot: {
      data: { name: "fan" },
      facet: { field: "group", type: "nominal", title: null },
      columns: 2,
      spec: {
        width: 280,
        height: 160,
        layer: [
          { mark: { type: "area", opacity: 0.25 }, encoding: { x: { field: "x", type: "temporal", title: null }, y: { field: "lo_90", type: "quantitative", title: null }, y2: { field: "hi_90" } } },
          { mark: { type: "line" }, encoding: { x: { field: "x", type: "temporal" }, y: { field: "point", type: "quantitative" } } },
          { mark: { type: "point", size: 12, filled: true }, encoding: { x: { field: "x", type: "temporal" }, y: { field: "observed", type: "quantitative" } } },
        ],
      },
    },
    caption_plain: "The band holds 90% of the model's predictions. Dots are the actual values.",
  },
  oldChart: {
    apiVersion: "mapi/v1",
    kind: "ChartTemplate",
    metadata: META("plain-fan", "Plain fan (old)", "The first try at a fan chart.", []),
    source_recipe: "core.ppc_fan@^1",
    plot: { data: { name: "fan" }, mark: "line", encoding: { x: { field: "x", type: "temporal" }, y: { field: "point", type: "quantitative" } } },
    caption_plain: "",
  },
  brokenPrep: {
    apiVersion: "mapi/v1",
    kind: "PrepPreset",
    metadata: META("cap-outliers", "Cap outliers at percentiles", "Uses the team's winsorize step.", ["prep"]),
    slots: { cols: { kind: "var_list", label: "Columns to cap" } },
    steps: [{ id: "cap", use: "acme.winsorize@^0", with: { vars: ["$slot:cols"] } }],
  },
};

function workflowFixture(): WorkflowTemplateDoc {
  const t = TEMPLATES[0];
  const document = clone((t?.document ?? { apiVersion: "mapi/v1", kind: "Workflow", metadata: { name: "readiness-check" } }) as unknown as JsonObject);
  return {
    apiVersion: "mapi/v1",
    kind: "WorkflowTemplate",
    metadata: META("readiness-check", "Readiness check before a forecast", "The team's checks before we trust a time-series forecast.", ["forecast", "checks"]),
    document,
  };
}

/** The three documents a ready sample adds to its project (`mapi_spec.samples.library_seed`). */
export const SAMPLE_SEED_DOCS: LibraryDoc[] = [
  {
    apiVersion: "mapi/v1",
    kind: "ModelRecipe",
    metadata: { ...META("revenue-and-price", "Revenue and price, region by region", "A simple model you can read: how revenue moves with price in each region."), tags: ["sample"] },
    base: "declarative",
    declarative: {
      outcome: { var: "revenue_gbp", family: "normal" },
      terms: [{ var: "avg_price", label: "Average price", transform: [{ use: "standardize" }], prior: { dist: "normal", sigma: 0.5 }, sign: "negative", varies_by: "region" }],
      groups: [{ dim: "region" }],
    },
  },
  { ...FIXTURE_DOCS.prep, metadata: { ...FIXTURE_DOCS.prep.metadata, summary: "Add up each week, then take log(1 + x) of the columns you pick.", tags: ["sample"] } },
  { ...FIXTURE_DOCS.chart, metadata: { ...FIXTURE_DOCS.chart.metadata, tags: ["sample"] } },
];

export function createLibraryState(): LibraryMockState {
  const state: LibraryMockState = { items: new Map(), seq: 0, previews: new Map(), previewRuns: new Map(), previewCalls: new Map() };
  const ana = PERSONAS.ana?.id ?? "usr_ana";
  const growth = "growth";
  const T = (h: number) => new Date(Date.parse(fx.T0) - h * 3600_000).toISOString();
  const d = FIXTURE_DOCS;

  // A declarative model recipe with two revisions (the first had a wider prior).
  const v1 = clone(d.model);
  v1.declarative!.terms[0]!.prior = { dist: "half_normal", sigma: 1 };
  const model = addItemSync(state, { tenant: "acme", project: growth, doc: v1, by: ana, message: "first version", at: T(72) }, compileModelRecipe(v1, "lib_r1"));
  const r2Label = libraryLabel(model.item.id, 2);
  reviseSync(model, clone(d.model), [], compileModelRecipe(clone(d.model), r2Label), ana, "a tighter prior on spend", T(30));
  model.revisions[0]!.compiled = compileModelRecipe(v1, libraryLabel(model.item.id, 1));

  const preset = addItemSync(state, { tenant: "acme", project: growth, doc: clone(d.preset), by: ana, at: T(48) }, null);
  preset.revisions[0]!.compiled = compileModelRecipe(d.preset, libraryLabel(preset.item.id, 1));
  const prep = addItemSync(state, { tenant: "acme", project: growth, doc: clone(d.prep), by: ana, at: T(40) }, null);
  prep.revisions[0]!.compiled = { pipeline: applyPrep(d.prep, {}).pipeline };
  const chart = addItemSync(state, { tenant: "acme", project: growth, doc: clone(d.chart), by: ana, at: T(20) }, null);
  chart.revisions[0]!.compiled = { plot: { id: `lib.${d.chart.metadata.name}`, title: d.chart.metadata.title } };
  addItemSync(state, { tenant: "acme", project: growth, doc: clone(d.oldChart), by: ana, archived: true, at: T(200) }, null);
  addItemSync(
    state,
    {
      tenant: "acme",
      project: growth,
      doc: clone(d.brokenPrep),
      by: ana,
      at: T(10),
      issues: [issue("/steps/0/use", "The step acme.winsorize is not in this catalog.", "EXTENSION_UNAVAILABLE")],
    },
    null,
  );
  const wf = workflowFixture();
  addItemSync(state, { tenant: "acme", project: null, doc: wf, by: ana, at: T(96) }, { document: clone(wf.document) });
  return state;
}

/** Add the sample's seed items to its project (once; `created_by: system`). */
export function seedSampleLibrary(state: LibraryMockState, tenant: string, project: string): void {
  const exists = [...state.items.values()].some((r) => r.tenant === tenant && r.project === project && r.item.created_by === "system");
  if (exists) return;
  for (const doc of SAMPLE_SEED_DOCS) {
    const rec = addItemSync(state, { tenant, project, doc: clone(doc), by: "system" }, null);
    const kind = rec.item.kind;
    const label = libraryLabel(rec.item.id, 1);
    rec.revisions[0]!.compiled = (
      kind === "model_recipe"
        ? compileModelRecipe(doc as ModelRecipeDoc, label)
        : kind === "prep_preset"
          ? { pipeline: applyPrep(doc as PrepPresetDoc, {}).pipeline }
          : { plot: { id: `lib.${doc.metadata.name}`, title: doc.metadata.title } }
    );
  }
}

// ----------------------------------------------------------------------------- handlers

function scopeOf(request: Request): Ctx | null {
  const header = request.headers.get("X-Mapi-Project");
  if (!header) return null;
  const [tenant, project] = header.split("/");
  return tenant && project ? { tenant, project } : null;
}

function visible(rec: ItemRecord, ctx: Ctx): boolean {
  return rec.tenant === ctx.tenant && (rec.project === null || rec.project === ctx.project);
}

/** Parse the `document` or `text` of a create/revise/validate body (exactly one). */
function bodyDoc(body: { document?: unknown; text?: unknown }): { doc: unknown } | { error: Response } {
  const hasDoc = body.document != null;
  const hasText = typeof body.text === "string";
  if (hasDoc === hasText) return { error: problem(422, "SCHEMA_INVALID", "Invalid request", "Give the document or its text, not both.", { errors: [issue("/document", "Give document or text.", "SCHEMA_INVALID")] }) };
  if (hasDoc) return { doc: body.document };
  try {
    const text = body.text as string;
    if (text.length > DOC_MAX_BYTES) return { error: problem(413, "PAYLOAD_TOO_LARGE", "Payload too large", "The text is larger than 256 KiB.") };
    return { doc: parseYaml(text, { maxAliasCount: 50 }) as unknown };
  } catch (e) {
    return { error: problem(400, "SPEC_PARSE_ERROR", "The document could not be parsed", e instanceof Error ? e.message.split("\n")[0] : "The text is not valid YAML or JSON.") };
  }
}

function withEtag(item: LibraryItem, status = 200) {
  return HttpResponse.json(item, { status, headers: { ETag: item.etag } });
}

function settingsIssues(schema: Record<string, unknown> | null | undefined, settings: Record<string, unknown>): Issue[] {
  if (!schema) return Object.keys(settings).length ? [issue("/settings", "This chart type has no settings.", "SCHEMA_INVALID")] : [];
  const props = (schema.properties ?? {}) as Record<string, Record<string, unknown>>;
  const out: Issue[] = [];
  for (const [k, v] of Object.entries(settings)) {
    const p = props[k];
    const ptr = `/settings/${k}`;
    if (!p) {
      out.push(issue(ptr, `There is no setting "${k}".`, "SCHEMA_INVALID"));
      continue;
    }
    if (p.type === "integer" && !(typeof v === "number" && Number.isInteger(v))) out.push(issue(ptr, "Must be a whole number.", "SCHEMA_INVALID"));
    if (typeof v === "number" && typeof p.minimum === "number" && v < p.minimum) out.push(issue(ptr, `Must be at least ${p.minimum}.`, "SCHEMA_INVALID"));
    if (typeof v === "number" && typeof p.maximum === "number" && v > p.maximum) out.push(issue(ptr, `Must be at most ${p.maximum}.`, "SCHEMA_INVALID"));
    if (Array.isArray(p.enum) && !p.enum.includes(v)) out.push(issue(ptr, `Pick one of: ${p.enum.join(", ")}.`, "SCHEMA_INVALID"));
  }
  return out;
}

function previewMs(): number {
  const v = Number(read(MOCK_PREVIEW_MS_KEY));
  return Number.isFinite(v) && v >= 0 && read(MOCK_PREVIEW_MS_KEY) !== null ? v : 1500;
}

function preferWaitMs(request: Request): number {
  const m = /wait=(\d+)/.exec(request.headers.get("Prefer") ?? "");
  return m ? Math.min(15, Number(m[1])) * 1000 : 0;
}

/** The example of a recipe drawn with `settings` (instant ones applied; `columns` honoured). */
function drawRecipe(ex: PlotSpec, settings: Record<string, unknown>, ref: string): PlotSpec {
  const plot = applyInstantSettings(clone(ex), settings);
  const spec = plot.spec as Record<string, unknown>;
  const { server } = splitSettings(settings);
  if (typeof server.columns === "number" && "columns" in spec) spec.columns = server.columns;
  return { ...plot, provenance: { ...plot.provenance, recipe: ref, generated_at: now() } };
}

export function createLibraryHandlers(state: LibraryMockState): HttpHandler[] {
  const R = (path: string) => new RegExp(`^${API_RE}${path}$`);
  const LIST = R("/v1/library");
  const VALIDATE = R("/v1/library:validate");
  const CHARTS = R("/v1/library/charts:preview");
  const ITEM = R(`/v1/library/${ID}`);
  const REVS = R(`/v1/library/${ID}/revisions`);
  const REV = R(`/v1/library/${ID}/revisions/(\\d+)`);
  const ACTION = R(`/v1/library/${ID}:(archive|unarchive|promote|use)`);
  const PREVIEW = R(`/v1/catalog/plots/${ID}/${ID}:preview`);
  const PREVIEW_RESULT = R(`/v1/catalog/plots/previews/${ID}`);

  const match = (re: RegExp, request: Request) => re.exec(request.url.split("?")[0] ?? "");
  const notFound = () => problem(404, "NOT_FOUND", "Not found", "There is no library item with this id in this project.");
  const find = (id: string, ctx: Ctx) => {
    const rec = isLibraryItemId(id) ? state.items.get(id) : undefined;
    return rec && visible(rec, ctx) ? rec : undefined;
  };
  const needScope = (request: Request): Ctx | Response => scopeOf(request) ?? problem(422, "SCHEMA_INVALID", "Invalid request", "Name the project in X-Mapi-Project.");
  const tenantAdmin = (ctx: Ctx) => persona()?.tenants[ctx.tenant] === "admin";
  const author = () => persona()?.id ?? "usr_unknown";

  return [
    // ------------------------------------------------------------------ items
    http.get(LIST, ({ request }) => {
      const ctx = needScope(request);
      if (ctx instanceof Response) return ctx;
      const url = new URL(request.url);
      const kind = url.searchParams.get("kind");
      const scope = url.searchParams.get("scope");
      const q = (url.searchParams.get("q") ?? "").trim().toLowerCase();
      const archived = url.searchParams.get("include_archived") === "true";
      const items = [...state.items.values()]
        .filter((r) => visible(r, ctx))
        .filter((r) => (!kind || r.item.kind === kind) && (!scope || r.item.scope === scope) && (archived || !r.item.archived))
        .filter((r) => !q || r.item.name.toLowerCase().includes(q) || r.item.title.toLowerCase().includes(q))
        .map((r) => r.item)
        .sort((a, b) => (a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0));
      // v0.21: `limit` (1 to 200, default 50) and `cursor`, as the server pages the list (the
      // global search asks for five).
      const limit = Math.max(1, Math.min(200, Number(url.searchParams.get("limit") ?? 50) || 50));
      const offset = Math.max(0, Number((url.searchParams.get("cursor") ?? "").replace(/^o:/, "")) || 0);
      const end = offset + limit;
      return HttpResponse.json({ items: items.slice(offset, end), next_cursor: end < items.length ? `o:${end}` : null });
    }),
    http.post(LIST, async ({ request }) => {
      const ctx = needScope(request);
      if (ctx instanceof Response) return ctx;
      const body = (await request.json()) as { scope?: string; document?: unknown; text?: unknown; message?: string | null };
      const parsed = bodyDoc(body);
      if ("error" in parsed) return parsed.error;
      const scope = body.scope ?? "project";
      if (scope === "tenant" && !tenantAdmin(ctx)) return problem(403, "FORBIDDEN", "Not allowed", "Only a tenant admin can add items for the whole tenant.");
      const { kind, issues } = await validateDoc(parsed.doc);
      if (!kind) return problem(422, "LIBRARY_INVALID", "The document is not a library item", issues[0]?.message, { errors: issues });
      if (kind === "workflow_template" && hasErrors(issues)) return problem(422, "LIBRARY_INVALID", "The workflow template is not valid", issues[0]?.message, { errors: issues });
      if (metadataIssues(parsed.doc as Record<string, unknown>).length) return problem(422, "LIBRARY_INVALID", "The item needs a name and a title", issues[0]?.message, { errors: issues });
      const doc = parsed.doc as LibraryDoc;
      const project = scope === "tenant" ? null : ctx.project;
      const inTenant = [...state.items.values()].filter((r) => r.tenant === ctx.tenant);
      if (inTenant.length >= MAX_ITEMS) return problem(409, "CONFLICT", "Conflict", `The tenant has ${MAX_ITEMS} library items, the most it can have. Archive is not enough: ask an admin.`);
      if (inTenant.some((r) => r.project === project && r.item.kind === kind && r.item.name === doc.metadata.name)) {
        return problem(409, "CONFLICT", "Conflict", `An item of this kind is already called "${doc.metadata.name}" here. Use another name.`);
      }
      const rec = addItemSync(state, { tenant: ctx.tenant, project, doc, by: author(), message: body.message ?? null, issues }, null);
      if (!hasErrors(issues)) rec.revisions[0]!.compiled = (await compiledOf(doc, libraryLabel(rec.item.id, 1)));
      return withEtag(rec.item, 201);
    }),
    http.get(ITEM, ({ request }) => {
      const ctx = needScope(request);
      if (ctx instanceof Response) return ctx;
      const rec = find(decodeURIComponent(match(ITEM, request)?.[1] ?? ""), ctx);
      return rec ? withEtag(rec.item) : notFound();
    }),
    http.get(REVS, ({ request }) => {
      const ctx = needScope(request);
      if (ctx instanceof Response) return ctx;
      const rec = find(decodeURIComponent(match(REVS, request)?.[1] ?? ""), ctx);
      return rec ? HttpResponse.json({ items: [...rec.revisions].reverse(), next_cursor: null }) : notFound();
    }),
    http.get(REV, ({ request }) => {
      const ctx = needScope(request);
      if (ctx instanceof Response) return ctx;
      const m = match(REV, request);
      const rec = find(decodeURIComponent(m?.[1] ?? ""), ctx);
      const rev = rec?.revisions.find((r) => r.revision === Number(m?.[2]));
      return rev ? HttpResponse.json(rev) : notFound();
    }),
    http.post(REVS, async ({ request }) => {
      const ctx = needScope(request);
      if (ctx instanceof Response) return ctx;
      const rec = find(decodeURIComponent(match(REVS, request)?.[1] ?? ""), ctx);
      if (!rec) return notFound();
      if (rec.project === null && !tenantAdmin(ctx)) return problem(403, "FORBIDDEN", "Not allowed", "Only a tenant admin can change a tenant item.");
      const ifMatch = request.headers.get("If-Match");
      if (ifMatch && ifMatch.replace(/^W\//, "") !== rec.item.etag) {
        return problem(412, "PRECONDITION_FAILED", "Precondition failed", "Someone saved this item after you opened it. Load the new version and make your change again.");
      }
      const body = (await request.json()) as { document?: unknown; text?: unknown; message?: string | null };
      const parsed = bodyDoc(body);
      if ("error" in parsed) return parsed.error;
      const { kind, issues } = await validateDoc(parsed.doc);
      if (kind !== rec.item.kind) return problem(422, "LIBRARY_INVALID", "The kind cannot change", `This item is a ${KIND_DOC[rec.item.kind]}.`, { errors: [issue("/kind", `Use kind: ${KIND_DOC[rec.item.kind]}.`)] });
      if (kind === "workflow_template" && hasErrors(issues)) return problem(422, "LIBRARY_INVALID", "The workflow template is not valid", issues[0]?.message, { errors: issues });
      if (rec.revisions.length >= MAX_REVISIONS) return problem(409, "CONFLICT", "Conflict", `The item has ${MAX_REVISIONS} revisions, the most it can have. Save it as a new item.`);
      const doc = parsed.doc as LibraryDoc;
      const compiled = hasErrors(issues) ? null : await compiledOf(doc, libraryLabel(rec.item.id, rec.item.latest_revision + 1));
      const rev = reviseSync(rec, doc, issues, compiled, author(), body.message ?? null);
      return HttpResponse.json(rev, { status: 201, headers: { ETag: rec.item.etag } });
    }),
    http.post(ACTION, async ({ request }) => {
      const ctx = needScope(request);
      if (ctx instanceof Response) return ctx;
      const m = match(ACTION, request);
      const rec = find(decodeURIComponent(m?.[1] ?? ""), ctx);
      if (!rec) return notFound();
      const action = m?.[2];
      if (action === "archive" || action === "unarchive") {
        if (rec.project === null && !tenantAdmin(ctx)) return problem(403, "FORBIDDEN", "Not allowed", "Only a tenant admin can archive a tenant item.");
        rec.item = { ...rec.item, archived: action === "archive", updated_at: now() };
        return withEtag(rec.item);
      }
      if (action === "promote") {
        if (!tenantAdmin(ctx)) return problem(403, "FORBIDDEN", "Not allowed", "Only a tenant admin can share an item with the whole tenant.");
        if (rec.project === null) return problem(409, "CONFLICT", "Conflict", "The item is already shared with the whole tenant.");
        const latest = rec.revisions.at(-1)!;
        const doc = clone(latest.document) as unknown as LibraryDoc;
        if ([...state.items.values()].some((r) => r.tenant === ctx.tenant && r.project === null && r.item.kind === rec.item.kind && r.item.name === doc.metadata.name)) {
          return problem(409, "CONFLICT", "Conflict", `The tenant already has an item called "${doc.metadata.name}".`);
        }
        const copy = addItemSync(state, { tenant: ctx.tenant, project: null, doc, by: author(), message: `shared from ${rec.item.id} r${latest.revision}`, issues: latest.issues ?? [] }, latest.compiled as JsonObject | null);
        return withEtag(copy.item, 201);
      }
      // :use
      const body = (await request.json().catch(() => ({}))) as { revision?: number | null; dataset?: string | null; slots?: JsonObject; exposed?: JsonObject; model_id?: string | null; model_version?: number | null };
      const rev = body.revision ? rec.revisions.find((r) => r.revision === body.revision) : rec.revisions.at(-1);
      if (!rev) return problem(404, "NOT_FOUND", "Not found", "The item has no such revision.");
      const doc = clone(rev.document) as unknown as LibraryDoc;
      const label = libraryLabel(rec.item.id, rev.revision);
      if (rec.item.kind === "chart_template") {
        if (!body.model_id || body.model_version == null) {
          return problem(422, "SCHEMA_INVALID", "Invalid request", "a chart template is applied to a model version: give model_id and model_version", { errors: [issue(body.model_id ? "/model_version" : "/model_id", "Give the model version.", "SCHEMA_INVALID")] });
        }
        if (body.model_id !== fx.MODEL_ID) return problem(404, "NOT_FOUND", "Not found", "There is no such model version.");
        const { plot, issues } = await drawChart(doc as ChartTemplateDoc, label);
        if (!plot) return problem(422, "LIBRARY_INVALID", "The chart template cannot be used", issues[0]?.message, { errors: issues });
        return HttpResponse.json({ kind: "chart_template", plot, labels: {}, issues } satisfies LibraryUseResult);
      }
      const { issues } = await validateDoc(doc, body.dataset);
      if (hasErrors(issues)) return problem(422, "LIBRARY_INVALID", "The item cannot be used", issues.find((i) => i.severity !== "info")?.message, { errors: issues });
      if (rec.item.kind === "model_recipe") {
        const fragment = compileModelRecipe(doc as ModelRecipeDoc, label, body.exposed ?? {});
        return HttpResponse.json({ kind: "model_recipe", spec_fragment: fragment, labels: { [LABEL_MODEL]: label }, issues } satisfies LibraryUseResult);
      }
      if (rec.item.kind === "prep_preset") {
        const { pipeline, issues: slotIssues } = applyPrep(doc as PrepPresetDoc, body.slots ?? {});
        if (hasErrors(slotIssues)) return problem(422, "LIBRARY_INVALID", "The prep preset cannot be used", slotIssues[0]?.message, { errors: slotIssues });
        return HttpResponse.json({ kind: "prep_preset", pipeline, labels: { [LABEL_PREP]: label }, issues } satisfies LibraryUseResult);
      }
      const wf = clone((doc as WorkflowTemplateDoc).document);
      const md = isObj(wf.metadata) ? (wf.metadata) : {};
      md.project = ctx.project;
      md.labels = { ...(isObj(md.labels) ? (md.labels) : {}), [LABEL_TEMPLATE]: label };
      wf.metadata = md;
      return HttpResponse.json({ kind: "workflow_template", workflow_document: wf, labels: { [LABEL_TEMPLATE]: label }, issues } satisfies LibraryUseResult);
    }),

    // ------------------------------------------------------------------ validate and chart preview
    http.post(VALIDATE, async ({ request }) => {
      const text = await request.text();
      if (text.length > VALIDATE_MAX_BYTES) return problem(413, "PAYLOAD_TOO_LARGE", "Payload too large", "Validate at most 64 KiB at a time.");
      const body = JSON.parse(text || "{}") as { document?: unknown; text?: unknown; dataset?: string | null };
      const parsed = bodyDoc(body);
      if ("error" in parsed) return parsed.error;
      await delay(80);
      const { kind, issues } = await validateDoc(parsed.doc, body.dataset);
      const valid = kind !== null && !hasErrors(issues);
      const compiled = valid ? await compiledOf(parsed.doc as LibraryDoc, "lib_preview") : null;
      return HttpResponse.json({ valid, issues, compiled });
    }),
    http.post(CHARTS, async ({ request }) => {
      const text = await request.text();
      if (text.length > VALIDATE_MAX_BYTES) return problem(413, "PAYLOAD_TOO_LARGE", "Payload too large", "Preview at most 64 KiB at a time.");
      const body = JSON.parse(text || "{}") as { document?: unknown };
      const { kind, issues } = await validateDoc(body.document);
      if (kind !== "chart_template") return HttpResponse.json({ plot: null, issues: kind ? [issue("/kind", "Only a ChartTemplate has a preview.")] : issues });
      if (hasErrors(issues)) return HttpResponse.json({ plot: null, issues });
      const { plot, issues: drawIssues } = await drawChart(body.document as ChartTemplateDoc, "lib_preview");
      return HttpResponse.json({ plot, issues: drawIssues });
    }),

    // ------------------------------------------------------------------ live recipe previews (D21)
    http.post(PREVIEW, async ({ request }) => {
      const ctx = needScope(request);
      if (ctx instanceof Response) return ctx;
      if (read(MOCK_PREVIEW_OFF_KEY) === "1") return problem(409, "PREVIEW_DISABLED", "Live previews are turned off", "Live previews are turned off on this server. Use Apply to draw the chart.");
      const m = match(PREVIEW, request);
      const id = decodeURIComponent(m?.[1] ?? "");
      const version = decodeURIComponent(m?.[2] ?? "");
      const entry = PLOTS.get(id);
      if (!entry || (entry.version ?? "1.0.0") !== version) return problem(404, "NOT_FOUND", "Not found", "There is no such chart type in the catalog.");
      const body = (await request.json().catch(() => ({}))) as { settings?: Record<string, unknown> };
      const settings = body.settings ?? {};
      const errors = settingsIssues(entry.settings_schema, settings);
      if (errors.length) return problem(422, "SCHEMA_INVALID", "The settings are not valid", errors[0]?.message, { errors });
      const ref = `${id}@${version}`;
      const key = `${ref}|${serverSettingsKey(settings)}`;
      const cached = state.previews.get(key);
      if (cached) return HttpResponse.json({ ...cached, cached: true }, { status: 200 });
      const who = persona()?.id ?? "anon";
      const minuteAgo = Date.now() - 60_000;
      const calls = (state.previewCalls.get(who) ?? []).filter((t) => t > minuteAgo);
      if (calls.length >= PREVIEW_PER_MINUTE) {
        const retry = Math.max(1, Math.ceil(((calls[0] ?? Date.now()) + 60_000 - Date.now()) / 1000));
        return problem(429, "RATE_LIMITED", "Too many previews", `You can ask for ${PREVIEW_PER_MINUTE} previews a minute. Try again in ${retry} s.`, { headers: { "Retry-After": String(retry) } });
      }
      calls.push(Date.now());
      state.previewCalls.set(who, calls);
      const ex = await exampleOf(ref);
      const runId = newId(state, "run_");
      const result: RecipePreviewResult = ex
        ? { run_id: runId, cached: false, done: true, plot: drawRecipe(ex, settings, ref), problem: null }
        : { run_id: runId, cached: false, done: true, plot: null, problem: { type: "urn:mapi:error:MODE_NOT_SUPPORTED", title: "No example", status: 422, code: "MODE_NOT_SUPPORTED", detail: "This chart type has no example to draw.", errors: [] } };
      const ms = previewMs();
      state.previewRuns.set(runId, { key, readyAt: Date.now() + ms, result });
      if (preferWaitMs(request) >= ms) {
        await delay(ms);
        state.previews.set(key, result);
        return HttpResponse.json(result, { status: 200 });
      }
      return HttpResponse.json({ run_id: runId, cached: false, done: false, plot: null, problem: null } satisfies RecipePreviewResult, { status: 202 });
    }),
    http.get(PREVIEW_RESULT, ({ request }) => {
      const ctx = needScope(request);
      if (ctx instanceof Response) return ctx;
      const runId = decodeURIComponent(match(PREVIEW_RESULT, request)?.[1] ?? "");
      const run = state.previewRuns.get(runId);
      if (!run) return problem(404, "NOT_FOUND", "Not found", "There is no such preview.");
      if (Date.now() < run.readyAt) return HttpResponse.json({ run_id: runId, cached: false, done: false, plot: null, problem: null } satisfies RecipePreviewResult);
      state.previews.set(run.key, run.result);
      return HttpResponse.json(run.result);
    }),

    // ------------------------------------------------------------------ diagnostics and describe (D12, D14)
    http.get(`${API}/v1/catalog/diagnostics`, () => {
      const p = persona();
      if (!p) return problem(401, "UNAUTHENTICATED", "Authentication required", "sign in to continue");
      const admin = p.operator || Object.values(p.tenants).some((r) => r === "admin");
      if (!admin) return problem(403, "FORBIDDEN", "Not allowed", "Only tenant admins and operators can see the catalog diagnostics.");
      return HttpResponse.json(MOCK_DIAGNOSTICS);
    }),
    http.post(R("/v1/admin/catalog:describe"), () => {
      const p = persona();
      if (!p?.operator) return problem(403, "FORBIDDEN", "Not allowed", "platform operators only");
      return HttpResponse.json({ ...fx.runRunning, id: "run_01jaf3d1e2s3c4r5i6b7e8x9y0", kind: "describe", state: "queued", progress: null, spec_id: null, spec_revision: null }, { status: 202 });
    }),
  ];
}
