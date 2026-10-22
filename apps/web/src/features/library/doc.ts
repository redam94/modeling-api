/**
 * Library documents in the builders: new documents with sensible defaults, tidy copies for
 * saving (the YAML round trip lives in `doc-yaml.ts`), the plain-words summary of a model recipe,
 * a prep preset's slots filled in the browser (for its preview), and the ModelSpec a used model
 * recipe becomes. Pure functions over the shapes in `types.ts`; no regular expressions.
 */
import {
  DECLARATIVE_MODEL_ID,
  POSITIVE_DISTS,
  SLOT_PREFIX,
  slotName,
  type ChartTemplateDoc,
  type DeclarativeModel,
  type GlmFamily,
  type Json,
  type JsonObject,
  type LibraryDoc,
  type ModelRecipeDoc,
  type PrepPresetDoc,
  type PriorSpec,
  type Sign,
  type StepCallDoc,
  type TermSpec,
  type TransformUse,
  type WorkflowTemplateDoc,
} from "./types";

export const API_VERSION = "mapi/v1";

// ------------------------------------------------------------------------------ plain words

/** The outcome kinds, in the words an analyst uses (D3: "a number", "a count", …). */
export const FAMILY_OPTIONS: { value: GlmFamily; label: string; hint: string }[] = [
  { value: "normal", label: "A number", hint: "Sales, revenue, a price: any amount that can go up or down." },
  { value: "student_t", label: "A number with outliers", hint: "Like a number, but a few weeks are far off the rest." },
  { value: "gamma", label: "A positive amount", hint: "Always above zero and skewed: order values, durations." },
  { value: "poisson", label: "A count", hint: "Whole numbers from zero: visits, orders, sign-ups." },
  { value: "negative_binomial", label: "A count that varies a lot", hint: "Counts that swing more than a plain count would." },
  { value: "zero_inflated_poisson", label: "A count with many zeros", hint: "Most rows are zero, the rest are counts." },
  { value: "zero_inflated_negative_binomial", label: "A count with many zeros that varies a lot", hint: "Many zeros, and the counts swing widely." },
  { value: "bernoulli", label: "Yes / no", hint: "0 or 1: bought or not, churned or not." },
  { value: "beta", label: "A share between 0 and 1", hint: "Conversion rate, share of voice, a percentage as a fraction." },
  { value: "ordinal", label: "A rating", hint: "Ordered categories: 1 to 5 stars, low / medium / high." },
];

export function familyLabel(family: GlmFamily | undefined): string {
  return FAMILY_OPTIONS.find((f) => f.value === (family ?? "normal"))?.label ?? String(family);
}

export const TRANSFORM_OPTIONS: { value: TransformUse; label: string; words: string; hint: string }[] = [
  { value: "log1p", label: "Log", words: "logged", hint: "log(1 + x): big values count a bit less." },
  { value: "standardize", label: "Standardise", words: "standardised", hint: "Centre at 0, scale to 1. Makes priors comparable." },
  { value: "max_abs_scale", label: "Scale to max", words: "scaled to its largest value", hint: "Divide by the largest absolute value." },
  { value: "mean_scale", label: "Scale to mean", words: "scaled to its mean", hint: "Divide by the mean." },
  { value: "adstock", label: "Carry-over", words: "carried over into later weeks", hint: "Spreads each week's value into the weeks after it." },
  { value: "saturation", label: "Diminishing returns", words: "bent for diminishing returns", hint: "The tenth million counts less than the first." },
  { value: "impute", label: "Fill gaps", words: "with gaps filled", hint: "Fill missing values (median by default)." },
];

export const SIGN_OPTIONS: { value: Sign; label: string; words: string }[] = [
  { value: "any", label: "Either way", words: "can move it either way" },
  { value: "positive", label: "Only lifts", words: "can only lift it" },
  { value: "negative", label: "Only lowers", words: "can only lower it" },
];

// ------------------------------------------------------------------------------ names

/** A metadata name (`Slug`) from a title: lower-case letters, digits and `-`, ≤ 64 characters. */
export function slugify(title: string, fallback = "my-item"): string {
  let out = "";
  let dash = false;
  for (const ch of title.toLowerCase()) {
    const ok = (ch >= "a" && ch <= "z") || (ch >= "0" && ch <= "9");
    if (ok) {
      out += ch;
      dash = false;
    } else if (!dash && out) {
      out += "-";
      dash = true;
    }
    if (out.length >= 64) break;
  }
  while (out.endsWith("-") || out.endsWith("_")) out = out.slice(0, -1);
  return out || fallback;
}

/** A variable name (`Name`) safe for a slot or step id: letters, digits and `_`. */
export function identifier(text: string, fallback = "x"): string {
  let out = "";
  for (const ch of text) {
    const ok = (ch >= "a" && ch <= "z") || (ch >= "A" && ch <= "Z") || (ch >= "0" && ch <= "9") || ch === "_";
    out += ok ? ch : "_";
    if (out.length >= 64) break;
  }
  if (!out || (out[0]! >= "0" && out[0]! <= "9")) out = `${fallback}_${out}`.slice(0, 64);
  return out;
}

// ------------------------------------------------------------------------------ new documents

export function newTerm(variable: string): TermSpec {
  // R3: standardise numeric drivers by default, so a normal(0, 1) prior means "a modest effect".
  return { var: variable, transform: [{ use: "standardize" }], prior: { dist: "normal", mu: 0, sigma: 1 }, sign: "any" };
}

export function newModelRecipe(title = ""): ModelRecipeDoc {
  return {
    apiVersion: API_VERSION,
    kind: "ModelRecipe",
    metadata: { name: slugify(title, "my-model"), title, summary: "", tags: [] },
    base: "declarative",
    declarative: { outcome: { var: "", family: "normal" }, terms: [], groups: [], interactions: [] },
  };
}

export function newPresetRecipe(base: string, title = ""): ModelRecipeDoc {
  return { apiVersion: API_VERSION, kind: "ModelRecipe", metadata: { name: slugify(title, "my-preset"), title, summary: "", tags: [] }, base, settings: {}, exposed: [] };
}

export function newPrepPreset(title = ""): PrepPresetDoc {
  return { apiVersion: API_VERSION, kind: "PrepPreset", metadata: { name: slugify(title, "my-prep"), title, summary: "", tags: [] }, slots: {}, steps: [] };
}

export function newChartTemplate(sourceRecipe: string, plot: JsonObject, title = ""): ChartTemplateDoc {
  return { apiVersion: API_VERSION, kind: "ChartTemplate", metadata: { name: slugify(title, "my-chart"), title, summary: "", tags: [] }, source_recipe: sourceRecipe, plot, caption_plain: "" };
}

export function newWorkflowTemplate(document: JsonObject, title = ""): WorkflowTemplateDoc {
  return { apiVersion: API_VERSION, kind: "WorkflowTemplate", metadata: { name: slugify(title, "my-workflow"), title, summary: "", tags: [] }, document };
}

// ------------------------------------------------------------------------------ tidy copies

function isEmpty(v: unknown): boolean {
  return v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0) || (typeof v === "object" && v !== null && !Array.isArray(v) && Object.keys(v).length === 0);
}

/**
 * `value` without `undefined` anywhere (JSON-safe), and without the listed keys when empty.
 * The parts under {@link OPAQUE_KEYS} (a chart's Vega-Lite spec, a workflow document, model
 * settings) are copied as they are, apart from `undefined`.
 */
export function prune<T>(value: T, dropEmpty: ReadonlySet<string> = OPTIONAL_KEYS): T {
  if (Array.isArray(value)) return value.map((v: unknown) => prune(v, dropEmpty)) as T;
  if (typeof value === "object" && value !== null) {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (v === undefined) continue;
      if (dropEmpty.has(k) && isEmpty(v)) continue;
      out[k] = OPAQUE_KEYS.has(k) ? prune(v, NONE) : prune(v, dropEmpty);
    }
    return out as T;
  }
  return value;
}

const NONE: ReadonlySet<string> = new Set();
/** Parts of a document that belong to someone else's schema: never tidied. */
const OPAQUE_KEYS: ReadonlySet<string> = new Set(["plot", "document", "settings", "bindings", "fit", "with"]);

/** Keys a library document may leave out when they are empty (the server's defaults apply). */
export const OPTIONAL_KEYS: ReadonlySet<string> = new Set(["summary", "tags", "about", "label", "transform", "varies_by", "interactions", "groups", "nu", "with", "exposed", "plots", "charts", "prep", "fit", "bindings", "caption_plain", "n_categories", "slots", "role"]);

/** The document as it is saved: empty optional parts left out. */
export function tidyDoc<T extends LibraryDoc>(doc: T): T {
  return prune(doc);
}

// ------------------------------------------------------------------------------ summary in words

function list(words: string[]): string {
  if (words.length <= 1) return words[0] ?? "";
  return `${words.slice(0, -1).join(", ")} and ${words.at(-1)}`;
}

function termWords(t: TermSpec): string {
  const name = t.label?.trim() ? `${t.label.trim()} (${t.var})` : t.var || "a column you have not picked";
  const tf = (t.transform ?? []).map((c) => TRANSFORM_OPTIONS.find((o) => o.value === c.use)?.words ?? c.use);
  const sign = POSITIVE_DISTS.has(t.prior?.dist ?? "normal") && (t.sign ?? "any") === "any" ? "positive" : (t.sign ?? "any");
  const parts = [name];
  if (tf.length) parts.push(list(tf));
  parts.push(SIGN_OPTIONS.find((s) => s.value === sign)?.words ?? "");
  if (t.varies_by) parts.push(`differently in each ${t.varies_by}`);
  return parts.filter(Boolean).join(", ");
}

/** A declarative model in plain sentences (the review panel and the item cards). */
export function summarizeDeclarative(m: DeclarativeModel | null | undefined): string[] {
  if (!m) return ["This model has no description yet."];
  const out: string[] = [];
  const outcome = m.outcome?.var ? `${m.outcome.var}` : "an outcome you have not picked";
  out.push(`Explains ${outcome}, which is ${familyLabel(m.outcome?.family).toLowerCase()}.`);
  const terms = m.terms ?? [];
  if (!terms.length) out.push("It has no drivers yet. Add at least one.");
  else out.push(`${terms.length === 1 ? "One driver" : `${terms.length} drivers`}: ${terms.map(termWords).join("; ")}.`);
  const groups = (m.groups ?? []).map((g) => g.dim).filter(Boolean);
  if (groups.length) out.push(`Each ${list(groups)} gets its own starting level.`);
  const inter = (m.interactions ?? []).filter((x) => x.a && x.b);
  if (inter.length) out.push(`It also looks at how ${list(inter.map((x) => `${x.a} and ${x.b}`))} work together.`);
  return out;
}

/** A model recipe (declarative or a preset) in plain sentences. */
export function summarizeModelRecipe(doc: ModelRecipeDoc): string[] {
  if ((doc.base ?? "declarative") === "declarative") return summarizeDeclarative(doc.declarative);
  const exposed = doc.exposed ?? [];
  const set = Object.keys(doc.settings ?? {});
  return [
    `Uses the catalog model ${doc.base} with ${set.length ? `your settings for ${list(set)}` : "its default settings"}.`,
    exposed.length ? `People who use it can change ${list(exposed)}.` : "People who use it cannot change any setting.",
  ];
}

// ------------------------------------------------------------------------------ prep presets

/** The slots a prep preset's steps name (`"$slot:x"` values, whole or in a list). */
export function slotsUsed(steps: readonly StepCallDoc[]): Set<string> {
  const out = new Set<string>();
  const walk = (v: unknown) => {
    const s = slotName(v);
    if (s !== null) out.add(s);
    else if (Array.isArray(v)) v.forEach(walk);
  };
  for (const s of steps) for (const v of Object.values(s.with ?? {})) walk(v);
  return out;
}

/**
 * A prep preset's steps with the slots filled, as `POST /v1/library/{id}:use` does
 * (`mapi_spec.prep_presets.apply_prep_preset`): a slot that is a whole value takes the given
 * value; a slot inside a list is replaced by the given value, or spliced in when that is a list.
 * Step ids get the prefix (`p_<id>`). Unfilled required slots come back in `missing`.
 */
export function fillPrepSlots(doc: PrepPresetDoc, values: Readonly<Record<string, Json>>, prefix = "p"): { steps: StepCallDoc[]; missing: string[] } {
  const missing = new Set<string>();
  const fill = (v: Json): Json => {
    const name = slotName(v);
    if (name !== null) {
      if (!(name in values)) {
        if (doc.slots?.[name]?.required !== false) missing.add(name);
        return null;
      }
      return values[name] ?? null;
    }
    if (Array.isArray(v)) {
      const out: Json[] = [];
      for (const x of v) {
        const filled = fill(x);
        if (slotName(x) !== null && Array.isArray(filled)) out.push(...filled);
        else out.push(filled);
      }
      return out;
    }
    return v;
  };
  const steps = doc.steps.map((s) => {
    const w: JsonObject = {};
    for (const [k, v] of Object.entries(s.with ?? {})) w[k] = fill(v);
    return { ...s, id: `${prefix}_${s.id}`, with: w };
  });
  return { steps, missing: [...missing] };
}

/** The `$slot:` reference for a list field (`["$slot:x"]`) or a single field (`"$slot:x"`). */
export function slotValue(name: string, isList: boolean): Json {
  return isList ? [`${SLOT_PREFIX}${name}`] : `${SLOT_PREFIX}${name}`;
}

/** The slot a `with` value is (whole, or a one-item list holding a slot), or `null`. */
export function slotOfValue(v: Json | undefined): string | null {
  if (Array.isArray(v) && v.length === 1) return slotName(v[0]);
  return slotName(v);
}

// ------------------------------------------------------------------------------ lists

/** `items` with the item at `from` moved to `to` (clamped). */
export function moveItem<T>(items: readonly T[], from: number, to: number): T[] {
  const out = [...items];
  if (from < 0 || from >= out.length) return out;
  const target = Math.max(0, Math.min(out.length - 1, to));
  const [it] = out.splice(from, 1);
  out.splice(target, 0, it as T);
  return out;
}

// ------------------------------------------------------------------------------ use in Build

export interface SpecTarget {
  /** `metadata.name` of the new spec. */
  name: string;
  project: string;
  /** `dataset://…@latest` or `@vN`. */
  dataset: string;
}

function asObject(v: unknown): JsonObject {
  return typeof v === "object" && v !== null && !Array.isArray(v) ? (v as JsonObject) : {};
}

/**
 * The ModelSpec a used model recipe becomes: its compiled fragment (`data.bind`, `pipeline`,
 * `model`, `outputs`, `metadata.labels` with the lineage label) on the chosen dataset.
 */
export function specFromFragment(fragment: JsonObject, target: SpecTarget): JsonObject {
  const md = asObject(fragment.metadata);
  const data = asObject(fragment.data);
  const spec: JsonObject = {
    apiVersion: API_VERSION,
    kind: "ModelSpec",
    metadata: { name: target.name, project: target.project, ...(Object.keys(asObject(md.labels)).length ? { labels: asObject(md.labels) } : {}) },
    data: { dataset: target.dataset, ...data },
    pipeline: Array.isArray(fragment.pipeline) ? fragment.pipeline : [],
    model: asObject(fragment.model),
  };
  if (fragment.outputs) spec.outputs = fragment.outputs;
  return spec;
}

/** A spec as YAML text. Async: the `yaml` package loads on first use (library-* budget). */
export async function specYaml(spec: JsonObject): Promise<string> {
  const { stringify } = await import("yaml");
  return stringify(spec, { lineWidth: 0, aliasDuplicateObjects: false });
}

/** A spec name from an item name: unique enough per use (`<name>-<yymmdd-hhmm>`), ≤ 64 characters. */
export function specNameFor(itemName: string, now: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  const stamp = `${String(now.getFullYear()).slice(2)}${p(now.getMonth() + 1)}${p(now.getDate())}-${p(now.getHours())}${p(now.getMinutes())}`;
  return `${slugify(itemName, "model").slice(0, 64 - stamp.length - 1)}-${stamp}`;
}

/** True when the document compiles to the declarative model (not a preset). */
export function isDeclarativeDoc(doc: ModelRecipeDoc): boolean {
  return (doc.base ?? "declarative") === "declarative";
}

export function modelUseOf(doc: ModelRecipeDoc): string {
  return isDeclarativeDoc(doc) ? `${DECLARATIVE_MODEL_ID}@^1` : (doc.base ?? "");
}

export type { PriorSpec };
