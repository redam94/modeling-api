/**
 * Pickers for parameters that name another catalog item, each showing the item's About card:
 * a model (`model.fit`, `model.backtest`, `search.grid`: `ts.sarimax@^1`), a governance check
 * (`governance.check`) and a saved workflow revision (`subworkflow.run`).
 */
import { useId } from "react";

import { Field, Input, NativeSelect } from "@/components/ui/primitives";
import { AboutCard } from "@/features/about/about-card";
import { useGovernanceChecks } from "@/features/governance/api";

import { useAbouts, useWorkflowRevisions, useWorkflows } from "./api";

export function splitModelRef(ref: string): { id: string; range: string } {
  const [id = "", range = ""] = ref.split("@", 2);
  return { id, range };
}

export function ModelPicker({ value, onChange, error }: { value: string; onChange: (v: string) => void; error?: string | undefined }) {
  const id = useId();
  const models = useAbouts("model");
  const { id: modelId, range } = splitModelRef(value);
  const items = models.data?.items ?? [];
  const about = items.find((m) => m.id === modelId)?.about;
  return (
    <div className="grid gap-2">
      <Field label="Model (required)" htmlFor={id} error={error} hint="The catalog model this node fits; the version range keeps later compatible versions.">
        <div className="flex gap-2">
          <NativeSelect id={id} className="min-w-0 flex-1 shrink" value={modelId} onChange={(e) => onChange(e.target.value ? `${e.target.value}@${range || "^1"}` : "")}>
            <option value="">Choose a model</option>
            {modelId && !items.some((m) => m.id === modelId) ? <option value={modelId}>{modelId}</option> : null}
            {items.map((m) => (
              <option key={m.id} value={m.id}>
                {m.about.title} ({m.id})
              </option>
            ))}
          </NativeSelect>
          <Input aria-label="Version range" className="w-20 font-mono text-xs" value={range} placeholder="^1" onChange={(e) => onChange(`${modelId}${e.target.value ? `@${e.target.value}` : ""}`)} />
        </div>
      </Field>
      {about ? <AboutCard about={about} compact headingLevel={4} /> : null}
    </div>
  );
}

export function CheckPicker({ value, onChange, error }: { value: string; onChange: (v: string) => void; error?: string | undefined }) {
  const id = useId();
  const checks = useGovernanceChecks();
  const def = checks.map.get(value);
  return (
    <div className="grid gap-2">
      <Field label="Check (required)" htmlFor={id} error={error}>
        <NativeSelect id={id} value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">Choose a governance check</option>
          {(checks.data?.items ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.title} ({c.id})
            </option>
          ))}
        </NativeSelect>
      </Field>
      {def ? <AboutCard about={def.about} compact headingLevel={4} /> : null}
    </div>
  );
}

/** `workflow://<project>/<name>@r<N>` or `@latest`. */
export function parseWorkflowRef(ref: string): { project: string; name: string; revision: number | "latest" | null } | null {
  const m = /^workflow:\/\/([^/]+)\/([^@\s]+)(?:@(?:r(\d+)|(latest)))?$/.exec(ref);
  if (!m) return null;
  return { project: m[1] ?? "", name: m[2] ?? "", revision: m[3] ? Number(m[3]) : m[4] ? "latest" : null };
}

export function WorkflowRefPicker({ value, onChange, project, self, error }: { value: string; onChange: (v: string) => void; project: string; self: string; error?: string | undefined }) {
  const id = useId();
  const workflows = useWorkflows();
  const parsed = parseWorkflowRef(value);
  const chosen = workflows.data?.items.find((w) => w.name === parsed?.name);
  const revisions = useWorkflowRevisions(chosen?.id);
  const make = (name: string, rev: number | "latest") => `workflow://${project}/${name}@${rev === "latest" ? "latest" : `r${rev}`}`;
  return (
    <div className="grid gap-3">
      <Field label="Workflow (required)" htmlFor={id} error={error} hint="Its inputs and outputs become this node's ports. Pin a revision so later edits do not change this workflow silently.">
        <NativeSelect id={id} value={parsed?.name ?? ""} onChange={(e) => onChange(e.target.value ? make(e.target.value, workflows.data?.items.find((w) => w.name === e.target.value)?.latest_revision ?? "latest") : "")}>
          <option value="">Choose a saved workflow</option>
          {(workflows.data?.items ?? [])
            .filter((w) => w.name !== self)
            .map((w) => (
              <option key={w.id} value={w.name}>
                {w.name}
                {w.description ? ` – ${w.description}` : ""}
              </option>
            ))}
        </NativeSelect>
      </Field>
      {parsed && chosen ? (
        <Field label="Revision" htmlFor={`${id}-rev`}>
          <NativeSelect id={`${id}-rev`} value={String(parsed.revision ?? "latest")} onChange={(e) => onChange(make(parsed.name, e.target.value === "latest" ? "latest" : Number(e.target.value)))}>
            <option value="latest">Always the latest revision</option>
            {(revisions.data?.items ?? []).map((r) => (
              <option key={r.revision} value={r.revision}>
                r{r.revision}
                {r.message ? ` – ${r.message}` : ""}
                {r.validation.valid ? "" : " (has problems)"}
              </option>
            ))}
          </NativeSelect>
        </Field>
      ) : null}
    </div>
  );
}
