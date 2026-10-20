/**
 * The shape of each score output, from the inline summary tables (`ScoreSummary.tables`) and
 * the runner's `ui/fields.json`: an output on a grid is a map (with a frame slider), one with
 * a time dim a line with its band, one with other dims an interval per cell, and one with no
 * dims a single value. The runner decides what is a grid (it has the manifest and the
 * binding); the tables give the rest, with `dims` / `time_dim` from newer servers and the
 * statistic names as the fallback.
 */
import type { ScoreSummary } from "@/api/types";
import type { FieldEntry, FieldsView } from "@/features/explore/views";
import type { Row } from "@mapi/plot-sandbox/protocol";

import { bandOf, CENTERS, quantile } from "./compare";

/** Where a predict, forecast or evaluate run writes the FieldsView of its grid outputs. */
export const SCORE_FIELDS_VIEW = "ui/fields.json";

/** The run's plots that the field viewer replaces when `ui/fields.json` is there. */
export const FIELD_PLOT_IDS: readonly string[] = ["sci.field_map", "sci.vector_field"];

// `dims` and `time_dim` are optional: older servers leave them out.
export type Table = NonNullable<ScoreSummary["tables"]>[number];
type Cell = Table["rows"][number][number];

export type OutputShape =
  | { kind: "grid"; variable: string; fields: FieldEntry[] }
  | { kind: "time"; variable: string; time: string; series?: string }
  | { kind: "categorical"; variable: string; dims: string[] }
  | { kind: "scalar"; variable: string };

export const SHAPE_LABEL: Record<OutputShape["kind"], string> = {
  grid: "map",
  time: "over time",
  categorical: "by category",
  scalar: "single value",
};

const TIME_NAME = /^(time|date|week|month|quarter|year|day|period|t|ds|step)$/i;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}/;
/** The most levels a second dim can have and still be drawn as coloured lines. */
const MAX_SERIES = 12;

function isStat(name: string): boolean {
  return CENTERS.includes(name) || name.endsWith("_lower") || name.endsWith("_upper") || quantile(name) !== null || name === "sd";
}

/** The coordinate columns of a table: its `dims`, else the leading columns that are not statistics. */
export function tableDims(t: Table): string[] {
  if (t.dims) return t.dims.filter((d) => t.columns.includes(d));
  const first = t.columns.findIndex(isStat);
  return first < 0 ? [] : t.columns.slice(0, first);
}

/** The statistic columns of a table (everything after the dims). */
export function tableStats(t: Table): string[] {
  const dims = tableDims(t);
  return t.columns.filter((c) => !dims.includes(c));
}

function column(t: Table, name: string): Cell[] {
  const i = t.columns.indexOf(name);
  return i < 0 ? [] : t.rows.map((r) => r[i] ?? null);
}

/** Whether the dim's values are ISO dates (a temporal axis), not labels or numbers. */
export function isDateDim(t: Table, dim: string): boolean {
  const values = column(t, dim).filter((v) => v !== null);
  return values.length > 0 && values.every((v) => typeof v === "string" && ISO_DATE.test(v));
}

function levels(t: Table, dim: string): number {
  return new Set(column(t, dim)).size;
}

/** The FieldViewer entries of `ui/fields.json` by output (several when the output has facets). */
export function gridOutputsOf(view: FieldsView | null | undefined): Map<string, FieldEntry[]> {
  const out = new Map<string, FieldEntry[]>();
  for (const f of view?.fields ?? []) out.set(f.output, [...(out.get(f.output) ?? []), f]);
  return out;
}

/** The shape of one table: a grid output, else by its dims (no dims, a time dim, other dims). */
export function classify(t: Table, gridOutputs: Map<string, FieldEntry[]>): OutputShape {
  const fields = gridOutputs.get(t.variable);
  if (fields?.length) return { kind: "grid", variable: t.variable, fields };
  const dims = tableDims(t);
  if (!dims.length) return { kind: "scalar", variable: t.variable };
  // A circular table (`period`) has an angle, not a time line: intervals for now.
  if (t.period == null) {
    const time = (t.time_dim && dims.includes(t.time_dim) ? t.time_dim : null) ?? dims.find((d) => isDateDim(t, d)) ?? dims.find((d) => TIME_NAME.test(d));
    if (time) {
      const others = dims.filter((d) => d !== time);
      const series = others.length === 1 && levels(t, others[0]!) <= MAX_SERIES ? others[0] : undefined;
      return series ? { kind: "time", variable: t.variable, time, series } : { kind: "time", variable: t.variable, time };
    }
  }
  return { kind: "categorical", variable: t.variable, dims };
}

/**
 * The shape of every output: the summary's tables in their order, then the grid outputs that
 * a truncated summary left out.
 */
export function shapesOf(summary: ScoreSummary | null | undefined, fields: FieldsView | null | undefined): OutputShape[] {
  const grids = gridOutputsOf(fields);
  const shapes = (summary?.tables ?? []).map((t) => classify(t, grids));
  for (const [variable, entries] of grids) {
    if (!shapes.some((s) => s.variable === variable)) shapes.push({ kind: "grid", variable, fields: entries });
  }
  return shapes;
}

/** The central statistic and the widest band of a table (null when it has none). */
export function statsOf(t: Table): { center: string | null; band: { lower: string; upper: string; label: string } | null } {
  const stats = tableStats(t);
  return { center: CENTERS.find((c) => stats.includes(c)) ?? null, band: bandOf(stats) };
}

/** The table as row objects (one per row, keyed by column), for the plot sandbox. */
export function tableRows(t: Table): Row[] {
  return t.rows.map((r) => Object.fromEntries(t.columns.map((c, i) => [c, r[i] ?? null])));
}

function num(v: Cell | undefined): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** One estimate with its band per row, labelled by its coordinates ("de · tv"). */
export function intervalRows(t: Table, dims: string[]): { label: string; mean: number | null; lower: number | null; upper: number | null }[] {
  const { center, band } = statsOf(t);
  const at = (name: string | null | undefined) => (name ? t.columns.indexOf(name) : -1);
  const [c, lo, hi] = [at(center), at(band?.lower), at(band?.upper)];
  const ds = dims.map((d) => at(d));
  return t.rows.map((r) => ({
    label: ds.map((i) => String(r[i] ?? "")).join(" · ") || t.variable,
    mean: c < 0 ? null : num(r[c]),
    lower: lo < 0 ? null : num(r[lo]),
    upper: hi < 0 ? null : num(r[hi]),
  }));
}
