/**
 * The pure model of the layout 2 readout (v0.17, plan §3.7): the section names, what each
 * panel block says in plain words (tiles, ranges, the "approximate" tag, the accessible
 * summaries), the browser's checks of a tried case, a batch file and a row-matched actuals
 * upload (they mirror the server's checks; the server checks everything again and answers with
 * `/rows/<i>/<name>` or `/inputs/...` pointers), and the one-time upgrade note.
 *
 * The web never recomputes a layout 2 metric (plan §3.3): accuracy, batch statistics and
 * predictions are the server's numbers, only put into words here. Nothing reads the network
 * or the DOM; the one exception is the upgrade note memory, which reads browser storage inside
 * try/catch and works without it.
 */
import { fixed, grouped, MISSING } from "@/components/ods/format";
import type { Cell } from "@/lib/actuals";

import { visibleSections } from "../client";
import type {
  AccuracyDetail,
  AccuracyPoint,
  BatchColumn,
  BatchSubmit,
  BatchSummary,
  BatchTemplate,
  ClientDashboard,
  DashboardClient,
  DashboardClientV2,
  DeltaDir,
  KpiBlock,
  KpiDef,
  KpiValue,
  PanelBlock,
  PredictionResult,
  PrincipalKind,
  Readout,
  ScenarioForm,
  ScenarioInputDef,
  ScenarioRow,
  SectionKey,
  SubjectDef,
  TabKey,
} from "../types";
import { csvCell, deltaWords, formatValue, metaLine, periodOf, plural, type Check, type Tag } from "./readout-model";

type Num = number | null | undefined;
const isNum = (v: Num): v is number => typeof v === "number" && Number.isFinite(v);

// ------------------------------------------------------------------------------ sections

export const SECTION_LABEL: Record<SectionKey, string> = {
  overview: "Overview",
  try: "Try a case",
  accuracy: "Check accuracy",
  batch: "Score a file",
  history: "History",
};

/** The sections to show: the server's list (else the inputs'), `overview` first, no duplicates. */
export function pageSections(d: Pick<ClientDashboard, "sections" | "inputs">): SectionKey[] {
  const listed = d.sections?.length ? d.sections : visibleSections(d.inputs ?? {});
  return ["overview", ...new Set(listed.filter((s) => s !== "overview"))];
}

/** A layout 1 tab name (a `?tab=` link from before the upgrade) as the layout 2 section. */
export function sectionOfTab(tab: TabKey | undefined): SectionKey | undefined {
  switch (tab) {
    case undefined:
      return undefined;
    case "update":
      return "accuracy";
    case "history":
      return "history";
    case "scenarios":
      return "try";
    default:
      return "overview";
  }
}

/** The client as a layout 2 one (scenario rows, batch, downloads, pictures), or `null`. */
export function asClientV2(client: DashboardClient): DashboardClientV2 | null {
  const c = client as Partial<DashboardClientV2>;
  return typeof c.predict === "function" && typeof c.submitBatch === "function" && typeof c.asset === "function" ? (client as DashboardClientV2) : null;
}

// ------------------------------------------------------------------------------ numbers

/** The subject's number form (currency, decimals) on the layout 1 KPI the dashboard carries. */
export function subjectKpi(d: Pick<ClientDashboard, "kpi" | "subject">): KpiDef {
  const s: SubjectDef | null | undefined = d.subject;
  if (!s) return d.kpi;
  return { ...d.kpi, currency: s.currency ?? d.kpi.currency ?? null, decimals: s.decimals ?? d.kpi.decimals ?? 0 };
}

/** A value in a tile's unit, in the subject's number form when the unit is the subject's. */
export function unitText(v: Num, unit: KpiValue["unit"], d: Pick<ClientDashboard, "kpi" | "subject">): string {
  const kpi = subjectKpi(d);
  const own = !d.subject || unit === d.subject.unit || unit === "currency";
  return formatValue(v, unit, own ? kpi : { currency: kpi.currency ?? null, decimals: unit === "number" || unit === "count" ? 2 : 0 });
}

/** A plain number of a chart axis or table cell: grouped, with the decimals the size needs. */
export function plainNumber(v: number): string {
  const a = Math.abs(v);
  if (a >= 100 || Number.isInteger(v)) return grouped(v, 0);
  if (a >= 1) return grouped(v, 2);
  return fixed(v, 3);
}

/** "likely between 1,200 and 1,900" (`null` without both ends). */
export function rangeWords(low: Num, high: Num, format: (v: number) => string): string | null {
  return isNum(low) && isNum(high) ? `Likely between ${format(low)} and ${format(high)}` : null;
}

export const APPROXIMATE_TAG: Tag = { label: "Approximate", tone: "neutral" };
/** The `approximate` glossary text (`mapi_spec.dashboards.GLOSSARY`). */
export const APPROXIMATE_TEXT = "This value is approximate. The model gives one value per case without a range, so the total does not show the uncertainty.";

export function blockTags(block: Pick<PanelBlock, "approximate">): Tag[] {
  return block.approximate ? [APPROXIMATE_TAG] : [];
}

export interface PanelTileView {
  id: string;
  label: string;
  value: string;
  delta: string | null;
  deltaDir: DeltaDir;
  caption: string;
  approximate: boolean;
}

/** One KPI block as a tile: the value, its likely range and the change against the readout before. */
export function panelTile(block: KpiBlock, d: Pick<ClientDashboard, "kpi" | "subject">): PanelTileView {
  const t = block.tile;
  const fmt = (v: number) => unitText(v, t.unit, d);
  const range = rangeWords(t.low, t.high, fmt);
  const caption = [range ? `${range}.` : null, t.caption || null, block.approximate || t.approximate ? "Approximate." : null].filter(Boolean).join(" ");
  return {
    id: block.panel_id,
    label: t.label || block.title,
    value: unitText(t.value, t.unit, d),
    delta: deltaWords(t),
    deltaDir: t.delta_dir ?? "flat",
    caption,
    approximate: Boolean(block.approximate || t.approximate),
  };
}

/** The KPI blocks of a snapshot in panel order, and every other block after them. */
export function splitBlocks(blocks: readonly PanelBlock[]): { tiles: KpiBlock[]; rest: PanelBlock[] } {
  const tiles: KpiBlock[] = [];
  const rest: PanelBlock[] = [];
  for (const b of blocks) {
    if (b.kind === "kpi") tiles.push(b);
    else rest.push(b);
  }
  return { tiles, rest };
}

/** Is a series block the subject line the media planner already draws (its default "fan")? */
export function isPlannerDuplicate(block: PanelBlock): boolean {
  return block.kind === "series" && block.panel_id === "fan";
}

// ------------------------------------------------------------------------------ predictions and batches

export interface PredictionLine {
  output: string;
  centre: string;
  range: string | null;
  label: string | null;
  probability: string | null;
}

/** The outputs of one tried case in words. */
export function predictionLines(result: PredictionResult, row: number, d: Pick<ClientDashboard, "kpi" | "subject">): PredictionLine[] {
  const outs = result.rows?.[row]?.outputs ?? {};
  const unit = d.subject?.unit ?? d.kpi.unit;
  return Object.entries(outs).map(([name, v]) => {
    const fmt = (x: number) => (unit === "probability" ? `${fixed(x * 100, 1)}%` : unitText(x, unit, d));
    return {
      output: name,
      centre: isNum(v.centre) ? fmt(v.centre) : MISSING,
      range: result.band === "none" ? null : rangeWords(v.low, v.high, fmt),
      label: v.label ?? null,
      probability: isNum(v.probability) ? `${fixed(v.probability * 100, 1)}%` : null,
    };
  });
}

/** The note under a prediction: what the range means (predictive, average or none). */
export function bandNote(band: PredictionResult["band"]): string | null {
  if (band === "outcome") return "The likely range shows where one new result will probably be. The model is 90% sure that the result is in this range.";
  if (band === "mean") return "The range shows the average, not single results.";
  return null;
}

/** The summary lines of a scored file ("spread of predictions", never an interval). */
export function batchLines(b: BatchSummary, d: Pick<ClientDashboard, "kpi" | "subject">): string[] {
  const lines = [`The model scored ${grouped(b.n_scored)} of ${grouped(b.n_rows)} ${plural("case", b.n_rows)}.`];
  if (isNum(b.share_positive)) {
    const pos = d.subject?.positive_label;
    lines.push(`${fixed(b.share_positive * 100, 1)}% of the cases are ${pos ? `likely "${pos}"` : "above the threshold"}.`);
  }
  const unit = d.subject?.unit ?? d.kpi.unit;
  for (const [name, s] of Object.entries(b.stats ?? {})) {
    const fmt = (x: number) => unitText(x, unit, d);
    if (!isNum(s.p50)) continue;
    const spread = isNum(s.p10) && isNum(s.p90) ? `; most are between ${fmt(s.p10)} and ${fmt(s.p90)}` : "";
    lines.push(`${name}: the middle prediction is ${fmt(s.p50)}${spread}. This is the spread of the predictions, not a likely range.`);
  }
  return lines;
}

// ------------------------------------------------------------------------------ accuracy

const pct = (v: Num) => (isNum(v) ? `${fixed(v * 100, 1)}%` : MISSING);

/** The accuracy of an upload in plain words, by kind (the server's numbers). */
export function accuracyDetailLines(a: AccuracyDetail, d: Pick<ClientDashboard, "kpi" | "subject" | "period_label">): string[] {
  const fmt = (v: Num) => (isNum(v) ? unitText(v, d.subject?.unit ?? d.kpi.unit, d) : MISSING);
  const period = periodOf(d);
  if (a.kind === "error") {
    const n = a.n ?? 0;
    const lines = [`We compared ${n} ${plural(period === "case" ? "case" : period, n)} with the predictions.`];
    if (isNum(a.mae)) lines.push(`The typical error is ${fmt(a.mae)}.`);
    if (isNum(a.mape)) lines.push(`On average the prediction was ${fixed(a.mape * 100, 1)}% from the actual number.`);
    if (isNum(a.bias)) lines.push(a.bias > 0 ? `The predictions were ${fmt(a.bias)} too high on average.` : a.bias < 0 ? `The predictions were ${fmt(-a.bias)} too low on average.` : "The predictions were not too high or too low on average.");
    if (isNum(a.band_hit_rate)) lines.push(`The actual number was in the likely range ${pct(a.band_hit_rate)} of the time.`);
    return lines;
  }
  if (a.kind === "classification") {
    const n = a.n ?? 0;
    const lines = [`We compared ${n} ${plural("case", n)} with the predictions.`];
    if (isNum(a.accuracy)) lines.push(`The model was right ${pct(a.accuracy)} of the time.`);
    if (isNum(a.rate_pred) && isNum(a.rate_obs)) lines.push(`It predicted ${pct(a.rate_pred)}${a.positive ? ` "${a.positive}"` : ""}; the actual share was ${pct(a.rate_obs)}.`);
    if (isNum(a.threshold)) lines.push(`A case counts as${a.positive ? ` "${a.positive}"` : " positive"} when its probability is ${pct(a.threshold)} or more.`);
    return lines;
  }
  const lines: string[] = [];
  if (isNum(a.total)) lines.push(`The change added about ${fmt(a.total)}${isNum(a.low) && isNum(a.high) ? ` (likely ${fmt(a.low)} to ${fmt(a.high)})` : ""} over ${a.n_periods ?? 0} ${plural(period, a.n_periods ?? 0)}.`);
  if (isNum(a.relative)) lines.push(`That is ${fixed(Math.abs(a.relative) * 100, 1)}% ${a.relative >= 0 ? "more" : "less"} than without it.`);
  if (a.note) lines.push(a.note);
  return lines;
}

/** The technical measures of an upload (name, value) for the visually quiet table under the words. */
export function accuracyMeasures(a: AccuracyDetail, d: Pick<ClientDashboard, "kpi" | "subject">): { label: string; value: string }[] {
  const num = (v: Num) => (isNum(v) ? unitText(v, d.subject?.unit ?? d.kpi.unit, d) : MISSING);
  const dec = (v: Num) => fixed(v, 3);
  if (a.kind === "error")
    return [
      { label: "Typical error (MAE)", value: num(a.mae) },
      { label: "RMSE", value: num(a.rmse) },
      { label: "MAPE", value: pct(a.mape) },
      { label: "WAPE", value: pct(a.wape) },
      { label: "R²", value: dec(a.r2) },
      { label: "Inside the likely range", value: pct(a.band_hit_rate) },
    ].filter((m) => m.value !== MISSING);
  if (a.kind === "classification")
    return [
      { label: "Accuracy", value: pct(a.accuracy) },
      { label: "Brier score", value: dec(a.brier) },
      { label: "Log score", value: dec(a.log_score) },
      { label: "AUC", value: dec(a.auc) },
    ].filter((m) => m.value !== MISSING);
  return [
    { label: "Effect", value: num(a.total) },
    { label: "Relative effect", value: pct(a.relative) },
  ].filter((m) => m.value !== MISSING);
}

/** The metric of the accuracy history to draw, by kind (the first that has values). */
export function historyMetric(points: readonly AccuracyPoint[]): string | null {
  const order = ["mae", "mape", "accuracy", "brier", "effect_total", "rmse", "band_hit_rate", "auc"];
  for (const k of order) if (points.some((p) => isNum(p.values?.[k]))) return k;
  for (const p of points) for (const [k, v] of Object.entries(p.values ?? {})) if (isNum(v)) return k;
  return null;
}

export const HISTORY_METRIC_LABEL: Record<string, string> = {
  mae: "Typical error",
  rmse: "RMSE",
  mape: "Average error (%)",
  wape: "WAPE",
  smape: "SMAPE",
  bias: "Bias",
  r2: "R²",
  band_hit_rate: "Inside the likely range",
  accuracy: "Right predictions",
  brier: "Brier score",
  log_score: "Log score",
  auc: "AUC",
  effect_total: "Effect",
  effect_relative: "Relative effect",
};

// ------------------------------------------------------------------------------ "Try a case" checks (mirror `dashboard_inputs.check_value`)

/** `dashboard_inputs.HORIZON_KEY`: the row key of the periods ahead (when no input has that name). */
export const HORIZON_KEY = "horizon";
const STEP_TOLERANCE = 1e-9;
const MAX_TEXT_CHARS = 128;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

/** Python's `{x:g}` for the numbers in the messages (6 significant digits, no trailing zeros). */
export function g(x: number): string {
  if (!Number.isFinite(x)) return String(x);
  if (x === 0) return "0";
  const exp = Math.floor(Math.log10(Math.abs(x)));
  if (exp < -4 || exp >= 6) {
    const [m, e] = x.toExponential(5).split("e");
    const mant = m!.replace(/\.?0+$/, "");
    const n = Number(e);
    return `${mant}e${n < 0 ? "-" : "+"}${String(Math.abs(n)).padStart(2, "0")}`;
  }
  return String(Number(x.toPrecision(6)));
}

/** `dashboard_metrics.normalise_label`: NFC, trimmed, case-folded, "1.0" → "1". */
export function normaliseLabel(s: string): string {
  const t = s.normalize("NFC").trim().toLowerCase();
  return /^[-+]?\d+\.0+$/.test(t) ? String(Number(t)) : t;
}

/** `dashboards._plain` of one line: no control characters, no HTML tags or entities. */
function isPlain(s: string): boolean {
  return !/[\u0000-\u001f\u007f]/.test(s) && !/<[A-Za-z/!?]|&(?:[A-Za-z][A-Za-z0-9]{1,31}|#[0-9]{1,7}|#x[0-9a-fA-F]{1,6});/.test(s);
}

/** Why `value` is not a valid value of the input (plain text), or `null` (`check_value`). */
export function checkValue(value: unknown, def: ScenarioInputDef): string | null {
  const label = def.label;
  if (def.type === "number" || def.type === "integer") {
    if (typeof value !== "number") return `${label} must be a number`;
    if (!Number.isFinite(value)) return `${label} must be a finite number`;
    const lo = def.min ?? -Infinity;
    const hi = def.max ?? Infinity;
    if (!(lo <= value && value <= hi)) return `${label} must be between ${g(lo)} and ${g(hi)}`;
    if (def.type === "integer" && !Number.isInteger(value)) return `${label} must be a whole number`;
    const step = def.step ?? (def.type === "integer" ? 1 : null);
    if (step !== null && Number.isFinite(lo) && Number.isFinite(hi)) {
      const k = Math.round((value - lo) / step);
      if (Math.abs(value - (lo + k * step)) > STEP_TOLERANCE * (hi - lo)) return `${label} must be ${g(lo)} plus a multiple of ${g(step)}`;
    }
    return null;
  }
  if (def.type === "toggle") return typeof value === "boolean" ? null : `${label} must be true or false`;
  if (def.type === "date") {
    if (typeof value !== "string" || !ISO_DAY.test(value) || Number.isNaN(Date.parse(`${value}T00:00:00Z`))) return `${label} must be a date as YYYY-MM-DD`;
    if ((def.date_min && value < def.date_min) || (def.date_max && value > def.date_max)) return `${label} must be between ${def.date_min ?? ""} and ${def.date_max ?? ""}`;
    return null;
  }
  if (typeof value === "boolean" || (typeof value !== "string" && typeof value !== "number")) return `${label} must be one of the choices`;
  if (typeof value === "string") {
    if (value.length > MAX_TEXT_CHARS) return `${label} holds more than ${MAX_TEXT_CHARS} characters`;
    if (!isPlain(value)) return `${label} must be plain text`;
  }
  const want = normaliseLabel(String(value));
  return (def.choices ?? []).some((c) => normaliseLabel(c) === want) ? null : `${label} must be one of the choices`;
}

/** A row of the form's defaults (plus the default horizon of a forecast form). */
export function defaultRow(form: ScenarioForm): ScenarioRow {
  const row: ScenarioRow = {};
  for (const i of form.inputs ?? []) row[i.name] = i.default;
  if (horizonKey(form)) row[HORIZON_KEY] = form.horizon!.default;
  return row;
}

export function horizonKey(form: Pick<ScenarioForm, "horizon" | "inputs">): string | null {
  if (!form.horizon || (form.inputs ?? []).some((i) => i.name === HORIZON_KEY)) return null;
  return HORIZON_KEY;
}

/** `check_rows`: every value the server would refuse, by pointer (`/rows/<i>/<name>`). */
export function checkRows(rows: readonly ScenarioRow[], form: ScenarioForm): Map<string, string> {
  const out = new Map<string, string>();
  if (!rows.length) out.set("/rows", "send at least one row");
  if (rows.length > form.max_rows) out.set("/rows", `send at most ${form.max_rows} rows (got ${rows.length})`);
  const byName = new Map((form.inputs ?? []).map((i) => [i.name, i]));
  const hkey = horizonKey(form);
  rows.forEach((row, r) => {
    for (const [name, value] of Object.entries(row)) {
      if (value === null || value === undefined) continue;
      const pointer = `/rows/${r}/${name}`;
      if (hkey && name === hkey) {
        const h = form.horizon!;
        const min = h.min ?? 1;
        if (typeof value !== "number" || !Number.isInteger(value)) out.set(pointer, "the number of periods must be a whole number");
        else if (value < min || value > h.max) out.set(pointer, `the number of periods must be ${min} to ${h.max}`);
        continue;
      }
      const def = byName.get(name);
      if (!def) {
        out.set(pointer, `'${name}' is not an input of this dashboard`);
        continue;
      }
      const why = checkValue(value, def);
      if (why) out.set(pointer, why);
    }
  });
  return out;
}

/** The text of a form field as the value it sends (a number field that does not parse stays text). */
export function fieldValue(text: string, def: Pick<ScenarioInputDef, "type">): string | number | boolean | null {
  const v = text.trim();
  if (def.type === "number" || def.type === "integer") {
    if (v === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : v;
  }
  return v === "" ? null : v;
}

/** The server's issues of a refused request mapped to fields (`/rows/0/price` → that field). */
export function issuesByPointer(issues: readonly { pointer?: string | null; message: string }[] | undefined): Map<string, string> {
  const out = new Map<string, string>();
  for (const i of issues ?? []) if (i.pointer && !out.has(i.pointer)) out.set(i.pointer, i.message);
  return out;
}

// ------------------------------------------------------------------------------ "Score a file"

export interface ParsedBatch {
  /** The file's columns, in the file's order. */
  header: string[];
  /** The rows as typed cells, in the template's column order. */
  rows: Cell[][];
  checks: Check[];
}

/** A cell of a batch file as the value it sends (by the template column's type). */
export function batchCell(text: string, col: Pick<BatchColumn, "type" | "role">): Cell {
  const v = text.trim();
  if (v === "") return null;
  if (col.role === "id") return v;
  if (col.type === "number" || col.type === "integer") {
    const n = Number(v.replace(/,/g, ""));
    return Number.isFinite(n) && /^[-+]?[\d,]*\.?\d+(e[-+]?\d+)?$/i.test(v) ? n : v;
  }
  if (col.type === "toggle") {
    const t = v.toLowerCase();
    return t === "true" || t === "1" || t === "yes" ? true : t === "false" || t === "0" || t === "no" ? false : v;
  }
  return v;
}

function batchCellIssue(value: Cell, col: BatchColumn): string | null {
  if (value === null) return `${col.name} is empty`;
  if (col.type === "number" || col.type === "integer") {
    if (typeof value !== "number") return `${col.name} must be a number`;
    if (col.type === "integer" && !Number.isInteger(value)) return `${col.name} must be a whole number`;
    if (isNum(col.min) && isNum(col.max) && !(col.min <= value && value <= col.max)) return `${col.name} must be between ${g(col.min)} and ${g(col.max)}`;
    return null;
  }
  if (col.type === "toggle") return typeof value === "boolean" ? null : `${col.name} must be true or false`;
  if (col.type === "date" && (typeof value !== "string" || !ISO_DAY.test(value))) return `${col.name} must be a date as YYYY-MM-DD`;
  if (col.type === "choice" && col.choices?.length && !col.choices.some((c) => normaliseLabel(c) === normaliseLabel(String(value)))) return `${col.name} must be one of the choices`;
  if (typeof value === "string" && value.length > MAX_TEXT_CHARS) return `${col.name} holds more than ${MAX_TEXT_CHARS} characters`;
  return null;
}

function listed(where: readonly string[]): string {
  return `${where.slice(0, 3).join("; ")}${where.length > 3 ? ` and ${where.length - 3} more` : ""}`;
}

/** The checks of a batch file before it is sent: the columns, the row cap, the ids and each cell. */
export function batchChecks(t: Pick<BatchTemplate, "columns" | "id_column" | "max_rows">, header: readonly string[], rows: readonly Cell[][], totalRows: number): Check[] {
  const cols = t.columns;
  const checks: Check[] = [];
  const missing = cols.filter((c) => c.required !== false && !header.includes(c.name)).map((c) => c.name);
  const unknown = header.filter((h) => h !== "" && !cols.some((c) => c.name === h));
  checks.push(
    missing.length
      ? { ok: false, text: `The file needs the ${plural("column", missing.length)} ${missing.join(", ")}.` }
      : unknown.length
        ? { ok: false, text: `Remove the ${plural("column", unknown.length)} ${unknown.join(", ")}. The model does not use ${unknown.length === 1 ? "it" : "them"}.` }
        : { ok: true, text: "The file has the columns of the template." },
  );
  checks.push(
    totalRows === 0
      ? { ok: false, text: "The file has no rows." }
      : totalRows > t.max_rows
        ? { ok: false, text: `Use ${grouped(t.max_rows)} rows or fewer. The file has ${grouped(totalRows)}.` }
        : { ok: true, text: `${grouped(totalRows)} ${plural("case", totalRows)} to score.` },
  );
  if (missing.length) return checks;
  const idAt = cols.findIndex((c) => c.name === t.id_column);
  const seen = new Map<string, number>();
  const dupes: string[] = [];
  const noId: string[] = [];
  rows.forEach((r, i) => {
    const id = idAt >= 0 ? r[idAt] : null;
    if (id === null || id === "") noId.push(`row ${i + 1}`);
    else {
      const k = String(id);
      if (seen.has(k)) dupes.push(`${k} (rows ${seen.get(k)! + 1} and ${i + 1})`);
      else seen.set(k, i);
    }
  });
  checks.push(noId.length ? { ok: false, text: `Every row needs a ${t.id_column}. Check ${listed(noId)}.` } : dupes.length ? { ok: false, text: `Each ${t.id_column} must occur one time. Check ${listed(dupes)}.` } : { ok: true, text: `Each row has its own ${t.id_column}.` });
  const bad: string[] = [];
  rows.forEach((r, i) =>
    cols.forEach((c, j) => {
      if (c.role === "id") return;
      const why = batchCellIssue(r[j] ?? null, c);
      if (why) bad.push(`row ${i + 1}: ${why}`);
    }),
  );
  checks.push(bad.length ? { ok: false, text: `Fix the values. ${listed(bad)}.` } : { ok: true, text: "All values are in the ranges the analyst set." });
  return checks;
}

/** The parsed CSV rows (strings, file order) in the template's column order, typed. */
export function batchRows(t: Pick<BatchTemplate, "columns">, header: readonly string[], rows: readonly string[][]): Cell[][] {
  const at = t.columns.map((c) => header.indexOf(c.name));
  return rows
    .filter((r) => r.some((c) => c.trim() !== ""))
    .map((r) => t.columns.map((c, j) => (at[j]! < 0 ? null : batchCell(r[at[j]!] ?? "", c))));
}

/** The rows as the one-dim table `submit_batch` reads: every column along the id column. */
export function batchToXr(t: Pick<BatchTemplate, "columns" | "id_column">, rows: readonly Cell[][]): BatchSubmit["inputs"] {
  const idAt = t.columns.findIndex((c) => c.name === t.id_column);
  const id = t.id_column;
  const data_vars: Record<string, { dims: string[]; data: Cell[] }> = {};
  t.columns.forEach((c, j) => {
    if (j === idAt) return;
    data_vars[c.name] = { dims: [id], data: rows.map((r) => r[j] ?? null) };
  });
  return { coords: { [id]: { dims: [id], data: rows.map((r) => r[idAt] ?? null) } }, data_vars };
}

/** "predictions-weekly-churn-12.csv". */
export function predictionsFileName(title: string, seq: number): string {
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "dashboard";
  return `predictions-${slug}-${seq}.csv`;
}

// ------------------------------------------------------------------------------ row-matched actuals

/** The checks of a row-matched upload: rows, unique ids, an outcome on each row. */
export function rowActualsChecks(idColumn: string, outcomeColumn: string, columns: readonly string[], rows: readonly Cell[][], maxRows: number): Check[] {
  const filled = rows.filter((r) => r.some((c) => c !== null && c !== ""));
  const checks: Check[] = [];
  checks.push(
    filled.length === 0
      ? { ok: false, text: "Add at least one row." }
      : filled.length > maxRows
        ? { ok: false, text: `Use ${grouped(maxRows)} rows or fewer. You have ${grouped(filled.length)}.` }
        : { ok: true, text: `${grouped(filled.length)} ${plural("case", filled.length)} to send.` },
  );
  const idAt = columns.indexOf(idColumn);
  const outAt = columns.indexOf(outcomeColumn);
  if (idAt < 0) checks.push({ ok: false, text: `The template has no column ${idColumn}.` });
  else {
    const seen = new Set<string>();
    const dupes: string[] = [];
    const noId: string[] = [];
    filled.forEach((r, i) => {
      const v = r[idAt];
      if (v === null || v === "" || v === undefined) noId.push(`row ${i + 1}`);
      else if (seen.has(String(v))) dupes.push(String(v));
      else seen.add(String(v));
    });
    checks.push(noId.length ? { ok: false, text: `Every row needs a ${idColumn}. Check ${listed(noId)}.` } : dupes.length ? { ok: false, text: `Each ${idColumn} must occur one time. Check ${listed(dupes)}.` } : { ok: true, text: `Each row has its own ${idColumn}.` });
  }
  if (outAt < 0) checks.push({ ok: false, text: `The template has no column ${outcomeColumn}.` });
  else {
    const empty = filled.map((r, i) => (r[outAt] === null || r[outAt] === "" ? `row ${i + 1}` : null)).filter((x): x is string => x !== null);
    checks.push(empty.length ? { ok: false, text: `Enter the actual result in the column ${outcomeColumn}. Check ${listed(empty)}.` } : { ok: true, text: `Each row has an actual result in ${outcomeColumn}.` });
  }
  return checks;
}

// ------------------------------------------------------------------------------ "Download summary (.csv)"

/** The latest layout 2 readout as a small CSV: the lead, then the KPI tiles. */
export function summaryCsvV2(d: Pick<ClientDashboard, "title" | "kpi" | "subject" | "period_label">, r: Readout | null, timeZone?: string): string {
  const lines: string[][] = [[d.title], [metaLine(r, periodOf(d), timeZone)]];
  const snap = r?.snapshot ?? null;
  if (snap) {
    lines.push([snap.lead], [], ["Measure", "Value", "Change", "Note"]);
    for (const b of splitBlocks(snap.panels ?? []).tiles) {
      const t = panelTile(b, d);
      lines.push([t.label, t.value, t.delta ?? "", t.caption]);
    }
  }
  return `${lines.map((l) => l.map(csvCell).join(",")).join("\r\n")}\r\n`;
}

// ------------------------------------------------------------------------------ the one-time upgrade note

const NOTE_KEY = "mapi.dashboards.upgradeNote.";
/** Share links keep the dismissal for the open page only (plan §10.1: in memory or the session). */
const MEMORY = new Map<string, string>();

function storageOf(kind: PrincipalKind): Storage | null {
  try {
    return kind === "share" ? window.sessionStorage : window.localStorage;
  } catch {
    return null;
  }
}

/** Has this viewer dismissed this note on this dashboard? (Storage that throws counts as no.) */
export function upgradeNoteDismissed(kind: PrincipalKind, dashboardId: string, note: string): boolean {
  const key = NOTE_KEY + dashboardId;
  if (MEMORY.get(key) === note) return true;
  try {
    return storageOf(kind)?.getItem(key) === note;
  } catch {
    return false;
  }
}

/** Remember that this viewer dismissed the note (for this note text: a new note shows again). */
export function dismissUpgradeNote(kind: PrincipalKind, dashboardId: string, note: string): void {
  const key = NOTE_KEY + dashboardId;
  MEMORY.set(key, note);
  try {
    storageOf(kind)?.setItem(key, note);
  } catch {
    // Storage is off (a private window, blocked site data): the note stays dismissed for this page.
  }
}

/** For tests: forget the in-memory dismissals. */
export function resetUpgradeNoteMemory(): void {
  MEMORY.clear();
}

