/** Score form state and its translation to a `ScoreRequest` document. */
import type { OutputSelector, ScoreRequest } from "@/api/types";

export type ScoreMode = NonNullable<ScoreRequest["mode"]>;
export type InterventionOp = "multiply" | "add" | "set";
/** Where the values to score come from: typed into the grid, a dataset, or nothing new. */
export type InputSource = "typed" | "dataset" | "none";
/** How a forecast fills future values of inputs it needs (`FutureCovariates`); "" = server default. */
export type FutureCovariates = "" | "supplied" | "carry_last" | "seasonal_naive" | "zero";

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
  source: InputSource;
  /** Payload dataset (future inputs, new levels, the window to score); empty = none. */
  data: string;
  futureCovariates: FutureCovariates;
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

/** How a model's output is represented (`Capabilities.representation`). */
export type Representation = "draws" | "quantiles" | "interval" | "point" | "moments";

/**
 * What each representation can be summarised as, its default summaries and whether draws can
 * be returned: `mapi_spec.capabilities.REPRESENTATION_RULES` / `_DEFAULT_SUMMARY` (the
 * server checks the request against the same rules).
 */
export const REPRESENTATIONS: Record<Representation, { options: string[]; defaults: string[]; draws: boolean }> = {
  draws: { options: ["mean", "median", "sd", "hdi_50", "hdi_90", "hdi_95", "q5", "q50", "q95"], defaults: ["mean", "hdi_90"], draws: true },
  quantiles: { options: ["median", "q5", "q25", "q50", "q75", "q95", "eti_90"], defaults: ["median", "q5", "q95"], draws: false },
  interval: { options: ["point", "interval"], defaults: ["point", "interval"], draws: false },
  point: { options: ["point", "mean"], defaults: ["point"], draws: false },
  moments: { options: ["mean", "sd", "eti_90", "point"], defaults: ["mean", "sd"], draws: false },
};

/** Summaries offered when the representation is unknown (an older model version). */
export const SUMMARY_OPTIONS = REPRESENTATIONS.draws.options;

export function asRepresentation(value: unknown): Representation | null {
  return typeof value === "string" && value in REPRESENTATIONS ? (value as Representation) : null;
}

export function defaultScoreForm(): ScoreForm {
  return {
    mode: "forecast",
    horizonDim: "time",
    horizonPeriods: "8",
    inputsStage: "raw",
    source: "typed",
    data: "",
    futureCovariates: "carry_last",
    scenario: "",
    sideTables: {},
    interventions: [],
    output: { kind: "expected", summary: ["mean", "hdi_90"], draws: "0", units: "original", names: "dataset", vars: "" },
    seed: "",
  };
}

/** Modes that read a dataset only (the server refuses inline data and no data for them). */
export const DATASET_ONLY: ScoreMode[] = ["filter", "evaluate"];

/** Whether the form can offer "No new data" for a mode, and the reason in plain words when not. */
export type NoDataRule = { ok: true } | { ok: false; reason: string };

/**
 * When a score request can go without new data. The server refuses a request that has no
 * `data`, no inline values, no `scenario` and no `horizon` ("the score request has no data",
 * `check_score_request` in `mapi_server/services/specs.py`). A forecast always sends its
 * horizon, and a scenario dataset is data. Filter and evaluate read a dataset only. `modes` are
 * the modes the model declares (`Capabilities.score_modes`), to tell the user if Forecast is
 * an alternative.
 */
export function noDataRule(mode: ScoreMode, modes: readonly ScoreMode[], scenario: string): NoDataRule {
  if (DATASET_ONLY.includes(mode)) return { ok: false, reason: "This mode reads new observations from a dataset. Select a dataset." };
  if (mode === "forecast" || scenario.trim()) return { ok: true };
  const base = "No new data is not available for this mode. The server needs values to score, a scenario dataset or a forecast horizon.";
  return {
    ok: false,
    reason: modes.includes("forecast")
      ? `${base} Type values or use a dataset. To score from the end of the fit without new data, select Forecast.`
      : `${base} This model cannot forecast, so it always needs values to score. Type values or use a dataset.`,
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
    if (eq <= 0) return `"${part}" does not have the form dim=value`;
    const dim = part.slice(0, eq).trim();
    if (!NAME_RE.test(dim)) return `"${dim}" is not a correct dim name`;
    const values = part.slice(eq + 1).split(",").map((s) => s.trim()).filter(Boolean);
    if (!values.length) return `${dim} has no values`;
    out[dim] = values.map(scalar);
  }
  return out;
}

/** Validate the form and build the request (or return the problems). With `representation`,
 * summaries and draws the model's output cannot give are problems, not server refusals. */
export function buildScoreRequest(
  form: ScoreForm,
  modelRef: string,
  representation: Representation | null = null,
): { request: ScoreRequest } | { problems: FormProblem[] } {
  const problems: FormProblem[] = [];
  const request: ScoreRequest = {
    apiVersion: "mapi/v1",
    kind: "ScoreRequest",
    model: modelRef,
    mode: form.mode,
    inputs_stage: form.inputsStage.trim() || "raw",
  };

  if (!/^[a-z_][a-z0-9_]{0,62}$/.test(request.inputs_stage ?? "raw")) {
    problems.push({ field: "inputsStage", message: "The stage name is not correct. Type raw or a step id in lowercase." });
  }
  if (form.mode === "forecast") {
    const n = Number(form.horizonPeriods);
    if (!NAME_RE.test(form.horizonDim)) problems.push({ field: "horizonDim", message: "Type the dim to extend." });
    if (!Number.isInteger(n) || n < 1 || n > 100_000) problems.push({ field: "horizonPeriods", message: "The number of periods is not correct. Type a whole number of 1 or more." });
    else request.horizon = { dim: form.horizonDim, periods: n };
  }
  if (form.mode === "forecast" && form.futureCovariates) request.future_covariates = form.futureCovariates;
  if (form.source === "dataset" && form.data.trim()) {
    if (!DATASET_REF_RE.test(form.data.trim())) problems.push({ field: "data", message: "The dataset reference is not correct. Type a reference, for example dataset://default/eu_media_future@v1." });
    else request.data = form.data.trim();
  }
  if (form.scenario.trim()) {
    if (!DATASET_REF_RE.test(form.scenario.trim())) problems.push({ field: "scenario", message: "The dataset reference is not correct. Type a reference, for example dataset://default/plan_2027h1@v1." });
    else request.scenario = form.scenario.trim();
  }
  const sideTables: Record<string, string> = {};
  for (const [name, ref] of Object.entries(form.sideTables)) {
    const value = ref.trim();
    if (!value) continue;
    if (!DATASET_REF_RE.test(value)) problems.push({ field: `sideTables.${name}`, message: `Side table ${name}: the value is not a dataset reference. Type a dataset reference.` });
    else sideTables[name] = value;
  }
  if (Object.keys(sideTables).length) request.side_tables = sideTables;
  const interventions: NonNullable<ScoreRequest["interventions"]> = [];
  form.interventions.forEach((row, i) => {
    if (!NAME_RE.test(row.var)) {
      problems.push({ field: `interventions.${i}`, message: `Intervention ${i + 1}: type a variable name.` });
      return;
    }
    const sel = parseSel(row.sel);
    if (typeof sel === "string") {
      problems.push({ field: `interventions.${i}`, message: `Intervention ${i + 1}: ${sel}.` });
      return;
    }
    const value = Number(row.value);
    if (row.value.trim() === "" || !Number.isFinite(value)) {
      problems.push({ field: `interventions.${i}`, message: `Intervention ${i + 1}: type a number.` });
      return;
    }
    const selFlat: Record<string, string | number | boolean | (string | number | boolean)[]> = {};
    for (const [k, v] of Object.entries(sel)) selFlat[k] = v.length === 1 ? (v[0] as string | number | boolean) : v;
    interventions.push({ var: row.var, sel: selFlat, [row.op]: value });
  });
  if (interventions.length) request.interventions = interventions;

  const draws = Number(form.output.draws || "0");
  if (!Number.isInteger(draws) || draws < 0 || draws > 10_000) problems.push({ field: "draws", message: "The number of draws is not correct. Type a number from 0 to 10000." });
  else if (draws > 0 && representation && !REPRESENTATIONS[representation].draws) {
    problems.push({ field: "draws", message: `This model returns ${representation}, not draws. Set draws to 0.` });
  }
  if (representation) {
    const bad = form.output.summary.filter((s) => !REPRESENTATIONS[representation].options.includes(s));
    if (bad.length) problems.push({ field: "summary", message: `This model returns ${representation}. The app cannot calculate ${bad.join(", ")} from it. Remove these summaries.` });
  }
  const vars = form.output.vars.split(",").map((s) => s.trim()).filter(Boolean);
  if (vars.some((v) => !NAME_RE.test(v))) problems.push({ field: "vars", message: "A variable name is not correct. Use only letters, digits, _ . -" });
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
    if (!Number.isInteger(seed) || seed < 0 || seed >= 2 ** 32) problems.push({ field: "seed", message: "The seed is not correct. Type a whole number less than 2³²." });
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
