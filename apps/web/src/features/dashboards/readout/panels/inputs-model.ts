/**
 * The browser's checks of what the layout 2 inputs send (v0.17, plan §3.2-§3.4): a tried case
 * (mirrors `dashboard_inputs.check_value` / `check_rows`, with the same `/rows/<i>/<name>`
 * pointers), a batch file (the columns, the row cap, the ids and each value, as
 * `cases.submit_batch` checks them) and a row-matched actuals upload. They only stop a request
 * that cannot work; the server checks everything again.
 */
import { grouped } from "@/components/ods/format";
import { numberOf, type Cell, type XrDataset } from "@/lib/actuals";

import type { ActualsTemplate, BatchColumn, BatchSubmit, BatchTemplate, ScenarioForm, ScenarioInputDef, ScenarioRow } from "../../types";
import { plural, type Check } from "../readout-model";

type Num = number | null | undefined;
const isNum = (v: Num): v is number => typeof v === "number" && Number.isFinite(v);

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

/**
 * `rows` on a changed form (the analyst published new inputs while the page was open): values of
 * inputs that are gone are dropped, new inputs get their defaults, and the other values stay.
 * Returns `rows` itself when nothing changed.
 */
export function reconcileRows(rows: readonly ScenarioRow[], form: ScenarioForm): ScenarioRow[] {
  const base = defaultRow(form);
  const keys = Object.keys(base);
  let changed = false;
  const next = rows.map((r) => {
    const out: ScenarioRow = {};
    for (const k of keys) out[k] = (k in r ? r[k] : base[k]) ?? null;
    if (Object.keys(r).length !== keys.length || keys.some((k) => !(k in r))) changed = true;
    return out;
  });
  return changed ? next : (rows as ScenarioRow[]);
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

/**
 * A cell of a batch file as the value it sends (by the template column's type). `decimalComma`: the
 * file is separated by `;`, so `1,5` is 1.5 (see `numberOf`); a comma that does not group thousands
 * is otherwise not a number and is refused.
 */
export function batchCell(text: string, col: Pick<BatchColumn, "type" | "role">, decimalComma = false): Cell {
  const v = text.trim();
  if (v === "") return null;
  if (col.role === "id") return v;
  if (col.type === "number" || col.type === "integer") return numberOf(v, decimalComma) ?? v;
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
export function batchRows(t: Pick<BatchTemplate, "columns">, header: readonly string[], rows: readonly string[][], decimalComma = false): Cell[][] {
  const at = t.columns.map((c) => header.indexOf(c.name));
  return rows
    .filter((r) => r.some((c) => c.trim() !== ""))
    .map((r) => t.columns.map((c, j) => (at[j]! < 0 ? null : batchCell(r[at[j]!] ?? "", c, decimalComma))));
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

/** The columns of a row-matched upload: the template's example CSV header (id, dims, drivers, result). */
export function rowActualsColumns(t: Pick<ActualsTemplate, "example_csv" | "input_table">): string[] {
  const first = (t.example_csv.split(/\r?\n/)[0] ?? "").trim();
  const names = first ? first.split(",").map((c) => c.trim().replace(/^"|"$/g, "")).filter((c) => c !== "") : [];
  return names.length ? names : (t.input_table.columns ?? []).map((c) => c.name);
}

/** A cell of a row-matched upload: blank is null, the id stays text, number-like text is a number. */
export function rowActualsCell(text: string, isId: boolean, decimalComma = false): Cell {
  const v = text.trim();
  if (v === "") return null;
  if (isId) return v;
  const n = numberOf(v, decimalComma);
  if (n !== null) return n;
  const t = v.toLowerCase();
  if (t === "true" || t === "false") return t === "true";
  return v;
}

/** The parsed CSV rows (file order) in `columns` order, typed; columns the file lacks are null. */
export function rowActualsRows(columns: readonly string[], idColumn: string, header: readonly string[], rows: readonly string[][], decimalComma = false): Cell[][] {
  const at = columns.map((c) => header.indexOf(c));
  return rows
    .filter((r) => r.some((c) => c.trim() !== ""))
    .map((r) => columns.map((c, j) => (at[j]! < 0 ? null : rowActualsCell(r[at[j]!] ?? "", c === idColumn, decimalComma))));
}

/** The rows as the one-dim table the server matches by id: every other column along the id column. */
export function rowActualsToXr(columns: readonly string[], idColumn: string, rows: readonly Cell[][]): XrDataset {
  const idAt = columns.indexOf(idColumn);
  const filled = rows.filter((r) => r.some((c) => c !== null && c !== ""));
  const data_vars: XrDataset["data_vars"] = {};
  columns.forEach((c, j) => {
    if (j === idAt) return;
    data_vars[c] = { dims: [idColumn], data: filled.map((r) => r[j] ?? null) };
  });
  return { coords: { [idColumn]: { dims: [idColumn], data: filled.map((r) => (idAt >= 0 ? (r[idAt] ?? null) : null)) } }, data_vars };
}
