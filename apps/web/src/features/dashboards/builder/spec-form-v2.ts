/**
 * The builder's pure model of a layout 2 draft (v0.17, plan §3.7): the presets and what each
 * allows (`mapi_spec.dashboard_v2.PRESET_RULES`), the panel edits (add from the source
 * catalog, remove, move up and down), a preset switch, the input sections, and the checks
 * that mirror the validators of `DashboardSpecV2` so the form says what is wrong before the
 * server does (`specIssuesV2`). The server still validates every save and every publish
 * (`check_spec_v2` adds the checks against the model version).
 */
import {
  MAX_BATCH_ROWS,
  MAX_CHANNELS,
  MAX_HORIZON,
  MAX_KPI_PANELS,
  MAX_PANELS,
  MAX_SCENARIO_INPUTS,
  MAX_SCENARIO_ROWS,
  MAX_SCENARIO_STEPS,
  MAX_SPEC_BYTES,
  MAX_TILES,
  PANEL_ID_RE,
  RECIPE_INTENTS,
  type CatalogInput,
  type CatalogOutput,
  type ChartIntent,
  type DashboardSpecV2,
  type Panel,
  type PanelKind,
  type PresetKey,
  type ScenarioInputDef,
  type ShareInput,
  type SourceCatalog,
  type SubjectUnit,
} from "../types";
import { channelLabel, channelsFrom, CURRENCY_RE, MAX_CLIENT_NAME, MAX_EXACT_RUNS, MAX_LABEL, MAX_NOTE, MAX_SUMMARY, MAX_TITLE, NAME_RE, type FormIssue, type SpecContext } from "./spec-form";

/** The inputs of a layout 2 dashboard (`dashboard_v2.InputKey`). */
export type InputKey = "whatif" | "scenario_rows" | "actuals" | "batch";
export type PanelOf<K extends PanelKind> = Extract<Panel, { kind: K }>;
export type KpiPanel = PanelOf<"kpi">;
export type ChartPanel = PanelOf<"chart">;
export type TablePanel = PanelOf<"table">;
export type NotePanel = PanelOf<"note">;
export type RecipePanel = PanelOf<"recipe">;
export type MmmPanel = PanelOf<"mmm">;
export type PanelSource = KpiPanel["source"];
export type ReduceHow = NonNullable<NonNullable<KpiPanel["reduce"]>["how"]>;
export type Band = NonNullable<KpiPanel["band"]>;
export type MmmPart = MmmPanel["part"];
export type PeriodLabelV2 = NonNullable<DashboardSpecV2["period_label"]>;

export const MAX_PANEL_TITLE = 80;
export const MAX_CAPTION = 200;
export const MAX_NOTE_TEXT = 1200;
export const MAX_TABLE_COLUMNS = 12;
export const MAX_TABLE_ROWS = 50;
export const MAX_CHART_ITEMS = 60;
export const MAX_BATCH_RUNS = 50;
export const MAX_SCENARIO_OUTPUTS = 4;
export const MAX_DECIMALS_V2 = 6;
export const MAX_HELP = 200;
export const MAX_POSITIVE_LABEL = 64;
/** `RecipePanel.recipe`: a platform recipe id (`pack.name`). */
export const RECIPE_RE = /^[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*$/;

// ------------------------------------------------------------------------------ presets

interface Rules {
  kinds: readonly PanelKind[];
  intents: readonly ChartIntent[];
  inputs: readonly InputKey[];
}

const BASE_KINDS = ["kpi", "chart", "table", "note", "recipe"] as const satisfies readonly PanelKind[];
const ALL_INTENTS = [
  "fan",
  "line",
  "bars",
  "range",
  "diverging",
  "scatter",
  "residuals",
  "calibration",
  "confusion",
  "probability_bars",
  "field",
  "area_bars",
  "effect",
  "accuracy_history",
  "decomposition",
  "state_sequence",
] as const satisfies readonly ChartIntent[];

const rules = (intents: string, inputs: string, mmm = false): Rules => ({
  kinds: mmm ? [...BASE_KINDS, "mmm"] : BASE_KINDS,
  intents: intents.split(" ") as ChartIntent[],
  inputs: inputs.split(" ") as InputKey[],
});

/** `PRESET_RULES`: the panel kinds, chart intents and inputs each preset allows. */
export const PRESET_RULES: Readonly<Record<PresetKey, Rules>> = {
  mmm_planner: rules("fan line bars range diverging decomposition accuracy_history scatter residuals", "whatif actuals", true),
  forecast: rules("fan line range bars accuracy_history decomposition state_sequence residuals scatter diverging", "scenario_rows actuals"),
  regression: rules("range bars scatter residuals accuracy_history line fan decomposition", "scenario_rows actuals batch"),
  classifier: rules("probability_bars bars calibration confusion accuracy_history range line", "scenario_rows actuals batch"),
  causal_impact: rules("effect diverging bars line fan range accuracy_history scatter", "actuals"),
  field_map: rules("field area_bars bars line fan range", "scenario_rows actuals"),
  image: rules("bars probability_bars confusion accuracy_history line", "batch"),
  generic: rules(ALL_INTENTS.join(" "), "scenario_rows actuals batch"),
};

export const PRESET_WORDS: Readonly<Record<PresetKey, { label: string; hint: string }>> = {
  mmm_planner: { label: "Media planner", hint: "The media scorecard, plan scenarios on the response grid and the return on media." },
  forecast: { label: "Forecast", hint: "The next periods with their likely range, and how close earlier forecasts were." },
  regression: { label: "Predictions", hint: "One value for each case, with its likely range. Clients can try cases and score a file." },
  classifier: { label: "Classifier", hint: "The chance of an outcome for each case, and how often the model is right." },
  causal_impact: { label: "Effect of a change", hint: "What a change did: the result against what would have happened without it." },
  field_map: { label: "Map", hint: "A map of the model's results, with key numbers for the whole area." },
  image: { label: "Image model", hint: "How well the model sorts images, from its fit, with the model's plots." },
  generic: { label: "General", hint: "Any model: key numbers and charts of its outputs." },
};

export const PANEL_KIND_WORD: Readonly<Record<PanelKind, string>> = {
  kpi: "Key number",
  chart: "Chart",
  table: "Table",
  note: "Note",
  recipe: "Model plot",
  mmm: "Planner part",
};

export const INTENT_WORD: Readonly<Record<ChartIntent, string>> = {
  fan: "Line with likely range",
  line: "Line",
  bars: "Bars",
  range: "Values with ranges",
  diverging: "Above and below zero",
  scatter: "Predicted against actual",
  residuals: "Errors over time",
  calibration: "Calibration",
  confusion: "Right and wrong answers",
  probability_bars: "Chance of each class",
  field: "Map",
  area_bars: "Areas as bars",
  effect: "Effect over time",
  accuracy_history: "Accuracy over time",
  decomposition: "Parts of the result",
  state_sequence: "States over time",
};

export const MMM_PART_WORD: Readonly<Record<MmmPart, string>> = {
  tiles: "This period's tiles",
  scorecard: "Channel scorecard",
  changes: "Changes against the plan",
  roi_trend: "Return on media over time",
  scenarios: "Saved plan scenarios",
};

export const SUBJECT_UNIT_WORD: Readonly<Record<SubjectUnit, string>> = {
  number: "Number",
  count: "Count (orders, visits)",
  currency: "Money",
  ratio: "Ratio",
  percent: "Percent",
  probability: "Chance (0 to 1)",
  rate: "Rate",
  index: "Index",
  label: "Class name",
};

export const PERIODS_V2: readonly { value: PeriodLabelV2; label: string }[] = [
  { value: "day", label: "Day" },
  { value: "week", label: "Week" },
  { value: "month", label: "Month" },
  { value: "quarter", label: "Quarter" },
  { value: "year", label: "Year" },
  { value: "period", label: "Period" },
  { value: "case", label: "Case" },
];

export const INPUT_WORD: Readonly<Record<InputKey, string>> = {
  whatif: "Plan scenarios",
  scenario_rows: "Try a case",
  actuals: "Number uploads",
  batch: "Score a file",
};

/** Why preset `preset` does not allow input `key`, or `null` when it does. */
export function inputReason(preset: PresetKey, key: InputKey): string | null {
  if (PRESET_RULES[preset].inputs.includes(key)) return null;
  return `The ${PRESET_WORDS[preset].label.toLowerCase()} layout has no ${INPUT_WORD[key].toLowerCase()}.`;
}

/** The inputs a spec turns on (`DashboardSpecV2.enabled_inputs`). */
export function enabledInputs(spec: DashboardSpecV2): InputKey[] {
  const out: InputKey[] = [];
  if (spec.mmm?.whatif?.enabled ?? false) out.push("whatif");
  if (spec.scenario_rows?.enabled) out.push("scenario_rows");
  if (spec.actuals?.enabled) out.push("actuals");
  if (spec.batch?.enabled) out.push("batch");
  return out;
}

/**
 * What a share link of this dashboard may allow (the access editor's toggles). Image
 * dashboards score files for signed-in analysts only: clients and links never get batch.
 */
export function shareInputsOfV2(spec: DashboardSpecV2): ShareInput[] {
  const on = new Set(enabledInputs(spec));
  if (spec.preset === "image") on.delete("batch");
  const order: ShareInput[] = ["whatif", "actuals", "scenario_rows", "batch"];
  return order.filter((k) => on.has(k));
}

/** Inputs that start runs (exact what-ifs, tried cases, uploads, scored files). */
export function startsRuns(spec: DashboardSpecV2): boolean {
  return (spec.mmm?.whatif?.exact ?? false) || Boolean(spec.scenario_rows?.enabled) || Boolean(spec.actuals?.enabled) || Boolean(spec.batch?.enabled);
}

/**
 * Warnings about the run limits (they do not stop a save): with 0 runs a day every upload,
 * tried case and scored file is refused, and with 0 batch runs no file can be scored.
 */
export function limitWarnings(spec: DashboardSpecV2): FormIssue[] {
  const out: FormIssue[] = [];
  const runs = spec.limits?.exact_runs_per_day ?? 20;
  const spend = [spec.actuals?.enabled ? "uploads" : null, spec.scenario_rows?.enabled ? "tried cases" : null, spec.batch?.enabled ? "scored files" : null, spec.mmm?.whatif?.exact ? "exact results" : null].filter(Boolean);
  if (runs === 0 && spend.length) out.push({ pointer: "/limits/exact_runs_per_day", message: `With 0 runs each day, every one of the ${spend.join(", ")} is refused. Allow at least 1 run.` });
  if (spec.batch?.enabled && (spec.limits?.batch_runs_per_day ?? 3) === 0) out.push({ pointer: "/limits/batch_runs_per_day", message: "With 0 scored files each day, every file is refused. Allow at least 1." });
  return out;
}

// ------------------------------------------------------------------------------ catalog

export function outputOf(catalog: SourceCatalog | null | undefined, name: string | null | undefined): CatalogOutput | null {
  return catalog?.outputs?.find((o) => o.name === name) ?? null;
}

/** The range a panel of output `o` shows: outcome when predictive works, mean otherwise, none for points. */
export function bandOf(o: CatalogOutput | null): Band {
  if (!o) return "outcome";
  if (o.representation === "point") return "none";
  return o.predictive_ok ? "outcome" : "mean";
}

/** Why band `band` does not fit output `o`, or `null`. */
export function bandReason(o: CatalogOutput | null, band: Band): string | null {
  if (!o || band === "none") return null;
  if (o.representation === "point") return "The model gives one value per case without a range.";
  if (band === "outcome" && !o.predictive_ok) return "The model cannot give the range of one new result for this output. Show the range of the average.";
  return null;
}

/** The intents a chart of output `o` may take on preset `preset`. */
export function chartIntentsOf(preset: PresetKey, o: CatalogOutput | null): ChartIntent[] {
  const allowed = PRESET_RULES[preset].intents.filter((i) => i !== "accuracy_history");
  return o ? allowed.filter((i) => (o.intents ?? []).includes(i)) : allowed;
}

/** The main output of the dashboard: the subject's, else `y_hat`, else the first one. */
export function primaryOutput(spec: DashboardSpecV2, catalog: SourceCatalog | null | undefined): CatalogOutput | null {
  const outs = catalog?.outputs ?? [];
  return outs.find((o) => o.name === spec.subject.variable) ?? outs.find((o) => o.name === "y_hat") ?? outs[0] ?? null;
}

/** The default reduction of a KPI on output `o` (the last period of a time output). */
export function defaultReduce(o: CatalogOutput | null): ReduceHow {
  const r = o?.reduces ?? [];
  if (o?.time_dim && r.includes("last")) return "last";
  if (o?.shape === "classification" && r.includes("top_class") && !(o.classes?.length === 2)) return "top_class";
  return r.includes("none") || !r.length ? "none" : (r[0] ?? "none");
}

/** A label for an output name ("y_hat" → "Prediction"). */
export function outputLabel(name: string): string {
  if (name === "y_hat") return "Prediction";
  return channelLabel(name) || name;
}

// ------------------------------------------------------------------------------ panels

/** A new panel id from `base` that no panel of the spec has. */
export function newPanelId(spec: Pick<DashboardSpecV2, "panels">, base: string): string {
  const stem = (base.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^[^a-z]+/, "") || "panel").slice(0, 28);
  const taken = new Set(spec.panels.map((p) => p.id));
  if (!taken.has(stem)) return stem;
  for (let n = 2; n < 1000; n++) {
    const id = `${stem}_${n}`;
    if (!taken.has(id)) return id;
  }
  return `${stem}_${Date.now() % 1000}`;
}

/** Why a panel of kind `kind` cannot be added now, in plain words, or `null`. */
export function addPanelReason(spec: DashboardSpecV2, kind: PanelKind, catalog: SourceCatalog | null | undefined): string | null {
  if (spec.panels.length >= MAX_PANELS) return `A dashboard has at most ${MAX_PANELS} panels. Remove one first.`;
  if (!PRESET_RULES[spec.preset].kinds.includes(kind)) return kind === "mmm" ? "Planner parts are only for the media planner layout." : `The ${PRESET_WORDS[spec.preset].label.toLowerCase()} layout has no ${PANEL_KIND_WORD[kind].toLowerCase()} panels.`;
  if (kind === "kpi" && spec.panels.filter((p) => p.kind === "kpi").length >= MAX_KPI_PANELS) return `A dashboard shows at most ${MAX_KPI_PANELS} key numbers. Remove one first.`;
  if (kind === "note") return null;
  if (catalog === undefined) return "Wait until the app has read what the model offers.";
  if (catalog === null) return "The app could not read what the model offers. Check the source.";
  const outs = catalog.outputs ?? [];
  if (kind === "kpi" && !outs.length && !(catalog.metrics ?? []).length) return "The model has no outputs or fit metrics to show.";
  if (kind === "chart" && !outs.some((o) => chartIntentsOf(spec.preset, o).length) && !PRESET_RULES[spec.preset].intents.includes("accuracy_history"))
    return "No output of the model fits a chart of this layout.";
  if (kind === "table" && !outs.length) return "The model has no outputs to show in a table.";
  if (kind === "recipe" && !(catalog.pictures ?? []).length) return "The model version has no plots that clients may see.";
  if (kind === "mmm" && spec.panels.filter((p) => p.kind === "mmm").length >= 5) return "Every planner part is on the dashboard.";
  return null;
}

/** A first panel of kind `kind`, bound to the main output (or `null` with {@link addPanelReason}). */
export function defaultPanel(spec: DashboardSpecV2, kind: PanelKind, catalog: SourceCatalog | null | undefined): Panel | null {
  if (addPanelReason(spec, kind, catalog)) return null;
  const o = primaryOutput(spec, catalog);
  switch (kind) {
    case "note":
      return { kind, id: newPanelId(spec, "note"), title: "About these numbers", text: "Write a note for the client." };
    case "kpi": {
      if (o) return { kind, id: newPanelId(spec, "kpi"), title: (spec.subject.label || outputLabel(o.name)).slice(0, MAX_PANEL_TITLE), source: { kind: "output", variable: o.name }, stat: "centre", reduce: { how: defaultReduce(o), over: [] }, band: bandOf(o), compare: "previous", good: "up" };
      const m = catalog?.metrics?.[0];
      return m ? { kind, id: newPanelId(spec, `m_${m.key}`), title: outputLabel(m.key).slice(0, MAX_PANEL_TITLE), source: { kind: "metric", key: m.key, labels: { ...(m.labels ?? {}) } }, stat: "value", band: "none", compare: "none", good: "none" } : null;
    }
    case "chart": {
      const withIntent = [o, ...(catalog?.outputs ?? [])].find((x) => x && chartIntentsOf(spec.preset, x).length) ?? null;
      if (withIntent) {
        const intent = chartIntentsOf(spec.preset, withIntent)[0]!;
        return { kind, id: newPanelId(spec, "chart"), title: outputLabel(withIntent.name).slice(0, MAX_PANEL_TITLE), intent, source: { kind: "output", variable: withIntent.name }, band: bandOf(withIntent), max_items: 20 };
      }
      return { kind, id: newPanelId(spec, "accuracy"), title: "Accuracy over time", intent: "accuracy_history", source: { kind: "accuracy", metric: spec.preset === "classifier" || spec.preset === "image" ? "accuracy" : "mae" }, band: "none", max_items: 20 };
    }
    case "table":
      return o ? { kind, id: newPanelId(spec, "table"), title: outputLabel(o.name).slice(0, MAX_PANEL_TITLE), source: { kind: "output", variable: o.name }, columns: [], max_rows: 20 } : null;
    case "recipe": {
      const pic = catalog?.pictures?.[0];
      return pic ? { kind, id: newPanelId(spec, `p_${pic.recipe.split(".").pop() ?? "plot"}`), title: (pic.title || pic.recipe).slice(0, MAX_PANEL_TITLE), recipe: pic.recipe, plot_id: pic.plot_id } : null;
    }
    case "mmm": {
      const used = new Set(spec.panels.flatMap((p) => (p.kind === "mmm" ? [p.part] : [])));
      const part = (Object.keys(MMM_PART_WORD) as MmmPart[]).find((x) => !used.has(x)) ?? "scorecard";
      return { kind, id: newPanelId(spec, part), title: MMM_PART_WORD[part], part };
    }
  }
}

export function addPanel(spec: DashboardSpecV2, panel: Panel): DashboardSpecV2 {
  return { ...spec, panels: [...spec.panels, panel] };
}

export function removePanel(spec: DashboardSpecV2, index: number): DashboardSpecV2 {
  return { ...spec, panels: spec.panels.filter((_, i) => i !== index) };
}

/** Move panel `index` by `delta` places (clamped; the order is the readout's order). */
export function movePanel(spec: DashboardSpecV2, index: number, delta: number): DashboardSpecV2 {
  const to = Math.max(0, Math.min(spec.panels.length - 1, index + delta));
  if (to === index || index < 0 || index >= spec.panels.length) return spec;
  const panels = [...spec.panels];
  const [p] = panels.splice(index, 1);
  panels.splice(to, 0, p!);
  return { ...spec, panels };
}

export function setPanel(spec: DashboardSpecV2, index: number, panel: Panel): DashboardSpecV2 {
  return { ...spec, panels: spec.panels.map((p, i) => (i === index ? panel : p)) };
}

/** The source of a KPI or chart re-bound to output `name`, its reduction and band fitted to it. */
export function bindOutput<P extends KpiPanel | ChartPanel>(p: P, o: CatalogOutput): P {
  const how = p.reduce?.how ?? "none";
  const reduce = (o.reduces ?? []).includes(how) ? p.reduce : { how: defaultReduce(o), over: [] };
  const current: string = (p.kind === "kpi" && "stat" in p && typeof p.stat === "string" ? p.stat : null) ?? "centre";
  const stat = p.kind === "kpi" && !(o.stats ?? ["centre"]).some((x) => x === current) ? "centre" : undefined;
  return { ...p, source: { kind: "output", variable: o.name }, reduce, band: bandOf(o), ...(stat ? { stat } : {}) };
}

// ------------------------------------------------------------------------------ preset switch

/** Why the spec cannot move to preset `to`, or `null`. */
export function presetSwitchReason(spec: DashboardSpecV2, to: PresetKey, ctx: SpecContext = {}): string | null {
  if (to === spec.preset) return null;
  if (to === "mmm_planner" && !spec.mmm && !ctx.gridChannels?.length) return "The media planner needs a model with a response grid of channels.";
  return null;
}

export interface PresetSwitch {
  spec: DashboardSpecV2;
  /** What the switch removes or turns off, in plain words (the confirm dialog lists it). */
  removed: string[];
}

/**
 * The spec on preset `to`: panels the preset does not allow go, inputs it does not allow turn
 * off, the planner part comes (channels from the grid) or goes. An empty layout gets a note.
 */
export function switchPreset(spec: DashboardSpecV2, to: PresetKey, ctx: SpecContext = {}): PresetSwitch {
  const r = PRESET_RULES[to];
  const removed: string[] = [];
  const panels = spec.panels.filter((p) => {
    const ok = r.kinds.includes(p.kind) && (p.kind !== "chart" || r.intents.includes(p.intent));
    if (!ok) removed.push(`The panel “${p.title}”.`);
    return ok;
  });
  let mmm = spec.mmm ?? null;
  if (to === "mmm_planner" && !mmm && ctx.gridChannels?.length) {
    mmm = { channels: channelsFrom(ctx.gridChannels), whatif: { enabled: true, exact: false }, tiles: ["expected", "vs_expected", "return_on_media", "in_range_spend"].slice(0, MAX_TILES) as NonNullable<NonNullable<DashboardSpecV2["mmm"]>["tiles"]> };
    panels.unshift({ kind: "mmm", id: newPanelId({ panels }, "tiles"), title: MMM_PART_WORD.tiles, part: "tiles" }, { kind: "mmm", id: newPanelId({ panels }, "scorecard"), title: MMM_PART_WORD.scorecard, part: "scorecard" });
  }
  if (to !== "mmm_planner" && mmm) {
    removed.push(`The ${mmm.channels.length} channels and the plan scenarios.`);
    mmm = null;
  }
  const next: DashboardSpecV2 = { ...spec, preset: to, panels, mmm };
  if (spec.scenario_rows?.enabled && !r.inputs.includes("scenario_rows")) {
    next.scenario_rows = { ...spec.scenario_rows, enabled: false };
    removed.push("Try a case is turned off.");
  }
  if (spec.actuals?.enabled && !r.inputs.includes("actuals")) {
    next.actuals = { ...spec.actuals, enabled: false, allow_refit_requests: false };
    removed.push("Number uploads are turned off.");
  }
  if (spec.batch?.enabled && !r.inputs.includes("batch")) {
    next.batch = { ...spec.batch, enabled: false };
    removed.push("Score a file is turned off.");
  }
  if (!next.panels.length) next.panels = [{ kind: "note", id: "about", title: "About this dashboard", text: "The results of the model show here when you add panels." }];
  return { spec: next, removed };
}

// ------------------------------------------------------------------------------ scenario inputs

export interface SeededInput {
  def: ScenarioInputDef;
  /** The range comes from the fit data (not declared bounds): the analyst must check it. */
  fromProfile: boolean;
}

const roundNice = (v: number) => Number(v.toPrecision(6));

/** A scenario input for catalog input `c`, its range from declared bounds, else the fit data. */
export function seedInput(c: CatalogInput, taken: readonly string[] = []): SeededInput | null {
  const name = c.var ?? c.dim ?? "";
  if (!name || taken.includes(name)) return null;
  const bind = c.var ? { var: c.var } : { dim: c.dim! };
  const label = channelLabel(name) || name;
  if (c.kind === "number" || c.kind === "integer") {
    const lo = c.bound_min ?? c.profile_min ?? null;
    const hi = c.bound_max ?? c.profile_max ?? null;
    const fromProfile = c.bound_min == null || c.bound_max == null;
    let min = lo ?? 0;
    let max = hi ?? (lo !== null ? lo + 1 : 1);
    if (c.kind === "integer") {
      min = Math.floor(min);
      max = Math.ceil(max);
    }
    if (!(max > min)) max = min + 1;
    const span = max - min;
    let step: number | null = c.kind === "integer" ? Math.max(1, Math.ceil(span / MAX_SCENARIO_STEPS)) : roundNice(span / 100);
    if (step !== null && !(step > 0)) step = null;
    const mid = c.kind === "integer" ? Math.round((min + max) / 2) : roundNice((min + max) / 2);
    return { def: { name, label, bind, type: c.kind, min: roundNice(min), max: roundNice(max), step, default: mid, applies: "row" }, fromProfile };
  }
  if (c.kind === "choice" || c.kind === "text") {
    const choices = (c.levels ?? []).slice(0, 200);
    if (!choices.length) return null;
    return { def: { name, label, bind, type: "choice", choices, default: choices[0]!, applies: "row" }, fromProfile: false };
  }
  if (c.kind === "date") {
    if (!c.date_min || !c.date_max) return null;
    return { def: { name, label, bind, type: "date", date_min: c.date_min, date_max: c.date_max, default: c.date_max, applies: "row" }, fromProfile: false };
  }
  return { def: { name, label, bind, type: "toggle", default: false, applies: "row" }, fromProfile: false };
}

/** Is the range of input `d` the fit data's range of catalog input `c` (to check by hand)? */
export function rangeFromProfile(d: ScenarioInputDef, c: CatalogInput | null | undefined): boolean {
  if (!c || (d.type !== "number" && d.type !== "integer")) return false;
  if (c.bound_min != null && c.bound_max != null) return false;
  return c.profile_min != null && c.profile_max != null;
}

export function catalogInputOf(catalog: SourceCatalog | null | undefined, d: Pick<ScenarioInputDef, "bind">): CatalogInput | null {
  const b = d.bind as { var?: string; dim?: string };
  return catalog?.inputs?.find((c) => (b.var ? c.var === b.var : c.dim === b.dim)) ?? null;
}

// ------------------------------------------------------------------------------ save

const blank = (v: string | null | undefined): string | null => (v && v.trim() ? v.trim() : null);

/** The layout 2 spec as it is saved: trimmed texts, empty optional texts as `null`. */
export function cleanSpecV2(spec: DashboardSpecV2): DashboardSpecV2 {
  const s = spec.subject;
  return {
    ...spec,
    title: spec.title.trim(),
    client_name: blank(spec.client_name),
    summary: blank(spec.summary),
    note: blank(spec.note),
    subject: { ...s, label: s.label.trim(), currency: s.unit === "currency" ? (blank(s.currency)?.toUpperCase() ?? null) : null, positive_label: blank(s.positive_label) },
    panels: spec.panels.map((p) => ({ ...p, title: p.title.trim(), caption: blank(p.caption) })),
    refresh: { ...spec.refresh, follow_endpoint: spec.source.kind === "endpoint" ? (spec.refresh?.follow_endpoint ?? false) : false },
  };
}

// ------------------------------------------------------------------------------ checks

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

function inputIssues(d: ScenarioInputDef, p: string, add: (pointer: string, message: string) => void): void {
  const who = d.label || d.name || "this input";
  if (!NAME_RE.test(d.name)) add(`${p}/name`, `The input name “${d.name}” is not correct. Use letters, digits, _, . and -.`);
  if (!d.label.trim()) add(`${p}/label`, `Type a label for ${d.name || "the input"}.`);
  else if (d.label.trim().length > MAX_LABEL) add(`${p}/label`, `Use ${MAX_LABEL} characters or fewer for the label of ${d.name}.`);
  if ((d.help ?? "").length > MAX_HELP) add(`${p}/help`, `Use ${MAX_HELP} characters or fewer for the help of ${who}.`);
  if (d.type === "number" || d.type === "integer") {
    const lo = d.min;
    const hi = d.max;
    if (!isNum(lo) || !isNum(hi)) return add(`${p}/min`, `Give ${who} a lowest and a highest value.`);
    if (lo >= hi) return add(`${p}/min`, `The lowest value of ${who} must be smaller than the highest.`);
    const step = d.step ?? (d.type === "integer" ? 1 : null);
    if (step !== null) {
      if (!isNum(step) || step <= 0 || step > hi - lo) add(`${p}/step`, `The step of ${who} must be more than 0 and smaller than its range.`);
      else if ((hi - lo) / step > MAX_SCENARIO_STEPS) add(`${p}/step`, `The range of ${who} holds more than ${MAX_SCENARIO_STEPS} steps. Use a larger step.`);
    }
    if (!isNum(d.default)) add(`${p}/default`, `Give ${who} a default number.`);
    else if (d.default < lo || d.default > hi) add(`${p}/default`, `The default of ${who} must be from ${lo} to ${hi}.`);
    if (d.type === "integer" && [lo, hi, d.step, d.default].some((v) => isNum(v) && !Number.isInteger(v))) add(`${p}/step`, `${who} takes whole numbers: use whole numbers for its range, step and default.`);
  } else if (d.type === "choice") {
    const ch = d.choices ?? [];
    if (!ch.length) add(`${p}/choices`, `Give ${who} at least one choice.`);
    else if (new Set(ch).size !== ch.length) add(`${p}/choices`, `Each choice of ${who} can be in the list one time.`);
    else if (typeof d.default !== "string" || !ch.includes(d.default)) add(`${p}/default`, `The default of ${who} must be one of its choices.`);
  } else if (d.type === "date") {
    if (!d.date_min || !d.date_max || d.date_min > d.date_max) add(`${p}/date_min`, `Give ${who} a first and a last date (first before last).`);
    else if (typeof d.default !== "string" || d.default < d.date_min || d.default > d.date_max) add(`${p}/default`, `The default date of ${who} must be from ${d.date_min} to ${d.date_max}.`);
  } else if (typeof d.default !== "boolean") {
    add(`${p}/default`, `The default of ${who} must be on or off.`);
  }
}

function panelIssues(spec: DashboardSpecV2, p: Panel, i: number, add: (pointer: string, message: string) => void): void {
  const at = `/panels/${i}`;
  const who = p.title.trim() || `panel ${i + 1}`;
  const r = PRESET_RULES[spec.preset];
  if (!PANEL_ID_RE.test(p.id)) add(`${at}/id`, `The id of ${who} must start with a lowercase letter and have only lowercase letters, digits and _ (at most 32).`);
  if (!p.title.trim()) add(`${at}/title`, "Type a title for the panel.");
  else if (p.title.trim().length > MAX_PANEL_TITLE) add(`${at}/title`, `Use ${MAX_PANEL_TITLE} characters or fewer for the title of ${who}.`);
  if ((p.caption ?? "").trim().length > MAX_CAPTION) add(`${at}/caption`, `Use ${MAX_CAPTION} characters or fewer for the caption of ${who}.`);
  if (!r.kinds.includes(p.kind)) add(`${at}/kind`, p.kind === "mmm" ? `${who} is a planner part. Only the media planner layout has them.` : `The ${PRESET_WORDS[spec.preset].label.toLowerCase()} layout has no ${PANEL_KIND_WORD[p.kind].toLowerCase()} panels.`);
  const reduce = p.kind === "kpi" || p.kind === "chart" ? p.reduce : undefined;
  if (reduce && (p.kind === "kpi" || p.kind === "chart")) {
    const how = reduce.how ?? "none";
    const over = reduce.over ?? [];
    if (p.source.kind !== "output" && how !== "none") add(`${at}/reduce/how`, `Only a model output can be summed or averaged: ${who} shows one value.`);
    if (how === "share_above" && !isNum(reduce.threshold)) add(`${at}/reduce/threshold`, `Give ${who} the value to count the share above.`);
    if (how !== "share_above" && reduce.threshold != null) add(`${at}/reduce/threshold`, `Only “share above a value” takes a value.`);
    if ((how === "mean" || how === "sum") && !over.length) add(`${at}/reduce/over`, `Choose what ${who} adds up or averages over.`);
    if (["none", "select", "last", "next"].includes(how) && over.length) add(`${at}/reduce/over`, `${who} does not add up or average, so it takes no dims.`);
    if (new Set(over).size !== over.length) add(`${at}/reduce/over`, `Each dim of ${who} can be chosen one time.`);
  }
  switch (p.kind) {
    case "chart":
      if (!r.intents.includes(p.intent)) add(`${at}/intent`, `The ${PRESET_WORDS[spec.preset].label.toLowerCase()} layout has no “${INTENT_WORD[p.intent].toLowerCase()}” charts.`);
      if ((p.intent === "accuracy_history") !== (p.source.kind === "accuracy")) add(`${at}/intent`, p.intent === "accuracy_history" ? `“Accuracy over time” shows an accuracy number: choose one for ${who}.` : `An accuracy number shows only as “accuracy over time”.`);
      if (p.max_items != null && (!Number.isInteger(p.max_items) || p.max_items < 1 || p.max_items > MAX_CHART_ITEMS)) add(`${at}/max_items`, `Show 1 to ${MAX_CHART_ITEMS} items in ${who}.`);
      break;
    case "table":
      if ((p.columns ?? []).length > MAX_TABLE_COLUMNS) add(`${at}/columns`, `A table has at most ${MAX_TABLE_COLUMNS} columns.`);
      if ((p.columns ?? []).some((c) => !c.trim() || c.length > 64)) add(`${at}/columns`, "Each column name has 1 to 64 characters.");
      if (p.max_rows != null && (!Number.isInteger(p.max_rows) || p.max_rows < 1 || p.max_rows > MAX_TABLE_ROWS)) add(`${at}/max_rows`, `Show 1 to ${MAX_TABLE_ROWS} rows in ${who}.`);
      break;
    case "note":
      if (!p.text.trim()) add(`${at}/text`, `Type the text of ${who}.`);
      else if (p.text.length > MAX_NOTE_TEXT) add(`${at}/text`, `Use ${MAX_NOTE_TEXT} characters or fewer in ${who}.`);
      break;
    case "recipe":
      if (!RECIPE_RE.test(p.recipe)) add(`${at}/recipe`, `Choose a plot for ${who}.`);
      else if (!(p.recipe in RECIPE_INTENTS) && !p.plot_id) add(`${at}/plot_id`, `Choose which of the model's plots ${who} shows.`);
      break;
    case "kpi":
      if (p.source.kind === "scorecard" && !p.source.check.trim()) add(`${at}/source/check`, `Choose a check for ${who}.`);
      break;
    case "mmm":
      break;
  }
  if ((p.kind === "kpi" || p.kind === "chart" || p.kind === "table") && p.source.kind === "output" && !NAME_RE.test(p.source.variable)) add(`${at}/source/variable`, `Choose an output for ${who}.`);
}

/**
 * What is wrong with a layout 2 draft, in plain words (empty: the server accepts its shape).
 * With `ctx.gridChannels`, the planner's channels are checked against the response grid too.
 */
export function specIssuesV2(spec: DashboardSpecV2, ctx: SpecContext = {}): FormIssue[] {
  const out: FormIssue[] = [];
  const add = (pointer: string, message: string) => void out.push({ pointer, message });

  const title = spec.title.trim();
  if (!title) add("/title", "Type a title.");
  else if (title.length > MAX_TITLE) add("/title", `Use ${MAX_TITLE} characters or fewer for the title.`);
  if ((spec.client_name ?? "").trim().length > MAX_CLIENT_NAME) add("/client_name", `Use ${MAX_CLIENT_NAME} characters or fewer for the client name.`);
  if ((spec.summary ?? "").trim().length > MAX_SUMMARY) add("/summary", `Use ${MAX_SUMMARY} characters or fewer for the summary.`);
  if ((spec.note ?? "").trim().length > MAX_NOTE) add("/note", `Use ${MAX_NOTE} characters or fewer for the note.`);

  const src = spec.source;
  if (src.kind === "endpoint" && !src.alias) add("/source/alias", "Choose an endpoint.");
  if (src.kind === "model_version") {
    if (!src.model_id) add("/source/model_id", "Choose a model.");
    else if (!src.version) add("/source/version", "Choose a model version.");
  }

  const s = spec.subject;
  if (!NAME_RE.test(s.variable)) add("/subject/variable", "Choose the model output that the dashboard is about.");
  if (!s.label.trim()) add("/subject/label", "Type a label for the result.");
  else if (s.label.trim().length > MAX_LABEL) add("/subject/label", `Use ${MAX_LABEL} characters or fewer for the label.`);
  if (s.unit === "currency" && !CURRENCY_RE.test((s.currency ?? "").trim().toUpperCase())) add("/subject/currency", "Type a currency code of 3 letters (for example EUR).");
  const decimals = s.decimals ?? 0;
  if (!Number.isInteger(decimals) || decimals < 0 || decimals > MAX_DECIMALS_V2) add("/subject/decimals", `Use 0 to ${MAX_DECIMALS_V2} decimals.`);
  if (s.positive_label != null && (s.positive_label.trim().length < 1 || s.positive_label.length > MAX_POSITIVE_LABEL)) add("/subject/positive_label", `The class to report has 1 to ${MAX_POSITIVE_LABEL} characters.`);
  if (s.value_per_unit != null && !(s.value_per_unit > 0)) add("/subject/value_per_unit", "The value of one unit must be more than 0.");

  if (!spec.panels.length) add("/panels", "Add at least one panel.");
  if (spec.panels.length > MAX_PANELS) add("/panels", `A dashboard has at most ${MAX_PANELS} panels.`);
  if (spec.panels.filter((p) => p.kind === "kpi").length > MAX_KPI_PANELS) add("/panels", `A dashboard shows at most ${MAX_KPI_PANELS} key numbers.`);
  const ids = new Set<string>();
  spec.panels.forEach((p, i) => {
    if (ids.has(p.id)) add(`/panels/${i}/id`, `Two panels have the id ${p.id}. Give each panel its own id.`);
    ids.add(p.id);
    panelIssues(spec, p, i, add);
  });

  const planner = spec.preset === "mmm_planner";
  if (planner && !spec.mmm) add("/mmm", "The media planner needs its channels.");
  if (!planner && spec.mmm) add("/mmm", "Only the media planner layout has channels.");
  if (spec.mmm) {
    const ch = spec.mmm.channels;
    if (!ch.length) add("/mmm/channels", "Add at least one channel.");
    if (ch.length > MAX_CHANNELS) add("/mmm/channels", `A dashboard can have at most ${MAX_CHANNELS} channels.`);
    const seen = new Set<string>();
    ch.forEach((c, i) => {
      if (!NAME_RE.test(c.name)) add(`/mmm/channels/${i}/name`, `The channel name “${c.name}” is not correct. Use letters, digits, _, . and -.`);
      else if (seen.has(c.name)) add(`/mmm/channels/${i}/name`, `The channel ${c.name} is in the list two times.`);
      seen.add(c.name);
      if (!c.label.trim()) add(`/mmm/channels/${i}/label`, `Type a label for ${c.name}.`);
      else if (c.label.trim().length > MAX_LABEL) add(`/mmm/channels/${i}/label`, `Use ${MAX_LABEL} characters or fewer for the label of ${c.name}.`);
    });
    const tiles = spec.mmm.tiles ?? [];
    if (tiles.length > MAX_TILES || new Set(tiles).size !== tiles.length || tiles.includes("panel")) add("/mmm/tiles", `Choose at most ${MAX_TILES} different tiles.`);
    const whatif = spec.mmm.whatif?.enabled ?? true;
    if (whatif && ctx.gridChannels === null) add("/mmm/whatif/enabled", "The model has no response grid. Turn off plan scenarios.");
    if (whatif && ctx.gridChannels) {
      const grid = new Set(ctx.gridChannels);
      ch.forEach((c, i) => {
        if (!grid.has(c.name)) add(`/mmm/channels/${i}/name`, `The response grid has no channel named ${c.name}.`);
      });
    }
  }

  for (const k of enabledInputs(spec)) {
    const why = inputReason(spec.preset, k);
    if (why) add(k === "whatif" ? "/mmm/whatif/enabled" : `/${k}/enabled`, `${why} Turn it off.`);
  }
  if (spec.refresh?.follow_endpoint && src.kind !== "endpoint") add("/refresh/follow_endpoint", "Only a dashboard on an endpoint can follow it.");
  const horizon = spec.refresh?.horizon;
  if (horizon != null && (!Number.isInteger(horizon) || horizon < 1 || horizon > MAX_HORIZON)) add("/refresh/horizon", `Forecast 1 to ${MAX_HORIZON} periods ahead.`);

  const rows = spec.scenario_rows ?? {};
  const maxRows = rows.max_rows ?? 5;
  if (!Number.isInteger(maxRows) || maxRows < 1 || maxRows > MAX_SCENARIO_ROWS) add("/scenario_rows/max_rows", `Let the client try 1 to ${MAX_SCENARIO_ROWS} cases at a time.`);
  const outs = rows.outputs ?? [];
  if (outs.length > MAX_SCENARIO_OUTPUTS) add("/scenario_rows/outputs", `Choose at most ${MAX_SCENARIO_OUTPUTS} outputs.`);
  if (new Set(outs).size !== outs.length) add("/scenario_rows/outputs", "Each output can be chosen one time.");
  const defs = rows.inputs ?? [];
  if (defs.length > MAX_SCENARIO_INPUTS) add("/scenario_rows/inputs", `A case has at most ${MAX_SCENARIO_INPUTS} inputs.`);
  const names = new Set<string>();
  defs.forEach((d, i) => {
    if (names.has(d.name)) add(`/scenario_rows/inputs/${i}/name`, `Two inputs have the name ${d.name}.`);
    names.add(d.name);
    if (d.name in (rows.fixed ?? {})) add(`/scenario_rows/inputs/${i}/name`, `${d.name} is also a fixed value. Remove one of them.`);
    inputIssues(d, `/scenario_rows/inputs/${i}`, add);
  });
  const h = rows.horizon;
  if (h) {
    const hmin = h.min ?? 1;
    if (!NAME_RE.test(h.dim)) add("/scenario_rows/horizon/dim", "Choose the time dim of the horizon.");
    if (![hmin, h.max, h.default].every((v) => Number.isInteger(v) && v >= 1 && v <= MAX_HORIZON)) add("/scenario_rows/horizon/max", `The horizon is 1 to ${MAX_HORIZON} periods.`);
    else if (!(hmin <= h.default && h.default <= h.max)) add("/scenario_rows/horizon/default", "The default horizon must be from the shortest to the longest.");
  }
  if (defs.some((d) => d.applies === "horizon") && !h) add("/scenario_rows/horizon", "Inputs that apply to the forecast need a horizon.");
  if (rows.enabled) {
    if (!outs.length) add("/scenario_rows/outputs", "Choose 1 to 4 outputs to predict.");
    if (!defs.length && !h) add("/scenario_rows/inputs", "Add an input or a horizon.");
  }

  const a = spec.actuals ?? {};
  if (a.enabled && a.match === "row" && !a.id_column) add("/actuals/id_column", "Choose the column that names each case.");
  if ((a.match ?? "time") === "time" && (a.id_column || a.label_column)) add("/actuals/id_column", "The case and outcome columns are only for uploads matched by case.");
  if (a.id_column && !NAME_RE.test(a.id_column)) add("/actuals/id_column", "The case column name is not correct. Use letters, digits, _, . and -.");
  if (a.label_column && !NAME_RE.test(a.label_column)) add("/actuals/label_column", "The outcome column name is not correct. Use letters, digits, _, . and -.");
  const th = a.threshold ?? 0.5;
  if (!isNum(th) || th < 0.05 || th > 0.95) add("/actuals/threshold", "Use a threshold from 0.05 to 0.95.");
  if (a.allow_refit_requests && !a.enabled) add("/actuals/allow_refit_requests", "Refit requests need uploads. Turn on uploads first.");
  if (a.allow_refit_requests && !a.refit_workflow_id) add("/actuals/refit_workflow_id", "Choose the workflow that refits the model.");

  const b = spec.batch;
  if (b) {
    if (!b.outputs.length || b.outputs.length > MAX_SCENARIO_OUTPUTS) add("/batch/outputs", `Choose 1 to ${MAX_SCENARIO_OUTPUTS} outputs for the scored file.`);
    else if (new Set(b.outputs).size !== b.outputs.length) add("/batch/outputs", "Each output can be chosen one time.");
    const mr = b.max_rows ?? 2000;
    if (!Number.isInteger(mr) || mr < 1 || mr > MAX_BATCH_ROWS) add("/batch/max_rows", `A file has 1 to ${MAX_BATCH_ROWS} rows.`);
    if (!NAME_RE.test(b.id_column)) add("/batch/id_column", "Choose the column that names each case.");
  }

  const runs = spec.limits?.exact_runs_per_day ?? 20;
  if (!Number.isInteger(runs) || runs < 0 || runs > MAX_EXACT_RUNS) add("/limits/exact_runs_per_day", `Use a whole number from 0 to ${MAX_EXACT_RUNS}.`);
  const br = spec.limits?.batch_runs_per_day ?? 3;
  if (!Number.isInteger(br) || br < 0 || br > MAX_BATCH_RUNS) add("/limits/batch_runs_per_day", `Use a whole number from 0 to ${MAX_BATCH_RUNS}.`);

  if (!out.length) {
    const size = new TextEncoder().encode(JSON.stringify(cleanSpecV2(spec))).length;
    if (size > MAX_SPEC_BYTES) add("/panels", `The dashboard is too large to save (${size} bytes; at most ${MAX_SPEC_BYTES}). Remove panels or shorten texts.`);
  }
  return out;
}

/** The builder tab where an issue at `pointer` of a layout 2 spec is fixed. */
export function tabOfIssueV2(pointer: string | null | undefined): "setup" | "inputs" {
  const p = pointer ?? "";
  if (p === "/actuals/threshold") return "setup";
  return /^\/(scenario_rows|actuals|batch|refresh|limits|mmm\/whatif)(\/|$)/.test(p) ? "inputs" : "setup";
}

/** A plain name for where an issue is (the upgrade banner's list): "Channels", "Panel 3", … */
export function placeOfIssue(pointer: string | null | undefined): string | null {
  const p = pointer ?? "";
  const panel = /^\/panels\/(\d+)/.exec(p);
  if (panel) return `Panel ${Number(panel[1]) + 1}`;
  const first = p.split("/")[1] ?? "";
  const words: Record<string, string> = {
    title: "Title",
    client_name: "Client name",
    summary: "Summary",
    note: "Note",
    source: "Source",
    kpi: "KPI",
    subject: "Result",
    channels: "Channels",
    mmm: "Channels",
    tiles: "Tiles",
    tabs: "Tabs",
    whatif: "Plan scenarios",
    actuals: "Number uploads",
    scenario_rows: "Try a case",
    batch: "Score a file",
    refresh: "Refresh",
    limits: "Limits",
    panels: "Panels",
    preset: "Layout",
  };
  return words[first] ?? null;
}

/**
 * Checks against what the model version offers (the source catalog): outputs, the values and
 * summaries each output allows, metrics, checks and plots. They show next to the fields; the
 * server repeats them at publish (`check_spec_v2`), so they never stop a save.
 */
export function catalogIssuesV2(spec: DashboardSpecV2, catalog: SourceCatalog | null | undefined): FormIssue[] {
  if (!catalog) return [];
  const out: FormIssue[] = [];
  const add = (pointer: string, message: string) => void out.push({ pointer, message });
  const names = new Set((catalog.outputs ?? []).map((o) => o.name));
  const known = (n: string) => names.has(n);
  // the planner reads its KPI from the model's variables and the response grid, never an output
  // (`check_spec_v2` skips it too)
  if (spec.preset !== "mmm_planner" && (catalog.outputs ?? []).length && !known(spec.subject.variable)) add("/subject/variable", `The model has no output named ${spec.subject.variable}.`);
  spec.panels.forEach((p, i) => {
    const at = `/panels/${i}`;
    if ((p.kind === "kpi" || p.kind === "chart" || p.kind === "table") && p.source.kind === "output") {
      const o = outputOf(catalog, p.source.variable);
      if (!o) return add(`${at}/source/variable`, `The model has no output named ${p.source.variable}.`);
      if (p.kind === "table") return;
      const how = p.reduce?.how ?? "none";
      if (!(o.reduces ?? []).includes(how)) add(`${at}/reduce/how`, `${outputLabel(o.name)} cannot be summarised like this. Choose another summary.`);
      for (const dim of p.reduce?.over ?? []) if (!(o.dims ?? []).includes(dim)) add(`${at}/reduce/over`, `${outputLabel(o.name)} has no dim ${dim}.`);
      const why = bandReason(o, p.band ?? "outcome");
      if (why) add(`${at}/band`, why);
      if (p.kind === "kpi" && !(o.stats ?? ["centre"]).includes(p.stat ?? "centre")) add(`${at}/stat`, `${outputLabel(o.name)} has no range: show its middle value.`);
      if (p.kind === "chart" && !(o.intents ?? []).includes(p.intent)) add(`${at}/intent`, `${outputLabel(o.name)} does not fit a “${INTENT_WORD[p.intent].toLowerCase()}” chart.`);
    }
    if (p.kind === "kpi" && p.source.kind === "metric") {
      const key = p.source.key;
      if (!(catalog.metrics ?? []).some((m) => m.key === key)) add(`${at}/source/key`, `The fit run logged no metric ${key}.`);
    }
    if (p.kind === "kpi" && p.source.kind === "scorecard") {
      const check = p.source.check;
      if (!(catalog.scorecard ?? []).includes(check)) add(`${at}/source/check`, `The scorecard has no check ${check}.`);
    }
    if (p.kind === "recipe" && p.plot_id && !(catalog.pictures ?? []).some((x) => x.plot_id === p.plot_id)) add(`${at}/plot_id`, "The model version has no such plot that clients may see.");
  });
  (spec.scenario_rows?.outputs ?? []).forEach((n) => (known(n) ? null : add("/scenario_rows/outputs", `The model has no output named ${n}.`)));
  (spec.batch?.outputs ?? []).forEach((n) => (known(n) ? null : add("/batch/outputs", `The model has no output named ${n}.`)));
  return out;
}
