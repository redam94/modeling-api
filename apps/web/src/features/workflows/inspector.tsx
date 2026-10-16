/**
 * The selected node's editor: what the node does (its About), its inputs (sources by typed
 * port, alternatives in order), the branch arm that gates it, its parameters (a form from the
 * node's JSON Schema; a branch gets the condition editor), how it runs, and the problems the
 * server found, each next to the field it concerns.
 */
import { ArrowDown, ArrowUp, Trash2, X } from "lucide-react";
import { useEffect, useId, useState } from "react";

import type { Issue } from "@/api/types";
import { IssueList } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Field, Input, NativeSelect } from "@/components/ui/primitives";
import { Checkbox } from "@/components/ui/overlays";
import { AboutCard } from "@/features/about/about-card";
import { SchemaForm, type JsonObject, type SchemaNode } from "@/features/forms/json-schema-form";

import { ConditionEditor, conditionReferences } from "./condition-editor";
import { bindingList, connect, disconnect, implId, moveAlternative, nodeIdProblem, renameNode, setNodeFields, setWhen, setWith } from "./doc";
import { ModelPicker, CheckPicker, WorkflowRefPicker } from "./pickers";
import { compatibleSources, gateCandidates, inputPorts, PORT_TYPE_META, type Catalog, type SubworkflowIndex } from "./ports";
import { KIND_LABEL } from "./status";
import type { WorkflowDoc, WorkflowNodeDoc } from "./types";
import { BRANCH_NODE, SUBWORKFLOW_NODE } from "./types";

/** Parameters with a dedicated picker instead of a plain field. */
const PICKED: Record<string, string> = {
  "model.fit": "model",
  "model.backtest": "model",
  "model.forecast": "model",
  "search.grid": "model",
  "governance.check": "check",
  "subworkflow.run": "workflow",
};

export interface NodeIssue {
  issue: Issue;
  /** Pointer segments below `/nodes/<i>` (`["with", "alpha"]`). */
  rest: string[];
}

function RenameField({ doc, node, onRename }: { doc: WorkflowDoc; node: WorkflowNodeDoc; onRename: (to: string) => void }) {
  const id = useId();
  const [draft, setDraft] = useState(node.id);
  useEffect(() => setDraft(node.id), [node.id]);
  const problem = draft === node.id ? null : nodeIdProblem(doc, draft, node.id);
  return (
    <Field label="Node id" htmlFor={id} error={problem} hint="Other nodes read its results as nodes.<id>; renaming updates them.">
      <Input
        id={id}
        className="font-mono"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          if (draft !== node.id && !nodeIdProblem(doc, draft, node.id)) onRename(draft);
          else setDraft(node.id);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
      />
    </Field>
  );
}

export function NodeInspector({
  doc,
  node,
  catalog,
  subs,
  issues,
  validating,
  onChange,
  onSelect,
  onRemove,
}: {
  doc: WorkflowDoc;
  node: WorkflowNodeDoc;
  catalog: Catalog;
  subs: SubworkflowIndex;
  issues: NodeIssue[];
  validating: boolean;
  onChange: (doc: WorkflowDoc) => void;
  onSelect: (id: string | null) => void;
  onRemove: () => void;
}) {
  const idBase = useId();
  const impl = catalog.get(implId(node.use));
  const isBranch = implId(node.use) === BRANCH_NODE;
  const ports = inputPorts(node, catalog, subs);
  const fieldErrors: Record<string, string> = {};
  const portErrors: Record<string, string[]> = {};
  const general: Issue[] = [];
  for (const { issue, rest } of issues) {
    if (rest[0] === "with" && rest[1]) fieldErrors[rest[1]] ??= issue.message;
    else if (rest[0] === "inputs" && rest[1]) (portErrors[rest[1]] ??= []).push(issue.message);
    else general.push(issue);
  }
  const w: JsonObject = node.with ?? {};
  const setParam = (key: string, v: string) => onChange(setWith(doc, node.id, v ? { ...w, [key]: v } : Object.fromEntries(Object.entries(w).filter(([k]) => k !== key))));
  const picked = PICKED[implId(node.use)];
  const conditionIssue = issues.find((i) => i.rest[0] === "with" && i.rest[1] === "condition")?.issue ?? null;
  const gates = gateCandidates(doc, node.id);

  return (
    <div className="grid gap-5" data-testid="node-inspector">
      <header className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs font-medium text-muted-foreground">{impl ? KIND_LABEL[impl.kind] : "Unknown node"}</p>
          <h2 className="truncate text-base font-semibold">{node.label || impl?.about.title || node.id}</h2>
          <code className="font-mono text-xs text-muted-foreground">{node.use}</code>
        </div>
        <Button variant="ghost" size="icon-sm" aria-label="Close the node editor" onClick={() => onSelect(null)}>
          <X />
        </Button>
      </header>

      {general.length ? (
        <section aria-label="Problems with this node" className="rounded-md border border-destructive/35 bg-destructive/6 p-2">
          <IssueList issues={general} />
        </section>
      ) : null}

      {impl ? (
        <AboutCard about={impl.about} compact headingLevel={3} heading={<span className="text-sm">What it does</span>} />
      ) : (
        <p role="alert" className="text-[13px] text-destructive">
          “{implId(node.use)}” is not in the node catalog. Pick another implementation in the YAML view, or remove the node.
        </p>
      )}

      <section className="grid gap-3" aria-labelledby={`${idBase}-name`}>
        <h3 id={`${idBase}-name`} className="text-sm font-semibold">
          Name
        </h3>
        <RenameField doc={doc} node={node} onRename={(to) => {
          onChange(renameNode(doc, node.id, to));
          onSelect(to);
        }} />
        <Field label="Label (optional)" htmlFor={`${idBase}-label`}>
          <Input id={`${idBase}-label`} value={node.label ?? ""} maxLength={120} placeholder={impl?.about.title} onChange={(e) => onChange(setNodeFields(doc, node.id, { label: e.target.value || null }))} />
        </Field>
      </section>

      {Object.keys(ports).length || (implId(node.use) === SUBWORKFLOW_NODE) ? (
        <section className="grid gap-3" aria-labelledby={`${idBase}-in`}>
          <h3 id={`${idBase}-in`} className="text-sm font-semibold">
            Inputs
          </h3>
          {Object.keys(ports).length === 0 ? <p className="text-[13px] text-muted-foreground">Choose the workflow first: its inputs become this node's inputs.</p> : null}
          {Object.entries(ports).map(([port, spec]) => {
            const bound = bindingList(node.inputs?.[port]);
            const sources = compatibleSources(doc, catalog, { node: node.id, port }, subs).filter((s) => !bound.includes(s.binding));
            const pid = `${idBase}-port-${port}`;
            return (
              <div key={port} className="grid gap-1.5 rounded-md border p-2" data-port={port}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[13px] font-medium">{port}</span>
                  <Badge variant="outline" style={{ color: PORT_TYPE_META[spec.type].color }}>
                    {PORT_TYPE_META[spec.type].plain}
                  </Badge>
                  {spec.required ? <Badge variant="secondary">required</Badge> : <Badge variant="outline">optional</Badge>}
                  {spec.multiple ? <Badge variant="outline">collects several</Badge> : null}
                </div>
                {spec.description ? <p className="text-xs text-muted-foreground">{spec.description}</p> : null}
                {bound.length ? (
                  <ol className="grid gap-1" aria-label={`Sources of ${port}`}>
                    {bound.map((b, k) => (
                      <li key={b} className="flex items-center gap-1 rounded bg-muted/50 px-1.5 py-0.5 text-xs">
                        {bound.length > 1 && !spec.multiple ? <span className="text-muted-foreground">{k === 0 ? "first" : "else"}</span> : null}
                        <code className="min-w-0 flex-1 truncate font-mono">{b}</code>
                        {bound.length > 1 ? (
                          <>
                            <Button variant="ghost" size="icon-sm" className="size-6" aria-label={`Move ${b} up`} disabled={k === 0} onClick={() => onChange(moveAlternative(doc, node.id, port, k, -1))}>
                              <ArrowUp />
                            </Button>
                            <Button variant="ghost" size="icon-sm" className="size-6" aria-label={`Move ${b} down`} disabled={k === bound.length - 1} onClick={() => onChange(moveAlternative(doc, node.id, port, k, 1))}>
                              <ArrowDown />
                            </Button>
                          </>
                        ) : null}
                        <Button variant="ghost" size="icon-sm" className="size-6" aria-label={`Disconnect ${b}`} onClick={() => onChange(disconnect(doc, node.id, port, b))}>
                          <X />
                        </Button>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="text-xs text-muted-foreground">Not connected.</p>
                )}
                <NativeSelect
                  id={pid}
                  aria-label={bound.length ? (spec.multiple ? `Add a source to ${port}` : `Add an alternative for ${port}`) : `Connect ${port}`}
                  value=""
                  disabled={!sources.length}
                  onChange={(e) => e.target.value && onChange(connect(doc, e.target.value, node.id, port, "append"))}
                >
                  <option value="">
                    {sources.length ? (bound.length ? (spec.multiple ? "Add a source…" : "Add an alternative (used if the first is skipped)…") : "Connect a source…") : `Nothing upstream gives ${PORT_TYPE_META[spec.type].label}`}
                  </option>
                  {sources.map((s) => (
                    <option key={s.binding} value={s.binding}>
                      {s.label}
                    </option>
                  ))}
                </NativeSelect>
                {portErrors[port]?.map((m) => (
                  <p key={m} role="alert" className="text-xs font-medium text-destructive">
                    {m}
                  </p>
                ))}
              </div>
            );
          })}
        </section>
      ) : null}

      {!isBranch ? (
        <section className="grid gap-2" aria-labelledby={`${idBase}-when`}>
          <h3 id={`${idBase}-when`} className="text-sm font-semibold">
            Runs
          </h3>
          <NativeSelect aria-labelledby={`${idBase}-when`} value={node.when ?? ""} onChange={(e) => onChange(setWhen(doc, node.id, e.target.value || null))}>
            <option value="">Always</option>
            {gates.flatMap((b) => [
              <option key={`${b}.then`} value={`${b}.then`}>
                Only when {b} says yes (then)
              </option>,
              <option key={`${b}.else`} value={`${b}.else`}>
                Only when {b} says no (else)
              </option>,
            ])}
          </NativeSelect>
          <p className="text-xs text-muted-foreground">A node on an arm that is not taken is skipped, and so is everything that needs it unless it has an alternative.</p>
        </section>
      ) : null}

      <section className="grid gap-3" aria-labelledby={`${idBase}-params`}>
        <h3 id={`${idBase}-params`} className="text-sm font-semibold">
          {isBranch ? "Decision" : "Settings"}
        </h3>
        {isBranch ? (
          <ConditionEditor
            value={typeof w.condition === "string" ? w.condition : ""}
            onChange={(v) => onChange(setWith(doc, node.id, { ...w, condition: v }))}
            issue={conditionIssue}
            checking={validating}
            references={conditionReferences(doc, catalog, node.id)}
          />
        ) : null}
        {picked === "model" ? <ModelPicker value={typeof w.model === "string" ? w.model : ""} onChange={(v) => setParam("model", v)} error={fieldErrors.model} /> : null}
        {picked === "check" ? <CheckPicker value={typeof w.check === "string" ? w.check : ""} onChange={(v) => setParam("check", v)} error={fieldErrors.check} /> : null}
        {picked === "workflow" ? (
          <WorkflowRefPicker value={typeof w.workflow === "string" ? w.workflow : ""} onChange={(v) => setParam("workflow", v)} project={doc.metadata.project} self={doc.metadata.name} error={fieldErrors.workflow} />
        ) : null}
        {impl ? (
          <SchemaForm
            schema={impl.params_schema as SchemaNode}
            value={w}
            onChange={(next) => onChange(setWith(doc, node.id, next))}
            errors={fieldErrors}
            bindings={{ params: Object.keys(doc.params ?? {}), expressions: true }}
            skip={[...(isBranch ? ["condition"] : []), ...(picked ? [picked] : [])]}
            empty={isBranch ? "" : "This node has no settings."}
          />
        ) : null}
      </section>

      {!isBranch ? (
        <details className="rounded-md border p-2">
          <summary className="cursor-pointer text-sm font-semibold">How it runs</summary>
          <div className="mt-3 grid gap-3">
            <Field label="Retries" htmlFor={`${idBase}-retries`} hint="Retry a transient failure up to 3 times.">
              <NativeSelect id={`${idBase}-retries`} value={node.retries ?? 0} onChange={(e) => onChange(setNodeFields(doc, node.id, { retries: Number(e.target.value) }))}>
                {[0, 1, 2, 3].map((n) => (
                  <option key={n} value={n}>
                    {n === 0 ? "No retries" : `${n} ${n === 1 ? "retry" : "retries"}`}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Time limit" htmlFor={`${idBase}-timeout`} hint="Like 20m or 2h; empty uses the server's default." error={fieldErrors.timeout}>
              <Input id={`${idBase}-timeout`} className="font-mono" value={node.timeout ?? ""} placeholder="30m" onChange={(e) => onChange(setNodeFields(doc, node.id, { timeout: e.target.value || null }))} />
            </Field>
            <Field label="When it fails" htmlFor={`${idBase}-fail`}>
              <NativeSelect id={`${idBase}-fail`} value={node.on_failure ?? "fail"} onChange={(e) => onChange(setNodeFields(doc, node.id, { on_failure: e.target.value as "fail" | "continue" }))}>
                <option value="fail">Stop the workflow</option>
                <option value="continue">Carry on as if it was skipped</option>
              </NativeSelect>
            </Field>
            <label className="flex items-center gap-2 text-[13px]">
              <Checkbox checked={node.cache !== false} onCheckedChange={(c) => onChange(setNodeFields(doc, node.id, { cache: c === true }))} />
              Reuse an identical earlier result
            </label>
          </div>
        </details>
      ) : null}

      <Button variant="outline" className="justify-self-start text-destructive" onClick={onRemove}>
        <Trash2 /> Remove node
      </Button>
    </div>
  );
}
