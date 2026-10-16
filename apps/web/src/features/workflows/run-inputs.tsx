/**
 * What a run takes: values for the workflow's parameters (a form generated from their
 * declarations) and a reference for each input (datasets picked from the project, `@latest`
 * or an exact version). Shared by the run dialog, the template wizard, schedules and the
 * Guided recipe flow.
 */
import { useId } from "react";

import { useDatasets, useDatasetVersions } from "@/api/queries";
import { Field, Input, NativeSelect } from "@/components/ui/primitives";
import { SchemaForm, type JsonObject, type SchemaNode } from "@/features/forms/json-schema-form";

import { PORT_TYPE_META } from "./ports";
import type { Json, WorkflowDoc, WorkflowInputDecl, WorkflowParamDecl } from "./types";

/** The parameters as a JSON Schema object (array item types inferred from the default). */
export function paramsSchema(params: Record<string, WorkflowParamDecl>): SchemaNode {
  const properties: Record<string, Json> = {};
  const required: string[] = [];
  for (const [name, p] of Object.entries(params)) {
    const prop: Record<string, Json> = { type: p.type };
    if (p.description) prop.description = p.description;
    if (p.default !== undefined && p.default !== null) prop.default = p.default;
    if (p.minimum != null) prop.minimum = p.minimum;
    if (p.maximum != null) prop.maximum = p.maximum;
    if (p.enum) prop.enum = p.enum;
    if (p.type === "array") {
      const first = Array.isArray(p.default) ? p.default[0] : undefined;
      prop.items = { type: typeof first === "number" ? (Array.isArray(p.default) && p.default.every((x) => Number.isInteger(x)) ? "integer" : "number") : "string" };
    }
    properties[name] = prop;
    if (p.required) required.push(name);
  }
  return { type: "object", properties, required };
}

/** Problems with the given values (required parameters missing). */
export function missingParams(params: Record<string, WorkflowParamDecl>, values: JsonObject): string[] {
  return Object.entries(params)
    .filter(([name, p]) => p.required && (values[name] === undefined || values[name] === null))
    .map(([name]) => name);
}

export function ParamsForm({ doc, value, onChange, errors }: { doc: Pick<WorkflowDoc, "params">; value: JsonObject; onChange: (v: JsonObject) => void; errors?: Record<string, string> }) {
  const params = doc.params ?? {};
  if (!Object.keys(params).length) return <p className="text-[13px] text-muted-foreground">This workflow has no parameters.</p>;
  return <SchemaForm schema={paramsSchema(params)} value={value} onChange={onChange} {...(errors ? { errors } : {})} />;
}

/** `dataset://project/name@latest` or `@vN` from a dataset ref and a version choice. */
export function datasetRef(base: string, version: number | "latest"): string {
  return `${base.replace(/@.*$/, "")}@${version === "latest" ? "latest" : `v${version}`}`;
}

export function parseDatasetRef(ref: string): { base: string; version: number | "latest" } | null {
  const m = /^(dataset:\/\/[^@\s]+)(?:@(latest|v(\d+)))?$/.exec(ref);
  if (!m) return null;
  return { base: m[1] ?? "", version: m[3] ? Number(m[3]) : "latest" };
}

export function DatasetRefPicker({ value, onChange, label = "Dataset", required = true, hint }: { value: string; onChange: (v: string) => void; label?: string; required?: boolean; hint?: string }) {
  const id = useId();
  const datasets = useDatasets();
  const parsed = parseDatasetRef(value);
  const chosen = datasets.data?.items.find((d) => d.ref === parsed?.base);
  const versions = useDatasetVersions(chosen?.id);
  return (
    <div className="grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <Field label={`${label}${required ? "" : " (optional)"}`} htmlFor={id} {...(hint ? { hint } : {})}>
        <NativeSelect id={id} value={parsed?.base ?? ""} onChange={(e) => onChange(e.target.value ? datasetRef(e.target.value, "latest") : "")}>
          <option value="">{datasets.isPending ? "Loading datasets…" : datasets.data?.items.length ? "Choose a dataset" : "No datasets yet: upload one first"}</option>
          {(datasets.data?.items ?? []).map((d) => (
            <option key={d.id} value={d.ref}>
              {d.name}
              {d.description ? ` – ${d.description}` : ""}
            </option>
          ))}
        </NativeSelect>
      </Field>
      <Field label="Version" htmlFor={`${id}-v`}>
        <NativeSelect id={`${id}-v`} value={String(parsed?.version ?? "latest")} disabled={!parsed} onChange={(e) => parsed && onChange(datasetRef(parsed.base, e.target.value === "latest" ? "latest" : Number(e.target.value)))}>
          <option value="latest">The latest when it runs</option>
          {(versions.data?.items ?? [])
            .filter((v) => v.status === "ready")
            .map((v) => (
              <option key={v.version} value={v.version}>
                v{v.version}
                {v.message ? ` – ${v.message}` : ""}
              </option>
            ))}
        </NativeSelect>
      </Field>
    </div>
  );
}

/** One field per declared input: datasets from the project, anything else as a reference. */
export function InputsForm({ inputs, value, onChange }: { inputs: Record<string, WorkflowInputDecl>; value: Record<string, string>; onChange: (v: Record<string, string>) => void }) {
  const idBase = useId();
  const set = (name: string, v: string) => {
    const next = { ...value };
    if (v) next[name] = v;
    else delete next[name];
    onChange(next);
  };
  if (!Object.keys(inputs).length) return <p className="text-[13px] text-muted-foreground">This workflow takes no inputs.</p>;
  return (
    <div className="grid gap-4">
      {Object.entries(inputs).map(([name, decl]) =>
        decl.type === "dataset" ? (
          <DatasetRefPicker
            key={name}
            label={Object.keys(inputs).length > 1 ? `Data for ${name}` : "Your data"}
            value={value[name] ?? ""}
            onChange={(v) => set(name, v)}
            required={(decl.required ?? true) && !decl.default}
            hint={decl.description ?? (decl.default ? `Default: ${decl.default}` : undefined) ?? undefined}
          />
        ) : (
          <Field key={name} label={`${name} (${PORT_TYPE_META[decl.type].plain})`} htmlFor={`${idBase}-${name}`} hint={decl.description ?? "A reference such as model://project/name@v3"}>
            <Input id={`${idBase}-${name}`} className="font-mono" value={value[name] ?? ""} placeholder={decl.default ?? ""} onChange={(e) => set(name, e.target.value)} />
          </Field>
        ),
      )}
    </div>
  );
}

/** Inputs that must be given and are not. */
export function missingInputs(inputs: Record<string, WorkflowInputDecl>, value: Record<string, string>): string[] {
  return Object.entries(inputs)
    .filter(([name, d]) => (d.required ?? true) && !d.default && !value[name])
    .map(([name]) => name);
}
