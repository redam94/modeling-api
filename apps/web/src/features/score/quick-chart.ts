/**
 * The quick chart of a raw output table (Score page, `ResultTable`): which columns it draws by
 * default and which rows. A table with more dims than the x axis and the colour (a grid
 * forecast: `quarter × row × col`) holds one series per cell; drawn as one line, every x value
 * collapses to a vertical stroke and the band has no width. The chart therefore draws one cell
 * at a time, picked by its levels ("3 · 7"), like the over-time view of "Results by output".
 */
import type { DecodedTable, Row } from "@/lib/arrow";
import { distinctOn, STAT_COLUMN } from "@/lib/summary";

/** The most cells offered in the picker. */
export const MAX_CELLS = 500;

const NUMERIC = /float|int|decimal/i;
const TEMPORAL = /timestamp|date/i;
const CATEGORICAL = /utf8|string|dictionary|bool/i;

export interface QuickChartChoice {
  x: string;
  y: string;
  /** `lower|upper`, or "" for no band. */
  band: string;
  /** A column to colour by, or "". */
  color: string;
  /** The dims that pick one cell (the table's dims that are not x, the colour or `stat`). */
  facets: string[];
}

/** The numeric columns of a table (the y and band candidates). */
export function numericColumns(table: DecodedTable): string[] {
  return table.columns.filter((c) => NUMERIC.test(c.type)).map((c) => c.name);
}

/** The label columns of a table (the colour candidates). */
export function categoricalColumns(table: DecodedTable): string[] {
  return table.columns.filter((c) => CATEGORICAL.test(c.type)).map((c) => c.name);
}

/** The first temporal column, if any. */
export function timeColumn(table: DecodedTable): string | undefined {
  return table.columns.find((c) => TEMPORAL.test(c.type))?.name;
}

/** The interval of `y` (`y_hdi_90_lower|y_hdi_90_upper`), else any interval, else "". */
export function defaultBand(numeric: readonly string[], y: string): string {
  const stem = y.replace(/_(mean|median)$/i, "");
  const pick = (re: RegExp) => numeric.find((n) => n.startsWith(stem) && re.test(n)) ?? numeric.find((n) => re.test(n));
  const lo = pick(/(lower|lo|hdi.*low|q0?5)$/i);
  const hi = pick(/(upper|hi|hdi.*high|q95)$/i);
  return lo && hi ? `${lo}|${hi}` : "";
}

/**
 * The dims that pick one cell for the given x and colour: the table's dims (Arrow `mapi.dims`
 * metadata) present as columns, without `stat`, x and the colour. A table without dims
 * metadata has none (its key columns are unknown).
 */
export function facetsFor(table: DecodedTable, x: string, color: string): string[] {
  const names = new Set(table.columns.map((c) => c.name));
  return (table.dims ?? []).filter((d) => d !== STAT_COLUMN && d !== x && d !== color && names.has(d));
}

/** The default chart of a table: time on x when there is a time column, the mean on y, its band. */
export function quickChartChoice(table: DecodedTable): QuickChartChoice {
  const numeric = numericColumns(table);
  const all = table.columns.map((c) => c.name);
  const x = timeColumn(table) ?? all[0] ?? "";
  const dims = new Set(table.dims ?? []);
  const values = numeric.filter((n) => !dims.has(n));
  const y = values.find((n) => /^y_hat_mean$/i.test(n)) ?? values.find((n) => /mean|median|value|y_hat/i.test(n)) ?? values[0] ?? numeric[0] ?? "";
  const color = categoricalColumns(table).find((c) => c !== x) ?? "";
  return { x, y, band: y ? defaultBand(numeric, y) : "", color, facets: facetsFor(table, x, color) };
}

/** The cell of a row: its facet levels joined with " · ". */
export function cellOf(row: Row, facets: readonly string[]): string {
  return facets.map((f) => String(row[f] ?? "")).join(" · ");
}

/**
 * The cells of the table in row order (at most {@link MAX_CELLS}) and the default one: the
 * first cell where `y` has a value (a grid cell outside the study area is all empty).
 */
export function cellLevels(rows: readonly Row[], facets: readonly string[], y: string): { cells: string[]; first: string; total: number } {
  if (!facets.length) return { cells: [], first: "", total: 0 };
  const seen = new Set<string>();
  const cells: string[] = [];
  let first: string | null = null;
  for (const row of rows) {
    const cell = cellOf(row, facets);
    if (first === null && typeof row[y] === "number") first = cell;
    if (seen.has(cell)) continue;
    seen.add(cell);
    if (cells.length < MAX_CELLS) cells.push(cell);
  }
  const start = first !== null && cells.includes(first) ? first : (cells[0] ?? "");
  return { cells, first: start, total: seen.size };
}

/**
 * The rows the chart draws: those of one cell (when the choice has facets), unique on the drawn
 * fields. A variable broadcast over a dim it does not have would otherwise draw one zigzag per
 * coordinate.
 */
export function quickChartRows(rows: readonly Row[], choice: Pick<QuickChartChoice, "x" | "y" | "band" | "color" | "facets">, cell: string | null): Row[] {
  const shown = choice.facets.length && cell !== null ? rows.filter((r) => cellOf(r, choice.facets) === cell) : [...rows];
  const [lower, upper] = choice.band ? choice.band.split("|") : [undefined, undefined];
  return distinctOn(shown, [choice.x, choice.color || undefined, choice.y, lower, upper]);
}
