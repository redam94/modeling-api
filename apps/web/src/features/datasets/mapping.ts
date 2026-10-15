/**
 * Column → dim/var mapping state for tabular uploads (Meridian CoordToColumns style), and
 * its conversion to the `IngestMapping` request body.
 */
import type { IngestMapping, Role } from "@/api/types";
import { guessColumn } from "@/lib/csv";

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
  if (!dims.length) problems.push({ column: null, message: "Map at least one column as a dim." });
  if (!vars.length) problems.push({ column: null, message: "Map at least one column as a variable." });
  const seen = new Set<string>();
  for (const m of [...dims, ...vars]) {
    if (!NAME_RE.test(m.name)) problems.push({ column: m.column, message: `"${m.name}" is not a valid name.` });
    if (seen.has(m.name)) problems.push({ column: m.column, message: `The name "${m.name}" is used twice.` });
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
  return null;
}
