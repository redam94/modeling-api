/**
 * Forecasts with actuals (`mapi_spec.actuals`): an endpoint's inline input as a long table
 * (`EndpointSchema.input_table`) - the time column, the target, the drivers - turned into the
 * forecast call the server expects. Rows whose target holds a value are actuals; rows after the
 * last actual with the target empty are the periods to forecast. Mirrors
 * `mapi_spec.xrjson.xrjson_from_rows` and `mapi_spec.endpoints.table_forecast_call` (the server
 * still checks everything).
 */
import type { EndpointScoreCall, ForecastConditioning, InputTable, InputTableColumn } from "@/api/types";

export type Cell = string | number | boolean | null;

/** An editor cell as JSON: blank is null; numbers in value columns (and integer coordinates) are numbers. */
export function parseCell(text: string, column: InputTableColumn): Cell {
  const v = text.trim();
  if (v === "") return null;
  if (column.role === "coord") {
    if (column.freq) return v;
    return /^-?\d+$/.test(v) ? Number(v) : v;
  }
  const n = Number(v.replace(/,/g, ""));
  if (v !== "" && Number.isFinite(n) && /^[-+]?[\d,]*\.?\d+(e[-+]?\d+)?$/i.test(v)) return n;
  if (v === "true" || v === "false") return v === "true";
  return v;
}

/** The text of a table cell in the editor. */
export function cellText(value: Cell | undefined): string {
  return value === null || value === undefined ? "" : String(value);
}

export interface XrVariable {
  dims: string[];
  data: unknown;
}
export interface XrDataset {
  coords: Record<string, XrVariable>;
  data_vars: Record<string, XrVariable>;
}

/** The table's rows (cells per column, in `table.columns` order) as an inline dataset. */
export function tableToXr(table: InputTable, rows: Cell[][]): XrDataset {
  const columns = table.columns ?? [];
  const at = new Map(columns.map((c, i) => [c.name, i]));
  const filled = rows.filter((r) => r.some((c) => c !== null && c !== ""));
  const index = new Map<string, Map<string, number>>();
  const labels = new Map<string, Cell[]>();
  const values = columns.filter((c) => c.role !== "coord");
  for (const v of values) {
    for (const dim of v.dims ?? []) {
      if (index.has(dim)) continue;
      const i = at.get(dim);
      if (i === undefined) throw new Error(`the table has no column "${dim}"`);
      const seen = new Map<string, number>();
      const list: Cell[] = [];
      filled.forEach((r, k) => {
        const label = r[i] ?? null;
        if (label === null || label === "") throw new Error(`row ${k + 1} has no "${dim}"`);
        const key = JSON.stringify(label);
        if (!seen.has(key)) {
          seen.set(key, list.length);
          list.push(label);
        }
      });
      index.set(dim, seen);
      labels.set(dim, list);
    }
  }
  const coords: Record<string, XrVariable> = {};
  for (const [dim, list] of labels) coords[dim] = { dims: [dim], data: list };
  const data_vars: Record<string, XrVariable> = {};
  for (const v of values) {
    const dims = v.dims ?? [];
    const i = at.get(v.name)!;
    const shape = dims.map((d) => labels.get(d)!.length);
    const empty = (k: number): unknown => (k === shape.length ? null : Array.from({ length: shape[k]! }, () => empty(k + 1)));
    const data = empty(0);
    for (const r of filled) {
      const value = r[i] ?? null;
      if (value === null || value === "") continue;
      const pos = dims.map((d) => index.get(d)!.get(JSON.stringify(r[at.get(d)!] ?? null))!);
      if (!pos.length) {
        data_vars[v.name] = { dims: [], data: value };
        continue;
      }
      let node = data as unknown[];
      for (let k = 0; k < pos.length - 1; k++) node = node[pos[k]!] as unknown[];
      const last = pos[pos.length - 1]!;
      if (node[last] !== null && node[last] !== value) throw new Error(`"${v.name}" has two different values at one row`);
      node[last] = value;
    }
    if (!(v.name in data_vars)) data_vars[v.name] = { dims, data };
  }
  return { coords, data_vars };
}

/** The periods to forecast: the labels after the last one with a target value (every label when none has one). */
export function forecastPeriods(table: InputTable, data: XrDataset): number {
  const dim = table.horizon_dim;
  if (!dim) return 0;
  const labels = (data.coords[dim]?.data as Cell[] | undefined) ?? [];
  const filled = new Set<number>();
  for (const c of (table.columns ?? []).filter((x) => x.role === "target")) {
    const v = data.data_vars[c.name];
    if (!v) continue;
    const axis = v.dims.indexOf(dim);
    const walk = (node: unknown, depth: number, pos: number) => {
      if (depth === v.dims.length) {
        if (node !== null) filled.add(pos);
        return;
      }
      (node as unknown[]).forEach((child, k) => walk(child, depth + 1, depth === axis ? k : pos));
    };
    if (axis >= 0) walk(v.data, 0, -1);
  }
  const order = labels.map((_, k) => k).sort((a, b) => (String(labels[a]) < String(labels[b]) ? -1 : 1));
  if (!filled.size) return labels.length;
  let after = 0;
  for (let k = order.length - 1; k >= 0 && !filled.has(order[k]!); k--) after++;
  return after;
}

/**
 * The forecast call over the table (`table_forecast_call`): when the table ends with an actual,
 * `fallbackPeriods` periods are appended after it. Empty future drivers are carried forward. A
 * table without any filled row sends no data: the example's horizon follows the fitted history.
 */
export function tableForecastCall(table: InputTable, rows: Cell[][], example: Record<string, unknown>, fallbackPeriods = 8): EndpointScoreCall {
  const dim = table.horizon_dim;
  if (!dim) throw new Error("this endpoint has no dim a forecast extends");
  if (!rows.some((r) => r.some((c) => c !== null && c !== ""))) {
    const given = (example.horizon as { periods?: number } | undefined)?.periods;
    const call: Record<string, unknown> = { mode: "forecast", horizon: { dim, periods: given ?? fallbackPeriods }, future_covariates: "carry_last" };
    if (example.output) call.output = example.output;
    call.data = null;
    return call;
  }
  const data = tableToXr(table, rows);
  if (!Object.keys(data.data_vars).length) throw new Error("the table has no values");
  const periods = forecastPeriods(table, data) || fallbackPeriods;
  const call: Record<string, unknown> = { mode: "forecast", horizon: { dim, periods }, future_covariates: "carry_last" };
  if (example.output) call.output = example.output;
  call.data = data;
  return call;
}

/** The label after `label` at frequency `freq` (ISO dates; integers step by 1), or "" when unknown. */
export function nextLabel(label: string, freq: string | null | undefined): string {
  if (!label) return "";
  if (/^-?\d+$/.test(label) && !freq) return String(Number(label) + 1);
  const d = new Date(`${label.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(d.getTime()) || !freq) return "";
  const m = /^(\d*)(MS|ME|QS|QE|YS|YE|D|W|h)/.exec(freq);
  if (!m) return "";
  const n = Number(m[1] || 1);
  const unit = m[2];
  const out = new Date(d);
  if (unit === "D") out.setUTCDate(out.getUTCDate() + n);
  else if (unit === "W") out.setUTCDate(out.getUTCDate() + 7 * n);
  else if (unit === "MS" || unit === "QS" || unit === "YS") out.setUTCMonth(out.getUTCMonth() + n * (unit === "MS" ? 1 : unit === "QS" ? 3 : 12));
  else if (unit === "ME" || unit === "QE" || unit === "YE") {
    const months = n * (unit === "ME" ? 1 : unit === "QE" ? 3 : 12);
    return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + months + 1, 0)).toISOString().slice(0, 10);
  } else return "";
  return out.toISOString().slice(0, 10);
}

/** A short line for the answer: "Uses 3 actuals up to 1961-03-01" or "Starts at the end of the fitted history (1960-12-01)". */
export function conditioningHeadline(c: ForecastConditioning): string {
  if (c.conditioned) {
    return `Uses ${c.n_actuals} actual${c.n_actuals === 1 ? "" : "s"} up to ${String(c.actuals_through ?? "")}`;
  }
  return `Starts at the end of the fitted history (${String(c.fit_end)})`;
}

/** Tab- or comma-separated text pasted from a spreadsheet (a header row first) as rows in the table's column order. */
export function parsePasted(table: InputTable, text: string): Cell[][] {
  const lines = text.split(/\r?\n/).filter((l) => l.trim() !== "");
  if (!lines.length) return [];
  const sep = lines[0]!.includes("\t") ? "\t" : ",";
  const header = lines[0]!.split(sep).map((h) => h.trim());
  const columns = table.columns ?? [];
  const known = columns.some((c) => header.includes(c.name));
  const body = known ? lines.slice(1) : lines;
  const order = known ? columns.map((c) => header.indexOf(c.name)) : columns.map((_, i) => i);
  return body.map((line) => {
    const cells = line.split(sep);
    return columns.map((c, i) => {
      const j = order[i]!;
      return j < 0 ? null : parseCell(cells[j] ?? "", c);
    });
  });
}
