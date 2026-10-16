/**
 * Local profiling of an upload for the Guided question flow (plan §11 Guided task 2).
 *
 * The browser reads the head of the user's own file, profiles every column
 * (`mapi_spec.guided.ColumnProfile`: type, counts, range, date frequency, a few samples) and
 * sends only the profiles to `POST /v1/guided/detect-roles`. Nothing hostile reaches the servers
 * just to be previewed; the file itself is parsed for real only by the sandboxed ingest run.
 *
 * MFF long tables (`Period`, key columns, `VariableName`, `VariableValue`) are pivoted first, so
 * every variable is profiled like a column (`origin: "mff_variable"`). A Google Meridian wide
 * table is recognised by its standard column names and pre-fills the answers.
 */
import { parseCsv } from "@/lib/csv";

import type { ColumnKind, ColumnProfile } from "./types";

/** How much of a file is read for the local preview and profile. */
export const PROFILE_BYTES = 8 * 1024 * 1024;
export const PROFILE_ROWS = 100_000;
const SAMPLE_VALUES = 5;

export const MFF_COLUMNS = ["Period", "Geography", "Product", "Outlet", "Campaign", "Creative", "VariableValue", "VariableName"] as const;
export const MFF_KEY_COLUMNS = ["Geography", "Product", "Outlet", "Campaign", "Creative"] as const;

export type UploadFormat = "csv" | "parquet" | "mff";

/** A table as the browser sees it: the header and (some of) the rows, as text. */
export interface LocalTable {
  header: string[];
  rows: string[][];
  /** More rows exist in the file than were read. */
  truncated: boolean;
  /** Set when the table is the pivot of an MFF long table. */
  mff: { variables: string[]; keys: string[]; longRows: number } | null;
}

export function formatFromName(name: string): UploadFormat | null {
  const n = name.toLowerCase();
  if (/\.(csv|tsv|txt)$/.test(n)) return "csv";
  if (/\.(parquet|pq)$/.test(n)) return "parquet";
  return null;
}

const MISSING = new Set(["", "na", "nan", "null", "none", "n/a", "#n/a"]);
const DATE_RE = /^\d{4}[-/]\d{2}[-/]\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/;
const INT_RE = /^[+-]?\d+$/;
const NUM_RE = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
const BOOL = new Set(["true", "false", "yes", "no"]);

export function isMissing(v: string | undefined): boolean {
  return v === undefined || MISSING.has(v.trim().toLowerCase());
}

/** The type of a column from its (non-missing) values. */
export function columnKind(values: readonly string[]): ColumnKind {
  const vs = values.map((v) => v.trim()).filter((v) => !isMissing(v));
  if (!vs.length) return "string";
  if (vs.every((v) => DATE_RE.test(v))) return "date";
  if (vs.every((v) => BOOL.has(v.toLowerCase()))) return "boolean";
  if (vs.every((v) => INT_RE.test(v))) return "integer";
  if (vs.every((v) => NUM_RE.test(v))) return "number";
  return "string";
}

function dayOf(v: string): number | null {
  const t = Date.parse(v.slice(0, 10).replace(/\//g, "-") + "T00:00:00Z");
  return Number.isNaN(t) ? null : Math.round(t / 86_400_000);
}

const WEEKDAYS = ["THU", "FRI", "SAT", "SUN", "MON", "TUE", "WED"]; // day 0 (1970-01-01) was a Thursday

/** A pandas frequency alias for evenly spaced dates (`W-SUN`, `D`, `MS`), else `null`. */
export function detectFrequency(dates: readonly string[]): string | null {
  const days = [...new Set(dates.map(dayOf).filter((d): d is number => d !== null))].sort((a, b) => a - b);
  if (days.length < 3) return null;
  const steps = new Set(days.slice(1).map((d, i) => d - (days[i] as number)));
  if (steps.size === 1) {
    const step = [...steps][0] as number;
    if (step === 1) return "D";
    if (step === 7) return `W-${WEEKDAYS[(((days[0] as number) % 7) + 7) % 7]}`;
  }
  const firsts = days.every((d) => new Date(d * 86_400_000).getUTCDate() === 1);
  if (firsts && [...steps].every((s) => s >= 28 && s <= 31)) return "MS";
  return null;
}

/** Read the head of a local CSV file (or an MFF long table, pivoted). */
export async function readLocalTable(file: Blob, maxBytes = PROFILE_BYTES): Promise<LocalTable> {
  const text = await file.slice(0, maxBytes).text();
  return tableFromText(text, file.size > maxBytes);
}

export function tableFromText(text: string, cut = false, maxRows = PROFILE_ROWS): LocalTable {
  const csv = parseCsv(text, maxRows);
  // A sample cut mid-file may end in a partial row: drop it.
  const rows = cut && csv.rows.length > 1 ? csv.rows.slice(0, -1) : csv.rows;
  const table: LocalTable = { header: csv.header, rows, truncated: cut || csv.truncated, mff: null };
  return isMffHeader(table.header) ? pivotMff(table) : table;
}

export function isMffHeader(header: readonly string[]): boolean {
  const set = new Set(header);
  return set.has("Period") && set.has("VariableName") && set.has("VariableValue");
}

/** One row per period and key combination, one column per `VariableName` (as the ingest pivots). */
export function pivotMff(long: LocalTable): LocalTable {
  const col = (name: string) => long.header.indexOf(name);
  const period = col("Period");
  const vname = col("VariableName");
  const value = col("VariableValue");
  const keys = MFF_KEY_COLUMNS.filter((k) => col(k) >= 0);
  const variables: string[] = [];
  const seenVar = new Set<string>();
  const byKey = new Map<string, { key: string[]; values: Map<string, string> }>();
  for (const r of long.rows) {
    const name = (r[vname] ?? "").trim();
    if (!name) continue;
    if (!seenVar.has(name)) {
      seenVar.add(name);
      variables.push(name);
    }
    const key = [r[period] ?? "", ...keys.map((k) => r[col(k)] ?? "")];
    const id = key.join("\u0000");
    let entry = byKey.get(id);
    if (!entry) {
      entry = { key, values: new Map() };
      byKey.set(id, entry);
    }
    entry.values.set(name, r[value] ?? "");
  }
  const header = ["Period", ...keys, ...variables];
  const rows = [...byKey.values()].map((e) => [...e.key, ...variables.map((v) => e.values.get(v) ?? "")]);
  return { header, rows, truncated: long.truncated, mff: { variables, keys: [...keys], longRows: long.rows.length } };
}

function values(table: LocalTable, j: number): string[] {
  return table.rows.map((r) => r[j] ?? "");
}

/** `ColumnProfile`s of every column of a local table (sent to role detection). */
export function profileTable(table: LocalTable): ColumnProfile[] {
  const variables = new Set(table.mff?.variables ?? []);
  return table.header.map((name, j) => {
    const raw = values(table, j);
    const present = raw.map((v) => v.trim()).filter((v) => !isMissing(v));
    const dtype = columnKind(present);
    const distinct = new Set(present);
    const profile: ColumnProfile = {
      name: name.slice(0, 256) || `column_${j + 1}`,
      dtype,
      n_rows: raw.length,
      n_missing: raw.length - present.length,
      n_distinct: distinct.size,
      sample: [...distinct].slice(0, SAMPLE_VALUES).map((v) => v.slice(0, 200)),
      origin: variables.has(name) ? "mff_variable" : "column",
    };
    if (dtype === "number" || dtype === "integer") {
      let min = Infinity;
      let max = -Infinity;
      for (const v of present) {
        const x = Number(v);
        if (x < min) min = x;
        if (x > max) max = x;
      }
      profile.min = min;
      profile.max = max;
    }
    if (dtype === "date") {
      const sorted = [...distinct].sort();
      profile.min_date = (sorted[0] ?? "").slice(0, 40);
      profile.max_date = (sorted[sorted.length - 1] ?? "").slice(0, 40);
      profile.detected_freq = detectFrequency(sorted);
    }
    if (dtype === "date" || dtype === "integer" || dtype === "number") {
      const keyed = present.map((v) => (dtype === "date" ? v : Number(v)));
      profile.monotonic = keyed.every((v, i) => i === 0 || (keyed[i - 1] as string | number) <= v);
    }
    return profile;
  });
}

/** Profiles for a Parquet file, which only the sandbox reads: from the names the user types. */
export function profilesFromNames(names: readonly string[]): ColumnProfile[] {
  return names
    .map((n) => n.trim())
    .filter(Boolean)
    .map((name) => ({
      name: name.slice(0, 256),
      dtype: /^(date|week|day|month|period|time|ds)$/i.test(name) || /_date$/i.test(name) ? ("date" as const) : /^(geo|market|region|country|store|channel)$/i.test(name) ? ("string" as const) : ("number" as const),
      n_rows: 0,
      origin: "column" as const,
    }));
}

// ----------------------------------------------------------------------------- preview series

/**
 * The values of `column` for a sparkline: summed per date when `dateColumn` is known (so a
 * panel of markets shows the total per week), else in file order. At most `max` points.
 */
export function seriesFor(table: LocalTable, column: string, dateColumn: string | null, max = 120): number[] {
  const j = table.header.indexOf(column);
  if (j < 0) return [];
  const d = dateColumn ? table.header.indexOf(dateColumn) : -1;
  if (d < 0) {
    return table.rows
      .map((r) => Number(r[j]))
      .filter(Number.isFinite)
      .slice(0, max);
  }
  const sums = new Map<string, number>();
  for (const r of table.rows) {
    const x = Number(r[j]);
    if (!Number.isFinite(x) || isMissing(r[j])) continue;
    const key = r[d] ?? "";
    sums.set(key, (sums.get(key) ?? 0) + x);
  }
  return [...sums.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([, v]) => v)
    .slice(-max);
}

// ----------------------------------------------------------------------------- presets

export interface PresetMatch {
  preset: "meridian" | "mff";
  label: string;
  answers: Record<string, string | string[]>;
}

const lower = (s: string) => s.toLowerCase();

/**
 * Recognise a Google Meridian `CoordToColumns` table by its standard names: `time`, an
 * optional `geo`, a `kpi` (or `conversions`/`revenue`) column, `<channel>_spend` columns and
 * the controls left over. Returns answers for the template's questions, or `null`.
 */
export function detectMeridian(profiles: readonly ColumnProfile[]): PresetMatch | null {
  const by = new Map(profiles.map((p) => [lower(p.name), p]));
  const time = by.get("time");
  const kpi = by.get("kpi") ?? by.get("conversions") ?? by.get("revenue");
  const spend = profiles.filter((p) => /_spend$/i.test(p.name) && (p.dtype === "number" || p.dtype === "integer"));
  if (!time || time.dtype !== "date" || !kpi || spend.length === 0) return null;
  const media = new Set(profiles.filter((p) => /_(impressions?|clicks|reach|frequency|grps?)$/i.test(p.name)).map((p) => p.name));
  const geo = by.get("geo");
  const used = new Set([time.name, kpi.name, ...spend.map((s) => s.name), ...(geo ? [geo.name] : []), ...media]);
  const skip = /^(population|revenue_per_kpi|revenue_per_conversion)$/i;
  const controls = profiles.filter((p) => !used.has(p.name) && !skip.test(p.name) && (p.dtype === "number" || p.dtype === "integer")).map((p) => p.name);
  const answers: Record<string, string | string[]> = { date: time.name, kpi: kpi.name, spend: spend.map((s) => s.name), controls };
  if (geo) answers.geo = geo.name;
  return { preset: "meridian", label: "Google Meridian table", answers };
}

/** An MFF long table: answers follow BayesInsight's names (`Period`, `Geography`). */
export function detectMff(table: LocalTable): PresetMatch | null {
  if (!table.mff) return null;
  const answers: Record<string, string | string[]> = { date: "Period" };
  if (table.mff.keys.includes("Geography")) answers.geo = "Geography";
  return { preset: "mff", label: "MFF long table (BayesInsight)", answers };
}

/** Plain-language checks of a local table before anything is uploaded. */
export function tableProblems(table: LocalTable): string[] {
  const out: string[] = [];
  if (!table.header.length || table.header.every((h) => !h)) out.push("The file has no header row. The first line should name the columns.");
  if (!table.rows.length) out.push("The file has a header but no rows of data.");
  const seen = new Set<string>();
  for (const h of table.header) {
    if (seen.has(h)) out.push(`Two columns are both called "${h}". Rename one of them in your spreadsheet.`);
    seen.add(h);
  }
  if (table.header.some((h) => !h.trim())) out.push("A column has no name. Give every column a name in the first row.");
  return [...new Set(out)];
}
