/**
 * The workflow's own settings, shown when no node is selected: description, run-time
 * parameters, inputs, named outputs (what a run hands back and what a parent workflow reads)
 * and run-wide limits.
 */
import { Plus, Trash2 } from "lucide-react";
import { useId, useState } from "react";

import type { Issue } from "@/api/types";
import { IssueList } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Field, Input, NativeSelect, Textarea } from "@/components/ui/primitives";
import { Checkbox } from "@/components/ui/overlays";

import { setOutputs } from "./doc";
import { outputPorts, PORT_TYPE_META, type Catalog, type SubworkflowIndex } from "./ports";
import type { Json, PortType, WorkflowDoc, WorkflowParamDecl } from "./types";
import { PORT_TYPES } from "./types";

const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const PARAM_TYPES: WorkflowParamDecl["type"][] = ["number", "integer", "boolean", "string", "array", "object"];

function parseDefault(text: string, type: WorkflowParamDecl["type"]): { ok: true; value: Json | undefined } | { ok: false; error: string } {
  if (!text.trim()) return { ok: true, value: undefined };
  if (type === "string") return { ok: true, value: text };
  try {
    const v = JSON.parse(text) as Json;
    const fits =
      (type === "number" && typeof v === "number") ||
      (type === "integer" && typeof v === "number" && Number.isInteger(v)) ||
      (type === "boolean" && typeof v === "boolean") ||
      (type === "array" && Array.isArray(v)) ||
      (type === "object" && v !== null && typeof v === "object" && !Array.isArray(v));
    return fits ? { ok: true, value: v } : { ok: false, error: `Not a ${type}.` };
  } catch {
    return { ok: false, error: type === "array" ? "Write a JSON list, like [1, 4, 8]." : `Not a ${type}.` };
  }
}

function ParamRow({ name, decl, onChange, onRemove }: { name: string; decl: WorkflowParamDecl; onChange: (d: WorkflowParamDecl) => void; onRemove: () => void }) {
  const id = useId();
  const [text, setText] = useState(decl.default === undefined || decl.default === null ? "" : typeof decl.default === "string" ? decl.default : JSON.stringify(decl.default));
  const [error, setError] = useState<string | null>(null);
  return (
    <li className="grid gap-2 rounded-md border p-2" data-param={name}>
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 truncate font-mono text-[13px] font-medium">params.{name}</code>
        <NativeSelect aria-label={`Type of ${name}`} className="h-8 w-28" value={decl.type} onChange={(e) => onChange({ ...decl, type: e.target.value as WorkflowParamDecl["type"] })}>
          {PARAM_TYPES.map((t) => (
            <option key={t}>{t}</option>
          ))}
        </NativeSelect>
        <Button variant="ghost" size="icon-sm" aria-label={`Remove parameter ${name}`} onClick={onRemove}>
          <Trash2 />
        </Button>
      </div>
      <Field label="Default" htmlFor={`${id}-d`} error={error}>
        <Input
          id={`${id}-d`}
          className="font-mono text-xs"
          value={text}
          disabled={decl.required}
          onChange={(e) => {
            setText(e.target.value);
            const p = parseDefault(e.target.value, decl.type);
            if (!p.ok) return setError(p.error);
            setError(null);
            const next = { ...decl };
            if (p.value === undefined) delete next.default;
            else next.default = p.value;
            onChange(next);
          }}
        />
      </Field>
      <label className="flex items-center gap-2 text-xs">
        <Checkbox
          checked={Boolean(decl.required)}
          onCheckedChange={(c) => {
            const next: WorkflowParamDecl = { ...decl, required: c === true };
            if (c === true) delete next.default;
            if (!next.required) delete next.required;
            onChange(next);
          }}
        />
        Must be given when the workflow runs
      </label>
      <Input aria-label={`Description of ${name}`} placeholder="What it controls, in plain words" value={decl.description ?? ""} onChange={(e) => onChange({ ...decl, description: e.target.value || null })} />
    </li>
  );
}

export function WorkflowSettings({
  doc,
  catalog,
  subs,
  issues,
  onChange,
}: {
  doc: WorkflowDoc;
  catalog: Catalog;
  subs: SubworkflowIndex;
  issues: Issue[];
  onChange: (doc: WorkflowDoc) => void;
}) {
  const id = useId();
  const [newParam, setNewParam] = useState("");
  const [newInput, setNewInput] = useState("");
  const [newOutput, setNewOutput] = useState("");
  const update = (patch: Partial<WorkflowDoc>) => onChange({ ...structuredClone(doc), ...patch });
  const params = doc.params ?? {};
  const inputs = doc.inputs ?? {};
  const outputs = doc.outputs ?? {};
  const allOutputs = doc.nodes.flatMap((n) => Object.entries(outputPorts(n, catalog, subs)).map(([port, type]) => ({ binding: `${n.id}.${port}`, type })));
  return (
    <div className="grid gap-5" data-testid="workflow-settings">
      <header>
        <h2 className="text-base font-semibold">Workflow settings</h2>
        <p className="text-xs text-muted-foreground">Select a node on the canvas to edit it.</p>
      </header>
      {issues.length ? (
        <section aria-label="Problems with the workflow" className="rounded-md border border-destructive/35 bg-destructive/6 p-2">
          <IssueList issues={issues} />
        </section>
      ) : null}
      <Field label="Description" htmlFor={`${id}-desc`}>
        <Textarea
          id={`${id}-desc`}
          maxLength={4000}
          value={doc.metadata.description ?? ""}
          onChange={(e) => update({ metadata: { ...doc.metadata, description: e.target.value || null } })}
          placeholder="What this workflow is for and when to run it"
        />
      </Field>

      <section className="grid gap-2" aria-labelledby={`${id}-params`}>
        <h3 id={`${id}-params`} className="text-sm font-semibold">
          Parameters
        </h3>
        <p className="text-xs text-muted-foreground">Values a run may change. Nodes take them with “Parameter”, conditions read them as params.&lt;name&gt;.</p>
        <ul className="grid gap-2">
          {Object.entries(params).map(([name, decl]) => (
            <ParamRow
              key={name}
              name={name}
              decl={decl}
              onChange={(d) => update({ params: { ...params, [name]: d } })}
              onRemove={() => update({ params: Object.fromEntries(Object.entries(params).filter(([k]) => k !== name)) })}
            />
          ))}
        </ul>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (!NAME_RE.test(newParam) || newParam in params) return;
            update({ params: { ...params, [newParam]: { type: "number" } } });
            setNewParam("");
          }}
        >
          <Input aria-label="New parameter name" placeholder="alpha" className="font-mono" value={newParam} onChange={(e) => setNewParam(e.target.value)} />
          <Button type="submit" variant="outline" disabled={!NAME_RE.test(newParam) || newParam in params}>
            <Plus /> Parameter
          </Button>
        </form>
      </section>

      <section className="grid gap-2" aria-labelledby={`${id}-inputs`}>
        <h3 id={`${id}-inputs`} className="text-sm font-semibold">
          Inputs
        </h3>
        <ul className="grid gap-2">
          {Object.entries(inputs).map(([name, decl]) => (
            <li key={name} className="flex flex-wrap items-center gap-2 rounded-md border p-2">
              <code className="min-w-0 flex-1 font-mono text-[13px]">inputs.{name}</code>
              <NativeSelect aria-label={`Type of input ${name}`} className="h-8 w-36" value={decl.type} onChange={(e) => update({ inputs: { ...inputs, [name]: { ...decl, type: e.target.value as PortType } } })}>
                {PORT_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {PORT_TYPE_META[t].plain}
                  </option>
                ))}
              </NativeSelect>
              <Button variant="ghost" size="icon-sm" aria-label={`Remove input ${name}`} onClick={() => update({ inputs: Object.fromEntries(Object.entries(inputs).filter(([k]) => k !== name)) })}>
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (!NAME_RE.test(newInput) || newInput in inputs) return;
            update({ inputs: { ...inputs, [newInput]: { type: "dataset" } } });
            setNewInput("");
          }}
        >
          <Input aria-label="New input name" placeholder="data" className="font-mono" value={newInput} onChange={(e) => setNewInput(e.target.value)} />
          <Button type="submit" variant="outline" disabled={!NAME_RE.test(newInput) || newInput in inputs}>
            <Plus /> Input
          </Button>
        </form>
      </section>

      <section className="grid gap-2" aria-labelledby={`${id}-outputs`}>
        <h3 id={`${id}-outputs`} className="text-sm font-semibold">
          Outputs
        </h3>
        <p className="text-xs text-muted-foreground">What a run hands back, and what a parent workflow reads from this one.</p>
        <ul className="grid gap-2">
          {Object.entries(outputs).map(([name, b]) => (
            <li key={name} className="flex flex-wrap items-center gap-2 rounded-md border p-2">
              <code className="font-mono text-[13px]">{name}</code>
              <NativeSelect aria-label={`Source of output ${name}`} className="h-8 min-w-0 flex-1" value={b} onChange={(e) => onChange(setOutputs(doc, { ...outputs, [name]: e.target.value }))}>
                {allOutputs.some((o) => o.binding === b) ? null : <option value={b}>{b}</option>}
                {allOutputs.map((o) => (
                  <option key={o.binding} value={o.binding}>
                    {o.binding} ({PORT_TYPE_META[o.type].label})
                  </option>
                ))}
              </NativeSelect>
              <Button variant="ghost" size="icon-sm" aria-label={`Remove output ${name}`} onClick={() => onChange(setOutputs(doc, Object.fromEntries(Object.entries(outputs).filter(([k]) => k !== name))))}>
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            const first = allOutputs[0]?.binding;
            if (!NAME_RE.test(newOutput) || newOutput in outputs || !first) return;
            onChange(setOutputs(doc, { ...outputs, [newOutput]: first }));
            setNewOutput("");
          }}
        >
          <Input aria-label="New output name" placeholder="report" className="font-mono" value={newOutput} onChange={(e) => setNewOutput(e.target.value)} />
          <Button type="submit" variant="outline" disabled={!NAME_RE.test(newOutput) || newOutput in outputs || !allOutputs.length}>
            <Plus /> Output
          </Button>
        </form>
      </section>

      <section className="grid gap-3" aria-labelledby={`${id}-exec`}>
        <h3 id={`${id}-exec`} className="text-sm font-semibold">
          Run limits
        </h3>
        <Field label="Nodes at once" htmlFor={`${id}-par`}>
          <Input
            id={`${id}-par`}
            type="number"
            min={1}
            max={32}
            value={doc.execution?.max_parallel ?? 4}
            onChange={(e) => update({ execution: { ...doc.execution, max_parallel: Math.min(32, Math.max(1, Number(e.target.value) || 1)) } })}
          />
        </Field>
        <Field label="Whole-run time limit" htmlFor={`${id}-to`} hint="Like 2h or 30m (between 1 minute and 7 days).">
          <Input id={`${id}-to`} className="font-mono" placeholder="6h" value={doc.execution?.timeout ?? ""} onChange={(e) => update({ execution: { ...doc.execution, ...(e.target.value ? { timeout: e.target.value } : { timeout: undefined }) } })} />
        </Field>
      </section>
    </div>
  );
}
