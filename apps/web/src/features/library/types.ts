/**
 * The library ("Make your own", v0.18 D1-D9): DTOs of `/v1/library` (generated from the
 * OpenAPI document) and the four document kinds the server stores as JSON
 * (`mapi_spec.library`), with type guards per kind.
 *
 * The documents arrive as `Record<string, JsonValue>`: the guards check only the shape the
 * web relies on (`kind`, `metadata`, the kind's required parts); the server is the
 * validator. Every string in a document is untrusted user text, rendered as React text only.
 *
 * Nothing here runs at load except constant tables; no regular expressions (the
 * `features/library` lint test forbids `RegExp(`).
 */
import type { components } from "@/api/schema";
import type { Issue, PlotSpec } from "@/api/types";

type Schemas = components["schemas"];

// ------------------------------------------------------------------------------ DTOs (I-2, I-5)

export type LibraryItem = Schemas["LibraryItem"];
export type LibraryRevision = Schemas["LibraryRevision"];
export type LibraryCreate = Schemas["LibraryCreate"];
export type LibraryRevisionCreate = Schemas["LibraryRevisionCreate"];
export type LibraryValidateRequest = Schemas["LibraryValidateRequest"];
export type LibraryValidation = Schemas["LibraryValidation"];
export type LibraryUseRequest = Schemas["LibraryUseRequest"];
export type LibraryUseResult = Omit<Schemas["LibraryUseResult"], "plot"> & { plot?: PlotSpec | null };
export type ChartPreviewRequest = Schemas["ChartPreviewRequest"];
export type ChartPreview = Omit<Schemas["ChartPreview"], "plot"> & { plot?: PlotSpec | null };
export type LibraryKind = LibraryItem["kind"];
export type LibraryScope = LibraryItem["scope"];
export type { Issue };

/** Any JSON value (the generated `JsonValue` is `unknown`). */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export const LIBRARY_KINDS = ["model_recipe", "prep_preset", "chart_template", "workflow_template"] as const satisfies readonly LibraryKind[];
export const LIBRARY_SCOPES = ["project", "tenant"] as const satisfies readonly LibraryScope[];

/** `kind` of a document → the item kind (`mapi_spec.library._KIND_OF`). */
export const DOC_KIND_OF = {
  ModelRecipe: "model_recipe",
  PrepPreset: "prep_preset",
  ChartTemplate: "chart_template",
  WorkflowTemplate: "workflow_template",
} as const satisfies Record<string, LibraryKind>;

export type LibraryDocKind = keyof typeof DOC_KIND_OF;

/** The item kind → the document `kind`. */
export const KIND_DOC: Record<LibraryKind, LibraryDocKind> = {
  model_recipe: "ModelRecipe",
  prep_preset: "PrepPreset",
  chart_template: "ChartTemplate",
  workflow_template: "WorkflowTemplate",
};

/** Plain names for the four kinds (UI copy; "recipe" stays the developer term for plots). */
export const KIND_LABEL: Record<LibraryKind, string> = {
  model_recipe: "Model",
  prep_preset: "Prep preset",
  chart_template: "Chart",
  workflow_template: "Workflow template",
};

// ------------------------------------------------------------------------------ caps (I-2)

export const LIBRARY_DOC_MAX_BYTES = 262_144;
export const LIBRARY_VALIDATE_MAX_BYTES = 65_536;
export const MAX_ITEMS_PER_TENANT = 500;
export const MAX_REVISIONS_PER_ITEM = 200;
export const PLOT_SPEC_MAX_BYTES = 65_536;
export const MAX_TERMS = 64;
export const MAX_INTERACTIONS = 16;
export const MAX_GROUPS = 3;
export const MAX_TRANSFORMS_PER_TERM = 3;
export const MAX_EXPOSED = 12;
export const MAX_SLOTS = 16;
export const MAX_TAGS = 8;
export const NAME_MAX = 64;
export const TITLE_MAX = 120;
export const SUMMARY_MAX = 300;

/** Lineage labels a compiled spec or workflow carries (D9): value `lib_<id>-r<n>`. */
export const LABEL_MODEL = "mapi.library/model";
export const LABEL_PREP = "mapi.library/prep";
export const LABEL_TEMPLATE = "mapi.library/template";

/** `lib_<id>-r<n>` (the label value and a chart template's `provenance.author`). */
export function libraryLabel(itemId: string, revision?: number | null): string {
  return revision == null ? itemId : `${itemId}-r${revision}`;
}

const ULID_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz";

/** `^lib_[0-9a-z]{26}$` without a regular expression. */
export function isLibraryItemId(value: unknown): value is string {
  if (typeof value !== "string" || value.length !== 30 || !value.startsWith("lib_")) return false;
  for (const c of value.slice(4)) if (!ULID_CHARS.includes(c)) return false;
  return true;
}

// ------------------------------------------------------------------------------ closed sets (D3)

export const PRIOR_DISTS = ["normal", "student_t", "laplace", "half_normal", "exponential", "lognormal"] as const;
export type PriorDist = (typeof PRIOR_DISTS)[number];

/** Priors that are positive by construction (a sign is implied, `negative` flips them). */
export const POSITIVE_DISTS: ReadonlySet<PriorDist> = new Set(["half_normal", "exponential", "lognormal"]);

export const GLM_FAMILIES = [
  "normal",
  "student_t",
  "poisson",
  "bernoulli",
  "negative_binomial",
  "zero_inflated_poisson",
  "zero_inflated_negative_binomial",
  "beta",
  "gamma",
  "ordinal",
] as const;
export type GlmFamily = (typeof GLM_FAMILIES)[number];

export const TRANSFORM_USES = ["log1p", "standardize", "max_abs_scale", "mean_scale", "adstock", "saturation", "impute"] as const;
export type TransformUse = (typeof TRANSFORM_USES)[number];

export const SIGNS = ["any", "positive", "negative"] as const;
export type Sign = (typeof SIGNS)[number];

export const SLOT_KINDS = ["var", "var_list", "dim"] as const;
export type SlotKind = (typeof SLOT_KINDS)[number];

/** The model the declarative builder compiles to (and its descoped fallback, D3). */
export const DECLARATIVE_MODEL_ID = "tabular.declarative_glm";
export const FALLBACK_MODEL_ID = "tabular.hierarchical_glm";

/** A whole-value slot placeholder of a prep preset: `"$slot:<name>"`. */
export const SLOT_PREFIX = "$slot:";

export function slotRef(name: string): string {
  return `${SLOT_PREFIX}${name}`;
}

/** The slot a `with` value names, or `null` when it is not a placeholder. */
export function slotName(value: unknown): string | null {
  return typeof value === "string" && value.startsWith(SLOT_PREFIX) && value.length > SLOT_PREFIX.length ? value.slice(SLOT_PREFIX.length) : null;
}

// ------------------------------------------------------------------------------ documents

export interface AboutLite {
  title: string;
  summary_plain: string;
  use_when?: string[];
  avoid_when?: string[];
}

export interface LibraryMetadata {
  name: string;
  title: string;
  summary?: string;
  tags?: string[];
  about?: AboutLite | null;
}

export interface LibraryRef {
  item: string;
  revision?: number | "latest";
}

export interface PriorSpec {
  dist?: PriorDist;
  mu?: number;
  sigma?: number;
  nu?: number | null;
}

export interface TransformCall {
  use: TransformUse;
  with?: JsonObject;
}

export interface TermSpec {
  var: string;
  label?: string;
  transform?: TransformCall[];
  prior: PriorSpec;
  sign?: Sign;
  varies_by?: string | null;
}

export interface InteractionSpec {
  a: string;
  b: string;
  prior?: PriorSpec;
}

export interface GroupSpec {
  dim: string;
  sigma_prior?: PriorSpec;
}

export interface OutcomeSpec {
  var: string;
  family?: GlmFamily;
  n_categories?: number | null;
}

export interface DeclarativeModel {
  outcome: OutcomeSpec;
  intercept?: PriorSpec;
  terms: TermSpec[];
  interactions?: InteractionSpec[];
  groups?: GroupSpec[];
  noise?: PriorSpec;
}

interface DocBase<K extends LibraryDocKind> {
  apiVersion: string;
  kind: K;
  metadata: LibraryMetadata;
}

export interface ModelRecipeDoc extends DocBase<"ModelRecipe"> {
  /** `"declarative"` or a `UseRef` (`<id>@<range>`) of a catalog model (a preset). */
  base?: string;
  declarative?: DeclarativeModel | null;
  settings?: JsonObject;
  exposed?: string[];
  prep?: LibraryRef | null;
  plots?: string[];
  charts?: LibraryRef[];
  fit?: JsonObject | null;
}

export interface Slot {
  kind: SlotKind;
  label: string;
  role?: string | null;
  required?: boolean;
}

export interface StepCallDoc {
  id: string;
  use: string;
  with?: JsonObject;
  [key: string]: Json | undefined;
}

export interface PrepPresetDoc extends DocBase<"PrepPreset"> {
  slots?: Record<string, Slot>;
  steps: StepCallDoc[];
}

export interface ChartTemplateDoc extends DocBase<"ChartTemplate"> {
  source_recipe: string;
  plot: JsonObject;
  bindings?: JsonObject | null;
  caption_plain?: string;
}

export interface WorkflowTemplateDoc extends DocBase<"WorkflowTemplate"> {
  document: JsonObject;
}

export type LibraryDoc = ModelRecipeDoc | PrepPresetDoc | ChartTemplateDoc | WorkflowTemplateDoc;

/** The document of each item kind. */
export interface LibraryDocOf {
  model_recipe: ModelRecipeDoc;
  prep_preset: PrepPresetDoc;
  chart_template: ChartTemplateDoc;
  workflow_template: WorkflowTemplateDoc;
}

// ------------------------------------------------------------------------------ guards

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isMetadata(v: unknown): v is LibraryMetadata {
  return isObject(v) && typeof v.name === "string" && typeof v.title === "string";
}

function hasBase(v: unknown, kind: LibraryDocKind): v is Record<string, unknown> & { metadata: LibraryMetadata } {
  return isObject(v) && v.kind === kind && typeof v.apiVersion === "string" && isMetadata(v.metadata);
}

export function isLibraryKind(v: unknown): v is LibraryKind {
  return typeof v === "string" && (LIBRARY_KINDS as readonly string[]).includes(v);
}

export function isDeclarativeModel(v: unknown): v is DeclarativeModel {
  return isObject(v) && isObject(v.outcome) && typeof v.outcome.var === "string" && Array.isArray(v.terms);
}

export function isModelRecipeDoc(v: unknown): v is ModelRecipeDoc {
  if (!hasBase(v, "ModelRecipe")) return false;
  const base = v.base ?? "declarative";
  if (typeof base !== "string") return false;
  return base === "declarative" ? v.declarative == null || isDeclarativeModel(v.declarative) : true;
}

export function isPrepPresetDoc(v: unknown): v is PrepPresetDoc {
  return hasBase(v, "PrepPreset") && Array.isArray(v.steps) && (v.slots === undefined || isObject(v.slots));
}

export function isChartTemplateDoc(v: unknown): v is ChartTemplateDoc {
  return hasBase(v, "ChartTemplate") && typeof v.source_recipe === "string" && isObject(v.plot);
}

export function isWorkflowTemplateDoc(v: unknown): v is WorkflowTemplateDoc {
  return hasBase(v, "WorkflowTemplate") && isObject(v.document);
}

const GUARDS: { [K in LibraryKind]: (v: unknown) => v is LibraryDocOf[K] } = {
  model_recipe: isModelRecipeDoc,
  prep_preset: isPrepPresetDoc,
  chart_template: isChartTemplateDoc,
  workflow_template: isWorkflowTemplateDoc,
};

/** The item kind of a document, or `null` when it is none of the four shapes. */
export function libraryKindOf(doc: unknown): LibraryKind | null {
  if (!isObject(doc) || typeof doc.kind !== "string") return null;
  const kind = (DOC_KIND_OF as Record<string, LibraryKind | undefined>)[doc.kind];
  return kind && GUARDS[kind](doc) ? kind : null;
}

/** `doc` as the document of `kind`, or `null` when it has another shape. */
export function docOfKind<K extends LibraryKind>(kind: K, doc: unknown): LibraryDocOf[K] | null {
  return GUARDS[kind](doc) ? doc : null;
}

/** A model recipe built with the builder (`base: declarative`, the default). */
export function isDeclarativeRecipe(doc: ModelRecipeDoc): doc is ModelRecipeDoc & { declarative: DeclarativeModel } {
  return (doc.base ?? "declarative") === "declarative" && isDeclarativeModel(doc.declarative);
}

/** A model recipe that presets a catalog model (`base: <id>@<range>`). */
export function isPresetRecipe(doc: ModelRecipeDoc): boolean {
  return (doc.base ?? "declarative") !== "declarative";
}

/** `"ts.sarimax@^1"` → `["ts.sarimax", "^1"]` (range `null` when absent). */
export function splitUseRef(ref: string): [string, string | null] {
  const at = ref.indexOf("@");
  return at < 0 ? [ref, null] : [ref.slice(0, at), ref.slice(at + 1)];
}

/** The dataset names a Vega-Lite spec reads (`data: {name}` at any depth). */
export function datasetNamesOf(spec: unknown): string[] {
  const out = new Set<string>();
  const walk = (v: unknown, depth: number) => {
    if (depth > 64) return;
    if (Array.isArray(v)) {
      for (const x of v) walk(x, depth + 1);
      return;
    }
    if (!isObject(v)) return;
    const data = v.data;
    if (isObject(data) && typeof data.name === "string") out.add(data.name);
    for (const [k, x] of Object.entries(v)) if (k !== "datasets") walk(x, depth + 1);
  };
  walk(spec, 0);
  return [...out];
}

/** Byte length of `value` as compact JSON (the server's cap measure). */
export function jsonBytes(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value) ?? "").length;
}

/** The issues that point at `pointer` or below it (for mapping `:validate` issues to fields). */
export function issuesAt(issues: readonly Issue[] | null | undefined, pointer: string): Issue[] {
  if (!issues) return [];
  return issues.filter((i) => {
    const p = i.pointer ?? "";
    return p === pointer || p.startsWith(`${pointer}/`);
  });
}

/** True when any issue is an error (warnings and infos do not block a save). */
export function hasErrors(issues: readonly Issue[] | null | undefined): boolean {
  return (issues ?? []).some((i) => (i.severity ?? "error") === "error");
}

// ------------------------------------------------------------------------------ list filters

export interface LibraryListFilters {
  kind?: LibraryKind | undefined;
  scope?: LibraryScope | undefined;
  q?: string | undefined;
  include_archived?: boolean | undefined;
}
