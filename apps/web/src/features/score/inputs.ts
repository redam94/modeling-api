/**
 * What a model version needs to score (`GET …/inputs`), in the score form's terms: plain
 * descriptions of its inputs, and the grid of values a user types instead of uploading a
 * dataset. The grid is the input table (`mapi_spec.endpoints.input_table`) with one row per
 * period to score crossed with every fitted level of the other dims (the server's example
 * table only takes the first two), and it becomes the call's inline XrJSON data
 * (`lib/actuals.tableToXr`).
 */
import type { CoordSummary, InputTable, InputTableColumn, ModelInputSchema } from "@/api/types";
import { nextLabel, parseCell, tableToXr, type XrDataset } from "@/lib/actuals";

import type { ScoreMode } from "./request";

type Snapshot = ModelInputSchema["snapshot"];

/** Rows the grid starts with at most (the inline payload is capped at 5 MB anyway). */
export const MAX_GRID_ROWS = 500;

const FREQ_UNIT: [RegExp, string][] = [
  [/^\d*h$/i, "hour"],
  [/^\d*D$/, "day"],
  [/^\d*W/, "week"],
  [/^\d*M[SE]?$/, "month"],
  [/^\d*Q[SE]?/, "quarter"],
  [/^\d*(Y|A)[SE]?/, "year"],
];

/** "week" for `W-SUN`, "month" for `MS`; null when unknown. */
export function freqUnit(freq: string | null | undefined): string | null {
  if (!freq) return null;
  return FREQ_UNIT.find(([re]) => re.test(freq))?.[1] ?? null;
}

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** The fitted range of a dim in words: "2024-10-01 to 2026-09-30 (730 days)", "3 levels: de, fr, it". */
export function fittedRange(c: CoordSummary | undefined): string | null {
  if (!c) return null;
  if (c.kind === "time" || c.start !== undefined) {
    if (c.start === undefined || c.start === null || c.end === undefined || c.end === null) return null;
    const unit = freqUnit(c.freq);
    return `${String(c.start)} to ${String(c.end)}${unit && c.size ? ` (${plural(c.size, unit)})` : ""}`;
  }
  const values = c.values ?? [];
  if (!values.length) return c.size ? plural(c.size, "level") : null;
  const shown = values.slice(0, 8).map(String).join(", ");
  return `${plural(c.size ?? values.length, "level")}: ${shown}${values.length > 8 ? ", …" : ""}`;
}

export interface InputVar {
  name: string;
  role: string;
  dims: string[];
  units: string | null;
  /** Needed on the periods a forecast covers. */
  future: boolean;
  description: string | null;
}

/** The model's variables, the target(s) first. Index and constant variables are left out. */
export function inputVars(snapshot: Snapshot): InputVar[] {
  const vars = Object.entries(snapshot.input_schema.vars ?? {});
  const future = new Set(snapshot.future_required ?? []);
  return vars
    .filter(([, v]) => v.role !== "index" && v.role !== "constant")
    .map(([name, v]) => ({
      name,
      role: v.role ?? "covariate",
      dims: v.dims,
      units: v.units ?? null,
      future: future.has(name) || Boolean(v.future_required),
      description: v.description ?? null,
    }))
    .sort((a, b) => Number(b.role === "target") - Number(a.role === "target"));
}

export function targets(snapshot: Snapshot): string[] {
  return inputVars(snapshot).filter((v) => v.role === "target").map((v) => v.name);
}

/** The labels of the `periods` periods after the fit (ISO dates or integers), or [] when unknown. */
export function futureLabels(fit: CoordSummary | undefined, periods: number): string[] {
  if (!fit || fit.end === undefined || fit.end === null) return [];
  const out: string[] = [];
  let label = String(fit.end);
  for (let k = 0; k < periods; k++) {
    label = nextLabel(label, fit.freq ?? (fit.kind === "time" ? null : undefined));
    if (!label) return [];
    out.push(label);
  }
  return out;
}

/**
 * The grid's columns for a mode: the coordinates, then the target only where the model takes
 * actuals in a forecast, then the inputs (drivers first).
 */
export function gridColumns(inputs: ModelInputSchema, mode: ScoreMode): InputTableColumn[] {
  const columns = inputs.input_table?.columns ?? [];
  const actuals = mode === "forecast" && inputs.snapshot.conditions_on_actuals;
  return columns.filter((c) => c.role !== "target" || actuals);
}

/**
 * The grid's starting rows (cells as text, in `columns` order): the periods to score crossed
 * with every fitted level of the other coordinate columns. A forecast covers the `periods`
 * periods after the fit; other modes start from the last fitted period.
 */
export function seedRows(inputs: ModelInputSchema, columns: InputTableColumn[], mode: ScoreMode, periods: number): string[][] {
  const snap = inputs.snapshot;
  const hdim = snap.horizon_dim ?? null;
  const levels: string[][] = columns
    .filter((c) => c.role === "coord")
    .map((c) => {
      const fit = snap.fit_coords?.[c.name];
      if (c.name === hdim) {
        if (mode === "forecast") return futureLabels(fit, periods);
        return fit?.end !== undefined && fit.end !== null ? [String(fit.end)] : [""];
      }
      const values = (fit?.values ?? []).map(String);
      return values.length ? values : [""];
    });
  let combos: string[][] = [[]];
  for (const values of levels) {
    if (!values.length) return [];
    combos = combos.flatMap((c) => values.map((v) => [...c, v])).slice(0, MAX_GRID_ROWS);
  }
  const blanks = columns.filter((c) => c.role !== "coord").map(() => "");
  return combos.map((c) => [...c, ...blanks]);
}

export interface GridSummary {
  rows: number;
  filled: number;
  /** Value columns with no value in any row. */
  emptyColumns: string[];
  /** Driver columns with some rows blank. */
  partialDrivers: string[];
}

export function summarize(columns: InputTableColumn[], rows: string[][]): GridSummary {
  const used = rows.filter((r) => r.some((c) => c.trim() !== ""));
  const values = columns.map((c, i) => ({ c, i })).filter(({ c }) => c.role !== "coord");
  return {
    rows: used.length,
    filled: used.reduce((n, r) => n + values.filter(({ i }) => (r[i] ?? "").trim() !== "").length, 0),
    emptyColumns: values.filter(({ i }) => used.every((r) => (r[i] ?? "").trim() === "")).map(({ c }) => c.name),
    partialDrivers: values
      .filter(({ c, i }) => c.role === "driver" && used.some((r) => (r[i] ?? "").trim() === "") && used.some((r) => (r[i] ?? "").trim() !== ""))
      .map(({ c }) => c.name),
  };
}

/**
 * The grid as the call's inline data (only columns with at least one value), or null when
 * nothing is typed. Throws with a readable message on an inconsistent grid.
 */
export function gridToInline(columns: InputTableColumn[], rows: string[][], horizonDim: string | null): XrDataset | null {
  const summary = summarize(columns, rows);
  const keep = columns.filter((c) => c.role === "coord" || !summary.emptyColumns.includes(c.name));
  if (!keep.some((c) => c.role !== "coord")) return null;
  const at = new Map(columns.map((c, i) => [c.name, i]));
  const parsed = rows.map((r) => keep.map((c) => parseCell(r[at.get(c.name)!] ?? "", c)));
  const table: InputTable = { horizon_dim: horizonDim, conditions_on_actuals: false, columns: keep, rows: [] };
  const data = tableToXr(table, parsed);
  // Coordinates no value column uses (a level dim only the target varies along) stay out.
  const usedDims = new Set(Object.values(data.data_vars).flatMap((v) => v.dims));
  for (const dim of Object.keys(data.coords)) if (!usedDims.has(dim)) delete data.coords[dim];
  return data;
}

/** Fill every row's cell of `column` with `value` (blank cells only, unless `overwrite`). */
export function fillColumn(rows: string[][], column: number, value: string, overwrite = false): string[][] {
  return rows.map((r) => (overwrite || (r[column] ?? "").trim() === "" ? r.map((c, j) => (j === column ? value : c)) : r));
}
