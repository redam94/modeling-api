/**
 * Column → dim/var mapping state for tabular uploads (Meridian CoordToColumns style), and
 * its conversion to the `IngestMapping` request body.
 *
 * M4: several files in one version (`IngestAssembly`: where each file goes in the tree and
 * how files of one node combine) and raw HDF5 files (`Hdf5Mapping`: which arrays become
 * which variables, with which dims and coords). The checks mirror `mapi_spec.ingest`, so
 * every problem shows before the upload instead of as a refused request.
 */
import type { AssemblySource, Hdf5Coord, Hdf5Var, IngestAssembly, IngestMapping, InspectedArray, Role } from "@/api/types";
import { guessColumn } from "@/lib/csv";

import { isAncestor, nodePathProblem, ROOT } from "./tree";

export type DimKind = "categorical" | "time" | "integer" | "float" | "index";
export type DimPolicy = "fixed" | "extend" | "new_levels";

export interface ColumnMapping {
  column: string;
  as: "dim" | "var" | "ignore";
  name: string;
  dimKind: DimKind;
  policy: DimPolicy;
  freq: string;
  role: Role;
  units: string;
  missing: "forbid" | "allow" | "mask";
}

export const ROLES: Role[] = ["target", "covariate", "media", "spend", "control", "index", "mask", "constant", "weight", "exposure"];

const NAME_RE = /^[A-Za-z_][A-Za-z0-9_.-]{0,127}$/;

export function safeName(column: string): string {
  const s = column.trim().replace(/[^A-Za-z0-9_.-]+/g, "_").replace(/^([^A-Za-z_])/, "_$1");
  return s.slice(0, 128) || "col";
}

export function initialMappings(header: string[], rows: string[][]): ColumnMapping[] {
  return header.map((column, i) => {
    const samples = rows.map((r) => r[i] ?? "").slice(0, 50);
    const g = guessColumn(column, samples);
    return {
      column,
      as: g.as,
      name: safeName(column),
      dimKind: g.kind ?? "categorical",
      policy: g.kind === "time" ? "extend" : "fixed",
      freq: g.kind === "time" ? "D" : "",
      role: /revenue|sales|target|^y$/i.test(column) ? "target" : /spend|cost/i.test(column) ? "spend" : "covariate",
      units: "",
      missing: "forbid",
    };
  });
}

export interface MappingProblem {
  column: string | null;
  message: string;
}

export function validateMappings(mappings: ColumnMapping[]): MappingProblem[] {
  const problems: MappingProblem[] = [];
  const dims = mappings.filter((m) => m.as === "dim");
  const vars = mappings.filter((m) => m.as === "var");
  if (!dims.length) problems.push({ column: null, message: "Map one or more columns as a dim." });
  if (!vars.length) problems.push({ column: null, message: "Map one or more columns as a variable." });
  const seen = new Set<string>();
  for (const m of [...dims, ...vars]) {
    if (!NAME_RE.test(m.name)) problems.push({ column: m.column, message: `"${m.name}" is not a valid name. Start with a letter or _. Then use letters, digits, _, . and - (a maximum of 128 characters).` });
    if (seen.has(m.name)) problems.push({ column: m.column, message: `Two columns use the name "${m.name}". Give each column a different name.` });
    seen.add(m.name);
  }
  return problems;
}

export function toIngestMapping(format: IngestMapping["format"], mappings: ColumnMapping[]): IngestMapping {
  const out: IngestMapping = { format, layout: "dense", dims: {}, vars: {} };
  for (const m of mappings) {
    if (m.as === "dim") {
      out.dims![m.name] = {
        column: m.column,
        spec: {
          kind: m.dimKind,
          policy: m.policy,
          ...(m.freq.trim() ? { freq: m.freq.trim() } : {}),
        },
      };
    } else if (m.as === "var") {
      out.vars![m.name] = {
        column: m.column,
        role: m.role,
        missing: m.missing,
        ...(m.units.trim() ? { units: m.units.trim() } : {}),
      };
    }
  }
  return out;
}

export function formatFromFilename(name: string): IngestMapping["format"] | null {
  const n = name.toLowerCase();
  if (n.endsWith(".csv") || n.endsWith(".tsv") || n.endsWith(".txt")) return "csv";
  if (n.endsWith(".parquet") || n.endsWith(".pq")) return "parquet";
  if (n.endsWith(".zarr.zip")) return "zarr_zip";
  if (n.endsWith(".nc") || n.endsWith(".nc4") || n.endsWith(".netcdf")) return "netcdf";
  // M4: raw HDF5 (an xarray/h5netcdf-written file can use "netcdf" instead).
  if (n.endsWith(".h5") || n.endsWith(".hdf5") || n.endsWith(".he5")) return "hdf5";
  return null;
}

export function isValidName(name: string): boolean {
  return NAME_RE.test(name);
}

const nameProblem = (what: string, name: string) =>
  name ? `${what} "${name}" is not a valid name. Start with a letter or _. Then use letters, digits, _, . and - (a maximum of 128 characters).` : `${what} is empty. Type a name.`;

/**
 * A label or a range value: a number when the text is a number, else the text. Text with a
 * leading zero (`007`, `-01`) stays text: a number would drop the zeros the user typed.
 */
export function parseCoordValue(text: string): number | string {
  const t = text.trim();
  if (/^[-+]?0\d/.test(t)) return t;
  return /^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$/.test(t) && Number.isFinite(Number(t)) ? Number(t) : t;
}

// ============================================================================ assembly

export type CombineRule = NonNullable<AssemblySource["combine"]>;
export type Join = NonNullable<AssemblySource["join"]>;

export const MAX_ASSEMBLY_SOURCES = 64;

/** Where one file of a multi-file version goes (`AssemblySource` without the upload). */
export interface Placement {
  /** The file name, for messages. */
  file: string;
  node: string;
  combine: CombineRule;
  concatDim: string;
  concatLabel: string;
  join: Join;
}

export const DEFAULT_PLACEMENT: Omit<Placement, "file"> = { node: ROOT, combine: "node", concatDim: "", concatLabel: "", join: "exact" };

export interface PlacementProblem {
  /** The index of the file, or `null` for a problem of several files. */
  index: number | null;
  field: "node" | "combine" | "concat_dim" | "concat_label" | "join" | null;
  message: string;
}

/** An assembly is needed for more than one file, or when a file is not plainly the root. */
export function needsAssembly(placements: Placement[]): boolean {
  return placements.length > 1 || placements.some((p) => p.node !== ROOT || p.combine !== "node");
}

/** Every problem of `IngestAssembly` / `AssemblySource` validation, in plain words. */
export function validatePlacements(placements: Placement[]): PlacementProblem[] {
  const problems: PlacementProblem[] = [];
  if (placements.length > MAX_ASSEMBLY_SOURCES) {
    problems.push({ index: null, field: null, message: `A version can have a maximum of ${MAX_ASSEMBLY_SOURCES} files. Remove ${placements.length - MAX_ASSEMBLY_SOURCES} files.` });
  }
  placements.forEach((p, i) => {
    const bad = nodePathProblem(p.node);
    if (bad) problems.push({ index: i, field: "node", message: `${p.file}: ${bad}` });
    if (p.combine === "concat") {
      const dim = p.concatDim.trim();
      if (!dim) problems.push({ index: i, field: "concat_dim", message: `${p.file}: type the dim to stack the files along (for example year).` });
      else if (!NAME_RE.test(dim)) problems.push({ index: i, field: "concat_dim", message: `${p.file}: ${nameProblem("The dim", dim)}` });
      if (!p.concatLabel.trim()) problems.push({ index: i, field: "concat_label", message: `${p.file}: type the label of this file along ${dim || "the dim"} (for example 2024).` });
    }
  });
  const byNode = new Map<string, number[]>();
  placements.forEach((p, i) => byNode.set(p.node, [...(byNode.get(p.node) ?? []), i]));
  for (const [node, idx] of byNode) {
    const group = idx.map((i) => placements[i]!);
    const names = group.map((p) => p.file).join(", ");
    const rules = new Set(group.map((p) => p.combine));
    if (rules.size > 1) {
      problems.push({ index: null, field: "combine", message: `Node ${node}: the files ${names} combine in different ways. Select the same rule for every file of a node.` });
      continue;
    }
    const rule = group[0]!.combine;
    if (rule === "node" && group.length > 1) {
      problems.push({ index: null, field: "combine", message: `Node ${node} has ${group.length} files (${names}). Stack them (concatenate) or merge them, or put each file in a different node.` });
    }
    if (rule === "concat") {
      if (new Set(group.map((p) => p.concatDim.trim())).size > 1) {
        problems.push({ index: null, field: "concat_dim", message: `Node ${node}: the files ${names} must stack along the same dim.` });
      }
      const labelled = group.filter((p) => p.concatLabel.trim()).map((p) => ({ text: p.concatLabel.trim(), value: parseCoordValue(p.concatLabel) }));
      if (new Set(labelled.map((l) => typeof l.value)).size > 1) {
        const texts = labelled.filter((l) => typeof l.value === "string").map((l) => l.text);
        problems.push({
          index: null,
          field: "concat_label",
          message: `Node ${node}: the labels mix numbers and text (${texts.join(", ")} ${texts.length === 1 ? "is" : "are"} text). Use only numbers or only text. A label with a leading zero, such as 007, is text.`,
        });
      } else {
        const seen = new Map<string, string>();
        for (const l of labelled) {
          const key = String(l.value);
          const first = seen.get(key);
          if (first !== undefined) {
            const same = first === l.text ? "" : ` (${first} and ${l.text} are the same number)`;
            problems.push({ index: null, field: "concat_label", message: `Node ${node}: two files have the label ${key}${same}. Give each file a different label.` });
          } else seen.set(key, l.text);
        }
      }
    }
    if ((rule === "concat" || rule === "merge") && new Set(group.map((p) => p.join)).size > 1) {
      problems.push({ index: null, field: "join", message: `Node ${node}: the files ${names} must align their coordinates in the same way.` });
    }
  }
  return problems;
}

/** The `AssemblySource` of one file. */
export function toAssemblySource(uploadId: string, mapping: IngestMapping, p: Placement): AssemblySource {
  return {
    upload_id: uploadId,
    mapping,
    node: p.node,
    combine: p.combine,
    ...(p.combine === "concat" ? { concat_dim: p.concatDim.trim(), concat_label: parseCoordValue(p.concatLabel) } : {}),
    ...(p.combine !== "node" ? { join: p.join } : {}),
  };
}

export function toAssembly(sources: { uploadId: string; mapping: IngestMapping; placement: Placement }[]): IngestAssembly {
  return { sources: sources.map((s) => toAssemblySource(s.uploadId, s.mapping, s.placement)) };
}

const RULE_TEXT: Record<CombineRule, string> = { node: "the only file of the node", concat: "stacked", merge: "merged" };

/** The rule of one node in the "How the files combine" summary (the files may disagree). */
export function combineSummaryText(group: Omit<Placement, "file">[]): string {
  const rules = new Set(group.map((p) => p.combine));
  if (rules.size > 1) return "← the files combine in different ways:";
  const first = group[0];
  if (!first || first.combine === "node") return group.length > 1 ? "← select how these files combine:" : "←";
  return first.combine === "concat" ? `← stacked along ${first.concatDim.trim() || "?"}:` : `← merged (${first.join}):`;
}

/** One file in that summary: its label for a stack, and its own rule when the node's files disagree. */
export function combineFileText(file: string, p: Omit<Placement, "file">, mixed: boolean): string {
  const label = p.combine === "concat" ? ` = ${p.concatLabel.trim() || "?"}` : "";
  if (!mixed) return `${file}${label}`;
  const rule = p.combine === "concat" ? `stacked along ${p.concatDim.trim() || "?"}` : RULE_TEXT[p.combine];
  return `${file}${label} (${rule})`;
}

// ============================================================================ raw HDF5

export const MAX_HDF5_VARS = 1024;
export const MAX_HDF5_COORDS = 64;
/** `Hdf5Var.units` (`max_length=64`). */
export const MAX_UNITS_CHARS = 64;

export type Missing = ColumnMapping["missing"];

/** One array of an inspected raw HDF5 file and how it is mapped. */
export interface Hdf5VarDraft {
  path: string;
  shape: number[];
  include: boolean;
  name: string;
  node: string;
  /** Comma-separated dim names (one per axis). */
  dims: string;
  role: Role;
  units: string;
  /** `Hdf5Var.missing`: what a NaN in the array means (`forbid`, the server's default, is not sent). */
  missing: Missing;
}

export type CoordSource = "index" | "path" | "range";

/** The coordinate of one dim of one node. */
export interface Hdf5CoordDraft {
  dim: string;
  node: string;
  source: CoordSource;
  path: string;
  start: string;
  step: string;
}

export function splitDims(text: string): string[] {
  return text
    .split(",")
    .map((d) => d.trim())
    .filter(Boolean);
}

/** One draft per array (none mapped yet), dims from the file's own dim names when it has them. */
export function initialHdf5Vars(arrays: InspectedArray[]): Hdf5VarDraft[] {
  return arrays.map((a) => {
    const name = safeName(a.path.slice(a.path.lastIndexOf("/") + 1) || "array");
    const shape = a.shape ?? [];
    const hint = a.dims_hint && a.dims_hint.length === shape.length ? a.dims_hint : null;
    return {
      path: a.path,
      shape,
      include: false,
      name,
      node: ROOT,
      dims: (hint ?? shape.map((_, i) => `${name}_${i}`)).join(", "),
      role: "covariate",
      units: typeof a.attrs?.units === "string" ? a.attrs.units.slice(0, MAX_UNITS_CHARS) : "",
      missing: "forbid",
    };
  });
}

/**
 * The coords the mapped variables need: one per dim, at the highest node that uses it (a
 * node sees the coords of its ancestors). Existing drafts keep what the user chose.
 */
export function coordDrafts(vars: Hdf5VarDraft[], current: Hdf5CoordDraft[]): Hdf5CoordDraft[] {
  const wanted: { dim: string; node: string }[] = [];
  for (const v of vars.filter((x) => x.include)) {
    for (const dim of splitDims(v.dims)) {
      if (wanted.some((w) => w.dim === dim && (w.node === v.node || isAncestor(w.node, v.node)))) continue;
      // A use higher up replaces the ones below it.
      for (let i = wanted.length - 1; i >= 0; i--) if (wanted[i]!.dim === dim && isAncestor(v.node, wanted[i]!.node)) wanted.splice(i, 1);
      wanted.push({ dim, node: v.node });
    }
  }
  return wanted.map((w) => current.find((c) => c.dim === w.dim && c.node === w.node) ?? { ...w, source: "index", path: "", start: "", step: "" });
}

/** The input of a variable row (`name` … `missing`) or of a coordinate row (`source` … `step`). */
export type Hdf5Field = "name" | "node" | "dims" | "units" | "source" | "path" | "start" | "step";

export interface Hdf5Problem {
  /** The array path or `coord:<node>:<dim>` the problem is about (`null`: the whole mapping). */
  key: string | null;
  /** The input of that row the problem is about (`null`: the row, or the whole mapping). */
  field: Hdf5Field | null;
  message: string;
}

/** Every problem of `Hdf5Mapping` validation (and the checks the sandbox makes), in plain words. */
export function validateHdf5(vars: Hdf5VarDraft[], coords: Hdf5CoordDraft[], arrays: InspectedArray[]): Hdf5Problem[] {
  const problems: Hdf5Problem[] = [];
  const mapped = vars.filter((v) => v.include);
  if (!mapped.length) problems.push({ key: null, field: null, message: "Select one or more arrays to make variables." });
  if (mapped.length > MAX_HDF5_VARS) problems.push({ key: null, field: null, message: `Map a maximum of ${MAX_HDF5_VARS} arrays.` });
  const seen = new Set<string>();
  const nodeDims = new Map<string, Set<string>>();
  const nodeVars = new Map<string, Set<string>>();
  // The length of each (node, dim), from the first array that uses it.
  const lengths: { node: string; dim: string; length: number; from: string }[] = [];
  for (const v of mapped) {
    const dims = splitDims(v.dims);
    if (!isValidName(v.name)) problems.push({ key: v.path, field: "name", message: `${v.path}: ${nameProblem("The variable name", v.name)}` });
    const bad = nodePathProblem(v.node);
    if (bad) problems.push({ key: v.path, field: "node", message: `${v.path}: ${bad}` });
    const units = v.units.trim();
    if (units.length > MAX_UNITS_CHARS) problems.push({ key: v.path, field: "units", message: `${v.path}: the units have ${units.length} characters. The maximum is ${MAX_UNITS_CHARS}.` });
    if (dims.length !== v.shape.length) {
      const axes = v.shape.length === 1 ? "axis" : "axes";
      problems.push({ key: v.path, field: "dims", message: `${v.path} has ${v.shape.length} ${axes} (shape ${v.shape.join(" × ") || "scalar"}). Type ${v.shape.length} dim ${v.shape.length === 1 ? "name" : "names"}, not ${dims.length}.` });
    }
    for (const d of dims) if (!isValidName(d)) problems.push({ key: v.path, field: "dims", message: `${v.path}: ${nameProblem("The dim", d)}` });
    if (new Set(dims).size !== dims.length) problems.push({ key: v.path, field: "dims", message: `${v.path}: each axis needs a different dim name.` });
    if (dims.includes(v.name)) problems.push({ key: v.path, field: "name", message: `${v.path}: the variable "${v.name}" has the name of one of its dims. Change one of the names.` });
    const key = `${v.node}:${v.name}`;
    if (seen.has(key)) problems.push({ key: v.path, field: "name", message: `Two arrays make the variable "${v.name}" in node ${v.node}. Give each one a different name or node.` });
    seen.add(key);
    nodeDims.set(v.node, new Set([...(nodeDims.get(v.node) ?? []), ...dims]));
    nodeVars.set(v.node, new Set([...(nodeVars.get(v.node) ?? []), v.name]));
    dims.forEach((d, i) => {
      const len = v.shape[i];
      if (len === undefined) return;
      const other = lengths.find((e) => e.dim === d && (e.node === v.node || isAncestor(e.node, v.node) || isAncestor(v.node, e.node)));
      if (other && other.length !== len) {
        problems.push({ key: v.path, field: "dims", message: `The dim "${d}" has ${len} values in ${v.path} but ${other.length} in ${other.from}. Use different dim names, or map arrays of the same size.` });
      } else if (!other) lengths.push({ node: v.node, dim: d, length: len, from: v.path });
    });
  }
  for (const [node, names] of nodeVars) {
    const clash = [...names].filter((n) => nodeDims.get(node)?.has(n)).sort();
    if (clash.length) problems.push({ key: null, field: null, message: `Node ${node}: ${clash.join(", ")} ${clash.length === 1 ? "is" : "are"} used as a dim and as a variable. Change one of the names.` });
    for (const [anc, dims] of nodeDims) {
      if (!isAncestor(anc, node)) continue;
      const hidden = [...names].filter((n) => dims.has(n)).sort();
      if (hidden.length) problems.push({ key: null, field: null, message: `Node ${node}: the variable ${hidden.join(", ")} has the name of a dim of the node ${anc} above it. Change the name.` });
    }
  }
  const arraysByPath = new Map(arrays.map((a) => [a.path, a]));
  if (coords.length > MAX_HDF5_COORDS) problems.push({ key: null, field: null, message: `Give a maximum of ${MAX_HDF5_COORDS} coordinates.` });
  for (const c of coords) {
    const key = `coord:${c.node}:${c.dim}`;
    const label = `Coordinate ${c.dim}${c.node === ROOT ? "" : ` (node ${c.node})`}`;
    const len = lengths.find((e) => e.dim === c.dim && (e.node === c.node || isAncestor(c.node, e.node)))?.length;
    if (c.source === "path") {
      const a = arraysByPath.get(c.path);
      const shape = a?.shape ?? [];
      if (!c.path) problems.push({ key, field: "path", message: `${label}: select the array that holds its labels.` });
      else if (!a) problems.push({ key, field: "path", message: `${label}: the file has no array ${c.path}.` });
      else if (shape.length !== 1) problems.push({ key, field: "path", message: `${label}: ${c.path} must be a 1-D array (its shape is ${shape.join(" × ") || "scalar"}).` });
      else if (len !== undefined && shape[0] !== len) problems.push({ key, field: "path", message: `${label}: ${c.path} has ${shape[0]} values but the dim has ${len}.` });
    } else if (c.source === "range") {
      if (!c.start.trim()) problems.push({ key, field: "start", message: `${label}: type the first value of the range (a number, or a date such as 2024-01-01).` });
      else if (c.step.trim()) {
        const start = parseCoordValue(c.start);
        const step = parseCoordValue(c.step);
        if (typeof start !== typeof step) problems.push({ key, field: "step", message: `${label}: a range is two numbers (start and step) or a date and a frequency (2024-01-01 and W-MON), not a mix.` });
        else if (step === 0) problems.push({ key, field: "step", message: `${label}: the step must not be 0.` });
      }
    }
  }
  return problems;
}

export function toHdf5Mapping(vars: Hdf5VarDraft[], coords: Hdf5CoordDraft[]): IngestMapping {
  const outVars: Hdf5Var[] = vars
    .filter((v) => v.include)
    .map((v) => ({
      path: v.path,
      node: v.node,
      name: v.name,
      dims: splitDims(v.dims),
      role: v.role,
      ...(v.units.trim() ? { units: v.units.trim() } : {}),
      ...(v.missing !== "forbid" ? { missing: v.missing } : {}),
    }));
  const outCoords: Hdf5Coord[] = coords
    .filter((c) => c.source !== "index")
    .map((c) =>
      c.source === "path"
        ? { dim: c.dim, node: c.node, path: c.path }
        : { dim: c.dim, node: c.node, start: parseCoordValue(c.start), ...(c.step.trim() ? { step: parseCoordValue(c.step) } : {}) },
    );
  return { format: "hdf5", hdf5: { vars: outVars, coords: outCoords } };
}
