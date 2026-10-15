/** Score form state and its translation to a `ScoreRequest` document. */
import type { OutputSelector, ScoreRequest } from "@/api/types";

export type ScoreMode = NonNullable<ScoreRequest["mode"]>;
export type InterventionOp = "multiply" | "add" | "set";

export interface InterventionRow {
  var: string;
  /** `dim=value[,value…]` pairs separated by `;`, e.g. `channel=meta;geo=de,fr`. */
  sel: string;
  op: InterventionOp;
  value: string;
}

export interface ScoreForm {
  mode: ScoreMode;
  horizonDim: string;
  horizonPeriods: string;
  inputsStage: string;
  /** Payload dataset (future inputs, new levels, the window to score); empty = none. */
  data: string;
  scenario: string;
  /** Side-table name → dataset reference; empty values are not sent. */
  sideTables: Record<string, string>;
  interventions: InterventionRow[];
  output: {
    kind: "expected" | "predictive";
    summary: string[];
    draws: string;
    units: "original" | "model";
    names: "dataset" | "canonical";
    vars: string;
  };
  seed: string;
}

export const SUMMARY_OPTIONS = ["mean", "median", "sd", "hdi_50", "hdi_90", "hdi_95", "q05", "q50", "q95"];

export function defaultScoreForm(): ScoreForm {
  return {
    mode: "forecast",
    horizonDim: "time",
    horizonPeriods: "8",
    inputsStage: "raw",
    data: "",
    scenario: "",
    sideTables: {},
    interventions: [],
    output: { kind: "expected", summary: ["mean", "hdi_90"], draws: "0", units: "original", names: "dataset", vars: "" },
    seed: "",
  };
}

export interface FormProblem {
  field: string;
  message: string;
}

const NAME_RE = /^[A-Za-z_][A-Za-z0-9_.-]{0,127}$/;
const DATASET_REF_RE = /^dataset:\/\/[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?\/[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?(?:@(latest|v\d{1,9}|\d{1,9}))?$/;

function scalar(v: string): string | number | boolean {
  const t = v.trim();
  if (t === "true") return true;
  if (t === "false") return false;
  if (/^-?\d+(\.\d+)?$/.test(t)) return Number(t);
  return t;
}

export function parseSel(sel: string): Record<string, (string | number | boolean)[]> | string {
  const out: Record<string, (string | number | boolean)[]> = {};
  for (const part of sel.split(";").map((s) => s.trim()).filter(Boolean)) {
    const eq = part.indexOf("=");
    if (eq <= 0) return `"${part}" is not dim=value`;
    const dim = part.slice(0, eq).trim();
    if (!NAME_RE.test(dim)) return `"${dim}" is not a valid dim name`;
    const values = part.slice(eq + 1).split(",").map((s) => s.trim()).filter(Boolean);
    if (!values.length) return `no values for ${dim}`;
    out[dim] = values.map(scalar);
  }
  return out;
}

/** Validate the form and build the request (or return the problems). */
export function buildScoreRequest(form: ScoreForm, modelRef: string): { request: ScoreRequest } | { problems: FormProblem[] } {
  const problems: FormProblem[] = [];
  const request: ScoreRequest = {
    apiVersion: "mapi/v1",
    kind: "ScoreRequest",
    model: modelRef,
    mode: form.mode,
    inputs_stage: form.inputsStage.trim() || "raw",
  };

  if (!/^[a-z_][a-z0-9_]{0,62}$/.test(request.inputs_stage ?? "raw")) {
    problems.push({ field: "inputsStage", message: "Stage names are lowercase step ids (or raw)." });
  }
  if (form.mode === "forecast") {
    const n = Number(form.horizonPeriods);
    if (!NAME_RE.test(form.horizonDim)) problems.push({ field: "horizonDim", message: "Enter the dim to extend." });
    if (!Number.isInteger(n) || n < 1 || n > 100_000) problems.push({ field: "horizonPeriods", message: "Periods must be a whole number from 1." });
    else request.horizon = { dim: form.horizonDim, periods: n };
  }
  if (form.data.trim()) {
    if (!DATASET_REF_RE.test(form.data.trim())) problems.push({ field: "data", message: "Use a dataset reference like dataset://default/eu_media_future@v1." });
    else request.data = form.data.trim();
  }
  if (form.scenario.trim()) {
    if (!DATASET_REF_RE.test(form.scenario.trim())) problems.push({ field: "scenario", message: "Use a dataset reference like dataset://default/plan_2027h1@v1." });
    else request.scenario = form.scenario.trim();
  }
  const sideTables: Record<string, string> = {};
  for (const [name, ref] of Object.entries(form.sideTables)) {
    const value = ref.trim();
    if (!value) continue;
    if (!DATASET_REF_RE.test(value)) problems.push({ field: `sideTables.${name}`, message: `Side table ${name}: use a dataset reference.` });
    else sideTables[name] = value;
  }
  if (Object.keys(sideTables).length) request.side_tables = sideTables;
  const interventions: NonNullable<ScoreRequest["interventions"]> = [];
  form.interventions.forEach((row, i) => {
    if (!NAME_RE.test(row.var)) {
      problems.push({ field: `interventions.${i}`, message: `Intervention ${i + 1}: enter a variable name.` });
      return;
    }
    const sel = parseSel(row.sel);
    if (typeof sel === "string") {
      problems.push({ field: `interventions.${i}`, message: `Intervention ${i + 1}: ${sel}.` });
      return;
    }
    const value = Number(row.value);
    if (row.value.trim() === "" || !Number.isFinite(value)) {
      problems.push({ field: `interventions.${i}`, message: `Intervention ${i + 1}: enter a number.` });
      return;
    }
    const selFlat: Record<string, string | number | boolean | (string | number | boolean)[]> = {};
    for (const [k, v] of Object.entries(sel)) selFlat[k] = v.length === 1 ? (v[0] as string | number | boolean) : v;
    interventions.push({ var: row.var, sel: selFlat, [row.op]: value });
  });
  if (interventions.length) request.interventions = interventions;

  const draws = Number(form.output.draws || "0");
  if (!Number.isInteger(draws) || draws < 0 || draws > 10_000) problems.push({ field: "draws", message: "Draws must be between 0 and 10000." });
  const vars = form.output.vars.split(",").map((s) => s.trim()).filter(Boolean);
  if (vars.some((v) => !NAME_RE.test(v))) problems.push({ field: "vars", message: "Variable names are letters, digits, _ . -" });
  const output: OutputSelector = {
    kind: form.output.kind,
    summary: form.output.summary.length ? form.output.summary : null,
    draws,
    units: form.output.units,
    names: form.output.names,
    ...(vars.length ? { vars } : {}),
  };
  request.output = output;

  if (form.seed.trim()) {
    const seed = Number(form.seed);
    if (!Number.isInteger(seed) || seed < 0 || seed >= 2 ** 32) problems.push({ field: "seed", message: "Seed must be a whole number below 2³²." });
    else request.seed = seed;
  }
  return problems.length ? { problems } : { request };
}

/** A side table the fitted spec declares, as the score form offers it. */
export interface SideTableOption {
  name: string;
  /** `pinned` tables are replayed from the fit and cannot be supplied. */
  policy: "pinned" | "latest" | "supplied";
  /** The reference the spec used at fit time (the form's default for `supplied`). */
  ref: string;
}

/** Side tables of a spec document (`data.side_tables`), tolerant of malformed input. */
export function sideTableOptions(document: unknown): SideTableOption[] {
  const data = (document as { data?: { side_tables?: unknown } } | null | undefined)?.data;
  const tables = data?.side_tables;
  if (typeof tables !== "object" || tables === null || Array.isArray(tables)) return [];
  const out: SideTableOption[] = [];
  for (const [name, raw] of Object.entries(tables as Record<string, unknown>)) {
    if (typeof raw !== "object" || raw === null) continue;
    const { ref, score_policy } = raw as { ref?: unknown; score_policy?: unknown };
    const policy = score_policy === "pinned" || score_policy === "latest" ? score_policy : "supplied";
    out.push({ name, policy, ref: typeof ref === "string" ? ref : "" });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}
