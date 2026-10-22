/**
 * The dataset a builder checks against: a picker over the project's datasets, and the columns
 * (variables and dims) of its latest ready version, so the builders can offer real names and
 * `:validate` can check the binding (`dataset://…` in the request).
 */
import { useId } from "react";

import { useDatasets, useDatasetVersions } from "@/api/queries";
import { Field, NativeSelect } from "@/components/ui/primitives";

/** The parts of a `DataSchema` the builders read. */
interface SchemaLike {
  vars?: Record<string, { dtype: string; dims?: string[]; role?: string | null }>;
  dims?: Record<string, unknown>;
}

export interface ColumnInfo {
  name: string;
  /** `str`, `float64`, … for a variable; `dim` for a dimension. */
  dtype: string;
  role: string | null;
  dims: string[];
  kind: "var" | "dim";
}

export interface DatasetColumns {
  /** `dataset://<project>/<name>@latest`, or `null` while nothing is picked. */
  ref: string | null;
  name: string | null;
  columns: ColumnInfo[];
  isPending: boolean;
  /** The dataset has no ready version with a schema yet. */
  empty: boolean;
}

const NUMERIC = new Set(["float64", "float32", "int64", "int32", "uint8", "bool"]);

export function isNumeric(c: ColumnInfo): boolean {
  return c.kind === "var" && NUMERIC.has(c.dtype);
}

/** Columns a group can be: dims (not the first, time-like one when there are others) and text variables. */
export function isGroupLike(c: ColumnInfo): boolean {
  return c.kind === "dim" || (c.kind === "var" && c.dtype === "str");
}

function base(ref: string): string {
  const at = ref.indexOf("@");
  return at < 0 ? ref : ref.slice(0, at);
}

export function columnsOfSchema(schema: SchemaLike | null | undefined): ColumnInfo[] {
  if (!schema) return [];
  const vars: ColumnInfo[] = Object.entries(schema.vars ?? {}).map(([name, v]) => ({ name, dtype: v.dtype, role: v.role ?? null, dims: v.dims ?? [], kind: "var" }));
  const dims: ColumnInfo[] = Object.keys(schema.dims ?? {}).map((name) => ({ name, dtype: "dim", role: null, dims: [], kind: "dim" }));
  return [...vars, ...dims];
}

/** The columns of the dataset `ref` names (its latest ready version). */
export function useDatasetColumns(ref: string | null): DatasetColumns {
  const datasets = useDatasets();
  const ds = ref ? datasets.data?.items.find((d) => d.ref === base(ref)) : undefined;
  const versions = useDatasetVersions(ds?.id);
  const ready = versions.data?.items.filter((v) => v.status === "ready" && v.data_schema).sort((a, b) => b.version - a.version)[0];
  const columns = columnsOfSchema(ready?.data_schema);
  return {
    ref,
    name: ds?.name ?? null,
    columns,
    isPending: Boolean(ref) && (datasets.isPending || (Boolean(ds) && versions.isPending)),
    empty: Boolean(ref) && !datasets.isPending && !versions.isPending && !columns.length,
  };
}

export function DatasetPicker({ value, onChange, label = "Which data will you use it on?", hint }: { value: string | null; onChange: (ref: string | null) => void; label?: string; hint?: string }) {
  const id = useId();
  const datasets = useDatasets();
  const items = datasets.data?.items ?? [];
  return (
    <Field label={label} htmlFor={id} hint={hint ?? (items.length ? "The builder offers this dataset's columns and checks your choices against it." : undefined)}>
      <NativeSelect id={id} data-pointer="/dataset" value={value ? base(value) : ""} disabled={datasets.isPending} onChange={(e) => onChange(e.target.value ? `${e.target.value}@latest` : null)}>
        <option value="">{datasets.isPending ? "Loading datasets…" : items.length ? "Pick a dataset" : "This project has no datasets yet"}</option>
        {items.map((d) => (
          <option key={d.id} value={d.ref}>
            {d.name}
          </option>
        ))}
      </NativeSelect>
    </Field>
  );
}
