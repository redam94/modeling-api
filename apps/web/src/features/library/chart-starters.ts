/**
 * The chart template builder's simple mode: the columns of a chart type's example datasets
 * (decoded in the browser, so their names and kinds are known), a few starting points that fit
 * those columns, and the Vega-Lite spec a set of channel choices becomes.
 *
 * The spec only ever reads the source recipe's named datasets (`data: {name}`): the server
 * keeps the real plot's datasets and swaps the spec (v0.18 D5). No URLs, no `usermeta`, no
 * expressions are generated here; the sanitizer checks whatever the "Advanced" JSON adds.
 */
import type { JsonObject, PlotDatasets, Row } from "@mapi/plot-sandbox/protocol";

export type ColumnType = "temporal" | "quantitative" | "nominal";

export interface Column {
  name: string;
  type: ColumnType;
}

export interface DatasetColumns {
  name: string;
  rows: number;
  columns: Column[];
}

function isIsoDate(v: string): boolean {
  if (v.length < 10 || v[4] !== "-" || v[7] !== "-") return false;
  for (const i of [0, 1, 2, 3, 5, 6, 8, 9]) {
    const c = v[i]!;
    if (c < "0" || c > "9") return false;
  }
  return true;
}

function typeOf(rows: readonly Row[], column: string): ColumnType {
  let seen = 0;
  let numbers = 0;
  let dates = 0;
  for (const r of rows) {
    const v = r[column];
    if (v === null || v === undefined) continue;
    seen++;
    if (typeof v === "number") numbers++;
    else if (typeof v === "string" && isIsoDate(v)) dates++;
    if (seen >= 50) break;
  }
  if (seen && dates === seen) return "temporal";
  if (seen && numbers === seen) return "quantitative";
  return "nominal";
}

/** The columns of each example dataset (names in first-row order, kinds from the values). */
export function columnsOf(datasets: PlotDatasets): DatasetColumns[] {
  return Object.entries(datasets).map(([name, rows]) => {
    const names: string[] = [];
    for (const r of rows.slice(0, 50)) for (const k of Object.keys(r)) if (!names.includes(k)) names.push(k);
    return { name, rows: rows.length, columns: names.map((c) => ({ name: c, type: typeOf(rows, c) })) };
  });
}

export type Mark = "line" | "area" | "bar" | "point";

export const MARKS: { value: Mark; label: string }[] = [
  { value: "line", label: "Line" },
  { value: "area", label: "Area" },
  { value: "bar", label: "Bars" },
  { value: "point", label: "Dots" },
];

/** The simple editor's choices. Empty strings mean "none". */
export interface SimpleChart {
  dataset: string;
  mark: Mark;
  x: string;
  y: string;
  color: string;
  facet: string;
  /** A shaded band behind the mark, from a low and a high column. */
  bandLo: string;
  bandHi: string;
  /** A second layer of dots (actual values), or "". */
  dots: string;
}

export const EMPTY_CHART: SimpleChart = { dataset: "", mark: "line", x: "", y: "", color: "", facet: "", bandLo: "", bandHi: "", dots: "" };

function typeIn(ds: DatasetColumns | undefined, column: string): ColumnType {
  return ds?.columns.find((c) => c.name === column)?.type ?? "nominal";
}

/** The Vega-Lite spec of a simple chart over one of the source recipe's datasets. */
export function simpleSpec(chart: SimpleChart, datasets: readonly DatasetColumns[]): JsonObject {
  const ds = datasets.find((d) => d.name === chart.dataset);
  const enc = (field: string, extra: JsonObject = {}): JsonObject => ({ field, type: typeIn(ds, field), ...extra });
  const x = chart.x ? enc(chart.x, { title: null }) : null;
  const layers: JsonObject[] = [];
  if (chart.bandLo && chart.bandHi && x) {
    layers.push({ mark: { type: "area", opacity: 0.25 }, encoding: { x, y: enc(chart.bandLo, { title: null }), y2: { field: chart.bandHi } } });
  }
  if (chart.y && x) {
    const encoding: JsonObject = { x, y: enc(chart.y, { title: null }) };
    if (chart.color) encoding.color = enc(chart.color, { title: null });
    layers.push({ mark: chart.mark === "point" ? { type: "point", filled: true } : { type: chart.mark }, encoding });
  }
  if (chart.dots && x) {
    layers.push({ mark: { type: "point", size: 14, filled: true }, encoding: { x, y: enc(chart.dots, { title: null }) } });
  }
  const inner: JsonObject = layers.length === 1 ? { ...layers[0]! } : { layer: layers };
  if (chart.facet) {
    return { data: { name: chart.dataset }, facet: enc(chart.facet, { title: null }), columns: 2, spec: { width: 260, height: 160, ...inner } };
  }
  return { data: { name: chart.dataset }, width: 560, height: 260, ...inner };
}

function first(ds: DatasetColumns, type: ColumnType, not: readonly string[] = []): string {
  return ds.columns.find((c) => c.type === type && !not.includes(c.name))?.name ?? "";
}

function has(ds: DatasetColumns, name: string): boolean {
  return ds.columns.some((c) => c.name === name);
}

export interface Starter {
  id: string;
  label: string;
  hint: string;
  chart: SimpleChart;
}

/** Starting points that fit the example's columns (the first one is the default). */
export function chartStarters(datasets: readonly DatasetColumns[]): Starter[] {
  const out: Starter[] = [];
  for (const ds of datasets) {
    if (ds.columns.length < 2) continue;
    const time = first(ds, "temporal");
    const group = first(ds, "nominal");
    const value = has(ds, "point") ? "point" : first(ds, "quantitative", [time]);
    const base = { ...EMPTY_CHART, dataset: ds.name };
    const loHi = (["lo_90", "hi_90"] as const).every((c) => has(ds, c)) ? ["lo_90", "hi_90"] : (["lo", "hi"] as const).every((c) => has(ds, c)) ? ["lo", "hi"] : null;
    if (time && value) {
      if (loHi) {
        out.push({
          id: `${ds.name}-band`,
          label: "Line with a band",
          hint: "The estimate as a line, its range as a shaded band" + (has(ds, "observed") ? ", actual values as dots." : "."),
          chart: { ...base, mark: "line", x: time, y: value, bandLo: loHi[0]!, bandHi: loHi[1]!, dots: has(ds, "observed") ? "observed" : "", facet: group },
        });
      }
      out.push({ id: `${ds.name}-line`, label: "Lines over time", hint: `${value} over ${time}${group ? `, one line for each ${group}` : ""}.`, chart: { ...base, mark: "line", x: time, y: value, color: group } });
      if (group) out.push({ id: `${ds.name}-small`, label: "Small multiples", hint: `One small chart for each ${group}.`, chart: { ...base, mark: "area", x: time, y: value, facet: group } });
    } else {
      const x = first(ds, "quantitative");
      const y = first(ds, "quantitative", [x]);
      if (x && y) out.push({ id: `${ds.name}-curve`, label: "Curve", hint: `${y} against ${x}${group ? `, one line for each ${group}` : ""}.`, chart: { ...base, mark: "line", x, y, color: group, ...(loHi ? { bandLo: loHi[0]!, bandHi: loHi[1]! } : {}) } });
      if (group && y) out.push({ id: `${ds.name}-bars`, label: "Bars by group", hint: `${y} for each ${group}.`, chart: { ...base, mark: "bar", x: group, y } });
      if (x && y) out.push({ id: `${ds.name}-dots`, label: "Dots", hint: `${y} against ${x} as dots.`, chart: { ...base, mark: "point", x, y, color: group } });
    }
  }
  return out;
}
