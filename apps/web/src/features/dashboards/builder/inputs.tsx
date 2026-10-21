/**
 * Builder › Inputs: what the client may do on the dashboard and what it costs. What-ifs on the
 * response grid are free; exact results, uploads and refreshes are score runs that run as the
 * publisher (plan §3.4). Uploads can carry a refit request to the analyst's refit workflow,
 * which an editor with the runner role approves (§10.4).
 */
import { Link } from "@tanstack/react-router";
import { ExternalLink } from "lucide-react";
import type * as React from "react";

import { InlineMessage } from "@/components/ods";
import { Switch } from "@/components/ui/overlays";
import { Field, Input, Label, NativeSelect } from "@/components/ui/primitives";
import { useWorkflows } from "@/features/workflows/api";

import { Section } from "../readout/parts";
import type { DashboardSpec } from "../types";
import type { SpecEditorProps } from "./setup";
import { issueAt, MAX_EXACT_RUNS, setActuals, setRefitWorkflow, setWhatIf } from "./spec-form";

function Toggle({ id, label, hint, checked, disabled, onChange, error }: { id: string; label: string; hint: React.ReactNode; checked: boolean; disabled?: boolean; onChange: (on: boolean) => void; error?: string | undefined }) {
  return (
    <div className="flex items-start gap-3">
      <Switch id={id} checked={checked} disabled={disabled} onCheckedChange={onChange} aria-describedby={`${id}-hint`} />
      <span className="grid gap-0.5">
        <Label htmlFor={id}>{label}</Label>
        <span id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </span>
        {error ? (
          <span role="alert" className="text-xs font-medium text-destructive">
            {error}
          </span>
        ) : null}
      </span>
    </div>
  );
}

function WorkflowPicker({ id, label, hint, value, onChange, error }: { id: string; label: string; hint: React.ReactNode; value: string | null; onChange: (v: string | null) => void; error?: string | undefined }) {
  const workflows = useWorkflows();
  const items = workflows.data?.items ?? [];
  const known = value ? items.some((w) => w.id === value) : true;
  return (
    <Field label={label} htmlFor={id} hint={hint} error={error}>
      <div className="flex flex-wrap items-center gap-3">
        <NativeSelect id={id} className="min-w-64" value={value ?? ""} onChange={(e) => onChange(e.target.value || null)}>
          <option value="">{workflows.isPending ? "Loading…" : "No workflow"}</option>
          {!known && value ? <option value={value}>{value} (not found)</option> : null}
          {items.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </NativeSelect>
        {value && known ? (
          <Link to="/workflows/$workflowId" params={{ workflowId: value }} search={{ tab: "automation" }} className="inline-flex items-center gap-1 text-sm text-primary hover:underline">
            <ExternalLink className="size-3.5" /> Schedules of the workflow
          </Link>
        ) : null}
      </div>
    </Field>
  );
}

export function InputsTab({ spec, onChange, issues, grid, readOnly = false }: SpecEditorProps) {
  const whatif = spec.whatif?.enabled ?? true;
  const exact = spec.whatif?.exact ?? false;
  const a = spec.actuals ?? {};
  const actuals = a.enabled ?? false;
  const noGrid = grid.channels === null;
  const runs = spec.limits?.exact_runs_per_day ?? 20;
  return (
    <fieldset disabled={readOnly} className="grid min-w-0 gap-5 xl:grid-cols-2">
      <legend className="sr-only">Inputs</legend>
      <Section title="Plan scenarios" description="Sliders on the model's response grid. Moving a slider costs nothing: no run starts.">
        <Toggle
          id="ds-whatif"
          label="Let the client try other spend (what-ifs)"
          hint={noGrid ? "This model has no response grid." : "Adds the Plan scenarios tab. Saved scenarios stay private to the person who saved them."}
          checked={whatif}
          disabled={noGrid && !whatif}
          error={issueAt(issues, "/whatif/enabled")}
          onChange={(on) => onChange((s) => setWhatIf(s, on))}
        />
        <Toggle
          id="ds-exact"
          label="Offer exact results"
          hint="“Get exact result” scores the scenario with the full model. Each one is a score run that runs as you and uses your project's compute. It can take up to two minutes."
          checked={exact}
          disabled={!whatif}
          onChange={(on) => onChange((s) => ({ ...s, whatif: { enabled: s.whatif?.enabled ?? true, exact: on } }))}
        />
      </Section>

      <Section title="Number uploads" description="The client sends new actuals. The dashboard compares them with what the model expected (or, for models that can, updates the forecast with them).">
        <Toggle
          id="ds-actuals"
          label="Let the client upload new numbers"
          hint="Adds the Update numbers tab. Each upload is a score run that runs as you and uses your project's compute."
          checked={actuals}
          error={issueAt(issues, "/actuals/enabled")}
          onChange={(on) => onChange((s) => setActuals(s, on))}
        />
        <fieldset className="grid gap-2" disabled={!actuals}>
          <legend className="mb-1 text-sm font-semibold">A new upload</legend>
          {(
            [
              ["review", "Waits for review (recommended)", "The uploader sees the result at once. An editor publishes it into the history on the History tab."],
              ["auto", "Goes into the history at once", "Everyone with access sees the result when it is ready."],
            ] as const
          ).map(([value, label, hint]) => (
            <label key={value} className="flex items-start gap-2 text-sm">
              <input
                type="radio"
                name="ds-actuals-publish"
                value={value}
                checked={(a.publish ?? "review") === value}
                onChange={() => onChange((s) => ({ ...s, actuals: { ...s.actuals, publish: value } }))}
                className="mt-1 accent-[var(--primary)]"
              />
              <span className="grid gap-0.5">
                <span className="font-medium">{label}</span>
                <span className="text-xs text-muted-foreground">{hint}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <fieldset className="grid gap-4 rounded-lg border p-4" disabled={!actuals}>
          <legend className="px-1 text-sm font-semibold">Refit requests</legend>
          <WorkflowPicker
            id="ds-refit-wf"
            label="Refit workflow"
            hint="A workflow of this project whose input node takes the dashboard's dataset. An approved request adds the uploaded numbers as a new dataset version and starts this workflow."
            value={a.refit_workflow_id ?? null}
            error={issueAt(issues, "/actuals/refit_workflow_id")}
            onChange={(id) => onChange((s) => setRefitWorkflow(s, id))}
          />
          <Toggle
            id="ds-refit-allow"
            label="Let the uploader ask for a refit"
            hint="The uploader can tick “Ask the analyst to refit with these numbers”. Nothing runs until an editor with the runner role approves the request on the History tab."
            checked={a.allow_refit_requests ?? false}
            disabled={!actuals || !a.refit_workflow_id}
            error={issueAt(issues, "/actuals/allow_refit_requests")}
            onChange={(on) => onChange((s) => ({ ...s, actuals: { ...s.actuals, allow_refit_requests: on } }))}
          />
        </fieldset>
      </Section>

      <Section title="Refresh" description="How new readouts arrive without the client doing anything.">
        <WorkflowPicker
          id="ds-refresh-wf"
          label="Refresh workflow"
          hint="Each successful run of this workflow (its model.score or model.forecast node) adds a readout. Schedule the workflow on its Automation tab."
          value={spec.refresh?.workflow_id ?? null}
          onChange={(id) => onChange((s) => ({ ...s, refresh: { ...s.refresh, workflow_id: id } }))}
        />
        <Toggle
          id="ds-follow"
          label="Follow the endpoint"
          hint={spec.source.kind === "endpoint" ? "When you promote a new model version to the endpoint, the dashboard publishes a new revision on that version and adds a readout." : "Only a dashboard on an endpoint can follow it."}
          checked={spec.refresh?.follow_endpoint ?? false}
          disabled={spec.source.kind !== "endpoint"}
          error={issueAt(issues, "/refresh/follow_endpoint")}
          onChange={(on) => onChange((s) => ({ ...s, refresh: { ...s.refresh, follow_endpoint: on } }))}
        />
      </Section>

      <Section title="Limits" description="A cap on the runs that the dashboard can start for you.">
        <Field
          label="Runs each day"
          htmlFor="ds-limit"
          error={issueAt(issues, "/limits/exact_runs_per_day")}
          hint={`Exact results, uploads and manual refreshes together, in 24 hours. 0 to ${MAX_EXACT_RUNS}. Free what-ifs do not count.`}
        >
          <Input
            id="ds-limit"
            type="number"
            min={0}
            max={MAX_EXACT_RUNS}
            step={1}
            className="w-32"
            value={runs}
            onChange={(e) => onChange((s): DashboardSpec => ({ ...s, limits: { exact_runs_per_day: e.target.value === "" ? 0 : Number(e.target.value) } }))}
          />
        </Field>
        {exact || actuals ? (
          <InlineMessage tone="info" size="s" role="none">
            Live inputs run as the person who publishes and use the project's compute and quotas.
          </InlineMessage>
        ) : null}
      </Section>
    </fieldset>
  );
}
