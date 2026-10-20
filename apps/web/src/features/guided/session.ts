/**
 * A Guided session is a stored spec (plan §11: "both modes edit the same ModelSpec").
 *
 * `POST /v1/guided/drafts` builds the draft and stores nothing; once the data is uploaded the
 * draft is saved with `POST /v1/specs` (its `metadata.labels.guided` names the template) and
 * every later step writes a new revision (`Guided: …` messages). Everything a step needs on
 * resume is derived from server state: the spec and its revisions, the dataset it names, the
 * fit runs of the spec, the model version the latest fit produced and the endpoints serving it.
 * Only the answers of a session that has not uploaded its data yet live in the browser
 * (sessionStorage), because the file itself has to be chosen again anyway.
 */
import { parsePointer } from "@/lib/pointer";

import type { GuidedTemplate, SpecDocument } from "./types";

export const GUIDED_LABEL = "guided";
export const GUIDED_MESSAGE = "Guided:";

export const STEP_IDS = ["data", "priors", "run", "report", "explore", "share"] as const;
export type GuidedStepId = (typeof STEP_IDS)[number];

export interface StepDef {
  id: GuidedStepId;
  title: string;
  /** What the step answers, for the stepper and the page header. */
  question: string;
}

export function hasTask(template: Pick<GuidedTemplate, "tasks">, task: NonNullable<GuidedTemplate["tasks"]>[number]): boolean {
  return (template.tasks ?? []).includes(task);
}

/** The explore step has content (what-if, scenarios or a forecast); the causal templates have none. */
export function hasExplore(template: Pick<GuidedTemplate, "tasks">): boolean {
  return hasTask(template, "whatif") || hasTask(template, "forecast") || hasTask(template, "counterfactual");
}

export function exploreTitle(template: Pick<GuidedTemplate, "tasks" | "family">): string {
  if (hasTask(template, "whatif")) return "What if…";
  if (hasTask(template, "forecast")) return "Forecast";
  return "Scenarios";
}

/**
 * The parts of the "What you believe" step: beliefs about each channel only where the model
 * takes business priors, experiments only where it takes lift tests (`mediated_mmm` takes lift
 * tests only, and its model refuses business priors).
 */
export function priorsSections(template: Pick<GuidedTemplate, "tasks">): { beliefs: boolean; lifts: boolean } {
  return { beliefs: hasTask(template, "business_priors"), lifts: hasTask(template, "lift_tests") };
}

/** The steps of a template's flow (business priors only where the template takes them, the explore step only where it has content). */
export function stepsFor(template: Pick<GuidedTemplate, "tasks" | "family">): StepDef[] {
  const steps: StepDef[] = [{ id: "data", title: "Your data", question: "What does each column contain?" }];
  if (hasTask(template, "business_priors") || hasTask(template, "lift_tests")) {
    steps.push({ id: "priors", title: "What you believe", question: "What do you already know about your channels?" });
  }
  steps.push(
    { id: "run", title: "Fit the model", question: "Let the model learn from your data" },
    { id: "report", title: "Results", question: "What did the model find? Can you trust it?" },
  );
  if (hasExplore(template)) {
    steps.push({ id: "explore", title: exploreTitle(template), question: hasTask(template, "whatif") ? "What happens if you change the budget?" : "What comes next?" });
  }
  steps.push({ id: "share", title: "Share", question: "Use the result" });
  return steps;
}

export function isStepId(v: unknown): v is GuidedStepId {
  return typeof v === "string" && (STEP_IDS as readonly string[]).includes(v);
}

// ----------------------------------------------------------------------------- the document

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

const ABSENT = Symbol("absent");

/** The value at a JSON pointer, or `ABSENT` when it is not there. */
function lookup(doc: unknown, pointer: string): unknown {
  let node: unknown = doc;
  for (const part of parsePointer(pointer)) {
    if (isRecord(node) && part in node) node = node[part];
    else if (Array.isArray(node) && /^\d+$/.test(part) && Number(part) < node.length) node = node[Number(part)];
    else return ABSENT;
  }
  return node;
}

/** The value at a JSON pointer (`undefined` when it is not there). */
export function valueAt(doc: unknown, pointer: string): unknown {
  const v = lookup(doc, pointer);
  return v === ABSENT ? undefined : v;
}

/** `metadata.labels.guided`: the template a spec was drafted from. */
export function guidedTemplateOf(doc: SpecDocument | null | undefined): string | null {
  const labels = valueAt(doc, "/metadata/labels");
  return isRecord(labels) && typeof labels[GUIDED_LABEL] === "string" ? labels[GUIDED_LABEL] : null;
}

/** `dataset://<project>/<name>@…` → `name`. */
export function datasetNameOf(doc: SpecDocument | null | undefined): string | null {
  const ref = valueAt(doc, "/data/dataset");
  if (typeof ref !== "string") return null;
  const m = /^dataset:\/\/[^/]+\/([^@/]+)(?:@.*)?$/.exec(ref);
  return m ? (m[1] ?? null) : null;
}

export function specName(doc: SpecDocument | null | undefined): string | null {
  const v = valueAt(doc, "/metadata/name");
  return typeof v === "string" ? v : null;
}

function stable(v: unknown): string {
  if (v === ABSENT) return "<absent>";
  if (Array.isArray(v)) return `[${v.map(stable).join(",")}]`;
  if (isRecord(v)) {
    return `{${Object.keys(v)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${stable(v[k])}`)
      .join(",")}}`;
  }
  return JSON.stringify(v) ?? "null";
}

/**
 * Pointers of the template's `advanced` and `hidden` settings whose value in `doc` differs
 * from `reference` (the revision Guided mode drafted). Guided mode keeps every such value as it
 * is and marks the session "advanced settings applied".
 */
export function advancedChanges(template: Pick<GuidedTemplate, "ui_levels">, doc: SpecDocument, reference: SpecDocument | null): string[] {
  if (!reference) return [];
  return Object.entries(template.ui_levels ?? {})
    .filter(([, level]) => level !== "basic")
    .map(([pointer]) => pointer)
    .filter((pointer) => stable(lookup(doc, pointer)) !== stable(lookup(reference, pointer)))
    .sort();
}

const DATA_SHAPES: Partial<Record<GuidedTemplate["family"], string>> = {
  mmm: "A CSV file or a Parquet file with one row for each week (or day) and market. Each row has the results and a spend column for each channel. The app finds MFF long tables and Google Meridian tables automatically.",
  forecast: "A CSV file or a Parquet file with one row for each week (or day). Each row has a value of the series to forecast.",
  tabular: "A CSV file or a Parquet file with one row for each store and week. Each row has the sales and the drivers of the sales: prices, promotions and the calendar.",
  choice: "A CSV file or a Parquet file with one row for each shopping trip. Each row has the customer, the selected product and a price column for each product. It can also have a promotion column for each product.",
  causal: "A CSV file or a Parquet file with one row for each week (or day), before and after the start of the campaign. Each row has the result. It can also have control columns that the campaign did not change, such as the sales of other regions or search traffic.",
};

/** Templates whose data differs from the rest of their family (national, more columns). */
const TEMPLATE_SHAPES: Record<string, string> = {
  mediated_mmm:
    "A CSV file or a Parquet file with one row for each week (or day) for the whole country. Each row has the result, a spend column for each channel and a column for each intermediate measure, such as visits or awareness. An intermediate measure can have empty rows, for example a monthly survey.",
  multi_kpi_mmm:
    "A CSV file or a Parquet file with one row for each week (or day) for the whole country. Each row has a result column for each product or KPI, in the same units, and a spend column for each channel.",
  geo_lift:
    "A CSV file or a Parquet file with one row for each market and week (or day), before and after the start of the test. Each row has the result. Some markets got the campaign and the other markets did not.",
};

/** What the upload should look like for this template, in plain words. */
export function dataShapePlain(template: Pick<GuidedTemplate, "family" | "description_plain"> & { id?: string }): string {
  const own = template.id !== undefined ? TEMPLATE_SHAPES[template.id] : undefined;
  return own ?? DATA_SHAPES[template.family] ?? `A CSV or Parquet file with the data for this analysis: ${template.description_plain}`;
}

export type PeriodUnit = "day" | "week" | "month" | "period";

/**
 * What one step of the data's time axis is: the weekly forecast's answered frequency (stored
 * as its season length), else the first frequency (`W-SUN`, `D`, `MS`) among the dataset's
 * dims (its schema's or its summary's).
 */
export function periodUnit(document: unknown, dims: readonly { kind: string; freq?: string | null }[]): PeriodUnit {
  const season = valueAt(document, "/model/settings/season_period");
  if (season === 365.25) return "day";
  if (season === 52.18) return "week";
  const freq = (dims.find((d) => d.kind === "time" && d.freq)?.freq ?? "").toUpperCase();
  if (freq.startsWith("W")) return "week";
  if (freq.startsWith("D") || freq === "B") return "day";
  if (freq.startsWith("M")) return "month";
  return "period";
}

/** "1 week", "13 weeks". */
export function periodWords(unit: PeriodUnit, n: number): string {
  return `${n} ${unit}${n === 1 ? "" : "s"}`;
}

/**
 * How far ahead a forecast may reach: the horizon the model was fitted for (`max_extension`,
 * the answered horizon; further needs a refit), else the template's limit, else 104 periods.
 */
export function forecastLimit(document: unknown, template: Pick<GuidedTemplate, "questions">): { max: number; fitted: boolean } {
  const fitted = valueAt(document, "/model/settings/max_extension");
  if (typeof fitted === "number" && Number.isInteger(fitted) && fitted >= 1) return { max: fitted, fitted: true };
  const q = template.questions.find((x) => x.id === "horizon");
  return { max: typeof q?.maximum === "number" ? q.maximum : 104, fitted: false };
}

/** The dim the template's date question binds (`time` for most, `t` for the weekly forecast). */
export function timeDimOf(template: Pick<GuidedTemplate, "spec_template">): string {
  const dims = valueAt(template.spec_template, "/data/bind/dims");
  if (isRecord(dims)) {
    for (const [k, v] of Object.entries(dims)) if (v === "{{date}}") return k;
  }
  return "time";
}

/**
 * The wide variable and dim the template's spend (or price) question builds: the spend
 * question when there is one, else the first other wide question. A wide result question (the
 * KPIs of `multi_kpi_mmm`, `y` over `outcome`) is never it: a what-if changes spend, not the
 * result.
 */
export function wideVarOf(template: Pick<GuidedTemplate, "questions">): { variable: string; dim: string; question: string } | null {
  const wide = template.questions.filter((x) => x.kind === "columns" && x.wide_dim && x.as_var && x.required !== false && x.role !== "target");
  const q = wide.find((x) => x.role === "spend") ?? wide[0];
  return q && q.as_var && q.wide_dim ? { variable: q.as_var, dim: q.wide_dim, question: q.id } : null;
}

// ----------------------------------------------------------------------------- names

/** A name that fits `mapi_spec._types.SLUG_RE` (lower-case letters, digits, `-`, `_`). */
export function slugify(text: string, fallback = "analysis"): string {
  const s = text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[-_]+|[-_]+$/g, "")
    .slice(0, 100)
    .replace(/[-_]+$/g, "");
  return s || fallback;
}

export function defaultSessionName(templateId: string, now: Date = new Date()): string {
  const d = now.toISOString().slice(0, 10).replace(/-/g, "");
  return slugify(`${templateId.replace(/_/g, "-")}-${d}`);
}

// ----------------------------------------------------------------------------- local answers

export interface LocalDraft {
  name: string;
  answers: Record<string, unknown>;
  savedAt: string;
}

const KEY = (templateId: string) => `mapi.guided.draft.${templateId}`;

/** The answers of an unsaved session (per template, this tab only). */
export function loadLocalDraft(templateId: string): LocalDraft | null {
  try {
    const raw = sessionStorage.getItem(KEY(templateId));
    if (!raw) return null;
    const v = JSON.parse(raw) as unknown;
    return isRecord(v) && typeof v.name === "string" && isRecord(v.answers) ? { name: v.name, answers: v.answers, savedAt: typeof v.savedAt === "string" ? v.savedAt : "" } : null;
  } catch {
    return null;
  }
}

export function saveLocalDraft(templateId: string, draft: Omit<LocalDraft, "savedAt">): void {
  try {
    sessionStorage.setItem(KEY(templateId), JSON.stringify({ ...draft, savedAt: new Date().toISOString() }));
  } catch {
    // storage unavailable (private mode): answers live as long as the page
  }
}

export function clearLocalDraft(templateId: string): void {
  try {
    sessionStorage.removeItem(KEY(templateId));
  } catch {
    // ignore
  }
}
