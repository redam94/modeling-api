/**
 * The branch condition editor. The text is checked live by the server's parser
 * (`POST /v1/workflows:validate`, the issue at `/nodes/<i>/with/condition`); the browser never
 * evaluates it. A reference list inserts `nodes.<id>.<field>` for every upstream node's result
 * fields, and `params.<name>` for the workflow's parameters.
 */
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { useId, useRef } from "react";

import type { Issue } from "@/api/types";
import { NativeSelect, Textarea } from "@/components/ui/primitives";

import { ancestors, implId, nodeDependencies } from "./doc";
import type { Catalog } from "./ports";
import type { WorkflowDoc } from "./types";

export interface ConditionReference {
  text: string;
  label: string;
  description: string;
}

/** What a condition at `nodeId` may read: upstream result fields and workflow parameters. */
export function conditionReferences(doc: WorkflowDoc, catalog: Catalog, nodeId: string): ConditionReference[] {
  const out: ConditionReference[] = [];
  const deps = nodeDependencies(doc);
  const up = ancestors(deps, nodeId);
  // Also offer nodes not wired in yet but not downstream (referencing one makes it upstream).
  const downstream = new Set(doc.nodes.filter((n) => ancestors(deps, n.id).has(nodeId)).map((n) => n.id));
  for (const n of doc.nodes) {
    if (n.id === nodeId || downstream.has(n.id)) continue;
    const impl = catalog.get(implId(n.use));
    for (const [field, spec] of Object.entries(impl?.result_fields ?? {})) {
      out.push({ text: `nodes.${n.id}.${field}`, label: `${n.id}.${field}${up.has(n.id) ? "" : " (not wired yet)"}`, description: `${spec.type}: ${spec.description}` });
    }
  }
  for (const [name, p] of Object.entries(doc.params ?? {})) out.push({ text: `params.${name}`, label: `params.${name}`, description: `${p.type}${p.description ? `: ${p.description}` : ""}` });
  return out;
}

export function ConditionEditor({
  value,
  onChange,
  issue,
  checking,
  references,
}: {
  value: string;
  onChange: (v: string) => void;
  /** The server's verdict on this condition (`null`: it parses and reads known fields). */
  issue: Issue | null;
  checking: boolean;
  references: ConditionReference[];
}) {
  const id = useId();
  const ref = useRef<HTMLTextAreaElement>(null);
  const insert = (text: string) => {
    const el = ref.current;
    const start = el?.selectionStart ?? value.length;
    const end = el?.selectionEnd ?? value.length;
    const next = `${value.slice(0, start)}${text}${value.slice(end)}`;
    onChange(next);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(start + text.length, start + text.length);
    });
  };
  const status = !value.trim() ? null : checking ? "checking" : issue ? "bad" : "ok";
  return (
    <div className="grid gap-2">
      <label htmlFor={id} className="text-[13px] font-medium">
        Condition
      </label>
      <Textarea
        ref={ref}
        id={id}
        rows={3}
        spellCheck={false}
        className="font-mono text-xs"
        placeholder="not nodes.adf.reject or nodes.kpss.reject"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={status === "bad" ? true : undefined}
        aria-describedby={`${id}-status ${id}-help`}
      />
      <p id={`${id}-status`} role="status" aria-live="polite" className="flex items-start gap-1.5 text-xs">
        {status === "checking" ? (
          <>
            <Loader2 className="mt-0.5 size-3.5 animate-spin text-muted-foreground" aria-hidden /> Checking…
          </>
        ) : status === "bad" ? (
          <>
            <XCircle className="mt-0.5 size-3.5 shrink-0 text-destructive" aria-hidden />
            <span className="text-destructive">{issue?.message}</span>
          </>
        ) : status === "ok" ? (
          <>
            <CheckCircle2 className="mt-0.5 size-3.5 text-success" aria-hidden />
            <span className="text-muted-foreground">The condition reads correctly.</span>
          </>
        ) : (
          <span className="text-muted-foreground">Write when the then arm should run.</span>
        )}
      </p>
      <NativeSelect
        aria-label="Insert a reference"
        value=""
        onChange={(e) => {
          if (e.target.value) insert(e.target.value);
        }}
      >
        <option value="">Insert a result or parameter…</option>
        {references.map((r) => (
          <option key={r.text} value={r.text} title={r.description}>
            {r.label}
          </option>
        ))}
      </NativeSelect>
      <details id={`${id}-help`} className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">How conditions are written</summary>
        <div className="mt-1 grid gap-1">
          <p>
            Read results as <code>nodes.adf.reject</code>, parameters as <code>params.alpha</code>. Combine with <code>and</code>, <code>or</code>, <code>not</code>; compare with{" "}
            <code>== != &lt; &lt;= &gt; &gt;=</code> and <code>in</code>.
          </p>
          <p>
            Functions: <code>abs min max len round any all exists coalesce if</code>. A missing result is an error unless wrapped in <code>exists(…)</code> or <code>coalesce(…, default)</code>.
          </p>
          <p>The server checks and evaluates conditions with a safe parser; your browser never runs them.</p>
        </div>
      </details>
    </div>
  );
}
