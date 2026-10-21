/**
 * Builder › Inputs of a layout 2 draft (v0.17, plan §3.2-§3.4 and §3.7): what clients may do and
 * what it costs. Each input shows only when the layout allows it (else it says why).
 *
 * - Plan scenarios (media planner only): what-ifs on the response grid, as on layout 1.
 * - Try a case (scenario rows): the outputs to predict, and the inputs a client edits, each
 *   seeded from the model's declared bounds or, without them, the fit data's range (the
 *   analyst checks those by hand); a forecast can also let the client choose the horizon.
 * - Number uploads: by period (as layout 1) or by case (an id column and an outcome column).
 * - Score a file (batch): the outputs, the id column and the row cap (never for clients or
 *   links on an image dashboard).
 * - Refresh and limits, with a warning when a limit refuses every run.
 */
import { Trash2 } from "lucide-react";

import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/overlays";
import { Field, Input, NativeSelect } from "@/components/ui/primitives";

import { Section } from "../readout/parts";
import { MAX_BATCH_ROWS, MAX_HORIZON, MAX_SCENARIO_INPUTS, MAX_SCENARIO_ROWS, type DashboardSpecV2, type ScenarioInputDef, type SourceCatalog } from "../types";
import { Toggle, WorkflowPicker } from "./inputs";
import { ListInput } from "./list-input";
import type { SpecEditorProps } from "./setup";
import { v2Change } from "./setup-v2";
import { issueAt, MAX_EXACT_RUNS, type FormIssue } from "./spec-form";
import {
  catalogInputOf,
  catalogIssuesV2,
  inputReason,
  limitWarnings,
  MAX_BATCH_RUNS,
  MAX_SCENARIO_OUTPUTS,
  outputLabel,
  primaryOutput,
  rangeFromProfile,
  seedInput,
  startsRuns,
} from "./spec-form-v2";
import { useBuilderCatalog } from "./use-builder";

type V2Props = Omit<SpecEditorProps, "spec"> & { spec: DashboardSpecV2 };
type Change = ReturnType<typeof v2Change>;

interface PartProps {
  spec: DashboardSpecV2;
  onChange: Change;
  issues: readonly FormIssue[];
  catalog: SourceCatalog | null | undefined;
}

const num = (v: string): number | null => (v === "" ? null : Number(v));

export function InputsV2({ spec, onChange, issues, grid, readOnly = false }: V2Props) {
  const change = v2Change(onChange);
  const cat = useBuilderCatalog(spec);
  const shown = [...issues, ...catalogIssuesV2(spec, cat.catalog)];
  const p: PartProps = { spec, onChange: change, issues: shown, catalog: cat.catalog };
  return (
    <fieldset disabled={readOnly} className="grid min-w-0 gap-5 xl:grid-cols-2">
      <legend className="sr-only">Inputs</legend>
      {spec.mmm ? <PlannerInputs {...p} noGrid={grid.channels === null} /> : null}
      <TryACase {...p} />
      <Uploads {...p} />
      <ScoreAFile {...p} />
      <Refresh {...p} />
      <Limits {...p} />
    </fieldset>
  );
}

function Unavailable({ why }: { why: string }) {
  return <p className="text-sm text-muted-foreground">{why}</p>;
}

// ------------------------------------------------------------------------------ plan scenarios

function PlannerInputs({ spec, onChange, issues, noGrid }: PartProps & { noGrid: boolean }) {
  const w = spec.mmm?.whatif ?? {};
  const setWhatIf = (patch: Partial<NonNullable<NonNullable<DashboardSpecV2["mmm"]>["whatif"]>>) => onChange((s) => (s.mmm ? { ...s, mmm: { ...s.mmm, whatif: { ...s.mmm.whatif, ...patch } } } : s));
  const on = w.enabled ?? true;
  return (
    <Section title="Plan scenarios" description="Sliders on the model's response grid. Moving a slider costs nothing: no run starts.">
      <Toggle
        id="ds-whatif"
        label="Let the client try other spend (what-ifs)"
        hint={noGrid ? "This model has no response grid." : "Saved scenarios stay private to the person who saved them."}
        checked={on}
        disabled={noGrid && !on}
        error={issueAt(issues, "/mmm/whatif/enabled")}
        onChange={(v) => setWhatIf({ enabled: v, exact: v ? (w.exact ?? false) : false })}
      />
      <Toggle
        id="ds-exact"
        label="Offer exact results"
        hint="“Get exact result” scores the scenario with the full model. Each one is a score run that runs as you and uses your project's compute."
        checked={w.exact ?? false}
        disabled={!on}
        onChange={(v) => setWhatIf({ exact: v })}
      />
    </Section>
  );
}

// ------------------------------------------------------------------------------ try a case

function OutputChecks({ id, legend, value, max, catalog, onChange, error }: { id: string; legend: string; value: readonly string[]; max: number; catalog: SourceCatalog | null | undefined; onChange: (v: string[]) => void; error?: string | undefined }) {
  const outs = catalog?.outputs ?? [];
  return (
    <fieldset className="grid gap-1.5" aria-describedby={`${id}-hint`}>
      <legend className="text-sm font-semibold">{legend}</legend>
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        Up to {max}. {value.length} chosen.
      </p>
      {outs.length ? (
        outs.map((o) => {
          const on = value.includes(o.name);
          return (
            <label key={o.name} className="flex items-center gap-2 text-sm">
              <Checkbox checked={on} disabled={!on && value.length >= max} onCheckedChange={() => onChange(on ? value.filter((x) => x !== o.name) : [...value, o.name])} />
              {outputLabel(o.name)} <span className="font-mono text-xs text-muted-foreground">{o.name}</span>
            </label>
          );
        })
      ) : (
        <ListInput aria-label={legend} className="font-mono" value={value} max={max} onChange={onChange} />
      )}
      {error ? (
        <p role="alert" className="text-xs font-medium text-destructive">
          {error}
        </p>
      ) : null}
    </fieldset>
  );
}

function TryACase({ spec, onChange, issues, catalog }: PartProps) {
  const why = inputReason(spec.preset, "scenario_rows");
  const rows = spec.scenario_rows ?? {};
  const set = (patch: Partial<NonNullable<DashboardSpecV2["scenario_rows"]>>) => onChange((s) => ({ ...s, scenario_rows: { ...s.scenario_rows, ...patch } }));
  const defs = rows.inputs ?? [];
  const setDef = (i: number, d: ScenarioInputDef) => set({ inputs: defs.map((x, j) => (j === i ? d : x)) });
  const free = (catalog?.inputs ?? []).filter((c) => !defs.some((d) => catalogInputOf(catalog, d) === c));
  const fixedRows = catalog?.row_policy === "fixed";
  const timeDim = catalog?.time_dim ?? null;
  const horizonOk = spec.preset === "forecast" || spec.preset === "field_map" || spec.preset === "generic";
  return (
    <Section title="Try a case" description="The client changes a few values and the model predicts the result, with its likely range. Each try is a score run that runs as you. Tries stay private to the person who made them.">
      {why ? (
        <Unavailable why={why} />
      ) : (
        <>
          <Toggle
            id="ds-rows"
            label="Let the client try cases"
            hint={fixedRows ? "The model scores only the cases it was fitted on, so a case can change only the forecast horizon." : "Adds the Try a case section."}
            checked={rows.enabled ?? false}
            error={issueAt(issues, "/scenario_rows/enabled")}
            onChange={(on) => {
              const first = primaryOutput(spec, catalog)?.name;
              set({ enabled: on, outputs: on && !(rows.outputs ?? []).length && first ? [first] : (rows.outputs ?? []) });
            }}
          />
          <fieldset className="grid gap-4" disabled={!rows.enabled}>
            <OutputChecks id="ds-rows-out" legend="Outputs to predict" value={rows.outputs ?? []} max={MAX_SCENARIO_OUTPUTS} catalog={catalog} onChange={(v) => set({ outputs: v })} error={issueAt(issues, "/scenario_rows/outputs")} />
            <Field label="Cases at a time" htmlFor="ds-rows-max" error={issueAt(issues, "/scenario_rows/max_rows")} hint={`1 to ${MAX_SCENARIO_ROWS}.`}>
              <Input id="ds-rows-max" className="w-32" type="number" min={1} max={MAX_SCENARIO_ROWS} step={1} value={rows.max_rows ?? 5} onChange={(e) => set({ max_rows: Number(e.target.value) })} />
            </Field>
            <div className="grid gap-2">
              <p className="text-sm font-semibold">Values the client changes</p>
              {issueAt(issues, "/scenario_rows/inputs") ? (
                <p role="alert" className="text-xs font-medium text-destructive">
                  {issueAt(issues, "/scenario_rows/inputs")}
                </p>
              ) : null}
              {defs.length ? (
                <ul aria-label="Values the client changes" className="grid gap-3">
                  {defs.map((d, i) => (
                    <li key={`${d.name}-${i}`} className="rounded-lg border p-3">
                      <InputDefEditor d={d} i={i} issues={issues} catalog={catalog} horizon={Boolean(rows.horizon)} onChange={(x) => setDef(i, x)} onRemove={() => set({ inputs: defs.filter((_, j) => j !== i) })} />
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">No values yet. The other values of a case come from the defaults.</p>
              )}
              {catalog?.inputs?.length ? (
                <div className="flex flex-wrap items-end gap-2">
                  <Field label="Add a value" htmlFor="ds-rows-add" hint={free.length ? "From the model's inputs." : "Every input of the model is in the list."}>
                    <NativeSelect
                      id="ds-rows-add"
                      value=""
                      disabled={!free.length || defs.length >= MAX_SCENARIO_INPUTS}
                      onChange={(e) => {
                        const c = free[Number(e.target.value)];
                        const seeded = c ? seedInput(c, defs.map((d) => d.name)) : null;
                        if (seeded) set({ inputs: [...defs, seeded.def] });
                      }}
                    >
                      <option value="">Choose an input</option>
                      {free.map((c, i) => (
                        <option key={`${c.var ?? c.dim}-${i}`} value={i}>
                          {c.var ?? c.dim} ({c.kind})
                        </option>
                      ))}
                    </NativeSelect>
                  </Field>
                </div>
              ) : catalog === undefined ? null : (
                <p className="text-xs text-muted-foreground">The app does not know the model's inputs, so it cannot add values here.</p>
              )}
            </div>
            {horizonOk && timeDim ? (
              <fieldset className="grid gap-3 rounded-lg border p-3">
                <legend className="px-1 text-sm font-semibold">Forecast horizon</legend>
                <Toggle
                  id="ds-rows-horizon"
                  label="Let the client choose how far ahead"
                  hint={`Periods of ${timeDim}, at most ${MAX_HORIZON}.`}
                  checked={Boolean(rows.horizon)}
                  error={issueAt(issues, "/scenario_rows/horizon", true)}
                  onChange={(on) => set({ horizon: on ? { dim: timeDim, min: 1, max: 12, default: Math.min(12, spec.refresh?.horizon ?? 6) } : null, inputs: on ? defs : defs.map((d) => ({ ...d, applies: "row" as const })) })}
                />
                {rows.horizon ? (
                  <div className="grid grid-cols-3 gap-2">
                    {(["min", "default", "max"] as const).map((k) => (
                      <Field key={k} label={k === "min" ? "Shortest" : k === "max" ? "Longest" : "Default"} htmlFor={`ds-rows-h-${k}`}>
                        <Input
                          id={`ds-rows-h-${k}`}
                          type="number"
                          min={1}
                          max={MAX_HORIZON}
                          step={1}
                          value={rows.horizon?.[k] ?? 1}
                          onChange={(e) => set({ horizon: rows.horizon ? { ...rows.horizon, [k]: Number(e.target.value) } : null })}
                        />
                      </Field>
                    ))}
                  </div>
                ) : null}
              </fieldset>
            ) : null}
          </fieldset>
        </>
      )}
    </Section>
  );
}

function InputDefEditor({ d, i, issues, catalog, horizon, onChange, onRemove }: { d: ScenarioInputDef; i: number; issues: readonly FormIssue[]; catalog: SourceCatalog | null | undefined; horizon: boolean; onChange: (d: ScenarioInputDef) => void; onRemove: () => void }) {
  const at = `/scenario_rows/inputs/${i}`;
  const fid = (k: string) => `ds-in-${i}-${k}`;
  const err = (k: string) => issueAt(issues, `${at}/${k}`);
  const c = catalogInputOf(catalog, d);
  const profile = rangeFromProfile(d, c);
  const numeric = d.type === "number" || d.type === "integer";
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="text-sm">
          <span className="font-medium">{d.label || d.name}</span> <span className="font-mono text-xs text-muted-foreground">{d.name}</span>{" "}
          <span className="text-xs text-muted-foreground">({d.type})</span>
        </p>
        <Button variant="ghost" size="icon-sm" aria-label={`Remove ${d.label || d.name}`} onClick={onRemove}>
          <Trash2 />
        </Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Label" htmlFor={fid("label")} error={err("label")}>
          <Input id={fid("label")} maxLength={60} value={d.label} onChange={(e) => onChange({ ...d, label: e.target.value })} />
        </Field>
        <Field label="Help (optional)" htmlFor={fid("help")} error={err("help")}>
          <Input id={fid("help")} maxLength={200} value={d.help ?? ""} onChange={(e) => onChange({ ...d, help: e.target.value || null })} />
        </Field>
      </div>
      {numeric ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Field label="Lowest" htmlFor={fid("min")} error={err("min")}>
            <Input id={fid("min")} type="number" step="any" value={d.min ?? ""} onChange={(e) => onChange({ ...d, min: num(e.target.value) })} />
          </Field>
          <Field label="Highest" htmlFor={fid("max")} error={err("max")}>
            <Input id={fid("max")} type="number" step="any" value={d.max ?? ""} onChange={(e) => onChange({ ...d, max: num(e.target.value) })} />
          </Field>
          <Field label="Step" htmlFor={fid("step")} error={err("step")}>
            <Input id={fid("step")} type="number" step="any" min={0} value={d.step ?? ""} onChange={(e) => onChange({ ...d, step: num(e.target.value) })} />
          </Field>
          <Field label="Default" htmlFor={fid("default")} error={err("default")}>
            <Input id={fid("default")} type="number" step="any" value={typeof d.default === "number" ? d.default : ""} onChange={(e) => onChange({ ...d, default: Number(e.target.value) })} />
          </Field>
        </div>
      ) : null}
      {d.type === "choice" ? (
        <div className="grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <Field label="Choices" htmlFor={fid("choices")} error={err("choices")} hint="Separated by commas.">
            <ListInput id={fid("choices")} value={d.choices ?? []} onChange={(choices) => onChange({ ...d, choices })} />
          </Field>
          <Field label="Default" htmlFor={fid("default")} error={err("default")}>
            <NativeSelect id={fid("default")} value={String(d.default)} onChange={(e) => onChange({ ...d, default: e.target.value })}>
              {(d.choices ?? []).map((x) => (
                <option key={x} value={x}>
                  {x}
                </option>
              ))}
            </NativeSelect>
          </Field>
        </div>
      ) : null}
      {d.type === "date" ? (
        <div className="grid grid-cols-3 gap-3">
          <Field label="First date" htmlFor={fid("dmin")} error={err("date_min")}>
            <Input id={fid("dmin")} type="date" value={d.date_min ?? ""} onChange={(e) => onChange({ ...d, date_min: e.target.value || null })} />
          </Field>
          <Field label="Last date" htmlFor={fid("dmax")} error={err("date_max")}>
            <Input id={fid("dmax")} type="date" value={d.date_max ?? ""} onChange={(e) => onChange({ ...d, date_max: e.target.value || null })} />
          </Field>
          <Field label="Default" htmlFor={fid("default")} error={err("default")}>
            <Input id={fid("default")} type="date" value={String(d.default)} onChange={(e) => onChange({ ...d, default: e.target.value })} />
          </Field>
        </div>
      ) : null}
      {d.type === "toggle" ? (
        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={d.default === true} onCheckedChange={(on) => onChange({ ...d, default: on === true })} /> On by default
        </label>
      ) : null}
      {horizon ? (
        <Field label="Applies to" htmlFor={fid("applies")}>
          <NativeSelect id={fid("applies")} value={d.applies ?? "row"} onChange={(e) => onChange({ ...d, applies: e.target.value as "row" | "horizon" })}>
            <option value="row">Each case</option>
            <option value="horizon">Every period of the forecast</option>
          </NativeSelect>
        </Field>
      ) : null}
      {profile ? (
        <InlineMessage tone="warning" size="s" role="none">
          This range comes from the data the model was fitted on ({c?.profile_min} to {c?.profile_max}), not from limits that the model declares. Check that it makes sense for your client before you publish.
        </InlineMessage>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------------------ uploads

function Uploads({ spec, onChange, issues, catalog }: PartProps) {
  const why = inputReason(spec.preset, "actuals");
  const a = spec.actuals ?? {};
  const on = a.enabled ?? false;
  const set = (patch: Partial<NonNullable<DashboardSpecV2["actuals"]>>) => onChange((s) => ({ ...s, actuals: { ...s.actuals, ...patch } }));
  const rowMatch = spec.preset === "regression" || spec.preset === "classifier" || spec.preset === "generic";
  const match = a.match ?? "time";
  const causal = spec.preset === "causal_impact";
  return (
    <Section title="Number uploads" description="The client sends the actual numbers. The dashboard shows how close the model was, or (for a model of a change) the effect so far.">
      {why ? (
        <Unavailable why={why} />
      ) : (
        <>
          <Toggle
            id="ds-actuals"
            label="Let the client upload new numbers"
            hint={causal ? "Each upload updates the effect with the new numbers. It is a score run that runs as you." : "Each upload is a score run that runs as you and uses your project's compute."}
            checked={on}
            error={issueAt(issues, "/actuals/enabled")}
            onChange={(v) => set({ enabled: v, allow_refit_requests: v ? (a.allow_refit_requests ?? false) : false })}
          />
          <fieldset className="grid gap-4" disabled={!on}>
            {rowMatch ? (
              <fieldset className="grid gap-2">
                <legend className="mb-1 text-sm font-semibold">Match the numbers</legend>
                {(
                  [
                    ["time", "By period", "One row for each date or period, as in a forecast."],
                    ["row", "By case", "One row for each case, with the case's id and its actual outcome."],
                  ] as const
                ).map(([value, label, hint]) => (
                  <label key={value} className="flex items-start gap-2 text-sm">
                    <input
                      type="radio"
                      name="ds-actuals-match"
                      value={value}
                      checked={match === value}
                      onChange={() => set(value === "row" ? { match: "row", id_column: a.id_column ?? catalog?.row_dim ?? null } : { match: "time", id_column: null, label_column: null })}
                      className="mt-1 accent-[var(--primary)]"
                    />
                    <span className="grid gap-0.5">
                      <span className="font-medium">{label}</span>
                      <span className="text-xs text-muted-foreground">{hint}</span>
                    </span>
                  </label>
                ))}
              </fieldset>
            ) : null}
            {match === "row" ? (
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Case id column" htmlFor="ds-actuals-id" error={issueAt(issues, "/actuals/id_column")} hint={catalog?.row_dim ? `The model names cases by ${catalog.row_dim}.` : undefined}>
                  <Input id="ds-actuals-id" className="font-mono" value={a.id_column ?? ""} onChange={(e) => set({ id_column: e.target.value.trim() || null })} />
                </Field>
                <Field label="Outcome column (optional)" htmlFor="ds-actuals-label" error={issueAt(issues, "/actuals/label_column")} hint="Empty: the model's target.">
                  <Input id="ds-actuals-label" className="font-mono" value={a.label_column ?? ""} onChange={(e) => set({ label_column: e.target.value.trim() || null })} />
                </Field>
              </div>
            ) : null}
            <fieldset className="grid gap-2">
              <legend className="mb-1 text-sm font-semibold">A new upload</legend>
              {(
                [
                  ["review", "Waits for review (recommended)", "The uploader sees the result at once. An editor publishes it into the history on the History tab."],
                  ["auto", "Goes into the history at once", "Everyone with access sees the result when it is ready."],
                ] as const
              ).map(([value, label, hint]) => (
                <label key={value} className="flex items-start gap-2 text-sm">
                  <input type="radio" name="ds-actuals-publish" value={value} checked={(a.publish ?? "review") === value} onChange={() => set({ publish: value })} className="mt-1 accent-[var(--primary)]" />
                  <span className="grid gap-0.5">
                    <span className="font-medium">{label}</span>
                    <span className="text-xs text-muted-foreground">{hint}</span>
                  </span>
                </label>
              ))}
            </fieldset>
            <fieldset className="grid gap-4 rounded-lg border p-4">
              <legend className="px-1 text-sm font-semibold">Refit requests</legend>
              <WorkflowPicker
                id="ds-refit-wf"
                label="Refit workflow"
                hint="A workflow of this project whose input node takes the dashboard's dataset. An approved request adds the uploaded numbers as a new dataset version and starts this workflow."
                value={a.refit_workflow_id ?? null}
                error={issueAt(issues, "/actuals/refit_workflow_id")}
                onChange={(id) => set({ refit_workflow_id: id, allow_refit_requests: id ? (a.allow_refit_requests ?? false) : false })}
              />
              <Toggle
                id="ds-refit-allow"
                label="Let the uploader ask for a refit"
                hint="Nothing runs until an editor with the runner role approves the request on the History tab."
                checked={a.allow_refit_requests ?? false}
                disabled={!on || !a.refit_workflow_id}
                error={issueAt(issues, "/actuals/allow_refit_requests")}
                onChange={(v) => set({ allow_refit_requests: v })}
              />
            </fieldset>
          </fieldset>
        </>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------------------ score a file

function ScoreAFile({ spec, onChange, issues, catalog }: PartProps) {
  const why = inputReason(spec.preset, "batch");
  const b = spec.batch ?? null;
  const on = b?.enabled ?? false;
  const set = (patch: Partial<NonNullable<DashboardSpecV2["batch"]>>) =>
    onChange((s) => {
      const first = primaryOutput(s, catalog)?.name ?? s.subject.variable;
      const base = s.batch ?? { enabled: false, outputs: [first], max_rows: 2000, id_column: catalog?.row_dim ?? "id", enforce_ranges: true };
      return { ...s, batch: { ...base, ...patch } };
    });
  const fixedRows = catalog?.row_policy === "fixed";
  return (
    <Section title="Score a file" description="The client sends a CSV file of new cases and gets the predictions back as a file, with a summary. Each file is a score run that runs as you.">
      {why ? (
        <Unavailable why={why} />
      ) : (
        <>
          {spec.preset === "image" ? (
            <InlineMessage tone="info" size="s" role="none">
              Only your team can score files on an image dashboard. Clients and share links do not see it.
            </InlineMessage>
          ) : null}
          <Toggle
            id="ds-batch"
            label="Let the client score a file"
            hint={fixedRows ? "The model scores only the cases it was fitted on, so it cannot score new cases from a file." : "Adds the Score a file section."}
            checked={on}
            error={issueAt(issues, "/batch/enabled")}
            onChange={(v) => set({ enabled: v })}
          />
          {b ? (
            <fieldset className="grid gap-4" disabled={!on}>
              <OutputChecks id="ds-batch-out" legend="Outputs in the file" value={b.outputs} max={MAX_SCENARIO_OUTPUTS} catalog={catalog} onChange={(v) => set({ outputs: v })} error={issueAt(issues, "/batch/outputs")} />
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Case id column" htmlFor="ds-batch-id" error={issueAt(issues, "/batch/id_column")} hint="The column that names each case in the file.">
                  <Input id="ds-batch-id" className="font-mono" value={b.id_column} onChange={(e) => set({ id_column: e.target.value.trim() })} />
                </Field>
                <Field label="Rows at most" htmlFor="ds-batch-max" error={issueAt(issues, "/batch/max_rows")} hint={`1 to ${MAX_BATCH_ROWS}.`}>
                  <Input id="ds-batch-max" type="number" min={1} max={MAX_BATCH_ROWS} step={1} value={b.max_rows ?? 2000} onChange={(e) => set({ max_rows: Number(e.target.value) })} />
                </Field>
              </div>
              <Toggle
                id="ds-batch-ranges"
                label="Check every value against the Try a case ranges"
                hint="A file with a value out of range is refused with the row and the column."
                checked={b.enforce_ranges ?? true}
                onChange={(v) => set({ enforce_ranges: v })}
              />
            </fieldset>
          ) : null}
        </>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------------------ refresh, limits

function Refresh({ spec, onChange, issues }: PartProps) {
  const r = spec.refresh ?? {};
  const set = (patch: Partial<NonNullable<DashboardSpecV2["refresh"]>>) => onChange((s) => ({ ...s, refresh: { ...s.refresh, ...patch } }));
  return (
    <Section title="Refresh" description="How new readouts arrive without the client doing anything.">
      <WorkflowPicker
        id="ds-refresh-wf"
        label="Refresh workflow"
        hint="Each successful run of this workflow (its model.score or model.forecast node) adds a readout. Schedule the workflow on its Automation tab."
        value={r.workflow_id ?? null}
        onChange={(id) => set({ workflow_id: id })}
      />
      <Toggle
        id="ds-follow"
        label="Follow the endpoint"
        hint={spec.source.kind === "endpoint" ? "When you promote a new model version to the endpoint, the dashboard publishes a new revision on that version and adds a readout." : "Only a dashboard on an endpoint can follow it."}
        checked={r.follow_endpoint ?? false}
        disabled={spec.source.kind !== "endpoint"}
        error={issueAt(issues, "/refresh/follow_endpoint")}
        onChange={(on) => set({ follow_endpoint: on })}
      />
      {spec.preset === "forecast" ? (
        <Field label="Periods to forecast" htmlFor="ds-horizon" error={issueAt(issues, "/refresh/horizon")} hint={`Each readout forecasts this many periods ahead. 1 to ${MAX_HORIZON}.`}>
          <Input id="ds-horizon" className="w-32" type="number" min={1} max={MAX_HORIZON} step={1} value={r.horizon ?? 6} onChange={(e) => set({ horizon: e.target.value === "" ? null : Number(e.target.value) })} />
        </Field>
      ) : null}
    </Section>
  );
}

function Limits({ spec, onChange, issues }: PartProps) {
  const l = spec.limits ?? {};
  const set = (patch: Partial<NonNullable<DashboardSpecV2["limits"]>>) => onChange((s) => ({ ...s, limits: { ...s.limits, ...patch } }));
  const warnings = limitWarnings(spec);
  return (
    <Section title="Limits" description="A cap on the runs that the dashboard can start for you.">
      <Field
        label="Runs each day"
        htmlFor="ds-limit"
        error={issueAt(issues, "/limits/exact_runs_per_day")}
        hint={`Exact results, tried cases, uploads, scored files and manual refreshes together, in 24 hours. 0 to ${MAX_EXACT_RUNS}.`}
      >
        <Input id="ds-limit" type="number" min={0} max={MAX_EXACT_RUNS} step={1} className="w-32" value={l.exact_runs_per_day ?? 20} onChange={(e) => set({ exact_runs_per_day: e.target.value === "" ? 0 : Number(e.target.value) })} />
      </Field>
      {spec.batch?.enabled ? (
        <Field label="Scored files each day" htmlFor="ds-limit-batch" error={issueAt(issues, "/limits/batch_runs_per_day")} hint={`They also count as runs. 0 to ${MAX_BATCH_RUNS}.`}>
          <Input id="ds-limit-batch" type="number" min={0} max={MAX_BATCH_RUNS} step={1} className="w-32" value={l.batch_runs_per_day ?? 3} onChange={(e) => set({ batch_runs_per_day: e.target.value === "" ? 0 : Number(e.target.value) })} />
        </Field>
      ) : null}
      {warnings.map((w) => (
        <InlineMessage key={w.pointer} tone="warning" size="s" role="none">
          {w.message}
        </InlineMessage>
      ))}
      {startsRuns(spec) ? (
        <InlineMessage tone="info" size="s" role="none">
          Live inputs run as the person who publishes and use the project's compute and quotas.
        </InlineMessage>
      ) : null}
    </Section>
  );
}
