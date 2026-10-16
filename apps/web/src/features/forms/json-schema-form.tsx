/**
 * A form generated from a JSON Schema object (node parameters, governance check parameters).
 * Simple fields get a native control (select for enums, checkbox, number, text, comma-separated
 * lists); anything richer (nested objects, `oneOf`, `$ref`) gets a JSON text field. Empty fields
 * are left out so the default applies. The server validates the result; the form only guides.
 *
 * With `bindings`, each field can also take a workflow parameter (`{$param: name}`) or an
 * expression computed from upstream results (`{$expr: "..."}`), which the server parses.
 */
import { useEffect, useId, useRef, useState } from "react";

import { Checkbox, SegmentedControl } from "@/components/ui/overlays";
import { Field, Input, NativeSelect, Textarea } from "@/components/ui/primitives";

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export type SchemaNode = { [key: string]: Json };

export interface BindingOptions {
  /** Workflow parameters a field may take (`{$param: name}`). */
  params: string[];
  /** Allow `{$expr: "..."}` values. */
  expressions: boolean;
}

type Kind = "enum" | "boolean" | "integer" | "number" | "string" | "list" | "json";

interface FieldModel {
  key: string;
  label: string;
  description: string | null;
  kind: Kind;
  itemType: "string" | "number" | "integer" | null;
  options: Json[];
  required: boolean;
  default: Json | undefined;
  minimum: number | null;
  maximum: number | null;
  schema: SchemaNode;
}

function obj(v: Json | undefined): SchemaNode | null {
  return v && typeof v === "object" && !Array.isArray(v) ? v : null;
}

/** Drop `null` from `anyOf: [T, null]`; keep richer unions as they are. */
function effective(schema: SchemaNode): SchemaNode {
  const any = Array.isArray(schema.anyOf) ? (schema.anyOf.map(obj).filter(Boolean) as SchemaNode[]) : null;
  if (any) {
    const nonNull = any.filter((s) => s.type !== "null");
    if (nonNull.length === 1) return { ...nonNull[0]!, ...(schema.description !== undefined ? { description: schema.description } : {}), ...(schema.default !== undefined ? { default: schema.default } : {}) };
  }
  return schema;
}

function humanize(key: string): string {
  const s = key.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function fieldsOf(schema: SchemaNode): FieldModel[] {
  const props = obj(schema.properties) ?? {};
  const required = new Set(Array.isArray(schema.required) ? schema.required.filter((x): x is string => typeof x === "string") : []);
  return Object.entries(props).map(([key, raw]) => {
    const s = effective(obj(raw) ?? {});
    const type = typeof s.type === "string" ? s.type : null;
    let kind: Kind = "json";
    let itemType: FieldModel["itemType"] = null;
    if (Array.isArray(s.enum) && s.enum.every((e) => typeof e !== "object" || e === null)) kind = "enum";
    else if (type === "boolean") kind = "boolean";
    else if (type === "integer") kind = "integer";
    else if (type === "number") kind = "number";
    else if (type === "string") kind = "string";
    else if (type === "array") {
      const it = obj(s.items);
      const t = it && typeof it.type === "string" ? it.type : null;
      if (t === "string" || t === "number" || t === "integer") {
        kind = "list";
        itemType = t;
      } else if (!it) {
        kind = "list";
        itemType = "string";
      }
    }
    const num = (v: Json | undefined) => (typeof v === "number" ? v : null);
    return {
      key,
      label: typeof s.title === "string" && s.title.toLowerCase() !== key.replace(/_/g, " ") ? s.title : humanize(key),
      description: typeof s.description === "string" ? s.description : null,
      kind,
      itemType,
      options: Array.isArray(s.enum) ? s.enum : [],
      required: required.has(key),
      default: s.default,
      minimum: num(s.minimum) ?? num(s.exclusiveMinimum),
      maximum: num(s.maximum) ?? num(s.exclusiveMaximum),
      schema: s,
    };
  });
}

export function isParamRef(v: Json | undefined): v is { $param: string } {
  return Boolean(v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 1 && typeof v.$param === "string");
}

export function isExpr(v: Json | undefined): v is { $expr: string } {
  return Boolean(v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 1 && typeof v.$expr === "string");
}

function display(v: Json | undefined): string {
  if (v === undefined || v === null) return "";
  return typeof v === "string" ? v : JSON.stringify(v);
}

function parseList(text: string, itemType: FieldModel["itemType"]): { ok: true; value: Json[] } | { ok: false; error: string } {
  const parts = text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (itemType === "string" || itemType === null) return { ok: true, value: parts };
  const nums = parts.map(Number);
  if (nums.some((n) => !Number.isFinite(n))) return { ok: false, error: "Use numbers separated by commas." };
  if (itemType === "integer" && nums.some((n) => !Number.isInteger(n))) return { ok: false, error: "Use whole numbers separated by commas." };
  return { ok: true, value: nums };
}

type Source = "value" | "param" | "expr";

function SourcePicker({ value, onChange, bindings, label }: { value: Source; onChange: (s: Source) => void; bindings: BindingOptions; label: string }) {
  const options: { value: Source; label: string; title: string }[] = [{ value: "value", label: "Value", title: "A fixed value" }];
  if (bindings.params.length) options.push({ value: "param", label: "Parameter", title: "A workflow parameter chosen per run" });
  if (bindings.expressions) options.push({ value: "expr", label: "Expression", title: "Computed from upstream results" });
  if (options.length < 2) return null;
  return <SegmentedControl label={`Where ${label} comes from`} value={value} onValueChange={onChange} options={options} className="h-7" />;
}

function SchemaField({
  field,
  value,
  onChange,
  error,
  bindings,
  idPrefix,
  disabled,
}: {
  field: FieldModel;
  value: Json | undefined;
  onChange: (v: Json | undefined) => void;
  error?: string | undefined;
  bindings?: BindingOptions | undefined;
  idPrefix: string;
  disabled?: boolean | undefined;
}) {
  const id = `${idPrefix}-${field.key}`;
  const source: Source = isParamRef(value) ? "param" : isExpr(value) ? "expr" : "value";
  const [text, setText] = useState(() => (field.kind === "list" && Array.isArray(value) ? value.map(String).join(", ") : field.kind === "json" && source === "value" && value !== undefined ? JSON.stringify(value) : ""));
  const [localError, setLocalError] = useState<string | null>(null);
  /** What this field last emitted: its own edits must not reformat the text being typed. */
  const emitted = useRef<string | undefined>(JSON.stringify(value));
  const emit = (v: Json | undefined) => {
    emitted.current = JSON.stringify(v);
    onChange(v);
  };
  // Follow outside changes (YAML edits, another node selected).
  useEffect(() => {
    if (JSON.stringify(value) === emitted.current) return;
    emitted.current = JSON.stringify(value);
    if (field.kind === "list") setText(Array.isArray(value) ? value.map(String).join(", ") : "");
    if (field.kind === "json" && !isParamRef(value) && !isExpr(value)) setText(value === undefined ? "" : JSON.stringify(value));
    setLocalError(null);
  }, [value, field.kind]);

  const hintParts = [field.description, field.default !== undefined ? `Default: ${display(field.default)}` : null, field.minimum !== null || field.maximum !== null ? `Range ${field.minimum ?? "…"} to ${field.maximum ?? "…"}` : null].filter(Boolean);
  const hint = hintParts.join(" · ") || undefined;
  const label = `${field.label}${field.required ? " (required)" : ""}`;

  let control: React.ReactNode;
  if (source === "param" && bindings) {
    control = (
      <NativeSelect id={id} value={isParamRef(value) ? value.$param : ""} disabled={disabled} onChange={(e) => onChange(e.target.value ? { $param: e.target.value } : undefined)}>
        <option value="">Choose a parameter</option>
        {bindings.params.map((p) => (
          <option key={p} value={p}>
            params.{p}
          </option>
        ))}
      </NativeSelect>
    );
  } else if (source === "expr") {
    control = (
      <Input
        id={id}
        className="font-mono text-xs"
        spellCheck={false}
        placeholder="if(nodes.wanders.value, 1, 0)"
        value={isExpr(value) ? value.$expr : ""}
        disabled={disabled}
        onChange={(e) => onChange({ $expr: e.target.value })}
      />
    );
  } else if (field.kind === "enum") {
    control = (
      <NativeSelect id={id} value={value === undefined ? "" : JSON.stringify(value)} disabled={disabled} onChange={(e) => onChange(e.target.value ? (JSON.parse(e.target.value) as Json) : undefined)}>
        <option value="">{field.default !== undefined ? `Default (${display(field.default)})` : "Not set"}</option>
        {field.options.map((o) => (
          <option key={JSON.stringify(o)} value={JSON.stringify(o)}>
            {display(o)}
          </option>
        ))}
      </NativeSelect>
    );
  } else if (field.kind === "boolean") {
    const checked = typeof value === "boolean" ? value : field.default === true;
    control = (
      <span className="flex items-center gap-2">
        <Checkbox id={id} checked={checked} disabled={disabled} onCheckedChange={(c) => onChange(c === true)} aria-describedby={hint ? `${id}-hint` : undefined} />
        <span className="text-xs text-muted-foreground">{value === undefined ? "Default" : checked ? "On" : "Off"}</span>
      </span>
    );
  } else if (field.kind === "integer" || field.kind === "number") {
    control = (
      <Input
        id={id}
        type="number"
        inputMode="decimal"
        step={field.kind === "integer" ? 1 : "any"}
        {...(field.minimum !== null ? { min: field.minimum } : {})}
        {...(field.maximum !== null ? { max: field.maximum } : {})}
        placeholder={field.default !== undefined ? display(field.default) : undefined}
        value={typeof value === "number" ? value : ""}
        disabled={disabled}
        onChange={(e) => {
          if (e.target.value === "") onChange(undefined);
          else {
            const n = Number(e.target.value);
            if (Number.isFinite(n)) onChange(field.kind === "integer" ? Math.trunc(n) : n);
          }
        }}
      />
    );
  } else if (field.kind === "string") {
    control = (
      <Input id={id} placeholder={field.default !== undefined ? display(field.default) : undefined} value={typeof value === "string" ? value : ""} disabled={disabled} onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)} />
    );
  } else if (field.kind === "list") {
    control = (
      <Input
        id={id}
        placeholder={Array.isArray(field.default) ? field.default.map(String).join(", ") : "a, b, c"}
        value={text}
        disabled={disabled}
        onChange={(e) => {
          setText(e.target.value);
          if (!e.target.value.trim()) {
            setLocalError(null);
            emit(undefined);
            return;
          }
          const parsed = parseList(e.target.value, field.itemType);
          if (parsed.ok) {
            setLocalError(null);
            emit(parsed.value);
          } else setLocalError(parsed.error);
        }}
      />
    );
  } else {
    control = (
      <Textarea
        id={id}
        className="min-h-16 font-mono text-xs"
        spellCheck={false}
        placeholder={field.default !== undefined ? JSON.stringify(field.default) : "{ }"}
        value={text}
        disabled={disabled}
        onChange={(e) => {
          setText(e.target.value);
          if (!e.target.value.trim()) {
            setLocalError(null);
            emit(undefined);
            return;
          }
          try {
            emit(JSON.parse(e.target.value) as Json);
            setLocalError(null);
          } catch {
            setLocalError("Not valid JSON yet.");
          }
        }}
      />
    );
  }

  return (
    <div className="grid gap-1.5" data-field={field.key}>
      <Field label={label} htmlFor={id} hint={hint} error={localError ?? error}>
        {control}
      </Field>
      {bindings ? (
        <SourcePicker
          label={field.label}
          bindings={bindings}
          value={source}
          onChange={(s) => {
            if (s === source) return;
            if (s === "param") onChange(bindings.params[0] ? { $param: bindings.params[0] } : undefined);
            else if (s === "expr") onChange({ $expr: "" });
            else onChange(undefined);
          }}
        />
      ) : null}
    </div>
  );
}

export function SchemaForm({
  schema,
  value,
  onChange,
  errors = {},
  bindings,
  skip = [],
  disabled,
  empty = "This item has no settings.",
}: {
  schema: SchemaNode;
  value: JsonObject;
  onChange: (next: JsonObject) => void;
  /** Field key → problem (e.g. from a validation pointer `/with/<key>`). */
  errors?: Record<string, string>;
  bindings?: BindingOptions;
  /** Keys rendered elsewhere (a branch's condition editor). */
  skip?: string[];
  disabled?: boolean;
  empty?: string;
}) {
  const idPrefix = useId().replace(/:/g, "");
  const fields = fieldsOf(schema).filter((f) => !skip.includes(f.key));
  const unknown = Object.keys(value).filter((k) => !fields.some((f) => f.key === k) && !skip.includes(k));
  if (!fields.length && !unknown.length) return <p className="text-[13px] text-muted-foreground">{empty}</p>;
  const set = (key: string, v: Json | undefined) => {
    const next = { ...value };
    if (v === undefined) delete next[key];
    else next[key] = v;
    onChange(next);
  };
  return (
    <div className="grid gap-4">
      {fields.map((f) => (
        <SchemaField key={f.key} field={f} value={value[f.key]} onChange={(v) => set(f.key, v)} error={errors[f.key]} bindings={bindings} idPrefix={idPrefix} disabled={disabled} />
      ))}
      {unknown.length ? (
        <p role="alert" className="text-xs text-warning">
          Not a setting of this item: {unknown.join(", ")}. Remove {unknown.length === 1 ? "it" : "them"} in the YAML view.
        </p>
      ) : null}
    </div>
  );
}
