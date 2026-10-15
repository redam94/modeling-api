/**
 * Shapes of the precomputed UI views the runner writes next to datasets and bundles
 * (`preview.json`, `profile.json`, `ui/tree_index.json`, `ui/preprocess.json`).
 *
 * These views are not part of mapi_spec yet (M1 gap): the formats below are the web app's
 * proposal and every parser validates defensively, so an unknown or malformed view degrades
 * to "not available" instead of breaking the page. Each view carries a `format` tag.
 */
import type { Scalar } from "./arrow";

export const VIEW_FORMATS = {
  preview: "mapi.preview/v1",
  profile: "mapi.profile/v1",
  treeIndex: "mapi.tree-index/v1",
  preprocess: "mapi.preprocess/v1",
} as const;

export class ViewFormatError extends Error {
  override name = "ViewFormatError";
}

type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const isStr = (v: unknown): v is string => typeof v === "string";
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isScalar = (v: unknown): v is Scalar => v === null || isStr(v) || isNum(v) || typeof v === "boolean";

function expectFormat(raw: unknown, format: string): Obj {
  if (!isObj(raw)) throw new ViewFormatError("view is not a JSON object");
  if (raw.format !== format) {
    throw new ViewFormatError(`unsupported view format ${JSON.stringify(raw.format)} (expected ${format})`);
  }
  return raw;
}

// ------------------------------------------------------------------- preview.json

export interface PreviewView {
  format: typeof VIEW_FORMATS.preview;
  columns: { name: string; dtype: string; role: "dim" | "var" }[];
  rows: Scalar[][];
  total_rows: number | null;
}

/** Long-format sample of the dataset: dim columns then variable columns. */
export function parsePreview(raw: unknown): PreviewView {
  const o = expectFormat(raw, VIEW_FORMATS.preview);
  if (!Array.isArray(o.columns) || !Array.isArray(o.rows)) throw new ViewFormatError("preview needs columns and rows");
  const columns = o.columns.map((c) => {
    if (!isObj(c) || !isStr(c.name)) throw new ViewFormatError("bad preview column");
    return { name: c.name, dtype: isStr(c.dtype) ? c.dtype : "unknown", role: c.role === "dim" ? "dim" : "var" } as const;
  });
  const rows = o.rows.slice(0, 10_000).map((r) => {
    if (!Array.isArray(r) || r.length !== columns.length || !r.every(isScalar)) {
      throw new ViewFormatError("preview rows must be arrays of scalars matching the columns");
    }
    return r;
  });
  return { format: VIEW_FORMATS.preview, columns, rows, total_rows: isNum(o.total_rows) ? o.total_rows : null };
}

// ------------------------------------------------------------------- profile.json

export interface VarProfile {
  dtype: string;
  count: number;
  missing: number;
  min: number | null;
  max: number | null;
  mean: number | null;
  std: number | null;
}

export interface MissingnessGrid {
  /** The time-like dim along x. */
  along: string;
  /** Labels on y: variable names, or `var · level` when broken down by a group dim. */
  labels: string[];
  /** Coordinate labels along x (ISO dates or numbers as text). */
  coords: string[];
  /** fraction[labelIndex][coordIndex] in [0, 1]. */
  fraction: number[][];
}

export interface ProfileView {
  format: typeof VIEW_FORMATS.profile;
  vars: Record<string, VarProfile>;
  missingness: MissingnessGrid | null;
}

const num = (v: unknown): number | null => (isNum(v) ? v : null);

export function parseProfile(raw: unknown): ProfileView {
  const o = expectFormat(raw, VIEW_FORMATS.profile);
  if (!isObj(o.vars)) throw new ViewFormatError("profile needs vars");
  const vars: Record<string, VarProfile> = {};
  for (const [name, v] of Object.entries(o.vars)) {
    if (!isObj(v)) continue;
    vars[name] = {
      dtype: isStr(v.dtype) ? v.dtype : "unknown",
      count: num(v.count) ?? 0,
      missing: num(v.missing) ?? 0,
      min: num(v.min),
      max: num(v.max),
      mean: num(v.mean),
      std: num(v.std),
    };
  }
  let missingness: MissingnessGrid | null = null;
  const m = o.missingness;
  if (isObj(m) && isStr(m.along) && Array.isArray(m.labels) && Array.isArray(m.coords) && Array.isArray(m.fraction)) {
    const labels = m.labels.filter(isStr);
    const coords = m.coords.map((c) => (isScalar(c) ? String(c) : ""));
    const fraction = m.fraction.map((row) =>
      Array.isArray(row) ? row.map((x) => (isNum(x) ? Math.min(1, Math.max(0, x)) : 0)) : [],
    );
    if (labels.length === fraction.length && fraction.every((r) => r.length === coords.length)) {
      missingness = { along: m.along, labels, coords, fraction };
    }
  }
  return { format: VIEW_FORMATS.profile, vars, missingness };
}

// ------------------------------------------------------------------- ui/tree_index.json

export interface TreeVar {
  dims: string[];
  shape: number[];
  dtype: string;
  units: string | null;
  description: string | null;
}

export interface TreeGroup {
  path: string;
  dims: Record<string, number>;
  coords: Record<string, { dims: string[]; dtype: string; size: number; values: Scalar[] | null }>;
  vars: Record<string, TreeVar>;
  attrs: Record<string, Scalar>;
}

export interface TreeIndexView {
  format: typeof VIEW_FORMATS.treeIndex;
  groups: TreeGroup[];
}

/** Index of the bundle DataTree (ArviZ groups) for the browser; arrays are never shipped. */
export function parseTreeIndex(raw: unknown): TreeIndexView {
  const o = expectFormat(raw, VIEW_FORMATS.treeIndex);
  if (!isObj(o.groups)) throw new ViewFormatError("tree index needs groups");
  const groups: TreeGroup[] = [];
  for (const [path, g] of Object.entries(o.groups)) {
    if (!isObj(g)) continue;
    const dims: Record<string, number> = {};
    if (isObj(g.dims)) for (const [d, n] of Object.entries(g.dims)) if (isNum(n)) dims[d] = n;
    const vars: Record<string, TreeVar> = {};
    if (isObj(g.vars)) {
      for (const [name, v] of Object.entries(g.vars)) {
        if (!isObj(v)) continue;
        vars[name] = {
          dims: Array.isArray(v.dims) ? v.dims.filter(isStr) : [],
          shape: Array.isArray(v.shape) ? v.shape.filter(isNum) : [],
          dtype: isStr(v.dtype) ? v.dtype : "unknown",
          units: isStr(v.units) ? v.units : null,
          description: isStr(v.description) ? v.description : null,
        };
      }
    }
    const coords: TreeGroup["coords"] = {};
    if (isObj(g.coords)) {
      for (const [name, c] of Object.entries(g.coords)) {
        if (!isObj(c)) continue;
        coords[name] = {
          dims: Array.isArray(c.dims) ? c.dims.filter(isStr) : [],
          dtype: isStr(c.dtype) ? c.dtype : "unknown",
          size: num(c.size) ?? 0,
          values: Array.isArray(c.values) ? c.values.filter(isScalar).slice(0, 1000) : null,
        };
      }
    }
    const attrs: Record<string, Scalar> = {};
    if (isObj(g.attrs)) for (const [k, v] of Object.entries(g.attrs)) if (isScalar(v)) attrs[k] = v;
    groups.push({ path: path.startsWith("/") ? path : `/${path}`, dims, coords, vars, attrs });
  }
  groups.sort((a, b) => a.path.localeCompare(b.path));
  return { format: VIEW_FORMATS.treeIndex, groups };
}

// ------------------------------------------------------------------- ui/preprocess.json

export interface StateArray {
  name: string;
  dims: string[];
  coords: Record<string, Scalar[]>;
  /** Row-major values; 1-D arrays have one row. */
  values: (number | null)[][];
}

export interface StepStateView {
  id: string;
  use: string;
  kind: string;
  stateful: boolean;
  learned: Record<string, unknown>;
  arrays: StateArray[];
}

export interface PreprocessView {
  format: typeof VIEW_FORMATS.preprocess;
  steps: StepStateView[];
}

/** Learned preprocessing state (e.g. `scale(geo, channel)`), small arrays only. */
export function parsePreprocess(raw: unknown): PreprocessView {
  const o = expectFormat(raw, VIEW_FORMATS.preprocess);
  if (!Array.isArray(o.steps)) throw new ViewFormatError("preprocess view needs steps");
  const steps = o.steps.filter(isObj).map((s): StepStateView => {
    const arrays: StateArray[] = Array.isArray(s.arrays)
      ? s.arrays.filter(isObj).flatMap((a) => {
          if (!isStr(a.name) || !Array.isArray(a.dims) || !Array.isArray(a.values)) return [];
          const dims = a.dims.filter(isStr).slice(0, 2);
          const rows = (dims.length <= 1 ? [a.values] : a.values) as unknown[];
          const values = rows.map((r) => (Array.isArray(r) ? r.map((x) => (isNum(x) ? x : null)) : []));
          const coords: Record<string, Scalar[]> = {};
          if (isObj(a.coords)) {
            for (const d of dims) {
              const c = a.coords[d];
              if (Array.isArray(c)) coords[d] = c.filter(isScalar);
            }
          }
          return [{ name: a.name, dims, coords, values }];
        })
      : [];
    return {
      id: isStr(s.id) ? s.id : "?",
      use: isStr(s.use) ? s.use : "",
      kind: isStr(s.kind) ? s.kind : "",
      stateful: s.stateful === true,
      learned: isObj(s.learned) ? s.learned : {},
      arrays,
    };
  });
  return { format: VIEW_FORMATS.preprocess, steps };
}
